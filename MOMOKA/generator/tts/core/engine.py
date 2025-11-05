from __future__ import annotations

import math
import logging
from typing import Optional, Iterable, List, Tuple

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None


class SBVITS2LiteEngine:
    """改善版: より人間らしい音声合成エンジン
    
    主な改善点:
    - フォルマント合成の強化
    - ピッチ変動の自然化
    - 子音の改善
    - より滑らかな音素遷移
    """

    def __init__(self, sample_rate: int = 48000, prefer_gpu: bool = True) -> None:
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
            # Fallback
            duration_sec = max(0.4, min(6.0, (len(text) / 14.0) * float(length_scale) / max(1e-3, float(speed))))
            total = int(self.sample_rate * duration_sec)
            return self._simple_synthesis(total, noise_scale, noise_w, style_vector, style_weight)

    def _simple_synthesis(self, total: int, noise_scale: float, noise_w: float, 
                          style_vector: Optional[object], style_weight: float) -> List[float]:
        """シンプルな合成（フォールバック用）"""
        base_freq = 200.0
        amp_env = self._build_envelope(total, style_vector, style_weight)
        
        phase = 0.0
        out = []
        for i in range(total):
            t = i / self.sample_rate
            # より自然なピッチ変動
            pitch_var = 15.0 * math.sin(2 * math.pi * 2.5 * t) + 8.0 * math.sin(2 * math.pi * 5.3 * t)
            freq = base_freq + pitch_var
            phase += (2 * math.pi * freq) / self.sample_rate
            
            # 豊かな倍音構造
            h1 = math.sin(phase)
            h2 = 0.4 * math.sin(2 * phase)
            h3 = 0.2 * math.sin(3 * phase)
            h4 = 0.1 * math.sin(4 * phase)
            voiced = h1 + h2 + h3 + h4
            
            a = amp_env[i] if 0 <= i < len(amp_env) else 1.0
            out.append(0.4 * a * voiced)
        
        return self._apply_smoothing(out)

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
        """音素ベースの高品質合成"""
        
        # 改善された日本語フォルマント (F1, F2, F3, bandwidth)
        vowel_formants = {
            'a': [(730, 1090, 2440), (80, 90, 120)],
            'i': [(270, 2290, 3010), (60, 90, 100)],
            'u': [(300, 870, 2240), (70, 80, 100)],
            'e': [(530, 1840, 2480), (70, 100, 120)],
            'o': [(570, 840, 2410), (80, 80, 100)],
        }
        vowels = set(vowel_formants.keys())

        # 子音タイプの定義
        consonant_types = {
            'fricative': ['s', 'sh', 'h', 'f', 'z'],
            'stop': ['k', 't', 'p', 'g', 'd', 'b'],
            'nasal': ['n', 'm', 'ng'],
            'liquid': ['r', 'l', 'y', 'w'],
        }

        def get_phoneme_info(p: str) -> Tuple[bool, str, str]:
            """音素の情報を取得"""
            p_lower = p.lower()
            for v in vowels:
                if v in p_lower:
                    return True, v, 'vowel'
            for ctype, consonants in consonant_types.items():
                for c in consonants:
                    if c in p_lower:
                        return False, 'a', ctype
            return False, 'a', 'stop'

        # 音素継続時間の計算（より自然な長さ）
        base_phone_ms = 100.0 / max(0.5, float(speed)) * float(length_scale)
        total_samples = 0
        phone_specs = []
        
        for i, p in enumerate(phonemes):
            is_vowel, v, ptype = get_phoneme_info(p)
            
            if p in {',', '、'}:
                dur = 120.0
                is_vowel = False
                v = 'a'
                ptype = 'pause'
            elif p in {'.', '。', '!', '?'}:
                dur = 200.0
                is_vowel = False
                v = 'a'
                ptype = 'pause'
            else:
                if is_vowel:
                    dur = base_phone_ms * 1.4
                elif ptype == 'fricative':
                    dur = base_phone_ms * 0.9
                elif ptype == 'stop':
                    dur = base_phone_ms * 0.5
                elif ptype == 'nasal':
                    dur = base_phone_ms * 0.8
                else:
                    dur = base_phone_ms * 0.7
            
            nsamp = int(self.sample_rate * dur / 1000.0)
            total_samples += nsamp
            phone_specs.append((is_vowel, v, ptype, nsamp))

        # エンベロープとピッチカーブの生成
        amp_env = self._build_phrase_envelope(total_samples, style_vector, style_weight, phonemes)
        f0_curve = self._build_natural_f0_curve(labels, total_samples, noise_w)
        
        # 音声合成
        out: List[float] = []
        n_global = 0
        phase = 0.0
        prev_formants = None
        
        for idx, (is_vowel, v, ptype, nsamp) in enumerate(phone_specs):
            if ptype == 'pause':
                # 無音区間
                out.extend([0.0] * nsamp)
                n_global += nsamp
                continue
            
            if is_vowel:
                formants, bandwidths = vowel_formants[v]
                # フォルマント間の補間（滑らかな遷移）
                if prev_formants is not None:
                    formants = self._interpolate_formants(prev_formants, formants, nsamp)
                else:
                    formants = [formants] * nsamp
                prev_formants = vowel_formants[v][0]
                
                # 母音の合成
                segment = self._synthesize_vowel(
                    nsamp, formants, bandwidths, f0_curve[n_global:n_global+nsamp],
                    amp_env[n_global:n_global+nsamp], phase
                )
                # 位相の更新
                for i in range(nsamp):
                    f0 = f0_curve[min(n_global + i, len(f0_curve) - 1)]
                    phase += (2 * math.pi * f0) / self.sample_rate
            else:
                # 子音の合成
                segment = self._synthesize_consonant(
                    nsamp, ptype, amp_env[n_global:n_global+nsamp],
                    f0_curve[n_global:n_global+nsamp], n_global
                )
            
            out.extend(segment)
            n_global += nsamp

        # 後処理
        out = self._apply_smoothing(out)
        out = self._normalize_audio(out)
        
        return out

    def _synthesize_vowel(self, nsamp: int, formants: List[Tuple[float, float, float]], 
                          bandwidths: Tuple[int, int, int], f0_curve: List[float],
                          amp_env: List[float], initial_phase: float) -> List[float]:
        """母音の高品質合成"""
        out = []
        phase = initial_phase
        
        # フォルマントフィルタの初期化
        filters = []
        for i in range(3):
            if isinstance(formants[0], tuple):
                fc = formants[0][i]
            else:
                fc = formants[i]
            bw = bandwidths[i]
            filters.append(self._resonator(fc, bw, self.sample_rate))
        
        for i in range(nsamp):
            # 現在のF0
            f0 = f0_curve[i] if i < len(f0_curve) else 200.0
            phase += (2 * math.pi * f0) / self.sample_rate
            
            # 豊かな声帯音源（パルス列 + 倍音）
            glottal = self._glottal_pulse(phase)
            
            # フォルマントフィルタリング
            signal = 0.0
            gains = [1.0, 0.6, 0.3]  # フォルマントごとのゲイン
            
            # フォルマント補間の処理
            if isinstance(formants[i] if i < len(formants) else formants[-1], tuple):
                current_formants = formants[i] if i < len(formants) else formants[-1]
                for j, filt in enumerate(filters):
                    filt.update_params(current_formants[j], bandwidths[j], self.sample_rate)
                    signal += gains[j] * filt.process(glottal)
            else:
                for j, filt in enumerate(filters):
                    signal += gains[j] * filt.process(glottal)
            
            # 振幅エンベロープの適用
            a = amp_env[i] if i < len(amp_env) else 1.0
            out.append(signal * a * 0.5)
        
        return out

    def _synthesize_consonant(self, nsamp: int, ctype: str, amp_env: List[float],
                              f0_curve: List[float], seed: int) -> List[float]:
        """子音の合成"""
        out = []
        
        if ctype == 'fricative':
            # 摩擦音（ホワイトノイズをフィルタリング）
            hp_filter = self._highpass_filter(3000, self.sample_rate)
            for i in range(nsamp):
                noise = (2.0 * self._rand(seed + i) - 1.0)
                filtered = hp_filter.process(noise)
                a = amp_env[i] if i < len(amp_env) else 1.0
                out.append(filtered * a * 0.4)
        
        elif ctype == 'stop':
            # 破裂音（短いバースト）
            burst_len = min(nsamp, int(0.02 * self.sample_rate))
            for i in range(nsamp):
                if i < burst_len:
                    noise = (2.0 * self._rand(seed + i) - 1.0)
                    envelope = math.exp(-10.0 * i / max(1, burst_len))
                    out.append(noise * envelope * 0.6)
                else:
                    out.append(0.0)
        
        elif ctype == 'nasal':
            # 鼻音（低いフォルマント）
            resonator = self._resonator(250, 100, self.sample_rate)
            phase = 0.0
            for i in range(nsamp):
                f0 = f0_curve[i] if i < len(f0_curve) else 150.0
                phase += (2 * math.pi * f0) / self.sample_rate
                glottal = self._glottal_pulse(phase)
                signal = resonator.process(glottal)
                a = amp_env[i] if i < len(amp_env) else 1.0
                out.append(signal * a * 0.5)
        
        else:  # liquid
            # 流音（弱い母音的性質）
            resonator = self._resonator(500, 150, self.sample_rate)
            phase = 0.0
            for i in range(nsamp):
                f0 = f0_curve[i] if i < len(f0_curve) else 180.0
                phase += (2 * math.pi * f0) / self.sample_rate
                glottal = self._glottal_pulse(phase)
                signal = resonator.process(glottal)
                a = amp_env[i] if i < len(amp_env) else 1.0
                out.append(signal * a * 0.4)
        
        return out

    def _glottal_pulse(self, phase: float) -> float:
        """より自然な声帯パルス波形（LF model簡易版）"""
        phase_norm = (phase % (2 * math.pi)) / (2 * math.pi)
        
        if phase_norm < 0.5:
            # 開放相（正弦波）
            t = phase_norm / 0.5
            return math.sin(math.pi * t)
        else:
            # 閉鎖相（急速な減衰）
            t = (phase_norm - 0.5) / 0.5
            return -0.3 * math.exp(-5.0 * t)

    def _interpolate_formants(self, f1: Tuple[float, float, float], 
                              f2: Tuple[float, float, float], 
                              nsamp: int) -> List[Tuple[float, float, float]]:
        """フォルマント周波数の滑らかな補間"""
        result = []
        for i in range(nsamp):
            t = i / max(1, nsamp - 1)
            # Cosine補間（より滑らか）
            t_smooth = (1 - math.cos(t * math.pi)) / 2
            interpolated = tuple(
                f1[j] * (1 - t_smooth) + f2[j] * t_smooth
                for j in range(3)
            )
            result.append(interpolated)
        return result

    def _build_natural_f0_curve(self, labels: List[str], total_samples: int, 
                                noise_w: float) -> List[float]:
        """より自然なF0カーブの生成"""
        base_f0 = 200.0  # 女性的な基本周波数
        curve = [base_f0] * total_samples
        
        # アクセント情報からの変動
        if labels:
            import re
            for i, lab in enumerate(labels):
                try:
                    a_match = re.search(r"/A:([\-\d]+)\+", lab)
                    if a_match:
                        a_val = int(a_match.group(1))
                        if a_val > 0:
                            pos = int(i / max(1, len(labels)) * total_samples)
                            length = int(total_samples / max(1, len(labels)) * a_val)
                            for j in range(pos, min(pos + length, total_samples)):
                                t = (j - pos) / max(1, length)
                                # アクセント核での下降
                                curve[j] = base_f0 - 30.0 * t
                except Exception:
                    continue
        
        # 自然なマイクロプロソディ
        for i in range(total_samples):
            t = i / self.sample_rate
            # 複数の周期的変動を重ね合わせ
            micro = 5.0 * math.sin(2 * math.pi * 3.0 * t)
            micro += 3.0 * math.sin(2 * math.pi * 7.5 * t)
            micro += 2.0 * math.sin(2 * math.pi * 12.0 * t)
            curve[i] += micro * noise_w * 0.5
        
        # 滑らか化
        return self._smooth_curve(curve, window=15)

    def _smooth_curve(self, curve: List[float], window: int = 5) -> List[float]:
        """移動平均による平滑化"""
        if len(curve) < window:
            return curve
        
        smoothed = []
        half_window = window // 2
        for i in range(len(curve)):
            start = max(0, i - half_window)
            end = min(len(curve), i + half_window + 1)
            smoothed.append(sum(curve[start:end]) / (end - start))
        return smoothed

    def _apply_smoothing(self, audio: List[float]) -> List[float]:
        """音声の平滑化（クリック音除去）"""
        if len(audio) < 3:
            return audio
        
        smoothed = [audio[0]]
        for i in range(1, len(audio) - 1):
            smoothed.append((audio[i-1] + 2*audio[i] + audio[i+1]) / 4.0)
        smoothed.append(audio[-1])
        return smoothed

    def _normalize_audio(self, audio: List[float], target_peak: float = 0.8) -> List[float]:
        """音声の正規化"""
        if not audio:
            return audio
        
        peak = max(abs(x) for x in audio)
        if peak > 1e-6:
            scale = target_peak / peak
            return [x * scale for x in audio]
        return audio

    class _Resonator:
        """共鳴フィルタ（2次IIR）"""
        def __init__(self, fc: float, bw: float, sr: int):
            self.fc = fc
            self.bw = bw
            self.sr = sr
            self.z1 = 0.0
            self.z2 = 0.0
            self._update_coeffs()
        
        def _update_coeffs(self):
            r = math.exp(-math.pi * self.bw / self.sr)
            theta = 2 * math.pi * self.fc / self.sr
            self.a1 = -2 * r * math.cos(theta)
            self.a2 = r * r
            self.b0 = 1 - r * r
        
        def update_params(self, fc: float, bw: float, sr: int):
            """パラメータの動的更新"""
            self.fc = fc
            self.bw = bw
            self.sr = sr
            self._update_coeffs()
        
        def process(self, x: float) -> float:
            y = self.b0 * x - self.a1 * self.z1 - self.a2 * self.z2
            self.z2 = self.z1
            self.z1 = y
            return y

    def _resonator(self, fc: float, bw: float, sr: int) -> "_Resonator":
        return self._Resonator(fc, bw, sr)

    class _HighpassFilter:
        """ハイパスフィルタ"""
        def __init__(self, fc: float, sr: int):
            w0 = 2 * math.pi * fc / sr
            alpha = math.sin(w0) / (2 * 0.707)
            
            b0 = (1 + math.cos(w0)) / 2
            b1 = -(1 + math.cos(w0))
            b2 = (1 + math.cos(w0)) / 2
            a0 = 1 + alpha
            a1 = -2 * math.cos(w0)
            a2 = 1 - alpha
            
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

    def _highpass_filter(self, fc: float, sr: int) -> "_HighpassFilter":
        return self._HighpassFilter(fc, sr)

    def _build_phrase_envelope(self, total: int, style_vector: Optional[object], 
                               style_weight: float, phonemes: List[str]) -> List[float]:
        """フレーズレベルのエンベロープ生成"""
        env = self._build_envelope(total, style_vector, style_weight)
        
        # より自然な強弱パターン
        if total > 0:
            for i in range(total):
                t = i / total
                # フレーズ全体の抑揚
                phrase_contour = 0.7 + 0.3 * math.sin(math.pi * t)
                env[i] *= phrase_contour
        
        return env

    def _build_envelope(self, total: int, style_vector: Optional[object], 
                        style_weight: float) -> List[float]:
        """基本エンベロープの生成"""
        if np is None or style_vector is None:
            # 改善されたADSR
            attack = int(0.08 * total)
            decay = int(0.1 * total)
            sustain_samples = total - attack - decay - int(0.15 * total)
            release = total - attack - decay - sustain_samples
            
            env = []
            for i in range(total):
                if i < attack:
                    # 滑らかなアタック
                    t = i / max(1, attack)
                    env.append(t * t * (3 - 2 * t))  # Smoothstep
                elif i < attack + decay:
                    # ディケイ
                    t = (i - attack) / max(1, decay)
                    env.append(1.0 - 0.2 * t)
                elif i < attack + decay + sustain_samples:
                    # サステイン
                    env.append(0.8)
                else:
                    # リリース
                    t = (i - attack - decay - sustain_samples) / max(1, release)
                    env.append(0.8 * (1 - t * t))
            return env
        
        # スタイルベクトルを使用
        try:
            vec = np.array(style_vector).astype(float).flatten()
            if vec.size < 8:
                return [1.0] * total
            
            t = np.linspace(0.0, 1.0, total, dtype=float)
            env = np.ones_like(t) * 0.8
            
            for k in range(min(8, vec.size)):
                env += 0.1 * float(vec[k]) * np.cos(2.0 * math.pi * (k + 1) * t)
            
            env = np.clip(env, 0.3, 1.0)
            return env.tolist()
        except Exception:
            return [1.0] * total

    @staticmethod
    def _rand(i: int) -> float:
        """決定論的疑似乱数"""
        x = (1103515245 * (i + 12345) + 12345) & 0x7FFFFFFF
        return x / 0x7FFFFFFF