# MOMOKA/scheduler/error/errors.py
# 時間調整Cogで使用するカスタムエラークラス定義
from discord.app_commands import AppCommandError


class MatchTimeError(AppCommandError):
    """時間調整Cogで発生するエラーの基底クラス"""
    pass


class InvalidTimeFormatError(MatchTimeError):
    """時刻のフォーマットが不正な場合に発生するエラー"""
    def __init__(self, message: str = "時刻の形式が正しくありません。HH:MM（例: 21:00）の形式で入力してください。"):
        super().__init__(message)


class TimeRangeError(MatchTimeError):
    """開始時刻が終了時刻より後の場合に発生するエラー"""
    def __init__(self, message: str = "開始時刻は終了時刻より前に設定してください。"):
        super().__init__(message)
