# MOMOKA/llm/utils/tips.py
import random
from typing import List, Dict, Any
import discord


class TipsManager:
    """LLM待機中に表示するランダムなtipsを管理するクラス"""

    def __init__(self):
        self.tips = self._create_tips_list()

    def _create_tips_list(self) -> List[Dict[str, Any]]:
        """tipsのリストを作成する"""
        return [
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**画像を送信できます！**\n画像URLを貼り付けるか、画像ファイルを添付してAIに説明を求めることができます。\n\n**You can send images!**\nPaste image URLs or attach image files to ask the AI for descriptions.",
                "color": discord.Color.blue()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**会話を続けるには返信機能を！**\nBotのメッセージに返信することで、メンションなしで会話を続けられます。\n\n**Use reply to continue conversations!**\nReply to bot messages to continue chatting without mentioning.",
                "color": discord.Color.green()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**モデルを切り替えられます！**\n`/switch-models`コマンドでこのチャンネルのAIモデルを変更できます。\n\n**You can switch models!**\nUse `/switch-models` command to change the AI model for this channel.",
                "color": discord.Color.orange()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**画像生成も可能！**\nAIに画像生成を依頼すると、StableDiffusionが画像生成AIが画像を作成します。\n\n**Image generation available!**\nAsk the AI to generate images and it will use StableDiffusion image generation AI.",
                "color": discord.Color.gold()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**検索機能を利用！**\nAIに最新情報を調べてもらうことができます。リアルタイムの情報取得が可能です。\n\n**Use search functionality!**\nAsk the AI to search for the latest information. Real-time information retrieval is available.",
                "color": discord.Color.red()
            }
        ]

    def get_random_tip(self) -> discord.Embed:
        """ランダムなtipのembedを取得する"""
        tip_data = random.choice(self.tips)
        embed = discord.Embed(
            title=tip_data["title"],
            description=tip_data["description"],
            color=tip_data["color"]
        )
        embed.set_footer(text="we are experiencing technical difficulties with our main server. \n full documentation : https://coffin299.net")
        return embed

    def get_waiting_embed(self, model_name: str) -> discord.Embed:
        """待機中のembedを取得する（tips付き）"""
        tip_embed = self.get_random_tip()
        tip_embed.title = f"⏳ Waiting for '{model_name}' response..."
        return tip_embed
