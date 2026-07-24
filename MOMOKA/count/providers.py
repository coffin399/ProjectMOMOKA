# MOMOKA/count/providers.py
# 掲載サイトごとのサーバー数 POST 実装。
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

import aiohttp

logger = logging.getLogger(__name__)

# プロバイダ関数の型（session, bot_id, server_count, token）
PostFn = Callable[
    [aiohttp.ClientSession, str, int, str],
    Awaitable[None],
]


async def post_topgg(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """top.gg に server_count を投稿する。"""
    # エンドポイントを組み立てる
    url = f"https://top.gg/api/bots/{bot_id}/stats"
    # Authorization ヘッダ
    headers = {"Authorization": token}
    # ペイロード
    payload = {"server_count": int(server_count)}
    # POST する
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        # 失敗なら本文付きで例外
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"topgg HTTP {resp.status}: {body[:300]}")


async def post_discordbotlist(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """Discord Bot List に guilds 数を投稿する。"""
    # エンドポイント
    url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/stats"
    # ヘッダ
    headers = {"Authorization": token}
    # DBL は guilds キー
    payload = {"guilds": int(server_count)}
    # POST する
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        # 失敗処理
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"discordbotlist HTTP {resp.status}: {body[:300]}")


async def post_discordbotsgg(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """discord.bots.gg に guildCount を投稿する。"""
    # エンドポイント
    url = f"https://discord.bots.gg/api/v1/bots/{bot_id}/stats"
    # ヘッダ
    headers = {"Authorization": token}
    # bots.gg は guildCount
    payload = {"guildCount": int(server_count)}
    # POST する
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        # 失敗処理
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"discordbotsgg HTTP {resp.status}: {body[:300]}")


# サイト id → 投稿関数
PROVIDERS: Dict[str, PostFn] = {
    "topgg": post_topgg,
    "discordbotlist": post_discordbotlist,
    "discordbotsgg": post_discordbotsgg,
}


def is_placeholder_token(token: str) -> bool:
    """未設定・プレースホルダ token か判定する。"""
    # 前後空白を落とす
    text = (token or "").strip()
    # 空はプレースホルダ扱い
    if not text:
        return True
    # YOUR_ で始まる雛形
    if text.upper().startswith("YOUR_"):
        return True
    # それ以外は実トークンとみなす
    return False


def resolve_bot_id(site_cfg: Dict[str, Any], fallback_bot_id: int) -> str:
    """サイト設定の bot_id、無ければ Bot 自身の id。"""
    # 設定値を読む
    raw = site_cfg.get("bot_id")
    # 文字列化して空白除去
    text = str(raw or "").strip()
    # 空ならフォールバック
    if not text:
        return str(fallback_bot_id)
    # 指定値を返す
    return text
