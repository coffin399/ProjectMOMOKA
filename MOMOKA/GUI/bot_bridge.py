# GUI スレッドから参照する Bot インスタンスの橋渡し

from typing import Any, Optional

# 起動前は None。main が PLANA 生成後に set_bot_ref する
_bot_ref: Optional[Any] = None


def set_bot_ref(bot: Any) -> None:
    """GUI から参照する Bot インスタンスを登録する。"""
    # モジュールグローバルへ書き込む
    global _bot_ref
    # 呼び出し側（通常は PLANA）を保持する
    _bot_ref = bot


def get_bot_ref() -> Optional[Any]:
    """登録済み Bot を返す（未登録時は None）。"""
    # 現在の参照をそのまま返す
    return _bot_ref
