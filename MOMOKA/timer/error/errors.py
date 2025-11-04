# MOMOKA/timer/error/errors.py
from discord.app_commands import AppCommandError

class TimerError(AppCommandError):
    """タイマーCogで発生するエラーの基底クラス"""
    pass

class TimerAlreadyStartedError(TimerError):
    """ユーザーが既にタイマーを開始している場合に発生するエラー"""
    def __init__(self, message: str = "既にタイマーを開始しています。"):
        super().__init__(message)

class TimerNotStartedError(TimerError):
    """ユーザーがタイマーを開始していない場合に発生するエラー"""
    def __init__(self, message: str = "タイマーが開始されていません。"):
        super().__init__(message)