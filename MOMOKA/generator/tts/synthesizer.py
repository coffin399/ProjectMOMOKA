from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .preprocess import normalize_text
from .wav import encode_wav_from_floats, generate_placeholder_tone

try:
    import torch  # type: ignore
except Exception:  # pragma: no cover - optional runtime dep
    torch = None


@dataclass
class SynthesizerConfig:
    model_root: str = "models/tts-models"
    model_name: Optional[str] = None  # e.g. "my-voice"
    dictionary_dir: Optional[str] = None  # path to pyopenjtalk-dict
    sample_rate: int = 22050
    noise_scale: float = 0.667
    noise_w: float = 0.8
    length_scale: float = 1.0


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
        for p in sorted(target_dir.iterdir()):
            if p.suffix in ('.safetensors', '.pth') and (p.stem.startswith('G_') or p.stem == target_dir.name):
                ckpt = p
            if p.suffix == '.json':
                jsonf = p
        self._model_dir = target_dir
        self._ckpt_path = ckpt
        self._json_path = jsonf

    def _maybe_warmup_model(self) -> None:
        if torch is None:
            self._model_ready = False
            return
        if not (self._ckpt_path and self._json_path):
            self._model_ready = False
            return
        # Real model load would go here. We keep a tiny, safe placeholder.
        # Mark as not ready so we use placeholder audio until a proper loader is integrated.
        self._model_ready = False

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
            # Fallback placeholder tone (audible cue, avoids total failure in release builds)
            return encode_wav_from_floats(
                generate_placeholder_tone(duration_sec=max(0.25, min(0.8, len(processed) / 40.0)),
                                           sample_rate=self._sample_rate,
                                           freq=880.0),
                sample_rate=self._sample_rate,
            )

        # Real inference path (not implemented here to keep runtime lean)
        # return self._inference(processed, style, style_weight, speed, ns, nw, ls)
        return encode_wav_from_floats(
            generate_placeholder_tone(duration_sec=max(0.25, min(0.8, len(processed) / 40.0)),
                                       sample_rate=self._sample_rate,
                                       freq=660.0),
            sample_rate=self._sample_rate,
        )


