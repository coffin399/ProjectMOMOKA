# MOMOKA/notifications/error/earthquake_errors.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import aiohttp
import discord

if TYPE_CHECKING:
    from ..earthquake_notification_cog import EarthquakeTsunamiCog

logger = logging.getLogger(__name__)

# --- カスタム例外クラス ---

class EarthquakeTsunamiError(Exception):
    """地震・津波情報Cogで発生するエラーの基底クラス。"""
    pass

class APIError(EarthquakeTsunamiError):
    """API関連のエラー（レート制限、サーバーエラーなど）"""
    pass

class DataParsingError(EarthquakeTsunamiError):
    """データ解析エラー（JSONデコード失敗など）"""
    pass

class ConfigError(EarthquakeTsunamiError):
    """設定ファイル関連のエラー"""
    pass

class NotificationError(EarthquakeTsunamiError):
    """通知送信時のエラー"""
    pass


# --- エラーハンドラクラス ---

class EarthquakeTsunamiExceptionHandler:
    """地震・津波情報Cogのエラーを処理し、ユーザーやログ向けのメッセージを生成するクラス。"""

    def __init__(self, cog_instance: "EarthquakeTsunamiCog"):
        """
        Args:
            cog_instance: EarthquakeTsunamiCogのインスタンス。
        """
        self.cog = cog_instance
        self.error_stats = cog_instance.error_stats

    def handle_api_error(self, error: Exception, url: str) -> APIError:
        """APIリクエスト中の例外を捕捉し、適切なカスタム例外を返す。"""
        if isinstance(error, asyncio.TimeoutError):
            logger.error(f"タイムアウト: {url}")
            self.error_stats['network_errors'] += 1
            return APIError("リクエストがタイムアウトしました。")

        if isinstance(error, aiohttp.ClientError):
            logger.error(f"ネットワークエラー: {url} - {error}")
            self.error_stats['network_errors'] += 1
            return APIError(f"ネットワークエラーが発生しました: {error}")

        logger.error(f"予期しないAPIリクエストエラー: {url} - {error}", exc_info=True)
        return APIError(f"予期しないエラーが発生しました: {error}")

    def handle_api_response_error(self, status: int, url: str) -> APIError:
        """APIのHTTPステータスコードに応じたエラーを返す。"""
        if status == 400:
            logger.error(f"APIリクエストエラー (Bad Request): {url} - ステータス: {status}")
            self.error_stats['api_errors'] += 1
            return APIError(f"APIへのリクエストが不正です (Code: {status})。")
        if status == 429:
            logger.warning(f"API レート制限: {url}")
            self.error_stats['api_errors'] += 1
            return APIError(f"APIの利用制限に達しました (Code: {status})。")

        logger.error(f"API エラー: {url} - ステータス: {status}")
        self.error_stats['api_errors'] += 1
        return APIError(f"APIサーバーがエラーを返しました (Code: {status})。")

    def handle_json_decode_error(self, error: json.JSONDecodeError, url: str) -> DataParsingError:
        """JSON解析エラーを処理する。"""
        logger.error(f"JSON解析エラー: {url} - {error}")
        self.error_stats['parsing_errors'] += 1
        return DataParsingError(f"APIからの応答データの解析に失敗しました。")

    def log_generic_error(self, error: Exception, context: str):
        """汎用的なエラーをログに出力する。"""
        logger.error(f"{context}で予期しないエラーが発生しました: {error}", exc_info=True)

    def get_user_friendly_message(self, error: Exception) -> str:
        """ユーザーに表示するための、分かりやすいエラーメッセージを生成する。"""
        if isinstance(error, (APIError, DataParsingError)):
            return f"❌ 情報の取得または解析中にエラーが発生しました: {error}"
        if isinstance(error, ConfigError):
            return f"❌ 設定処理中にエラーが発生しました: {error}"
        if isinstance(error, NotificationError):
             return f"❌ 通知の送信に失敗しました: {error}"
        if isinstance(error, discord.Forbidden):
            return "❌ 権限が不足しているため、操作を完了できませんでした。"

        return f"❌ 予期しないエラーが発生しました。詳細はボットのログを確認してください。"