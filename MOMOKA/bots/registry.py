# Bot レジストリ: PLANA / ARONA Client の登録と相互参照。
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


class BotRegistry:
    """1プロセス内の複数 Bot Client を bot_id で管理する。"""

    def __init__(self) -> None:
        # bot_id -> Bot インスタンス
        self._bots: Dict[str, "commands.Bot"] = {}
        # 表示名解決用（config 由来）
        self._display_names: Dict[str, str] = {}

    def register(self, bot_id: str, bot: "commands.Bot", display_name: str) -> None:
        """Bot を登録する。"""
        # 既に同じ id があれば上書き警告する
        if bot_id in self._bots:
            logger.warning("BotRegistry: overwriting bot_id=%s", bot_id)
        # Client を保存する
        self._bots[bot_id] = bot
        # 表示名を保存する
        self._display_names[bot_id] = display_name
        # 登録ログ
        logger.info("BotRegistry: registered %s (%s)", bot_id, display_name)

    def get(self, bot_id: str) -> Optional["commands.Bot"]:
        """bot_id の Client を返す。無ければ None。"""
        # 辞書から取り出す
        return self._bots.get(bot_id)

    def require(self, bot_id: str) -> "commands.Bot":
        """必須取得。無ければ KeyError。"""
        # エントリを探す
        bot = self._bots.get(bot_id)
        # 無ければ例外
        if bot is None:
            raise KeyError(f"Bot '{bot_id}' is not registered.")
        # 返す
        return bot

    def display_name(self, bot_id: str) -> str:
        """表示名（ログタグ用）。未登録なら bot_id 大文字。"""
        # 保存済み表示名を優先する
        return self._display_names.get(bot_id) or bot_id.upper()

    def all_bots(self) -> List["commands.Bot"]:
        """登録済み Client の一覧。"""
        # 値のリストを返す
        return list(self._bots.values())

    def all_ids(self) -> List[str]:
        """登録済み bot_id 一覧。"""
        # キー一覧を返す
        return list(self._bots.keys())

    def partner_id(self, bot_id: str, default_partner: str = "arona") -> str:
        """相方 bot_id を返す（plana↔arona）。"""
        # plana なら arona、それ以外は plana を相方とする
        if bot_id == "plana":
            return default_partner
        return "plana"

    def user_id(self, bot_id: str) -> Optional[int]:
        """ログイン済み user.id。未ログインなら None。"""
        # Client を取得する
        bot = self._bots.get(bot_id)
        # 無ければ None
        if bot is None or bot.user is None:
            return None
        # Discord user id を返す
        return bot.user.id

    def mention(self, bot_id: str) -> str:
        """`<@user_id>`。未ログインなら表示名テキスト。"""
        # user id を取得する
        uid = self.user_id(bot_id)
        # ログイン済みならメンション文字列
        if uid is not None:
            return f"<@{uid}>"
        # フォールバックは表示名
        return self.display_name(bot_id)

    async def close_all(self) -> None:
        """全 Client を閉じる（シャットダウン用）。"""
        # 登録済みを走査する
        for bot_id, bot in list(self._bots.items()):
            try:
                # まだ閉じていなければ close する
                if not bot.is_closed():
                    await bot.close()
                    logger.info("BotRegistry: closed %s", bot_id)
            except Exception as e:
                # 1件失敗しても他を閉じ続ける
                logger.warning("BotRegistry: failed to close %s: %s", bot_id, e)

    def iter_entries(self) -> Iterable[tuple]:
        """(bot_id, bot, display_name) を順に返す。"""
        # 登録順で返す
        for bot_id, bot in self._bots.items():
            yield bot_id, bot, self._display_names.get(bot_id, bot_id.upper())


# プロセス共通のシングルトン
registry = BotRegistry()
