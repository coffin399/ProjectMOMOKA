# MOMOKA/music/errors/errors.py
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Dict, Any

import discord
from discord import app_commands

if TYPE_CHECKING:
    from MOMOKA.music.plugins.ytdlp_wrapper import Track

logger = logging.getLogger(__name__)


class MusicCogExceptionHandler:
    """MusicCogに関連するエラーを処理し、ユーザー向けのメッセージを生成するクラス。"""

    def __init__(self, music_config: Dict[str, Any]):
        """
        Args:
            music_config (Dict[str, Any]): musicセクションのコンフィグ。
                                           'messages'キーからメッセージテンプレートを取得します。
        """
        self.messages = music_config.get('messages', {})

    def get_message(self, key: str, **kwargs) -> str:
        """
        コンフィグからメッセージテンプレートを取得し、フォーマットして返す。

        Args:
            key (str): メッセージのキー。
            **kwargs: テンプレートに渡す引数。

        Returns:
            str: フォーマット済みのメッセージ文字列。
        """
        template = self.messages.get(key, f"Message key '{key}' not found.")
        kwargs.setdefault('prefix', '/')
        try:
            return template.format(**kwargs)
        except KeyError as e:
            logger.warning(f"メッセージ '{key}' のフォーマット中にキーエラーが発生しました: {e}")
            return f"メッセージ '{key}' の表示エラー: 不足している引数があります。"

    def handle_error(self, error: Exception, guild: discord.Guild) -> str:
        """
        汎用的なエラーハンドラ。例外の種類に応じて適切なメッセージを返す。

        Args:
            error (Exception): 捕捉された例外オブジェクト。
            guild (discord.Guild): エラーが発生したギルド。

        Returns:
            str: ユーザーに表示するエラーメッセージ。
        """
        guild_log_info = f"Guild {guild.id} ({guild.name})"
        logger.error(f"{guild_log_info}: An error occurred: {error}", exc_info=True)

        # --- Voice Channel Connection Errors ---
        if isinstance(error, asyncio.TimeoutError):
            return self.get_message("error_playing", error="ボイスチャンネルへの接続がタイムアウトしました。")
        if isinstance(error, discord.ClientException):
            return self.get_message("error_playing", error="ボイスチャンネルへの接続に失敗しました。ボットが既に他の操作を行っている可能性があります。")

        # --- Song Fetching/Extraction Errors ---
        # ytdlp_wrapperからのRuntimeErrorを想定
        if isinstance(error, RuntimeError) and "ストリーム" in str(error):
            return self.get_message("error_fetching_song", error=str(error))

        # --- Playback Errors ---
        # FFmpegが見つからない場合など
        if "No such file or directory: 'ffmpeg'" in str(error):
             return self.get_message("error_playing", error="再生に必要なコンポーネント(FFmpeg)が見つかりません。")

        # --- Generic Fallback ---
        return self.get_message("error_playing", error=f"予期せぬエラーが発生しました: {type(error).__name__}")

    async def handle_generic_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """
        コマンドで発生した予期せぬエラーを処理し、ユーザーに応答する汎用ハンドラ。

        Args:
            interaction (discord.Interaction): エラーが発生したインタラクション。
            error (app_commands.AppCommandError): 発生したエラー。
        """
        command_name = interaction.command.qualified_name if interaction.command else "Unknown Command"
        logger.error(f"An unexpected error occurred in command '{command_name}': {error}", exc_info=True)

        # ユーザーに表示するメッセージ
        message = "コマンドの実行中に予期せぬエラーが発生しました。\nAn unexpected error occurred while executing the command."

        try:
            # 応答がまだ送信されていないか確認
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                # 既に応答済み（deferなど）の場合はfollowupを使用
                await interaction.followup.send(message, ephemeral=True)
        except discord.errors.HTTPException as e:
            # 応答送信に失敗した場合のログ
            logger.error(f"Failed to send error message for command '{command_name}': {e}")