import discord
import aiohttp
import logging

# ロガーの設定
logger = logging.getLogger(__name__)

class TTSCogExceptionHandler:
    """TTSCogのエラーハンドリングとメッセージ管理を行うクラス"""

    def __init__(self):
        self.messages = {
            "bot_not_in_voice": "BOTがボイスチャンネルに参加していません。\n先に `/join` コマンドでBOTを参加させてください。",
            "tts_in_progress": "現在、別の読み上げリクエストを処理中です。しばらくお待ちください。",
            "api_synthesis_failed": "音声の生成に失敗しました。\n`ステータスコード: {status}`\n`エラー: {error_text}`",
            "api_connection_failed": "APIサーバーへの接続に失敗しました。サーバーが起動しているか確認してください。",
            "unexpected_error": "予期せぬエラーが発生しました。\n`{error}`",
            "tts_success": "読み上げ: `{text}`",
        }

    def get_message(self, key: str, **kwargs) -> str:
        """メッセージテンプレートを取得し、フォーマットする"""
        return self.messages.get(key, f"メッセージキー '{key}' が見つかりません。").format(**kwargs)

    async def send_message(self, interaction: discord.Interaction, key: str, ephemeral: bool = False, followup: bool = False, **kwargs):
        """整形済みメッセージを送信するヘルパー関数"""
        content = self.get_message(key, **kwargs)
        try:
            if followup or interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)
        except discord.errors.InteractionResponded:
            # フォールバックとしてfollowupを使用
            await interaction.followup.send(content, ephemeral=ephemeral)
        except Exception as e:
            logger.error(f"Failed to send message for interaction {interaction.id}: {e}")

    async def handle_api_error(self, interaction: discord.Interaction, response: aiohttp.ClientResponse):
        """APIからのエラーステータスを処理し、ユーザーに通知する"""
        try:
            error_text = await response.text()
        except Exception:
            error_text = "(レスポンスの読み取りに失敗)"
        await self.send_message(
            interaction,
            "api_synthesis_failed",
            followup=True,
            ephemeral=True,
            status=response.status,
            error_text=error_text
        )

    async def handle_connection_error(self, interaction: discord.Interaction):
        """APIサーバーへの接続エラーを処理し、ユーザーに通知する"""
        await self.send_message(interaction, "api_connection_failed", followup=True, ephemeral=True)

    async def handle_unexpected_error(self, interaction: discord.Interaction, error: Exception):
        """予期せぬエラーを処理し、ログに記録してユーザーに通知する"""
        logger.error(f"An unexpected error occurred in TTSCog for guild {interaction.guild_id}: {error}", exc_info=True)
        await self.send_message(interaction, "unexpected_error", followup=True, ephemeral=True, error=str(error))