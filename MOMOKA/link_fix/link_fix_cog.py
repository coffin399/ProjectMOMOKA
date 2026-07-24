# MOMOKA/link_fix/link_fix_cog.py
# 壊れた SNS embed を検知し、Fix プロキシ URL を silent 引用返信する。
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from MOMOKA.link_fix.locale_flags import resolve_locale
from MOMOKA.link_fix.presets import (
    get_link_fix_config,
    list_site_ids,
    supports_translation,
)
from MOMOKA.link_fix.settings_store import LinkFixSettingsStore
from MOMOKA.link_fix.settings_view import LinkFixSettingsView
from MOMOKA.link_fix.translation_view import maybe_make_twitter_translation_view
from MOMOKA.link_fix.url_utils import (
    extract_previewable_urls,
    is_embed_broken_or_missing,
)
from MOMOKA.link_fix.websites import (
    MatchedLink,
    format_reply_line,
    match_urls,
)

logger = logging.getLogger(__name__)


class LinkFixCog(commands.Cog):
    """SNS リンクの壊れた embed を Fix URL で代替表示する Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # 設定 dict
        self.bot_config: Dict[str, Any] = getattr(bot, "config", None) or {}
        # link_fix セクション
        self.section = get_link_fix_config(self.bot_config)
        # プロジェクトルート（MOMOKA/link_fix → 親の親の親）
        root = Path(__file__).resolve().parents[2]
        # ギルド設定ストア
        self.store = LinkFixSettingsStore(self.bot_config, project_root=root)

    def _cfg(self, key: str, default: Any = None) -> Any:
        """link_fix セクションから値を取る。"""
        return self.section.get(key, default)

    async def _wait_for_embeds(
        self,
        message: discord.Message,
        timeout: float,
    ) -> discord.Message:
        """embed が増えるまで message_edit を待つ。タイムアウト後は再取得。"""
        # 既に embed があればそのまま
        if message.embeds:
            return message

        # 編集チェック
        def _check(before: discord.Message, after: discord.Message) -> bool:
            # 同一メッセージで embed が増えたか
            return after.id == message.id and len(after.embeds) > len(before.embeds)

        # 待つ
        try:
            _, after = await self.bot.wait_for(
                "message_edit",
                check=_check,
                timeout=timeout,
            )
            # 編集後を返す
            return after
        except asyncio.TimeoutError:
            # チャンネルから再取得を試みる
            try:
                return await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.HTTPException):
                # 失敗時は元を返す
                return message

    def _build_reply_content(self, links: List[MatchedLink]) -> str:
        """返信本文（複数リンク＋注記）。"""
        # 各行
        lines = [format_reply_line(link) for link in links]
        # 注記
        footnote = str(self._cfg("footnote") or "")
        # 注記があれば -# 行を付ける
        if footnote:
            lines.append(f"-# {footnote}")
        # 結合する
        return "\n".join(lines)

    async def _suppress_original(self, message: discord.Message) -> None:
        """可能なら元メッセージの embed を抑制する。"""
        # ギルド無ければ不可
        if not message.guild:
            return
        # me
        me = message.guild.me
        if me is None:
            return
        # 権限
        perms = message.channel.permissions_for(me)
        # Manage Messages が無ければスキップ
        if not perms.manage_messages:
            return
        # 抑制を試みる
        try:
            await message.edit(suppress=True)
            # Discord が再付与することがあるので短く待って再試行
            await asyncio.sleep(1.0)
            # 再取得
            try:
                refreshed = await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.HTTPException):
                return
            # まだ embed があれば再抑制
            if refreshed.embeds:
                await refreshed.edit(suppress=True)
        except (discord.Forbidden, discord.HTTPException) as exc:
            # 抑制失敗は致命ではない
            logger.debug("suppress failed: %s", exc)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """対象 URL があり公式 embed が壊れているときだけ Fix 返信する。"""
        # Bot / webhook / システムは無視
        if message.author.bot or message.webhook_id or message.is_system():
            return
        # ギルドのみ（DM は設定が無い）
        if not message.guild or not message.content:
            return
        # ギルド全体オフなら終了
        if not self.store.is_feature_enabled(message.guild.id):
            return
        # 無視キーワード
        ignore_kw = str(self._cfg("ignore_keyword") or "fxignore").strip()
        if ignore_kw and ignore_kw.lower() in message.content.lower():
            return
        # 権限チェック（送信・embed）
        me = message.guild.me
        if me is None:
            return
        perms = message.channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links):
            return
        # URL 抽出
        urls = extract_previewable_urls(message.content)
        if not urls:
            return
        # ギルドサイト上書き
        guild_sites = self.store.get_all_sites_overrides(message.guild.id)
        # 無効サイトを擬似上書きで落とす（match 前フィルタ）
        effective_sites: Dict[str, Any] = dict(guild_sites)

        for site_id in list_site_ids(self.bot_config):
            # サイト無効なら enabled=False を載せる
            if not self.store.is_site_enabled(message.guild.id, site_id):
                entry = dict(effective_sites.get(site_id) or {})
                entry["enabled"] = False
                effective_sites[site_id] = entry
        # locale（Twitter 翻訳用）
        locale_info = resolve_locale(getattr(message.guild, "preferred_locale", None))
        translate_lang = locale_info[0] if locale_info else None
        # マッチ（初期は翻訳 lang 付きで Twitter を組む）
        matched = match_urls(
            urls,
            self.bot_config,
            effective_sites,
            translate_lang=translate_lang,
        )
        if not matched:
            return
        # 公式 embed 待ち
        wait_s = float(self._cfg("embed_wait_seconds") or 5.0)
        refreshed = await self._wait_for_embeds(message, wait_s)
        # 壊れているリンクだけ残す
        broken: List[MatchedLink] = []
        for link in matched:
            if is_embed_broken_or_missing(refreshed.embeds, link.original_url):
                broken.append(link)
        # 無ければ何もしない
        if not broken:
            return
        # 返信本文
        content = self._build_reply_content(broken)
        # Twitter 単独かつ翻訳対応なら View を付ける
        view: Optional[discord.ui.View] = None
        if len(broken) == 1 and broken[0].site_id == "twitter":
            link = broken[0]
            view = maybe_make_twitter_translation_view(
                site_id=link.site_id,
                original_url=link.original_url,
                label=link.label,
                fixer_name=link.fixer_name,
                fix_url=link.fix_url,
                fix_domain=link.fix_domain,
                supports_tr=supports_translation(
                    self.bot_config, link.site_id, link.fix_domain
                ),
                locale_info=locale_info,
                footnote=str(self._cfg("footnote") or ""),
                timeout=float(self._cfg("translation_view_timeout") or 3600),
            )
        # silent 引用返信
        silent = bool(self._cfg("silent", True))
        try:
            sent = await message.reply(
                content,
                silent=silent,
                mention_author=False,
                view=view,
            )
        except discord.HTTPException as exc:
            # 送信失敗
            logger.warning("link fix reply failed: %s", exc)
            return
        # Fix 側 embed 待ち
        fixed_wait = float(self._cfg("fixed_embed_wait_seconds") or 6.0)
        fixed_msg = await self._wait_for_embeds(sent, fixed_wait)
        # embed が無ければ削除（誤爆防止）
        if not fixed_msg.embeds:
            try:
                await sent.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            return
        # 元メッセージの embed 抑制
        await self._suppress_original(refreshed)

    @app_commands.command(
        name="linkfix",
        description="Configure Link Fix (broken social embed replacement)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def linkfix_settings(self, interaction: discord.Interaction) -> None:
        """ギルド向け Components V2 設定パネルを開く。"""
        # ギルド必須
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return
        # Manage Server
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.manage_guild:
            await interaction.response.send_message(
                "Manage Server permission required.",
                ephemeral=True,
            )
            return
        # View を作る
        view = LinkFixSettingsView(
            self.bot,
            self.store,
            interaction.guild.id,
        )
        # ephemeral で送る
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Cog をロードする。"""
    # 追加する
    await bot.add_cog(LinkFixCog(bot))
