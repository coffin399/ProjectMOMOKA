# PLANA/utilities/error/errors.py
from discord.app_commands import AppCommandError

# --- ベースとなるカスタムエラークラス ---
class DiceCommandError(AppCommandError):
    """ダイス関連コマンドの基底エラークラス"""
    pass

# --- 具体的なエラークラス ---
class InvalidDiceNotationError(DiceCommandError):
    """不正なダイス表記が指定されたときのエラー"""
    def __init__(self, message: str = "不正なダイス表記です。`1d100`や`2d6+5`のような形式で入力してください。"):
        self.message = message
        super().__init__(self.message)

class DiceValueError(DiceCommandError):
    """ダイスの数や面、範囲指定が不正なときのエラー"""
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)