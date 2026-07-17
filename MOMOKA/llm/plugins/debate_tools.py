# debate / cross_check ツール（SearchAgent パターン）。
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


class DebateTool:
    """重量級: 多ラウンド討論を開始する。"""

    name = "debate"
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Start a multi-round debate between PLANA and ARONA. "
                "Use ONLY when the user clearly wants a discussion/debate. "
                "Prefer cross_check for light verification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Discussion/debate topic",
                    },
                    "position_plana": {
                        "type": "string",
                        "description": "Optional position for PLANA",
                    },
                    "position_arona": {
                        "type": "string",
                        "description": "Optional position for ARONA",
                    },
                },
                "required": ["topic"],
            },
        },
    }

    def __init__(self, bot: "commands.Bot") -> None:
        # 起動側 Bot
        self.bot = bot

    async def run(
        self,
        *,
        arguments: Dict[str, Any],
        channel_id: int,
        user_id: int,
    ) -> str:
        """Orchestrator.start_debate を呼ぶ。"""
        from MOMOKA.llm.debate.orchestrator import orchestrator

        # Orchestrator 未初期化
        if orchestrator is None:
            return "Debate orchestrator is not initialized."
        # チャンネル取得
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as e:
                return f"Cannot resolve channel: {e}"
        # ギルド
        guild = getattr(channel, "guild", None)
        # 引数
        topic = str(arguments.get("topic") or "").strip()
        if not topic:
            return "topic is required."
        # 開始
        return await orchestrator.start_debate(
            channel=channel,
            guild=guild,
            topic=topic,
            starter_user_id=user_id,
            position_plana=arguments.get("position_plana"),
            position_arona=arguments.get("position_arona"),
        )


class CrossCheckTool:
    """軽量: PLANA案→ARONA検証。戻り値は検証全文（Step3はLLM最終応答）。"""

    name = "cross_check"
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Quickly verify your own answer with ARONA before finalizing. "
                "Use when the user asks for confirmation, fact-checking, or a second opinion, "
                "or when you are unsure. Much lighter than 'debate'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "ユーザーの元の質問・確認対象",
                    },
                    "draft_answer": {
                        "type": "string",
                        "description": "PLANAの一次回答（これをARONAが検証する）",
                    },
                },
                "required": ["question", "draft_answer"],
            },
        },
    }

    def __init__(self, bot: "commands.Bot") -> None:
        # 起動側 Bot
        self.bot = bot

    async def run(
        self,
        *,
        arguments: Dict[str, Any],
        channel_id: int,
        user_id: int,
    ) -> str:
        """Orchestrator.run_cross_check を呼ぶ。"""
        from MOMOKA.llm.debate.orchestrator import orchestrator

        # 未初期化
        if orchestrator is None:
            return "cross_check orchestrator is not initialized."
        # チャンネル
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as e:
                return f"Cannot resolve channel: {e}"
        guild = getattr(channel, "guild", None)
        question = str(arguments.get("question") or "").strip()
        draft = str(arguments.get("draft_answer") or "").strip()
        if not question:
            return "question is required."
        # initiator
        initiator = getattr(self.bot, "bot_id", "plana")
        # 実行（user_id は将来の監査用に受け取る）
        _ = user_id
        return await orchestrator.run_cross_check(
            channel=channel,
            guild=guild,
            question=question,
            draft_answer=draft,
            initiator_bot_id=initiator,
        )
