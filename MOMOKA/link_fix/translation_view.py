# MOMOKA/link_fix/translation_view.py
# Twitter Fix 返信用の 🌐 / 国旗 切替（クラシック View）。
from __future__ import annotations

import logging
from typing import Optional

import discord

from MOMOKA.link_fix.websites import apply_twitter_lang, format_reply_line
from MOMOKA.link_fix.websites import MatchedLink

logger = logging.getLogger(__name__)


class TwitterTranslationView(discord.ui.View):
    """左🌐・右国旗で原文／翻訳 Fix URL を切り替える。"""

    def __init__(
        self,
        *,
        original_url: str,
        label: str,
        fixer_name: str,
        base_fix_url: str,
        lang: str,
        flag_emoji: str,
        footnote: str,
        timeout: float = 3600,
    ) -> None:
        # 長めのタイムアウトで初期化する
        super().__init__(timeout=timeout)
        # 元投稿 URL
        self.original_url = original_url
        # 表示ラベル
        self.label = label
        # Fixer 名
        self.fixer_name = fixer_name
        # 言語サフィックス無しの Fix URL
        self.base_fix_url = apply_twitter_lang(base_fix_url, None)
        # 翻訳先 ISO
        self.lang = lang
        # 注記
        self.footnote = footnote
        # 現在が翻訳中か（初期は翻訳）
        self.is_translated = True
        # 🌐 ボタンを左に追加する
        global_btn = discord.ui.Button(
            emoji="🌐",
            style=discord.ButtonStyle.secondary,
            custom_id="linkfix_tr_global",
            row=0,
        )
        # コールバックを結ぶ
        global_btn.callback = self._on_global
        # 追加する
        self.add_item(global_btn)
        # 国旗ボタンを右に追加する
        flag_btn = discord.ui.Button(
            emoji=flag_emoji,
            style=discord.ButtonStyle.primary,
            custom_id="linkfix_tr_flag",
            row=0,
        )
        # コールバックを結ぶ
        flag_btn.callback = self._on_flag
        # 追加する
        self.add_item(flag_btn)

    def _build_content(self, fix_url: str) -> str:
        """返信本文を組み立てる。"""
        # MatchedLink 相当の1行
        link = MatchedLink(
            site_id="twitter",
            label=self.label,
            original_url=self.original_url,
            fix_url=fix_url,
            fixer_name=self.fixer_name,
            fix_domain="",
        )
        # 本文＋注記
        return f"{format_reply_line(link)}\n-# {self.footnote}"

    async def _on_global(self, interaction: discord.Interaction) -> None:
        """原文（言語サフィックス無し）へ切替。"""
        # 応答を保留する
        await interaction.response.defer()
        # 既に原文なら何もしない
        if not self.is_translated:
            return
        # フラグを更新する
        self.is_translated = False
        # メッセージを編集する
        try:
            await interaction.message.edit(content=self._build_content(self.base_fix_url), view=self)
        except discord.HTTPException as exc:
            # 失敗をログする
            logger.warning("Failed to switch to global twitter fix: %s", exc)

    async def _on_flag(self, interaction: discord.Interaction) -> None:
        """翻訳 URL へ切替。"""
        # 応答を保留する
        await interaction.response.defer()
        # 既に翻訳なら何もしない
        if self.is_translated:
            return
        # フラグを更新する
        self.is_translated = True
        # 翻訳 URL
        translated = apply_twitter_lang(self.base_fix_url, self.lang)
        # メッセージを編集する
        try:
            await interaction.message.edit(content=self._build_content(translated), view=self)
        except discord.HTTPException as exc:
            # 失敗をログする
            logger.warning("Failed to switch to translated twitter fix: %s", exc)


def maybe_make_twitter_translation_view(
    *,
    site_id: str,
    original_url: str,
    label: str,
    fixer_name: str,
    fix_url: str,
    fix_domain: str,
    supports_tr: bool,
    locale_info: Optional[tuple[str, str]],
    footnote: str,
    timeout: float,
) -> Optional[TwitterTranslationView]:
    """条件を満たすときだけ TranslationView を返す。"""
    # Twitter 以外は無し
    if site_id != "twitter":
        return None
    # 翻訳非対応ドメインは無し
    if not supports_tr:
        return None
    # locale 不明は無し
    if not locale_info:
        return None
    # 分解する
    lang, flag = locale_info
    # View を作って返す
    return TwitterTranslationView(
        original_url=original_url,
        label=label,
        fixer_name=fixer_name,
        base_fix_url=fix_url,
        lang=lang,
        flag_emoji=flag,
        footnote=footnote,
        timeout=timeout,
    )
