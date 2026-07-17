# LLM 応答待機中の Components V2 LayoutView。
from __future__ import annotations

from typing import Any, Optional

import discord

from MOMOKA.utilities.donation import donation_from_bot, make_subtle_link_button


class WaitingLayoutView(discord.ui.LayoutView):
    """⏳ Waiting for... 用 LayoutView（Tips + 控えめ Ko-fi）。"""

    def __init__(
        self,
        *,
        body: str,
        accent: discord.Color,
        bot: Any = None,
        timeout: Optional[float] = 300.0,
    ) -> None:
        # 待機は短命なので適当なタイムアウト
        super().__init__(timeout=timeout)
        # 本文・色・Bot を保持する
        self.body = body
        self.accent = accent
        self.bot = bot
        # UI を組み立てる
        self._rebuild()

    def _rebuild(self) -> None:
        """TextDisplay + 任意の寄付ボタンを載せる。"""
        # 既存を消す
        self.clear_items()
        # コンテナ
        container = discord.ui.Container(accent_color=self.accent)
        # 待機本文
        container.add_item(discord.ui.TextDisplay(self.body))
        # 控えめ寄付（enabled 時のみ）
        if self.bot is not None:
            btn = make_subtle_link_button(donation_from_bot(self.bot))
            if btn is not None:
                row = discord.ui.ActionRow()
                row.add_item(btn)
                container.add_item(row)
        # ルートへ
        self.add_item(container)
