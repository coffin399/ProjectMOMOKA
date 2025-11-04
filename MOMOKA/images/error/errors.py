# PLANA/cogs/images/errors.py

import logging
import discord
import aiohttp

logger = logging.getLogger(__name__)

async def handle_meow_api_error(interaction: discord.Interaction, response: aiohttp.ClientResponse):
    """/meowコマンドのAPIエラー(ステータスコードが200以外)を処理します。"""
    try:
        error_text = await response.text()
        logger.error(f"/meow: TheCatAPIエラー - Status: {response.status}, Response: {error_text[:200]}")
        await interaction.followup.send(
            f"猫の画像の取得に失敗しました (ステータス: {response.status})。\nFailed to fetch a cat picture (Status: {response.status}).",
            ephemeral=True
        )
    except Exception as e:
        # エラーレスポンスの読み取り中にさらにエラーが発生した場合のフォールバック
        logger.error(f"/meow: APIエラーハンドラ内でさらにエラーが発生しました: {e}", exc_info=True)
        await interaction.followup.send(
            "猫の画像の取得に失敗し、エラー情報の解析にも失敗しました。", ephemeral=True
        )

async def handle_meow_connection_error(interaction: discord.Interaction, error: aiohttp.ClientError):
    """/meowコマンドの接続エラー(aiohttp.ClientError)を処理します。"""
    logger.error(f"/meow コマンド実行中に接続エラーが発生しました: {error}", exc_info=True)
    await interaction.followup.send(
        "猫画像サイトへの接続に失敗しました。\nFailed to connect to the cat picture site.", ephemeral=True
    )

async def handle_meow_unexpected_error(interaction: discord.Interaction, error: Exception):
    """/meowコマンドの予期せぬエラー(Exception)を処理します。"""
    logger.error(f"/meow コマンド実行中に予期せぬエラーが発生しました: {error}", exc_info=True)
    await interaction.followup.send(
        "猫の画像の取得中に予期せぬエラーが発生しました。\nAn unexpected error occurred while fetching a cat picture.",
        ephemeral=True
    )

async def handle_meow_invalid_data(interaction: discord.Interaction):
    """/meowコマンドでAPIから返されたデータが不正、または空だった場合の処理をします。"""
    logger.warning("/meow: TheCatAPIからのデータ形式が不正または空でした。")
    await interaction.followup.send(
        "猫の画像が見つかりませんでした。\nCould not find a cat picture.", ephemeral=True
    )