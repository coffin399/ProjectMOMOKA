# MOMOKA/link_fix/locale_flags.py
# Discord preferred_locale → ISO 639-1 / 国旗絵文字。
from __future__ import annotations

from typing import Any, Optional, Tuple

# locale（完全一致優先）→ (iso639-1, flag emoji)
_LOCALE_MAP = {
    "ja": ("ja", "🇯🇵"),
    "ja-JP": ("ja", "🇯🇵"),
    "en-US": ("en", "🇺🇸"),
    "en-GB": ("en", "🇬🇧"),
    "en": ("en", "🇺🇸"),
    "ko": ("ko", "🇰🇷"),
    "ko-KR": ("ko", "🇰🇷"),
    "zh-CN": ("zh", "🇨🇳"),
    "zh-TW": ("zh", "🇹🇼"),
    "zh-HK": ("zh", "🇭🇰"),
    "fr": ("fr", "🇫🇷"),
    "fr-FR": ("fr", "🇫🇷"),
    "de": ("de", "🇩🇪"),
    "de-DE": ("de", "🇩🇪"),
    "es-ES": ("es", "🇪🇸"),
    "es-419": ("es", "🇲🇽"),
    "pt-BR": ("pt", "🇧🇷"),
    "pt-PT": ("pt", "🇵🇹"),
    "it": ("it", "🇮🇹"),
    "it-IT": ("it", "🇮🇹"),
    "nl": ("nl", "🇳🇱"),
    "nl-NL": ("nl", "🇳🇱"),
    "ru": ("ru", "🇷🇺"),
    "ru-RU": ("ru", "🇷🇺"),
    "uk": ("uk", "🇺🇦"),
    "pl": ("pl", "🇵🇱"),
    "pl-PL": ("pl", "🇵🇱"),
    "sv-SE": ("sv", "🇸🇪"),
    "da": ("da", "🇩🇰"),
    "da-DK": ("da", "🇩🇰"),
    "fi": ("fi", "🇫🇮"),
    "fi-FI": ("fi", "🇫🇮"),
    "no": ("no", "🇳🇴"),
    "nb-NO": ("no", "🇳🇴"),
    "tr": ("tr", "🇹🇷"),
    "tr-TR": ("tr", "🇹🇷"),
    "th": ("th", "🇹🇭"),
    "th-TH": ("th", "🇹🇭"),
    "vi": ("vi", "🇻🇳"),
    "vi-VN": ("vi", "🇻🇳"),
    "id": ("id", "🇮🇩"),
    "id-ID": ("id", "🇮🇩"),
    "hi": ("hi", "🇮🇳"),
    "hi-IN": ("hi", "🇮🇳"),
    "ar": ("ar", "🇸🇦"),
    "bg": ("bg", "🇧🇬"),
    "cs": ("cs", "🇨🇿"),
    "el": ("el", "🇬🇷"),
    "hu": ("hu", "🇭🇺"),
    "ro": ("ro", "🇷🇴"),
    "lt": ("lt", "🇱🇹"),
    "hr": ("hr", "🇭🇷"),
}


def resolve_locale(preferred_locale: Any) -> Optional[Tuple[str, str]]:
    """preferred_locale から (iso_lang, flag_emoji) を返す。未対応なら None。"""
    # 空なら不明
    if not preferred_locale:
        return None
    # Locale enum 等は value / str に落とす
    if hasattr(preferred_locale, "value"):
        key = str(getattr(preferred_locale, "value"))
    else:
        key = str(preferred_locale).strip()
    # 空文字なら不明
    if not key:
        return None
    # 大文字小文字を無視して探す
    lower = key.lower()
    for loc, value in _LOCALE_MAP.items():
        if loc.lower() == lower:
            return value
    # 言語部分だけ（en-US → en）
    primary = key.split("-")[0].lower()
    for loc, value in _LOCALE_MAP.items():
        if loc.lower() == primary or loc.lower().startswith(primary + "-"):
            # プライマリ一致の最初を返す
            if loc.lower() == primary:
                return value
    # プライマリだけのフォールバック辞書
    primary_fallback = {
        "ja": ("ja", "🇯🇵"),
        "en": ("en", "🇺🇸"),
        "ko": ("ko", "🇰🇷"),
        "zh": ("zh", "🇨🇳"),
        "fr": ("fr", "🇫🇷"),
        "de": ("de", "🇩🇪"),
        "es": ("es", "🇪🇸"),
        "pt": ("pt", "🇧🇷"),
        "it": ("it", "🇮🇹"),
        "ru": ("ru", "🇷🇺"),
    }
    # あれば返す
    if primary in primary_fallback:
        return primary_fallback[primary]
    # 不明
    return None
