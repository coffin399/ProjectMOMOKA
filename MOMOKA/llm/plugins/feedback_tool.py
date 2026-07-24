# MOMOKA/llm/plugins/feedback_tool.py
# LLM 向けフィードバックツール（form / submit）。
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import discord
from discord.ext import commands

from MOMOKA.utilities.feedback import (
    FeedbackCategoryView,
    FeedbackConfirmView,
    FeedbackService,
    category_label,
    normalize_category,
    preview_embed_for_confirm,
)

logger = logging.getLogger(__name__)


class FeedbackTool:
    """バグ報告・機能リクエストを開発者チャンネルへ届ける LLM ツール。"""

    # OpenAI 互換 function calling 名
    name = "feedback"

    # ツール定義（LLM に渡すスキーマ）
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Help the user send a bug report or feature request to the bot developers. "
                "Use mode=form when details are incomplete or the user just wants to report something. "
                "Use mode=submit when category, title, and body can be drafted from the conversation "
                "(the user must still confirm with a button)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["form", "submit"],
                        "description": (
                            "form: send category buttons so the user opens a Modal. "
                            "submit: draft category/title/body and ask the user to confirm."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": ["bug", "feature_request", "other"],
                        "description": "Required for mode=submit. Report category.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Required for mode=submit. Short summary (max ~100 chars).",
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Required for mode=submit. Details: steps to reproduce, "
                            "expected vs actual, or feature rationale."
                        ),
                    },
                },
                "required": ["mode"],
            },
        },
    }

    def __init__(self, bot: commands.Bot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # 共有 FeedbackService を作る
        self.service = FeedbackService(bot)

    async def run(
        self,
        arguments: Dict[str, Any],
        channel_id: int,
        user_id: int = 0,
    ) -> str:
        """ツール実行。チャンネルへ UI を送り、LLM 向け案内文字列を返す。"""
        # 投稿先未設定なら即エラーを返す
        if not self.service.is_configured():
            return (
                "Error: Feedback destination is not configured "
                "(feedback.channel_ids is empty). "
                "Tell the user that /feedback is unavailable until an admin configures it. "
                "フィードバック送信先が未設定です。"
            )
        # モードを正規化する
        mode = str(arguments.get("mode") or "").strip().lower()
        # 不正モードは拒否する
        if mode not in ("form", "submit"):
            return (
                "Error: mode must be 'form' or 'submit'. "
                "モードは form または submit である必要があります。"
            )
        # チャンネルを解決する
        channel = self.bot.get_channel(channel_id)
        # キャッシュに無ければ fetch する
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("feedback tool: channel fetch failed %s: %s", channel_id, exc)
                return (
                    "Error: Could not find the Discord channel to send the feedback UI. "
                    "チャンネルが見つかりませんでした。"
                )
        # 送信可能か確認する
        if not hasattr(channel, "send"):
            return (
                "Error: This channel cannot receive the feedback UI. "
                "このチャンネルにはフィードバック UI を送れません。"
            )
        # 依頼者を解決する
        requester: Optional[discord.abc.User] = None
        if user_id:
            requester = self.bot.get_user(user_id)
            if requester is None:
                try:
                    requester = await self.bot.fetch_user(user_id)
                except Exception:  # noqa: BLE001
                    requester = None
        # user_id が無い場合は安全に失敗する
        if requester is None:
            return (
                "Error: Missing requester user_id; cannot authorize the feedback UI. "
                "依頼ユーザーが特定できないため UI を出せません。"
            )
        # form モード: カテゴリボタンを送る
        if mode == "form":
            return await self._run_form(channel, requester)
        # submit モード: 確認 View を送る
        return await self._run_submit(arguments, channel, requester)

    async def _run_form(
        self,
        channel: discord.abc.Messageable,
        requester: discord.abc.User,
    ) -> str:
        """カテゴリボタン View をチャンネルへ送る。"""
        # View を組み立てる
        view = FeedbackCategoryView(self.service, requester.id)
        # 案内 Embed を作る
        embed = discord.Embed(
            title="フィードバック / Feedback",
            description=(
                f"{requester.mention}\n"
                "カテゴリを選ぶと入力フォーム（Modal）が開きます。\n"
                "Select a category to open the input form (Modal)."
            ),
            color=discord.Color.blurple(),
        )
        # チャンネルへ送る
        try:
            await channel.send(embed=embed, view=view)
        except Exception as exc:  # noqa: BLE001
            logger.error("feedback tool form send failed: %s", exc)
            return (
                "Error: Failed to send the feedback form buttons. "
                "フォーム用ボタンの送信に失敗しました。"
            )
        # LLM 向け案内を返す
        return (
            "Sent category buttons for the feedback form. "
            "Tell the user to click a category button to open the Modal and fill title/details. "
            "Do not claim the report was already submitted. "
            "カテゴリボタンを送りました。ボタンを押してフォームに入力するよう案内してください。"
        )

    async def _run_submit(
        self,
        arguments: Dict[str, Any],
        channel: discord.abc.Messageable,
        requester: discord.abc.User,
    ) -> str:
        """下書き確認 View をチャンネルへ送る。"""
        # カテゴリを正規化する
        category_id = normalize_category(arguments.get("category"))
        # 必須チェックする
        if category_id is None:
            return (
                "Error: mode=submit requires category in {bug, feature_request, other}. "
                "submit モードには category が必要です。"
            )
        # タイトルを取る
        title = str(arguments.get("title") or "").strip()
        # 本文を取る
        body = str(arguments.get("body") or "").strip()
        # 空なら拒否する
        if not title or not body:
            return (
                "Error: mode=submit requires non-empty title and body. "
                "If details are incomplete, call again with mode=form. "
                "title / body が不足しています。足りなければ mode=form を使ってください。"
            )
        # 長さをクリップする
        title = title[:100]
        body = body[:1500]
        # ギルド / チャンネル ID を取る
        guild = getattr(channel, "guild", None)
        source_guild_id = guild.id if guild is not None else None
        source_channel_id = getattr(channel, "id", None)
        # 確認 View を組み立てる
        view = FeedbackConfirmView(
            self.service,
            requester.id,
            category_id=category_id,
            title=title,
            body=body,
            source_guild_id=source_guild_id,
            source_channel_id=source_channel_id,
        )
        # プレビュー Embed を作る
        embed = preview_embed_for_confirm(
            category_id=category_id,
            title=title,
            body=body,
            requester=requester,
        )
        # チャンネルへ送る
        try:
            await channel.send(
                content=f"{requester.mention}",
                embed=embed,
                view=view,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("feedback tool submit send failed: %s", exc)
            return (
                "Error: Failed to send the feedback confirmation UI. "
                "確認 UI の送信に失敗しました。"
            )
        # LLM 向け案内を返す
        return (
            f"Sent a confirmation panel for category={category_label(category_id)}. "
            "Tell the user to press Submit to deliver, Cancel to abort, "
            "or Edit in form to revise in a Modal. "
            "Do not claim it was already submitted until they confirm. "
            "確認パネルを送りました。送信ボタンを押すまで未送信です。"
        )
