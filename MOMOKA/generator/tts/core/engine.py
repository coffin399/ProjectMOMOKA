from __future__ import annotations

import math
import logging
from typing import Optional, Iterable, List, Tuple

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None


class SBVITS2LiteEngine:
    """A lightweight, SBVITS2-like placeholder engine.

    Goals:
      - Provide deterministic, speech-like audio (not a single beep)
      - Honor noise parameters (noise_scale, noise_w) and length_scale
      - Accept optional style vector to shape envelope
    This is NOT a neural TTS; it's a DSP stub that generates a voiced/unvoiced
    excitation with basic envelopes so users have audible feedback without external deps.
    """

    def __init__(self, sample_rate: int = 22050, prefer_gpu: bool = True) -> None:
        self.sample_rate = int(sample_rate)
        self.logger = logging.getLogger(__name__)
        self._device = 'cpu'
        if prefer_gpu:
            try:
                import torch  # type: ignore
                if torch.cuda.is_available():
                    self._device = 'cuda'
            except Exception:
                self._device = 'cpu'

    def synthesize(
        self,
        text: str,
        style: Optional[str] = None,
        style_vector: Optional[object] = None,
        style_weight: float = 5.0,
        speed: float = 1.0,
        noise_scale: float = 0.667,
        noise_w: float = 0.8,
        length_scale: float = 1.0,
    ) -> Iterable[float]:
        # If pyopenjtalk is available, use phoneme-based source-filter synthesis
        try:
            import pyopenjtalk  # type: ignore
            phonemes = pyopenjtalk.g2p(text, kana=False).split()
            try:
                labels = pyopenjtalk.extract_fullcontext(text)
            except Exception:
                labels = []
        except Exception:
            phonemes = []
            labels = []

        if phonemes:
            return self._synthesize_phoneme_based(
                phonemes=phonemes,
                style_vector=style_vector,
                style_weight=style_weight,
                speed=speed,
                noise_scale=noise_scale,
                noise_w=noise_w,
                length_scale=length_scale,
                labels=labels,
            )
        else:
            # Fallback to character-based contour if g2p unavailable
            duration_sec = max(0.4, min(6.0, (len(text) / 14.0) * float(length_scale) / max(1e-3, float(speed))))
            total = int(self.sample_rate * duration_sec)
            base_freq = 180.0
            freq_jitter = 40.0 * float(noise_w)
            amp_env = self._build_envelope(total, style_vector, style_weight)
            noise_amount = max(0.0, min(1.0, float(noise_scale)))
            phase = 0.0
            out = []
            for i in range(total):
                t = i / self.sample_rate
                freq = base_freq + freq_jitter * math.sin(2 * math.pi * 1.3 * t) + 10.0 * math.sin(2 * math.pi * 7.0 * t)
                phase += (2 * math.pi * freq) / self.sample_rate
                voiced = math.sin(phase) + 0.5 * math.sin(2 * phase)
                noise = (2.0 * self._rand(i) - 1.0) * 0.7
                sample = (1.0 - noise_amount) * voiced + noise_amount * noise
                a = amp_env[i] if 0 <= i < len(amp_env) else 1.0
                out.append(0.25 * a * sample)
            return out

    # ------------------------- phoneme-based core -------------------------
    def _synthesize_phoneme_based(
        self,
        phonemes: List[str],
        style_vector: Optional[object],
        style_weight: float,
        speed: float,
        noise_scale: float,
        noise_w: float,
        length_scale: float,
        labels: List[str],
    ) -> List[float]:
        # Simple JP vowel formants (Hz): a, i, u, e, o
        vowel_formants = {
            'a': (800, 1150, 2900),
            'i': (350, 2000, 2800),
            'u': (325,  700, 2700),
            'e': (500, 1750, 2500),
            'o': (450,  800, 2830),
        }
        vowels = set(vowel_formants.keys())

        # Map phoneme token to vowel/consonant and target F0
        def token_info(p: str) -> Tuple[bool, str]:
            v = p.lower()[0]
            if v in vowels:
                return True, v
            return False, 'a'

        # Durations (heuristic mora-based)
        base_phone_ms = 85.0 / max(0.5, float(speed)) * float(length_scale)
        total_samples = 0
        phone_specs = []
        for p in phonemes:
            is_vowel, v = token_info(p)
            # longer for vowels, shorter for consonants; pauses on punctuations
            if p in {',', '、'}:
                dur = 80.0
                is_vowel = False
                v = 'a'
            elif p in {'.', '。', '!', '?'}:
                dur = 140.0
                is_vowel = False
                v = 'a'
            else:
                dur = base_phone_ms * (1.25 if is_vowel else 0.55)
            nsamp = int(self.sample_rate * dur / 1000.0)
            total_samples += nsamp
            phone_specs.append((is_vowel, v, nsamp))

        amp_env = self._build_phrase_envelope(total_samples, style_vector, style_weight, phonemes)
        noise_amount = max(0.0, min(1.0, float(noise_scale)))
        jitter = float(noise_w)

        # Base F0 contour with accent phrases from full-context if available
        accent_curve = self._build_accent_curve(labels, total_samples)
        def f0_at(n: int) -> float:
            if 0 <= n < len(accent_curve):
                return accent_curve[n]
            t = n / max(1, total_samples)
            base = 170.0 + 45.0 * (4.0 * t * (1.0 - t))
            return base

        # Oversampling (x2) for crude anti-aliasing
        os = 2
        sr_os = self.sample_rate * os
        out_os: List[float] = []
        n_global = 0
        phase = 0.0
        for is_vowel, v, nsamp in phone_specs:
            f1, f2, f3 = vowel_formants.get(v, vowel_formants['a'])
            # Biquad bandpass filters per formant
            bp1 = self._biquad_bandpass(f1, q=10.0, sr=sr_os)
            bp2 = self._biquad_bandpass(f2, q=7.0, sr=sr_os)
            bp3 = self._biquad_bandpass(f3, q=5.0, sr=sr_os)
            # Per-phoneme oversampled synthesis
            nsamp_os = nsamp * os
            for i in range(nsamp_os):
                # map oversampled index to base sample idx for envelopes/F0
                base_idx = n_global + (i // os)
                t = base_idx / self.sample_rate
                f0 = f0_at(base_idx)
                f0 += 20.0 * jitter * math.sin(2 * math.pi * 2.2 * t)
                if is_vowel:
                    phase += (2 * math.pi * f0) / sr_os
                    exc = math.sin(phase)
                else:
                    exc = (2.0 * self._rand(base_idx * 7 + 11) - 1.0)

                # Bandpass chain for vowels, light filtering for consonants
                if is_vowel:
                    s = 0.9 * bp1.process(exc) + 0.6 * bp2.process(exc) + 0.5 * bp3.process(exc)
                else:
                    s = 0.5 * bp1.process(exc)

                noise = (2.0 * self._rand(base_idx * 13 + 3) - 1.0) * 0.6
                mix = (1.0 - noise_amount) * s + noise_amount * noise
                a = amp_env[base_idx] if 0 <= base_idx < len(amp_env) else 1.0
                out_os.append(0.22 * a * mix)
            n_global += nsamp

        # Downsample by 2 with simple lowpass
        out = self._decimate_by_2(out_os)
        return out

    # ------------------------- helpers -------------------------
    class _Biquad:
        def __init__(self, b0, b1, b2, a0, a1, a2):
            self.b0 = b0 / a0
            self.b1 = b1 / a0
            self.b2 = b2 / a0
            self.a1 = a1 / a0
            self.a2 = a2 / a0
            self.z1 = 0.0
            self.z2 = 0.0

        def process(self, x: float) -> float:
            y = self.b0 * x + self.z1
            self.z1 = self.b1 * x - self.a1 * y + self.z2
            self.z2 = self.b2 * x - self.a2 * y
            return y

    def _biquad_bandpass(self, fc: float, q: float, sr: int) -> "SBVITS2LiteEngine._Biquad":
        # Cookbook bandpass (constant skirt gain, peak gain = Q)
        w0 = 2.0 * math.pi * fc / sr
        alpha = math.sin(w0) / (2.0 * q)
        b0 = q * alpha
        b1 = 0.0
        b2 = -q * alpha
        a0 = 1.0 + alpha
        a1 = -2.0 * math.cos(w0)
        a2 = 1.0 - alpha
        return self._Biquad(b0, b1, b2, a0, a1, a2)

    def _decimate_by_2(self, data: List[float]) -> List[float]:
        if len(data) < 4:
            return data[::2]
        # Simple 3-tap lowpass then decimate
        out: List[float] = []
        z1 = 0.0
        z2 = 0.0
        for i, x in enumerate(data):
            y = 0.25 * x + 0.5 * z1 + 0.25 * z2
            z2 = z1
            z1 = x
            if i % 2 == 0:
                out.append(y)
        return out

    def _build_phrase_envelope(self, total: int, style_vector: Optional[object], style_weight: float, phonemes: List[str]):
        # Start with style-based envelope
        env = self._build_envelope(total, style_vector, style_weight)
        # Phrase-level shaping: rise then fall per chunk separated by punctuation
        if total <= 0:
            return env
        # Build chunk map roughly splitting in 2-3 segments
        num_chunks = 3
        seg_len = max(1, total // num_chunks)
        for i in range(total):
            pos = (i % seg_len) / seg_len
            # light emphasis near early in each chunk
            env[i] *= 0.85 + 0.3 * (4.0 * pos * (1.0 - pos))
        return env

    def _build_accent_curve(self, labels: List[str], total_samples: int) -> List[float]:
        # Heuristic accent curve from full-context labels.
        # Parse A (accent type) and F (position in accent phrase) if available.
        import re
        if not labels or total_samples <= 0:
            # fallback base
            return [170.0] * total_samples
        accent_marks: List[Tuple[int, int]] = []  # (start_idx, end_idx)
        phrase_len = max(1, total_samples // max(1, len(labels)))
        for i, lab in enumerate(labels):
            try:
                a = re.search(r"/A:([\-\d]+)\+", lab)
                f = re.search(r"/F:([\-\d]+)\+", lab)
                if a and f:
                    a_val = int(a.group(1))
                    f_val = int(f.group(1))
                    # Approximate accent: when F==1 in phrase with a_val>0, create a drop
                    if a_val > 0 and f_val == 1:
                        start = i * phrase_len
                        end = min(total_samples, start + phrase_len * max(1, a_val))
                        accent_marks.append((start, end))
            except Exception:
                continue
        curve = [170.0] * total_samples
        for start, end in accent_marks:
            for n in range(start, min(end, total_samples)):
                t = (n - start) / max(1, end - start)
                base = 185.0 - 35.0 * t  # falling within the accent phrase
                curve[n] = base
        # Smooth with simple 5-tap
        smoothed: List[float] = []
        z = [curve[0]] * 4
        for v in curve:
            z = [v] + z[:4]
            smoothed.append((z[0] + z[1] + z[2] + z[3] + z[4]) / 5.0 if len(z) >= 5 else v)
        return smoothed

    def _build_envelope(self, total: int, style_vector: Optional[object], style_weight: float):
        if np is None or style_vector is None:
            # Fallback ADSR
            attack = int(0.05 * total)
            release = int(0.15 * total)
            sustain = total - attack - release
            env = [0.0] * total
            for i in range(total):
                if i < attack:
                    env[i] = i / max(1, attack)
                elif i < attack + sustain:
                    env[i] = 1.0
                else:
                    env[i] = max(0.0, 1.0 - (i - attack - sustain) / max(1, release))
            return env

        try:
            vec = np.array(style_vector).astype(float).flatten()
            if vec.size < 8:
                return [1.0] * total
            # Use first 8 dims to define slow envelope via cosine basis
            t = np.linspace(0.0, 1.0, total, dtype=float)
            env = np.zeros_like(t)
            for k in range(8):
                env += float(vec[k]) * np.cos(2.0 * math.pi * (k + 1) * t)
            env = env - env.min()
            vmax = env.max() if env.max() > 0 else 1.0
            env = env / vmax
            # Style weight shapes dynamic range
            w = max(0.1, min(10.0, float(style_weight)))
            env = np.power(env, 1.0 / w)
            env = 0.3 + 0.7 * env
            return env.tolist()
        except Exception:
            return [1.0] * total

    @staticmethod
    def _rand(i: int) -> float:
        # Deterministic LCG for reproducibility
        # Not cryptographic quality; sufficient for noise shaping here
        x = (1103515245 * (i + 12345) + 12345) & 0x7FFFFFFF
        return (x / 0x7FFFFFFF)


