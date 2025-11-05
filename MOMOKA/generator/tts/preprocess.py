import re
from typing import Optional

try:
    import pyopenjtalk
except Exception:  # pragma: no cover - optional dependency at runtime
    pyopenjtalk = None


def normalize_text(text: str, dictionary_dir: Optional[str] = None) -> str:
    """Normalize Japanese text for TTS.

    - Strips URLs
    - Collapses whitespace
    - Optionally uses pyopenjtalk for reading normalization if available
    - Keeps it conservative to avoid overprocessing
    """
    if not text:
        return ''

    # Remove URLs
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    if not text:
        return ''

    # Optional pyopenjtalk normalization
    if pyopenjtalk is not None:
        try:
            if dictionary_dir:
                # If user provided dictionary dir (e.g., pyopenjtalk-dict), try loaddic
                try:
                    pyopenjtalk.set_user_dict(dictionary_dir)  # type: ignore[attr-defined]
                except Exception:
                    pass
            # Convert to phoneme-like reading for better robustness
            # Note: pyopenjtalk.g2p returns phoneme sequence; we keep original text here
            # but g2p can be useful for downstream models expecting phonemes.
            # We return original text to leave flexibility to synthesizer side.
            _ = pyopenjtalk.g2p(text)  # ensure dictionary works; ignore output for now
        except Exception:
            # If pyopenjtalk fails, continue with original text
            pass

    return text


