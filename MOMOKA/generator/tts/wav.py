import io
import math
import struct
from typing import Iterable, Optional


def encode_wav_from_floats(samples: Iterable[float], sample_rate: int = 48000) -> bytes:
    """Encode mono float samples (-1..1) into PCM16 WAV bytes.

    Minimal dependency version for portability in releases.
    """
    buf = io.BytesIO()
    data_bytes = bytearray()
    for s in samples:
        s_clamped = max(-1.0, min(1.0, float(s)))
        data_bytes += struct.pack('<h', int(s_clamped * 32767))

    num_channels = 1
    byte_rate = sample_rate * num_channels * 2
    block_align = num_channels * 2
    subchunk2_size = len(data_bytes)
    chunk_size = 36 + subchunk2_size

    # RIFF header
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', chunk_size))
    buf.write(b'WAVE')

    # fmt chunk
    buf.write(b'fmt ')  # Subchunk1ID
    buf.write(struct.pack('<I', 16))  # Subchunk1Size for PCM
    buf.write(struct.pack('<H', 1))   # AudioFormat PCM
    buf.write(struct.pack('<H', num_channels))
    buf.write(struct.pack('<I', sample_rate))
    buf.write(struct.pack('<I', byte_rate))
    buf.write(struct.pack('<H', block_align))
    buf.write(struct.pack('<H', 16))  # BitsPerSample

    # data chunk
    buf.write(b'data')
    buf.write(struct.pack('<I', subchunk2_size))
    buf.write(data_bytes)

    return buf.getvalue()


def generate_placeholder_tone(duration_sec: float = 0.35, sample_rate: int = 48000, freq: float = 880.0):
    total = int(duration_sec * sample_rate)
    for i in range(total):
        yield 0.2 * math.sin(2.0 * math.pi * freq * (i / sample_rate))


