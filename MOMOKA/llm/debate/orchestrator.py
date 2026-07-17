# 討論・クロスチェック Orchestrator（プロセス内協調）。
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import discord

from MOMOKA.bots.registry import registry
from MOMOKA.llm.debate.channel_lock import channel_lock
from MOMOKA.llm.debate.stop_view import DebateStopView

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)

# LLM 出力から誤って含まれるメンション markup を除去する
_MENTION_RE = re.compile(r"<@!?\d+>")


@dataclass
class DebateSession:
    """進行中の討論セッション状態。"""

    session_id: str
    channel_id: int
    guild_id: int
    topic: str
    starter_user_id: int
    max_rounds: int
    status: str = "running"  # running | cancelled | finished
    current_round: int = 0
    transcript: List[Dict[str, str]] = field(default_factory=list)
    panel_message: Optional[discord.Message] = None
    positions: Dict[str, str] = field(default_factory=dict)


class DebateOrchestrator:
    """PLANA ↔ ARONA 討論と cross_check を駆動する。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        # マージ済み全体 config
        self.config = config
        # debate セクション
        self.debate_cfg = (config.get("debate") or {})
        # cross_check セクション
        self.cross_cfg = (config.get("cross_check") or {})
        # 管理者 ID
        self.admin_ids = set(config.get("admin_user_ids") or [])
        # 実行中バックグラウンドタスク
        self._tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 公開 API: debate（即返し + バックグラウンド完走）
    # ------------------------------------------------------------------
    async def start_debate(
        self,
        *,
        channel: discord.abc.Messageable,
        guild: Optional[discord.Guild],
        topic: str,
        starter_user_id: int,
        position_plana: Optional[str] = None,
        position_arona: Optional[str] = None,
    ) -> str:
        """討論を開始する。ツール結果は即時文字列。実処理は Task。"""
        # 無効なら拒否
        if not self.debate_cfg.get("enabled", True):
            return "Debate is disabled in config."
        # チャンネル ID
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return "Debate requires a text channel."
        # ギルド必須
        if guild is None:
            return "Debate is only available in guild channels."
        # チャンネル排他
        acquired = await channel_lock.try_acquire(channel_id, "debate")
        if not acquired:
            owner = channel_lock.owner(channel_id) or "another session"
            return f"This channel is busy with '{owner}'. Wait until it finishes."
        # 相方在籍チェック
        partner_id = self.debate_cfg.get("partner_bot", "arona")
        partner_ok, partner_msg = await self._ensure_partner_in_guild(guild, partner_id)
        if not partner_ok:
            # ロック解放
            await channel_lock.release(channel_id)
            return partner_msg
        # ポジション既定
        pos_p = position_plana or "cautious / skeptical perspective"
        pos_a = position_arona or "optimistic / proactive perspective"
        # セッション作成
        session = DebateSession(
            session_id=uuid.uuid4().hex[:12],
            channel_id=channel_id,
            guild_id=guild.id,
            topic=topic,
            starter_user_id=starter_user_id,
            max_rounds=int(self.debate_cfg.get("max_rounds", 2)),
            positions={"plana": pos_p, "arona": pos_a},
        )
        # 開会メッセージ
        opening = self._format_opening(topic)
        try:
            # パネル View を作る
            view = DebateStopView(
                session=session,
                admin_ids=self.admin_ids,
                on_stop=self._cancel_session,
            )
            # パネル投稿
            panel = await channel.send(view=view)
            # 参照を保存
            session.panel_message = panel
            # 開会テキスト（通常メッセージ）
            await channel.send(opening)
        except Exception as e:
            # 失敗時はロック解放
            await channel_lock.release(channel_id)
            logger.error("Failed to post debate panel: %s", e, exc_info=True)
            return f"Failed to start debate UI: {e}"
        # バックグラウンドで討論を走らせる
        task = asyncio.create_task(
            self._run_debate(session, channel),
            name=f"debate-{session.session_id}",
        )
        self._tasks[session.session_id] = task
        # ツール結果は即返し（長時間ブロックしない）
        return (
            f"Debate started (session={session.session_id}). "
            f"Topic: {topic}. "
            "A stop button is posted in the channel. "
            "Do not summarize the debate yourself yet; wait for the transcript in a later update if needed. "
            "Tell Sensei briefly that the debate has started."
        )

    async def _cancel_session(self, session: DebateSession) -> None:
        """中止ボタンから呼ばれる。"""
        # ステータスをキャンセルへ
        session.status = "cancelled"
        # ログ
        logger.info(
            "[%s] Debate cancelled by user (session=%s)",
            "DEBATE",
            session.session_id,
        )

    async def _run_debate(
        self,
        session: DebateSession,
        channel: discord.abc.Messageable,
    ) -> None:
        """交互討論 → 評定 → 終了。"""
        try:
            # ラウンドループ
            for r in range(1, session.max_rounds + 1):
                # キャンセル確認
                if session.status == "cancelled":
                    break
                # 進捗更新
                session.current_round = r
                await self._refresh_panel(session)
                # PLANA ターン
                await self._debate_turn(session, channel, "plana", r)
                if session.status == "cancelled":
                    break
                # ARONA ターン
                await self._debate_turn(session, channel, "arona", r)
                # 投稿間隔
                delay = float(self.debate_cfg.get("post_delay_sec", 1.5))
                await asyncio.sleep(delay)
            # 自然終了なら評定
            if session.status == "running":
                await self._judge_turn(session, channel)
                session.status = "finished"
            # パネル最終更新
            await self._refresh_panel(session)
            # 中断告知
            if session.status == "cancelled":
                await channel.send("討論は中断されました。 / Debate was cancelled.")
        except Exception as e:
            logger.error("Debate run failed: %s", e, exc_info=True)
            try:
                await channel.send(f"討論中にエラーが発生しました: {e}")
            except Exception:
                pass
            session.status = "cancelled"
        finally:
            # チャンネルロック解放
            await channel_lock.release(session.channel_id)
            # タスク辞書から除去
            self._tasks.pop(session.session_id, None)

    async def _refresh_panel(self, session: DebateSession) -> None:
        """パネル View を再構築して edit。"""
        # メッセージが無ければスキップ
        if session.panel_message is None:
            return
        # 新しい View
        view = DebateStopView(
            session=session,
            admin_ids=self.admin_ids,
            on_stop=self._cancel_session,
        )
        try:
            await session.panel_message.edit(view=view)
        except Exception as e:
            logger.warning("Panel refresh failed: %s", e)

    async def _debate_turn(
        self,
        session: DebateSession,
        channel: discord.abc.Messageable,
        speaker_id: str,
        round_i: int,
    ) -> None:
        """1 発言者の討論ターン。"""
        # システムプロンプト（討論人格）
        system = self._build_debate_system(speaker_id, session)
        # 相手の直前発言
        opponent_text = self._last_opponent_text(session, speaker_id)
        # ターン指示
        turn_tmpl = self.debate_cfg.get("turn_instruction") or ""
        turn_inst = turn_tmpl.format(
            round=round_i,
            max_rounds=session.max_rounds,
            max_chars_per_turn=self.debate_cfg.get("max_chars_per_turn", 800),
        )
        # user メッセージ
        if opponent_text:
            user_content = f"{opponent_text}\n\n{turn_inst}"
        else:
            user_content = (
                f"Debate topic: {session.topic}\n"
                f"Open the debate from your position.\n\n{turn_inst}"
            )
        # LLM 呼び出し
        text = await self._generate(
            speaker_id=speaker_id,
            system=system,
            user_content=user_content,
            max_chars=int(self.debate_cfg.get("max_chars_per_turn", 800)),
            tag="DEBATE",
        )
        # メンション付与して投稿
        partner = "arona" if speaker_id == "plana" else "plana"
        posted = await self._post_with_mention(channel, speaker_id, partner, text)
        # transcript へ
        session.transcript.append(
            {"speaker": speaker_id, "text": posted, "round": str(round_i)}
        )

    async def _judge_turn(
        self,
        session: DebateSession,
        channel: discord.abc.Messageable,
    ) -> None:
        """評定ターン（デフォルト PLANA）。メンションなし。"""
        # 評定担当
        judge_id = self.debate_cfg.get("judge_bot", "plana")
        # 評定プロンプト
        judge_tmpl = self.debate_cfg.get("judge_prompt") or ""
        # transcript 文字列
        lines = []
        for entry in session.transcript:
            lines.append(f"[{entry['speaker'].upper()}] {entry['text']}")
        transcript_text = "\n".join(lines)
        # 評定用 system（討論人格ではなく judge 指示）
        system = judge_tmpl.format(topic=session.topic)
        user_content = f"Transcript:\n{transcript_text}"
        # 生成
        text = await self._generate(
            speaker_id=judge_id,
            system=system,
            user_content=user_content,
            max_chars=1200,
            tag="DEBATE_JUDGE",
        )
        # メンションなしで投稿
        clean = _MENTION_RE.sub("", text).strip()
        bot = registry.require(judge_id)
        ch = bot.get_channel(session.channel_id) or channel
        await ch.send(f"**評定 / Verdict**\n{clean}")
        session.transcript.append({"speaker": f"judge:{judge_id}", "text": clean, "round": "judge"})

    # ------------------------------------------------------------------
    # cross_check（同期: Step1/2 投稿、戻り値は検証コメント）
    # ------------------------------------------------------------------
    async def run_cross_check(
        self,
        *,
        channel: discord.abc.Messageable,
        guild: Optional[discord.Guild],
        question: str,
        draft_answer: str,
        initiator_bot_id: str = "plana",
    ) -> str:
        """Step1/2 を投稿し、ARONA 検証全文を返す（Step3 は LLM ループ側）。"""
        # 無効なら拒否
        if not self.cross_cfg.get("enabled", True):
            return "cross_check is disabled in config."
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return "cross_check requires a text channel."
        # 排他
        acquired = await channel_lock.try_acquire(channel_id, "cross_check")
        if not acquired:
            owner = channel_lock.owner(channel_id) or "another session"
            return f"This channel is busy with '{owner}'. Cannot cross_check now."
        try:
            reviewer_id = self.cross_cfg.get("reviewer_bot", "arona")
            max_chars = int(self.cross_cfg.get("max_chars_review", 600))
            # Step1: draft を投稿（initiator 視点。通常は PLANA）
            draft_bot = "plana" if initiator_bot_id != "arona" else "plana"
            # フロー固定: PLANA 案 → ARONA 検証
            draft_bot = "plana"
            # draft 本文（引数を優先。空なら生成）
            draft = (draft_answer or "").strip()
            if not draft:
                draft = await self._generate_cross_draft(question, max_chars)
            # Step1 投稿
            if guild is not None:
                await self._post_with_mention(channel, draft_bot, reviewer_id, draft)
            else:
                await channel.send(draft)
            # 相方在籍
            partner_present = False
            if guild is not None:
                partner_present, invite_msg = await self._ensure_partner_in_guild(
                    guild, reviewer_id
                )
            else:
                invite_msg = ""
            # Step2: 検証
            if partner_present:
                review = await self._generate_cross_review(question, draft, max_chars)
                await self._post_with_mention(channel, reviewer_id, draft_bot, review)
            else:
                # フォールバック: プロセス内で ARONA persona により検証（投稿省略）
                review = await self._generate_cross_review(question, draft, max_chars)
                review = (
                    f"[ARONA offline review — not posted]\n{review}\n"
                    f"(Partner not in guild. {invite_msg})"
                )
            # tool 戻り値 = 検証全文（Step3 は LLM 最終応答のみ）
            return review
        finally:
            # ロック解放
            await channel_lock.release(channel_id)

    async def _generate_cross_draft(self, question: str, max_chars: int) -> str:
        """Step1 用ドラフト生成（通常 persona）。"""
        # 指示テンプレ
        tmpl = self.cross_cfg.get("draft_instruction") or ""
        instruction = tmpl.format(max_chars_review=max_chars)
        # 通常 persona system
        system = self._normal_persona_system("plana") + "\n\n" + instruction
        return await self._generate(
            speaker_id="plana",
            system=system,
            user_content=question,
            max_chars=max_chars,
            tag="CROSS_CHECK",
        )

    async def _generate_cross_review(
        self, question: str, draft: str, max_chars: int
    ) -> str:
        """Step2 検証生成（通常 ARONA persona）。"""
        tmpl = self.cross_cfg.get("review_prompt") or ""
        system = tmpl.format(
            question=question,
            draft_answer=draft,
            max_chars_review=max_chars,
        )
        # review_prompt 自体が指示なので、通常 persona を前置きしてもよいが
        # plan では「通常 persona を維持」→ ARONA system + review 指示
        persona = self._normal_persona_system("arona")
        full_system = f"{persona}\n\n{system}"
        return await self._generate(
            speaker_id="arona",
            system=full_system,
            user_content="Please review the draft above.",
            max_chars=max_chars,
            tag="CROSS_CHECK",
        )

    # ------------------------------------------------------------------
    # ヘルパ
    # ------------------------------------------------------------------
    def _format_opening(self, topic: str) -> str:
        """開会文（日英）。"""
        ja = (self.debate_cfg.get("opening_notice_ja") or "").format(topic=topic)
        en = (self.debate_cfg.get("opening_notice_en") or "").format(topic=topic)
        return f"{ja.strip()}\n{en.strip()}".strip()

    def _build_debate_system(self, speaker_id: str, session: DebateSession) -> str:
        """討論人格 system（通常 persona・tools なし）。"""
        rules_tmpl = self.debate_cfg.get("debate_rules") or ""
        position = session.positions.get(speaker_id, "free discussion from your own perspective")
        rules = rules_tmpl.format(topic=session.topic, position=position)
        persona_key = f"debate_persona_{speaker_id}"
        persona_tmpl = self.debate_cfg.get(persona_key) or ""
        return persona_tmpl.format(debate_rules=rules)

    def _normal_persona_system(self, persona_key: str) -> str:
        """通常チャット用 persona system（tools なし・日付展開）。"""
        from datetime import datetime, timezone, timedelta

        llm = self.config.get("llm") or {}
        personas = llm.get("personas") or {}
        entry = personas.get(persona_key) or {}
        tmpl = entry.get("system_prompt") or ""
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst)
        try:
            return tmpl.format(
                current_date=now.strftime("%Y-%m-%d"),
                current_time=now.strftime("%H:%M"),
            )
        except (KeyError, ValueError):
            return (
                tmpl.replace("{current_date}", now.strftime("%Y-%m-%d"))
                .replace("{current_time}", now.strftime("%H:%M"))
            )

    def _last_opponent_text(
        self, session: DebateSession, speaker_id: str
    ) -> str:
        """直前の相手発言本文。"""
        for entry in reversed(session.transcript):
            if entry["speaker"] != speaker_id and not entry["speaker"].startswith("judge"):
                return entry["text"]
        return ""

    async def _ensure_partner_in_guild(
        self, guild: discord.Guild, partner_bot_id: str
    ) -> tuple:
        """相方がギルドにいるか fetch_member で確認。"""
        # 相方 user id
        partner_uid = registry.user_id(partner_bot_id)
        # 未ログイン
        if partner_uid is None:
            invite = self._invite_url(partner_bot_id)
            name = registry.display_name(partner_bot_id)
            return False, (
                f"Partner bot {name} is not logged in. "
                f"Invite: {invite}"
            )
        try:
            # Members Intent 不要の fetch
            await guild.fetch_member(partner_uid)
            return True, ""
        except discord.NotFound:
            invite = self._invite_url(partner_bot_id)
            name = registry.display_name(partner_bot_id)
            return False, (
                f"討論には {name} がこのサーバーに必要です。"
                f"招待して、同じ権限ロールを付けてください。\n"
                f"Debate requires {name} in this server.\n"
                f"[Invite {name}]({invite})"
            )
        except discord.HTTPException as e:
            return False, f"Failed to check partner presence: {e}"

    def _invite_url(self, bot_id: str) -> str:
        """bots_config の invite_url。"""
        bots = self.config.get("bots") or {}
        entry = bots.get(bot_id) or {}
        return entry.get("invite_url") or ""

    async def _post_with_mention(
        self,
        channel: discord.abc.Messageable,
        speaker_id: str,
        mention_target_id: str,
        text: str,
    ) -> str:
        """Orchestrator がメンションを文頭付与して投稿する。"""
        # LLM 出力の <@...> を除去
        clean = _MENTION_RE.sub("", text or "").strip()
        # 文字数制限
        max_len = 1900
        if len(clean) > max_len:
            clean = clean[: max_len - 1] + "…"
        # メンション要否
        mention_on = True
        if speaker_id in ("plana", "arona"):
            # debate / cross の設定を参照
            mention_on = bool(
                self.debate_cfg.get("mention_each_other", True)
                if True
                else True
            )
        # 文頭メンション
        prefix = registry.mention(mention_target_id) if mention_on else ""
        content = f"{prefix} {clean}".strip() if prefix else clean
        # 発言 Bot の Client で投稿（見た目をその Bot にする）
        bot = registry.get(speaker_id)
        if bot is None:
            # フォールバック: 渡された channel
            msg = await channel.send(content)
            return clean
        # 同一チャンネルを Bot 側から取得
        ch_id = getattr(channel, "id", None)
        target = bot.get_channel(ch_id) if ch_id else None
        if target is None:
            # fetch を試す
            try:
                if ch_id:
                    target = await bot.fetch_channel(ch_id)
            except Exception:
                target = channel
        # 投稿
        await target.send(content)
        # ログタグ
        tag = registry.display_name(speaker_id)
        logger.info("🤖 [LLM_RESPONSE][%s] %s", tag, clean.replace("\n", " ")[:500])
        return clean

    async def _generate(
        self,
        *,
        speaker_id: str,
        system: str,
        user_content: str,
        max_chars: int,
        tag: str,
    ) -> str:
        """指定 Bot の LLMCog 経由で一括生成（tools なし）。"""
        bot = registry.require(speaker_id)
        llm_cog = bot.get_cog("LLM")
        if llm_cog is None:
            return f"(LLM cog missing on {speaker_id})"
        # Cog のヘルパを使う
        if hasattr(llm_cog, "generate_plain"):
            text = await llm_cog.generate_plain(
                system=system,
                user_content=user_content,
                max_chars=max_chars,
            )
        else:
            text = "(generate_plain not available)"
        # ログ
        display = registry.display_name(speaker_id)
        logger.info(
            "🤖 [LLM_RESPONSE][%s][%s] %s",
            display,
            tag,
            (text or "").replace("\n", " ")[:500],
        )
        return (text or "").strip()


# グローバル Orchestrator（main 起動時に config で再初期化）
orchestrator: Optional[DebateOrchestrator] = None


def init_orchestrator(config: Dict[str, Any]) -> DebateOrchestrator:
    """プロセス共通 Orchestrator を初期化する。"""
    global orchestrator
    orchestrator = DebateOrchestrator(config)
    return orchestrator
