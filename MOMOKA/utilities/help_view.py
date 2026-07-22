# MOMOKA/utilities/help_view.py
# /help と /invite 用 Components V2 LayoutView。
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import discord
from discord.ext import commands

from MOMOKA.utilities.donation import (
    donation_from_bot,
    help_donation_body,
    make_help_link_button,
)

logger = logging.getLogger(__name__)

# ページ総数（0..4）
_HELP_PAGE_COUNT = 5

# 無効とみなす招待 URL プレースホルダ
_INVALID_INVITE_PLACEHOLDERS = frozenset(
    {
        "",
        "YOUR_BOT_INVITE_LINK_HERE",
        "HOGE_FUGA_PIYO",
    }
)


def _is_valid_invite_url(url: Optional[str]) -> bool:
    """招待 URL が実運用可能か判定する。"""
    # 空や非文字列は無効
    if not url or not isinstance(url, str):
        return False
    # 前後空白を除去する
    cleaned = url.strip()
    # http(s) で始まらなければリンクボタンに使えない
    if not cleaned.startswith("http"):
        return False
    # 既知プレースホルダは無効
    if cleaned in _INVALID_INVITE_PLACEHOLDERS:
        return False
    # YOUR_ を含む未設定値も無効
    if "YOUR_" in cleaned.upper():
        return False
    # ここまで来たら有効
    return True


def resolve_invite_urls(bot: commands.Bot) -> Tuple[str, str]:
    """bots.plana / bots.arona の invite_url を返す（無ければ空文字）。"""
    # config 辞書を取得する（無ければ空）
    config: Dict[str, Any] = getattr(bot, "config", None) or {}
    # bots セクションを読む
    bots = config.get("bots") or {}
    # PLANA エントリ
    plana = bots.get("plana") or {}
    # ARONA エントリ
    arona = bots.get("arona") or {}
    # 各 invite_url（無ければ空）
    plana_url = str(plana.get("invite_url") or "").strip()
    arona_url = str(arona.get("invite_url") or "").strip()
    # タプルで返す
    return plana_url, arona_url


def _is_companion(bot: commands.Bot) -> bool:
    """companion（ARONA）ロールかどうか。"""
    # bot_role 属性を読む（未設定時は primary）
    return getattr(bot, "bot_role", "primary") == "companion"


class HelpLayoutView(discord.ui.LayoutView):
    """ /help 用ページング LayoutView（Components V2）。"""

    def __init__(self, bot: commands.Bot, *, page: int = 0) -> None:
        # 長時間表示するためタイムアウトなし
        super().__init__(timeout=None)
        # Bot 参照を保持する
        self.bot = bot
        # 招待 URL を config から解決する
        self.plana_invite, self.arona_invite = resolve_invite_urls(bot)
        # companion なら PLANA 誘導注記を出す
        self.is_companion = _is_companion(bot)
        # ページ番号を範囲内に正規化する
        self.page = max(0, min(int(page), _HELP_PAGE_COUNT - 1))
        # UI を組み立てる
        self._rebuild()

    def _rebuild(self) -> None:
        """現在ページの TextDisplay / ボタンを再構築する。"""
        # 既存アイテムを全部消す
        self.clear_items()
        # アクセント色（ティール系）
        accent = discord.Color.teal()
        # コンテナを作る
        container = discord.ui.Container(accent_color=accent)
        # ページ本文を取得する
        body = self._page_text(self.page)
        # TextDisplay で本文を載せる
        container.add_item(discord.ui.TextDisplay(body))
        # Overview のみ招待リンク行を付ける
        if self.page == 0:
            # 招待用 ActionRow
            invite_row = discord.ui.ActionRow()
            # ボタン追加有無フラグ
            has_invite_btn = False
            # PLANA 招待が有効ならリンクボタンを追加
            if _is_valid_invite_url(self.plana_invite):
                invite_row.add_item(
                    discord.ui.Button(
                        label="Invite PLANA",
                        style=discord.ButtonStyle.link,
                        url=self.plana_invite,
                        emoji="💌",
                    )
                )
                # 追加済みにする
                has_invite_btn = True
            # ARONA 招待が有効ならリンクボタンを追加
            if _is_valid_invite_url(self.arona_invite):
                invite_row.add_item(
                    discord.ui.Button(
                        label="Invite ARONA",
                        style=discord.ButtonStyle.link,
                        url=self.arona_invite,
                        emoji="🎵",
                    )
                )
                # 追加済みにする
                has_invite_btn = True
            # ボタンが1つ以上あれば行を載せる
            if has_invite_btn:
                container.add_item(invite_row)
            # 寄付（enabled 時は堂々と本文＋ボタン）
            donation = donation_from_bot(self.bot)
            if donation.enabled:
                # 日英の支援文を載せる
                container.add_item(discord.ui.TextDisplay(help_donation_body(donation)))
                # Ko-fi リンクボタン行
                donation_row = discord.ui.ActionRow()
                help_btn = make_help_link_button(donation)
                if help_btn is not None:
                    donation_row.add_item(help_btn)
                    container.add_item(donation_row)
        # Prev / Next 用 ActionRow
        nav_row = discord.ui.ActionRow()
        # 前へボタン（先頭ページでは無効）
        prev_btn = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="help_prev",
            disabled=(self.page <= 0),
        )
        # コールバックを結ぶ
        prev_btn.callback = self._prev_callback
        # 行に追加
        nav_row.add_item(prev_btn)
        # ページ表示用の無効ボタン（ラベルのみ）
        page_btn = discord.ui.Button(
            label=f"{self.page + 1}/{_HELP_PAGE_COUNT}",
            style=discord.ButtonStyle.primary,
            custom_id="help_page_indicator",
            disabled=True,
        )
        # 行に追加
        nav_row.add_item(page_btn)
        # 次へボタン（最終ページでは無効）
        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="help_next",
            disabled=(self.page >= _HELP_PAGE_COUNT - 1),
        )
        # コールバックを結ぶ
        next_btn.callback = self._next_callback
        # 行に追加
        nav_row.add_item(next_btn)
        # ナビ行をコンテナへ
        container.add_item(nav_row)
        # ルートにコンテナを追加
        self.add_item(container)

    def _page_text(self, page: int) -> str:
        """ページ番号に応じた日英併記本文。"""
        # ページ分岐
        if page == 0:
            return self._overview_text()
        if page == 1:
            return self._llm_text()
        if page == 2:
            return self._music_text()
        if page == 3:
            return self._utilities_text()
        return self._guidelines_text()

    def _overview_text(self) -> str:
        """Page 0: Overview。"""
        # Bot 表示名
        bot_name = self.bot.user.name if self.bot.user else "MOMOKA"
        # companion 向け機能説明を分岐する
        if self.is_companion:
            # ARONA: 重い機能は PLANA 誘導
            features_ja = (
                f"**この Bot（{bot_name} / ARONA）で使える機能**\n"
                "• AI 対話（メンション / `/chat`）\n"
                "• 音楽再生（`/play` など）\n"
                "• ユーティリティ（ダイス・`/help`・`/invite` など）\n\n"
                "**PLANA 専用（こちらでは使えません）**\n"
                "• TTS / 画像生成 / 通知 / トラッカー\n"
                "→ `/invite` から PLANA を追加し、そちらでご利用ください。"
            )
            features_en = (
                f"**Available on this bot ({bot_name} / ARONA)**\n"
                "• AI chat (mention / `/chat`)\n"
                "• Music (`/play`, etc.)\n"
                "• Utilities (dice, `/help`, `/invite`, …)\n\n"
                "**PLANA-only (not on ARONA)**\n"
                "• TTS / image generation / notifications / trackers\n"
                "→ Invite PLANA via `/invite` and use those features there."
            )
        else:
            # PLANA: フル機能概要
            features_ja = (
                f"**この Bot（{bot_name} / PLANA）の主な機能**\n"
                "• AI 対話・ツール・討論\n"
                "• 音楽再生\n"
                "• TTS / 画像生成 / 通知 / トラッカー\n"
                "• ユーティリティ（ダイス・更新履歴・サポート）"
            )
            features_en = (
                f"**Main features on this bot ({bot_name} / PLANA)**\n"
                "• AI chat, tools, and debate\n"
                "• Music playback\n"
                "• TTS / image generation / notifications / trackers\n"
                "• Utilities (dice, updates, support)"
            )
        # 表紙本文を組み立てる
        return (
            f"### 📜 MOMOKA Help — Overview\n"
            f"**MOMOKA** は PLANA + ARONA のマルチ Bot 基盤です。\n"
            f"**MOMOKA** is the multi-bot platform powering **PLANA** and **ARONA**.\n\n"
            f"{features_ja}\n\n{features_en}\n\n"
            f"ページ切替: Next / Prev  |  詳細は各ページへ\n"
            f"Use Next / Prev to browse pages (LLM / Music / Utilities / Guidelines)."
        )

    def _llm_text(self) -> str:
        """Page 1: LLM（旧 llm_help 相当）。"""
        # companion 注記
        companion_note = ""
        if self.is_companion:
            companion_note = (
                "\n\n⚠️ 画像生成などの高度ツールは PLANA 専用です。"
                "\n⚠️ Advanced tools such as image generation are PLANA-only."
            )
        return (
            "### 🤖 AI / LLM\n"
            "**使い方 / How to use**\n"
            "• Bot をメンションして話しかける / Mention the bot to chat\n"
            "• Bot の返信にリプライで会話継続 / Reply to bot messages to continue\n"
            "• `/chat <message>` — メンションなしの単発対話（履歴非保存）\n"
            "• `/chat <message>` — one-shot chat without mention (no history)\n\n"
            "**便利なコマンド / Useful commands**\n"
            "• `/switch-models` — チャンネルの AI モデル切替 / Switch channel model\n"
            "• `/clear_history` — 会話履歴リセット / Reset conversation history\n\n"
            "**メモ / Notes**\n"
            "• 対応モデルなら画像添付を認識できます / Vision-capable models can read images\n"
            "• 履歴はチャンネル単位で保持されます / History is kept per channel"
            f"{companion_note}"
        )

    def _music_text(self) -> str:
        """Page 2: Music（旧 music_help 相当）。"""
        return (
            "### 🎵 Music\n"
            "PLANA / ARONA のどちらでも音楽機能を利用できます。\n"
            "Music works on both PLANA and ARONA.\n\n"
            "**再生コントロール / Playback**\n"
            "`/play <name|URL>` — 再生/キュー追加 / Play or enqueue\n"
            "`/pause` `/resume` `/stop` `/skip` — 一時停止・再開・停止・スキップ\n"
            "`/seek <time>` — 指定時刻へ移動 / Seek\n"
            "`/volume <0-200>` — 音量 / Volume\n\n"
            "**キュー / Queue**\n"
            "`/queue` `/nowplaying` `/shuffle` `/clear` `/remove <n>` `/loop <off|one|all>`\n\n"
            "**ボイス / Voice**\n"
            "`/join` `/leave` — VC 接続・切断 / Join or leave VC\n\n"
            "Now Playing パネルのボタンでも Pause / Skip / Stop 操作ができます。\n"
            "You can also control playback from the Now Playing panel buttons."
        )

    def _utilities_text(self) -> str:
        """Page 3: Utilities。"""
        return (
            "### 🛠️ Utilities\n"
            "**ダイス / Dice**\n"
            "• `/roll <notation>` — nDn（例: `2d6+3`）\n"
            "• `/diceroll <min> <max>` — 範囲指定ロール\n"
            "• `/check <notation> [cond] [target]` — 条件判定付き\n\n"
            "**招待・更新・サポート / Invite & Support**\n"
            "• `/invite` — PLANA / ARONA の招待リンク\n"
            "• `/updates` — GitHub コミット履歴\n"
            "• `/support` — 開発者への連絡方法\n"
            "• Overview ページの Ko-fi — サーバー代の支援 / Server-cost support\n\n"
            "**メディアダウンロード / Media download**（Components V2）\n"
            "• `/download_video <query>` — 動画を取得し Google Drive 経由で共有\n"
            "• `/download_video <query>` — download video and share via Google Drive\n"
            "• `/download_audio <query> <format>` — 音声抽出（mp3 / m4a / opus / flac / wav）\n"
            "• `/download_audio <query> <format>` — extract audio (mp3 / m4a / opus / flac / wav)\n"
            "• 動画選択後は最良音声と自動結合します / Selected video is auto-merged with best audio\n\n"
            "**その他 / Other**\n"
            "• `/ping` `/serverinfo` `/userinfo` `/avatar` `/gacha` `/meow`"
        )

    def _guidelines_text(self) -> str:
        """Page 4: Guidelines。"""
        # companion 向け PLANA 専用注記
        if self.is_companion:
            plana_only = (
                "\n\n**ARONA について / About ARONA**\n"
                "TTS・画像生成・通知・トラッカーは **PLANA 専用**です。\n"
                "TTS, image generation, notifications, and trackers are **PLANA-only**.\n"
                "下の `/invite` または Overview の Invite PLANA から追加してください。\n"
                "Invite PLANA from `/invite` or the Overview page buttons."
            )
        else:
            plana_only = (
                "\n\n**PLANA 向け高度機能 / Advanced (PLANA)**\n"
                "TTS・画像・通知・トラッカーは主に PLANA で提供されます。\n"
                "TTS, images, notifications, and trackers are primarily on PLANA.\n"
                "相方の ARONA は LLM + 音楽中心のコンパニオンです。\n"
                "ARONA is a companion focused on LLM + music."
            )
        return (
            "### 📋 Guidelines — AI 利用について\n"
            "**お願い / Please**\n"
            "• 法令・Discord ToS・サーバー規則を守ってください\n"
            "• Follow laws, Discord ToS, and your server rules\n"
            "• 個人情報や秘密情報をむやみに送らないでください\n"
            "• Do not share personal or confidential data casually\n"
            "• AI の回答は誤る可能性があります。重要判断は自己責任で\n"
            "• AI output can be wrong; verify critical decisions yourself\n"
            "• 生成コンテンツの公開・商用利用は各モデル規約に従ってください\n"
            "• Follow each model provider's terms for generated content"
            f"{plana_only}\n\n"
            "リポジトリ / Repository: https://github.com/coffin399/ProjectMOMOKA"
        )

    async def _prev_callback(self, interaction: discord.Interaction) -> None:
        """前ページへ移動してメッセージを編集する。"""
        # 先頭なら何もしない
        if self.page <= 0:
            # 応答だけ返す
            await interaction.response.defer()
            return
        # ページを1つ戻す
        self.page -= 1
        # UI を組み直す
        self._rebuild()
        # 元メッセージを view のみで更新する（V2 は embed 併用不可）
        await interaction.response.edit_message(view=self)

    async def _next_callback(self, interaction: discord.Interaction) -> None:
        """次ページへ移動してメッセージを編集する。"""
        # 末尾なら何もしない
        if self.page >= _HELP_PAGE_COUNT - 1:
            # 応答だけ返す
            await interaction.response.defer()
            return
        # ページを1つ進める
        self.page += 1
        # UI を組み直す
        self._rebuild()
        # 元メッセージを view のみで更新する
        await interaction.response.edit_message(view=self)


class InviteLayoutView(discord.ui.LayoutView):
    """ /invite 用 LayoutView（PLANA / ARONA リンク）。"""

    def __init__(self, bot: commands.Bot) -> None:
        # 長時間表示
        super().__init__(timeout=None)
        # Bot 参照
        self.bot = bot
        # 招待 URL 解決
        self.plana_invite, self.arona_invite = resolve_invite_urls(bot)
        # UI 構築
        self._rebuild()

    def _rebuild(self) -> None:
        """本文とリンクボタンを組み立てる。"""
        # クリア
        self.clear_items()
        # 有効判定
        plana_ok = _is_valid_invite_url(self.plana_invite)
        arona_ok = _is_valid_invite_url(self.arona_invite)
        # 本文（日英）
        if plana_ok or arona_ok:
            body = (
                "### 💌 Invite PLANA / ARONA\n"
                "下のボタンから Bot をサーバーに招待できます。\n"
                "Use the buttons below to invite the bots to your server.\n\n"
                "• **PLANA** — フル機能（LLM / 音楽 / TTS / 画像 / 通知 / tracker）\n"
                "• **ARONA** — コンパニオン（LLM / 音楽 / ユーティリティ）"
            )
        else:
            body = (
                "### 💌 Invite\n"
                "招待 URL が `bots.plana.invite_url` / `bots.arona.invite_url` に"
                "正しく設定されていません。管理者にご連絡ください。\n\n"
                "Invite URLs are not set correctly in "
                "`bots.plana.invite_url` / `bots.arona.invite_url`. "
                "Please contact the bot administrator."
            )
        # コンテナ
        container = discord.ui.Container(accent_color=discord.Color.og_blurple())
        # 本文
        container.add_item(discord.ui.TextDisplay(body))
        # リンク行
        if plana_ok or arona_ok:
            row = discord.ui.ActionRow()
            # PLANA
            if plana_ok:
                row.add_item(
                    discord.ui.Button(
                        label="Invite PLANA",
                        style=discord.ButtonStyle.link,
                        url=self.plana_invite,
                        emoji="💌",
                    )
                )
            # ARONA
            if arona_ok:
                row.add_item(
                    discord.ui.Button(
                        label="Invite ARONA",
                        style=discord.ButtonStyle.link,
                        url=self.arona_invite,
                        emoji="🎵",
                    )
                )
            # 行を載せる
            container.add_item(row)
        # ルートへ
        self.add_item(container)
