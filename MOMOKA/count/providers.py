# MOMOKA/count/providers.py
# 掲載サイトごとのサーバー数 / コマンド一覧 POST 実装。
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Sequence

import aiohttp

logger = logging.getLogger(__name__)

# プロバイダ関数の型（session, bot_id, server_count, token）
PostFn = Callable[
    [aiohttp.ClientSession, str, int, str],
    Awaitable[None],
]

# サイトごとの最短投稿間隔（秒）。再投稿しすぎ防止。
SITE_MIN_INTERVAL_SECONDS: Dict[str, float] = {
    # Void Bots: "every 3 minutes"
    "voidbots": 180.0,
}


class RateLimitedError(RuntimeError):
    """掲載サイト側のレート制限（HTTP 429）。"""

    def __init__(self, site_id: str, detail: str) -> None:
        # サイト id を保持する
        self.site_id = site_id
        # 親へメッセージを渡す
        super().__init__(f"{site_id} rate limited: {detail}")


async def _post_json(
    session: aiohttp.ClientSession,
    *,
    site_id: str,
    url: str,
    token: str,
    payload: Any,
) -> None:
    """Authorization + JSON POST の共通処理。"""
    # ヘッダを組み立てる（token はそのまま Authorization に載せる）
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }
    # POST する（payload は dict / list どちらも可）
    async with session.post(
        url,
        json=payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        # 429 は想定内の制限なので専用例外にする
        if resp.status == 429:
            body = await resp.text()
            raise RateLimitedError(site_id, body[:300])
        # その他の失敗は本文付きで例外
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"{site_id} HTTP {resp.status}: {body[:300]}")


async def post_topgg(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """top.gg に server_count を投稿する。"""
    # エンドポイントを組み立てる
    url = f"https://top.gg/api/bots/{bot_id}/stats"
    # POST する
    await _post_json(
        session,
        site_id="topgg",
        url=url,
        token=token,
        payload={"server_count": int(server_count)},
    )


async def post_discordbotlist(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """Discord Bot List に guilds 数を投稿する。"""
    # エンドポイント
    url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/stats"
    # POST する
    await _post_json(
        session,
        site_id="discordbotlist",
        url=url,
        token=token,
        payload={"guilds": int(server_count)},
    )


async def post_discordbotlist_commands(
    session: aiohttp.ClientSession,
    bot_id: str,
    commands: Sequence[Dict[str, Any]],
    token: str,
) -> None:
    """Discord Bot List にスラッシュコマンド一覧を投稿する。"""
    # 公式: POST /api/v1/bots/:id/commands（Discord API と同じ配列）
    url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/commands"
    # 配列へ正規化する（呼び出し側の型ゆれを吸収）
    payload: List[Dict[str, Any]] = [dict(item) for item in commands]
    # POST する
    await _post_json(
        session,
        site_id="discordbotlist_commands",
        url=url,
        token=token,
        payload=payload,
    )


def app_commands_to_payload(
    commands: Sequence[Any],
    tree: Any,
) -> List[Dict[str, Any]]:
    """discord.app_commands 系オブジェクトを Discord API 形式へ変換する。"""
    # 変換結果
    payloads: List[Dict[str, Any]] = []
    # 各コマンドを走査する
    for cmd in commands:
        # to_dict が無ければスキップする
        to_dict = getattr(cmd, "to_dict", None)
        if not callable(to_dict):
            continue
        # discord.py 2.x は Command.to_dict(tree) が必須
        data = to_dict(tree)
        # dict だけ採用する
        if isinstance(data, dict):
            payloads.append(data)
    # 返す
    return payloads


async def post_discordbotsgg(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """discord.bots.gg に guildCount を投稿する。"""
    # エンドポイント
    url = f"https://discord.bots.gg/api/v1/bots/{bot_id}/stats"
    # POST する
    await _post_json(
        session,
        site_id="discordbotsgg",
        url=url,
        token=token,
        payload={"guildCount": int(server_count)},
    )


async def post_voidbots(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """voidbots.net に server_count を投稿する。"""
    # 公式 npm / BotBlock: POST /bot/stats/:id
    url = f"https://api.voidbots.net/bot/stats/{bot_id}"
    # POST する（shard_count は単一プロセス想定で 0）
    await _post_json(
        session,
        site_id="voidbots",
        url=url,
        token=token,
        payload={
            "server_count": int(server_count),
            "shard_count": 0,
        },
    )


async def post_discordextremelist(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """discordextremelist.xyz に guildCount を投稿する。"""
    # BotBlock / 公式例: POST /v2/bot/:id/stats
    url = f"https://api.discordextremelist.xyz/v2/bot/{bot_id}/stats"
    # POST する
    await _post_json(
        session,
        site_id="discordextremelist",
        url=url,
        token=token,
        payload={"guildCount": int(server_count)},
    )


async def post_dscbot(
    session: aiohttp.ClientSession,
    bot_id: str,
    server_count: int,
    token: str,
) -> None:
    """dsc.bot（nightly）向け。公開 stats API が未確認のため明示エラー。"""
    # 未実装であることをはっきり伝える
    raise RuntimeError(
        "dscbot: public server-count API is not documented yet "
        "(https://nightly.dsc.bot/). Disable sites.dscbot until an endpoint is published."
    )


# サイト id → 投稿関数
PROVIDERS: Dict[str, PostFn] = {
    "topgg": post_topgg,
    "discordbotlist": post_discordbotlist,
    "discordbotsgg": post_discordbotsgg,
    "voidbots": post_voidbots,
    "discordextremelist": post_discordextremelist,
    "dscbot": post_dscbot,
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
