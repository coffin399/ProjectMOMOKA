# MOMOKA/notifications/error/star_resonance_errors.py

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StarResonanceError(Exception):
    """スターレゾナンス通知の基底例外クラス"""
    pass


class SpreadsheetError(StarResonanceError):
    """スプレッドシート関連のエラー"""
    pass


class DataParsingError(StarResonanceError):
    """データパースエラー"""
    pass


class ConfigError(StarResonanceError):
    """設定エラー"""
    pass


class NotificationError(StarResonanceError):
    """通知送信エラー"""
    pass


class StarResonanceExceptionHandler:
    """スターレゾナンス通知の例外ハンドラ"""

    def __init__(self, cog):
        self.cog = cog

    def handle_api_error(self, error: Exception, context: str) -> SpreadsheetError:
        """APIエラーをハンドリング"""
        logger.error(f"{context}: APIエラー - {error}", exc_info=True)
        return SpreadsheetError(f"スプレッドシートの取得に失敗しました: {error}")

    def handle_parsing_error(self, error: Exception, context: str) -> DataParsingError:
        """パースエラーをハンドリング"""
        logger.error(f"{context}: パースエラー - {error}", exc_info=True)
        return DataParsingError(f"データの解析に失敗しました: {error}")

    def log_generic_error(self, error: Exception, context: str):
        """一般的なエラーをログに記録"""
        logger.error(f"{context}: エラー - {error}", exc_info=True)

    def get_user_friendly_message(self, error: Exception) -> str:
        """ユーザーフレンドリーなエラーメッセージを取得"""
        if isinstance(error, SpreadsheetError):
            return f"❌ スプレッドシートエラー: {error}"
        elif isinstance(error, DataParsingError):
            return f"❌ データ解析エラー: {error}"
        elif isinstance(error, ConfigError):
            return f"❌ 設定エラー: {error}"
        elif isinstance(error, NotificationError):
            return f"❌ 通知送信エラー: {error}"
        else:
            return f"❌ 予期しないエラーが発生しました: {error}"

