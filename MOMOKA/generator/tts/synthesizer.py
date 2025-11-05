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
    sample_rate: int = 22050
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
        if torch is None:
            self._model_ready = False
            return
        if not (self._ckpt_path and self._json_path):
            self._model_ready = False
            return
        # Load config.json (optional but recommended)
        try:
            with open(self._json_path, 'r', encoding='utf-8') as f:
                self._config_data = json.load(f)
        except Exception:
            self._config_data = None

        # Load style_vectors.npy (optional)
        if self._style_vectors_path and np is not None:
            try:
                self._style_vectors = np.load(str(self._style_vectors_path), allow_pickle=True)
            except Exception:
                self._style_vectors = None
        else:
            self._style_vectors = None

        # Real model load would go here. We keep a tiny, safe placeholder.
        # Try import external SBVITS2 engine if user provided a module path
        self._engine = None
        module_path = self.config.sbvits2_module_path
        if module_path:
            try:
                mod = importlib.import_module(module_path)
                # Try common factory names
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
                    # Try common call signatures
                    try:
                        self._engine = factory(
                            config_path=str(self._json_path),
                            checkpoint_path=str(self._ckpt_path),
                            device='cuda' if torch.cuda.is_available() else 'cpu',
                        )
                    except TypeError:
                        try:
                            self._engine = factory(
                                str(self._json_path), str(self._ckpt_path),
                            )
                        except Exception:
                            self._engine = None
                if self._engine is not None:
                    logging.getLogger(__name__).info(
                        "Loaded SBVITS2 engine from %s", module_path
                    )
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Failed to import SBVITS2 module '%s': %s", module_path, e
                )

        # Mark readiness only if engine present (until native path is implemented)
        self._model_ready = self._engine is not None

    def synthesize_to_wav(self, text: str, style: Optional[str] = None,
                           style_weight: float = 5.0, speed: float = 1.0,
                           noise_scale: Optional[float] = None,
                           noise_w: Optional[float] = None,
                           length_scale: Optional[float] = None) -> bytes:
        processed = normalize_text(text, self.config.dictionary_dir)
        if not processed:
            return encode_wav_from_floats([], self._sample_rate)

        # Parameters with defaults
        ns = self.config.noise_scale if noise_scale is None else float(noise_scale)
        nw = self.config.noise_w if noise_w is None else float(noise_w)
        ls = self.config.length_scale if length_scale is None else float(length_scale)
        _ = (style, style_weight, speed, ns, nw, ls)  # reserved for real model

        if not self._model_ready:
            # Use lightweight internal engine to generate speech-like audio
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
        # Attempt generic engine inference
        try:
            style_vec = None
            if self._style_vectors is not None and style is not None:
                # Try get style by key/index
                try:
                    if isinstance(self._style_vectors, dict):
                        style_vec = self._style_vectors.get(style)
                    elif isinstance(self._style_vectors, (list, tuple)):
                        style_vec = self._style_vectors[0]
                    else:
                        # numpy array
                        style_vec = getattr(self._style_vectors, 'item', lambda: None)()
                except Exception:
                    style_vec = None

            # Common inference call patterns; pass noise parameters where supported
            candidates = [
                getattr(self._engine, 'infer', None),
                getattr(self._engine, 'synthesize', None),
                getattr(self._engine, '__call__', None),
            ]
            for run in candidates:
                if callable(run):
                    try:
                        wav: Optional[bytes] = None
                        # Try with kwargs
                        try:
                            wav = run(
                                text=processed,
                                style=style,
                                style_vector=style_vec,
                                style_weight=style_weight,
                                speed=speed,
                                noise_scale=ns,
                                noise_w=nw,
                                length_scale=ls,
                                sample_rate=self._sample_rate,
                            )
                        except TypeError:
                            # Fallback minimal signature
                            out = run(processed)
                            wav = out
                        if isinstance(wav, (bytes, bytearray)):
                            return bytes(wav)
                        # If returns float array, encode to WAV
                        try:
                            return encode_wav_from_floats(wav, sample_rate=self._sample_rate)
                        except Exception:
                            pass
                    except Exception as e:
                        logging.getLogger(__name__).warning("SBVITS2 inference attempt failed: %s", e)
                        continue
        except Exception as e:
            logging.getLogger(__name__).error("SBVITS2 inference error: %s", e)

        # If engine path failed, still return placeholder to avoid silence
        return encode_wav_from_floats(
            generate_placeholder_tone(duration_sec=max(0.25, min(0.8, len(processed) / 40.0)),
                                       sample_rate=self._sample_rate,
                                       freq=700.0),
            sample_rate=self._sample_rate,
        )


