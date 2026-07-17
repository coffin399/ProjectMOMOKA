# 討論 UI の Bot 別アクセント色。
from __future__ import annotations

import discord

# PLANA: 見やすい紫（GUI ログタグ #b388ff と同系）
_PLANA_RGB = (179, 136, 255)
# ARONA: 水色（music UI と同系）
_ARONA_RGB = (79, 194, 255)


def initiator_accent_color(bot_id: str) -> discord.Color:
    """起動 Bot に応じた Embed / Container アクセント色を返す。"""
    # 正規化して判定する
    bid = (bot_id or "plana").lower()
    # ARONA は水色
    if bid == "arona":
        return discord.Color.from_rgb(*_ARONA_RGB)
    # 既定（PLANA）は紫
    return discord.Color.from_rgb(*_PLANA_RGB)
