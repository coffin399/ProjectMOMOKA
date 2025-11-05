from __future__ import annotations

import os
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Optional

from .preprocess import normalize_text
from .wav import encode_wav_from_floats, generate_placeholder_tone
from .core.engine import SBVITS2LiteEngine

try:
    import torch  # type: ignore
except Exception:  # pragma: no cover - optional runtime dep
    torch = None

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - optional runtime dep
    np = None
import importlib
import logging


@dataclass
class SynthesizerConfig:
    model_root: str = "models/tts-models"
    model_name: Optional[str] = None  # e.g. "my-voice"
    dictionary_dir: Optional[str] = None  # path to pyopenjtalk-dict
    sample_rate: int = 48000  # Discord standard: 48kHz
    noise_scale: float = 0.667
    noise_w: float = 0.8
    length_scale: float = 1.0
    sbvits2_module_path: Optional[str] = None  # optional: e.g. 'style_bert_vits2'


class StyleBertVITS2Synthesizer:
    """Thin wrapper that discovers Style-Bert-VITS2 model artifacts and performs synthesis.

    Notes:
        - This implementation provides a minimal interface and a robust fallback tone
          to keep the bot functional even without GPU / model present in releases.
        - If `torch` or expected model files are missing, a short tone WAV is returned.
        - Expected model directory structure:
            models/tts-models/<model_name>/<model_name>.safetensors or G_*.pth
            and an accompanying JSON config file in the same directory.
    """

    def __init__(self, config: SynthesizerConfig):
        self.config = config
        self._model_ready = False
        self._device = 'cpu'
        self._sample_rate = int(config.sample_rate)
        self._model_dir: Optional[Path] = None
        self._ckpt_path: Optional[Path] = None
        self._json_path: Optional[Path] = None
        self._style_vectors_path: Optional[Path] = None
        self._config_data: Optional[dict] = None
        self._style_vectors: Optional[object] = None
        self._engine = None  # external engine object if available
        self._lite = SBVITS2LiteEngine(sample_rate=self._sample_rate)

        self._discover_model_paths()
        self._maybe_warmup_model()

    def _discover_model_paths(self) -> None:
        root = Path(self.config.model_root)
        if not root.exists():
            return
        target_dir: Optional[Path] = None
        if self.config.model_name:
            candidate = root / self.config.model_name
            if candidate.exists() and candidate.is_dir():
                target_dir = candidate
        else:
            # pick first model dir that contains a checkpoint
            for d in root.iterdir():
                if d.is_dir():
                    if any(p.suffix in ('.safetensors', '.pth') for p in d.iterdir()):
                        target_dir = d
                        break
        if not target_dir:
            return
        ckpt = None
        jsonf = None
        stylef = None
        for p in sorted(target_dir.iterdir()):
            if p.suffix in ('.safetensors', '.pth') and (p.stem.startswith('G_') or p.stem == target_dir.name):
                ckpt = p
            if p.name == 'config.json':
                jsonf = p
            if p.name == 'style_vectors.npy':
                stylef = p
        self._model_dir = target_dir
        self._ckpt_path = ckpt
        self._json_path = jsonf
        self._style_vectors_path = stylef

    def _maybe_warmup_model(self) -> None:
        """Style-Bert-VITS2モデルをロードします。"""
        if torch is None:
            self._model_ready = False
            logging.getLogger(__name__).warning("PyTorch not available, using fallback engine")
            return
        if not (self._ckpt_path and self._json_path):
            self._model_ready = False
            logging.getLogger(__name__).warning("Model files not found, using fallback engine")
            return

        # Load config.json
        try:
            with open(self._json_path, 'r', encoding='utf-8') as f:
                self._config_data = json.load(f)
        except Exception as e:
            self._config_data = None
            logging.getLogger(__name__).warning(f"Failed to load config.json: {e}")

        # Load style_vectors.npy (optional)
        if self._style_vectors_path and np is not None:
            try:
                self._style_vectors = np.load(str(self._style_vectors_path), allow_pickle=True)
            except Exception as e:
                self._style_vectors = None
                logging.getLogger(__name__).debug(f"Style vectors not loaded: {e}")
        else:
            self._style_vectors = None

        # Try to load Style-Bert-VITS2 TTSModel
        self._engine = None
        self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        
        # First, try direct import of style_bert_vits2
        try:
            from style_bert_vits2.tts_model import TTSModel
            self._engine = TTSModel(
                model_path=str(self._ckpt_path),
                config_path=str(self._json_path),
                style_vec_path=str(self._style_vectors_path) if self._style_vectors_path else None,
                device=self._device,
            )
            self._engine.load()  # Load the model
            self._model_ready = True
            logging.getLogger(__name__).info(
                f"Loaded Style-Bert-VITS2 model from {self._ckpt_path} on {self._device}"
            )
            return
        except ImportError:
            # style_bert_vits2 not available, try module path
            pass
        except Exception as e:
            logging.getLogger(__name__).warning(
                f"Failed to load Style-Bert-VITS2 model: {e}"
            )

        # Fallback: try custom module path if provided
        module_path = self.config.sbvits2_module_path
        if module_path:
            try:
                mod = importlib.import_module(module_path)
                # Try TTSModel class
                if hasattr(mod, 'TTSModel'):
                    TTSModel = getattr(mod, 'TTSModel')
                    self._engine = TTSModel(
                        model_path=str(self._ckpt_path),
                        config_path=str(self._json_path),
                        style_vec_path=str(self._style_vectors_path) if self._style_vectors_path else None,
                        device=self._device,
                    )
                    if hasattr(self._engine, 'load'):
                        self._engine.load()
                    self._model_ready = True
                    logging.getLogger(__name__).info(
                        f"Loaded SBVITS2 engine from {module_path}"
                    )
                    return
                # Try other common factory names
                candidates = [
                    getattr(mod, 'load_synthesizer', None),
                    getattr(mod, 'create_synthesizer', None),
                    getattr(mod, 'Synthesizer', None),
                    getattr(mod, 'SynthesizerTrn', None),
                ]
                factory = None
                for c in candidates:
                    if callable(c):
                        factory = c
                        break
                if factory is not None:
                    try:
                        self._engine = factory(
                            config_path=str(self._json_path),
                            checkpoint_path=str(self._ckpt_path),
                            device=self._device,
                        )
                        if self._engine is not None:
                            self._model_ready = True
                            logging.getLogger(__name__).info(
                                f"Loaded SBVITS2 engine from {module_path}"
                            )
                            return
                    except Exception as e:
                        logging.getLogger(__name__).debug(f"Factory method failed: {e}")
            except Exception as e:
                logging.getLogger(__name__).warning(
                    f"Failed to import SBVITS2 module '{module_path}': {e}"
                )

        # No model loaded, use fallback
        self._model_ready = False
        logging.getLogger(__name__).info(
            "Style-Bert-VITS2 model not available, using fallback DSP engine"
        )

    def synthesize_to_wav(self, text: str, style: Optional[str] = None,
                           style_weight: float = 5.0, speed: float = 1.0,
                           noise_scale: Optional[float] = None,
                           noise_w: Optional[float] = None,
                           length_scale: Optional[float] = None) -> bytes:
        """テキストを音声に変換します。Style-Bert-VITS2が利用可能な場合はそれを使用し、そうでない場合はフォールバックエンジンを使用します。"""
        processed = normalize_text(text, self.config.dictionary_dir)
        if not processed:
            return encode_wav_from_floats([], self._sample_rate)

        # Parameters with defaults
        ns = self.config.noise_scale if noise_scale is None else float(noise_scale)
        nw = self.config.noise_w if noise_w is None else float(noise_w)
        ls = self.config.length_scale if length_scale is None else float(length_scale)

        # Use Style-Bert-VITS2 if available
        if self._model_ready and self._engine is not None:
            try:
                # Style-Bert-VITS2 TTSModel.infer() signature:
                # infer(text, language, speaker_id, reference_audio_path, sdp_ratio, noise, noise_w, length, ...)
                # For our use case, we'll use default speaker_id=0 and language=JP
                from style_bert_vits2.constants import Languages
                
                # Determine style ID
                style_id = 0
                if style and hasattr(self._engine, 'style2id'):
                    style_id = self._engine.style2id.get(style, 0)
                
                # Perform inference
                sr, audio = self._engine.infer(
                    text=processed,
                    language=Languages.JP,
                    speaker_id=0,  # Default speaker
                    reference_audio_path=None,
                    sdp_ratio=0.2,  # Default SDP ratio
                    noise=ns,
                    noise_w=nw,
                    length=ls / speed if speed != 1.0 else ls,
                    line_split=False,
                    split_interval=0.0,
                    assist_text=None,
                    assist_text_weight=0.0,
                    use_assist_text=False,
                    style=style or 'Neutral',
                    style_weight=style_weight,
                )
                
                # Convert numpy array to WAV bytes
                if np is not None and isinstance(audio, np.ndarray):
                    # Ensure audio is in the right format
                    audio = audio.astype(np.float32)
                    # Normalize to [-1, 1] range
                    if audio.max() > 1.0 or audio.min() < -1.0:
                        audio = audio / max(abs(audio.max()), abs(audio.min()))
                    return encode_wav_from_floats(audio, sample_rate=int(sr))
                else:
                    # Fallback: try to encode as-is
                    return encode_wav_from_floats(audio, sample_rate=int(sr) if hasattr(sr, '__int__') else self._sample_rate)
            except ImportError:
                # style_bert_vits2.constants not available, try generic inference
                pass
            except Exception as e:
                logging.getLogger(__name__).error(f"Style-Bert-VITS2 inference failed: {e}")
                # Fall through to fallback engine

        # Fallback: Use lightweight DSP engine
        samples = self._lite.synthesize(
            text=processed,
            style=style,
            style_vector=self._style_vectors,
            style_weight=style_weight,
            speed=speed,
            noise_scale=ns,
            noise_w=nw,
            length_scale=ls,
        )
        return encode_wav_from_floats(samples, sample_rate=self._sample_rate)


