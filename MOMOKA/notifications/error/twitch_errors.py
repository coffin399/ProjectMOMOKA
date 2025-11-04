# MOMOKA/notification/error/twitch_errors.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import aiohttp
import discord

if TYPE_CHECKING:
    # 循環参照を避けるための型チェック用インポート
    from ..twitch_notification_cog import TwitchNotification

logger = logging.getLogger(__name__)

# --- カスタム例外クラス ---

class TwitchNotificationError(Exception):
    """Twitch通知Cogで発生するエラーの基底クラス。"""
    pass

class TwitchAPIError(TwitchNotificationError):
    """Twitch API関連のエラー（レート制限、サーバーエラーなど）"""
    pass

class DataParsingError(TwitchNotificationError):
    """データ解析エラー（JSONデコード失敗など）"""
    pass

class ConfigError(TwitchNotificationError):
    """設定ファイル関連のエラー"""
    pass

class NotificationError(TwitchNotificationError):
    """通知送信時のエラー"""
    pass


# --- エラーハンドラクラス ---

class TwitchExceptionHandler:
    """Twitch通知Cogのエラーを処理し、ユーザーやログ向けのメッセージを生成するクラス。"""

    def __init__(self, cog_instance: "TwitchNotification"):
        """
        Args:
            cog_instance: TwitchNotificationのインスタンス。
        """
        self.cog = cog_instance

    def handle_api_error(self, error: Exception, context: str) -> TwitchAPIError:
        """APIリクエスト中の例外を捕捉し、適切なカスタム例外を返す。"""
        if isinstance(error, asyncio.TimeoutError):
            logger.error(f"Twitch APIリクエストがタイムアウトしました: {context}")
            return TwitchAPIError("Twitch APIへのリクエストがタイムアウトしました。")

        if isinstance(error, aiohttp.ClientError):
            logger.error(f"Twitch APIへのネットワークエラー: {context} - {error}")
            return TwitchAPIError(f"Twitch APIへの接続中にネットワークエラーが発生しました: {error}")

        logger.error(f"予期しないTwitch APIリクエストエラー: {context} - {error}", exc_info=True)
        return TwitchAPIError(f"Twitch APIへのリクエスト中に予期しないエラーが発生しました: {error}")

    def handle_api_response_error(self, status: int, url: str, text: str) -> TwitchAPIError:
        """Twitch APIのHTTPステータスコードに応じたエラーを返す。"""
        if status == 400:
            logger.error(f"Twitch APIリクエストエラー (Bad Request): {url} - ステータス: {status}, 応答: {text}")
            return TwitchAPIError(f"Twitch APIへのリクエストが不正です (Code: {status})。")
        if status == 401:
            logger.error(f"Twitch API認証エラー (Unauthorized): {url} - ステータス: {status}")
            return TwitchAPIError(f"Twitch APIの認証に失敗しました (Code: {status})。アクセストークンが無効か期限切れの可能性があります。")
        if status == 429:
            logger.warning(f"Twitch API レート制限: {url}")
            return TwitchAPIError(f"Twitch APIの利用制限に達しました (Code: {status})。しばらくしてから再試行されます。")

        logger.error(f"Twitch API エラー: {url} - ステータス: {status}, 応答: {text}")
        return TwitchAPIError(f"Twitch APIサーバーがエラーを返しました (Code: {status})。")

    def handle_json_decode_error(self, error: json.JSONDecodeError, context: str) -> DataParsingError:
        """JSON解析エラーを処理する。"""
        logger.error(f"Twitch APIからのJSON解析エラー: {context} - {error}")
        return DataParsingError(f"Twitch APIからの応答データの解析に失敗しました。")

    def log_generic_error(self, error: Exception, context: str):
        """汎用的なエラーをログに出力する。"""
        logger.error(f"{context}で予期しないエラーが発生しました: {error}", exc_info=True)

    def get_user_friendly_message(self, error: Exception) -> str:
        """ユーザーに表示するための、分かりやすいエラーメッセージを生成する。"""
        if isinstance(error, (TwitchAPIError, DataParsingError)):
            return f"❌ Twitchとの連携中にエラーが発生しました: {error}"
        if isinstance(error, ConfigError):
            return f"❌ 設定処理中にエラーが発生しました: {error}"
        if isinstance(error, NotificationError):
             return f"❌ 通知の送信に失敗しました: {error}"
        if isinstance(error, discord.Forbidden):
            return "❌ 権限が不足しているため、操作を完了できませんでした。メッセージの送信や埋め込みリンクの権限を確認してください。"

        return f"❌ 予期しないエラーが発生しました。詳細はボットのログを確認してください。"