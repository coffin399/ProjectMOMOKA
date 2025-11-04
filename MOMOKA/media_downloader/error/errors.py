# MOMOKA/media_downloader/error/errors.py
from __future__ import annotations

import json
import logging
from typing import Dict, Any

import openai
import yt_dlp
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Discordのメッセージ最大長
DISCORD_MESSAGE_MAX_LENGTH = 1990


class LLMExceptionHandler:
    """LLM関連のAPI例外を処理し、ユーザー向けの整形されたエラーメッセージを生成するクラス。"""

    def __init__(self, llm_config: Dict[str, Any]):
        self.llm_config = llm_config

    def handle_exception(self, e: Exception) -> str:
        error_detail = ""
        error_messages = self.llm_config.get('error_msg', {})

        if isinstance(e, openai.RateLimitError):
            logger.warning(f"LLM API rate limit exceeded: {e.status_code} - {e.response.text if e.response else 'N/A'}")
            base_msg_key, default_msg = 'ratelimit_error', "⚠️ 生成AIが現在非常に混雑しています。(Code: {status_code})"
        elif isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
            logger.error(f"LLM API connection error: {e}")
            return error_messages.get('general_error', "Failed to connect to the AI service.")
        elif isinstance(e, openai.APIStatusError):
            logger.error(f"LLM API status error: {e.status_code} - {e.response.text if e.response else 'N/A'}")
            base_msg_key, default_msg = 'api_status_error', "AIとの通信でエラーが発生しました。(Code: {status_code})"
        else:
            logger.error(f"An unexpected error occurred during LLM interaction: {e}", exc_info=True)
            return error_messages.get('general_error', "An unexpected error occurred.")

        if hasattr(e, 'response') and e.response:
            try:
                error_data = e.response.json()
                detail = error_data.get('detail') or error_data.get('message') or error_data.get('title')
                error_detail = f"\n> **Details**: {detail}" if detail else f"\n> **Response**: `{str(error_data)[:500]}`"
            except json.JSONDecodeError:
                error_detail = f"\n> **Raw Response**: `{e.response.text[:500]}`"

        base_message = error_messages.get(base_msg_key, default_msg).format(status_code=e.status_code)
        return f"{base_message}{error_detail}"[:DISCORD_MESSAGE_MAX_LENGTH]


# --- ここから新規追加 ---

class YTDLPExceptionHandler:
    """yt-dlpとGoogle Drive関連のエラーを処理し、ユーザー向けのメッセージを生成するクラス。"""

    def handle_exception(self, e: Exception) -> str:
        """
        例外オブジェクトを受け取り、種類に応じて適切なエラーメッセージ文字列を返す。

        Args:
            e (Exception): 捕捉された例外オブジェクト。

        Returns:
            str: Discordに返信するエラーメッセージ。
        """
        logger.error(f"An error occurred in YTDLP/GDrive process: {e}", exc_info=True)

        if isinstance(e, yt_dlp.utils.DownloadError):
            return (
                f"動画が見つからないか、ダウンロードが許可されていません。検索クエリやURLを確認してください。\n"
                f"Video not found or download is not allowed. Please check the query or URL.\n"
                f"```{str(e)}```"
            )
        elif isinstance(e, HttpError):
            return (
                f"Google Drive APIでエラーが発生しました。認証情報やフォルダID、APIの割り当てを確認してください。\n"
                f"An error occurred with the Google Drive API. Please check credentials, folder ID, and API quota.\n"
                f"```{str(e)}```"
            )
        else:
            return (
                f"処理中に予期せぬエラーが発生しました。\n"
                f"An unexpected error occurred during processing.\n"
                f"```{type(e).__name__}: {str(e)}```"
            )

    def get_gdrive_init_error(self) -> str:
        """Google Drive APIが初期化されていない場合のエラーメッセージを返す。"""
        return (
            "エラー: Google Drive APIが初期化されていません。コンソールを確認してください。\n"
            "Error: Google Drive API is not initialized. Please check the console."
        )

    def get_merge_error(self) -> str:
        """動画と音声の結合に失敗した場合のエラーメッセージを返す。"""
        return "エラー: 動画と音声の結合に失敗しました。\nError: Failed to merge video and audio."

    def get_upload_error(self) -> str:
        """Google Driveへのアップロードに失敗した場合のエラーメッセージを返す。"""
        return "エラー: Google Driveへのアップロードに失敗しました。\nError: Failed to upload to Google Drive."

    def get_conversion_error(self) -> str:
        """ファイル変換に失敗した場合のエラーメッセージを返す。"""
        return (
            "エラー: ファイル変換に失敗しました。FFmpegがインストールされていますか？\n"
            "Error: File conversion failed. Is FFmpeg installed?"
        )