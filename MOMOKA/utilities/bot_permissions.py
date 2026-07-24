# MOMOKA/utilities/bot_permissions.py
"""Bot 必須権限の定数と不足時の再認可案内。"""
from __future__ import annotations

from typing import Any, Optional

import discord

# 招待 URL に載せる必須権限ビット（ユーザー指定の固定値）
REQUIRED_BOT_PERMISSIONS = 6516795221339600

# 二重付与判定用の英語側マーカー
_PERMISSION_HINT_MARKER = "Required permissions were updated"


def is_missing_required_permissions(
    guild: Optional[discord.Guild],
) -> bool:
    """ギルド上の Bot 権限が REQUIRED を1ビットでも欠くなら True。"""
    # ギルドが無ければ判定不能なので不足扱いにしない
    if guild is None:
        return False
    # 自身のメンバー情報を取得する
    me = guild.me
    # 未キャッシュなら不足判定できない
    if me is None:
        return False
    # 現在のギルド権限ビットを取る
    current = me.guild_permissions.value
    # REQUIRED の全ビットが立っているか確認する
    return (current & REQUIRED_BOT_PERMISSIONS) != REQUIRED_BOT_PERMISSIONS


def build_permission_update_hint(invite_url: str) -> str:
    """英語優先→日本語の再認可案内（-# サブテキスト）を組み立てる。"""
    # 前後の空白を落とす
    url = (invite_url or "").strip()
    # URL が無い場合は空文字（呼び出し側で追記しない）
    if not url:
        return ""
    # Discord の小さ文字注記として返す
    return (
        "\n-# Required permissions were updated. "
        f"Please re-authorize the bot: {url} / "
        "必要な権限が更新されました。"
        f"ボットを再認可してください: {url}"
    )


def resolve_bot_invite_url(bot: Any) -> str:
    """当該 Bot の bots.<bot_id>.invite_url を返す（無ければ空）。"""
    # bot.config から bots セクションを取る
    config = getattr(bot, "config", None) or {}
    # bots 辞書を読む
    bots = config.get("bots") or {}
    # 実行中 Bot の識別子（未設定時は plana）
    bot_id = getattr(bot, "bot_id", "plana")
    # 当該エントリを取る
    entry = bots.get(bot_id) or {}
    # invite_url を文字列化して返す
    return str(entry.get("invite_url") or "").strip()


def append_permission_update_hint(
    content: Optional[str],
    guild: Optional[discord.Guild],
    invite_url: str,
    *,
    max_length: int = 2000,
) -> str:
    """権限不足かつ invite_url があるときだけ本文末尾に再認可案内を付ける。"""
    # 本文が空なら何もしない
    if not content:
        return content or ""
    # 案内用 URL が無ければ付けない（計画どおり URL 必須）
    url = (invite_url or "").strip()
    if not url:
        return content
    # 権限が揃っているなら付けない
    if not is_missing_required_permissions(guild):
        return content
    # 既に同系文言があるなら二重付与しない
    if _PERMISSION_HINT_MARKER in content:
        return content
    # 案内文言を組み立てる
    hint = build_permission_update_hint(url)
    # ヒントが空ならそのまま
    if not hint:
        return content
    # 本文と結合する
    combined = f"{content}{hint}"
    # Discord 上限を超えるなら追記せず本文を返す
    if len(combined) > max_length:
        return content
    # 追記済み本文を返す
    return combined
