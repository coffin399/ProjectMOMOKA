# チャンネル単位の排他ロック（debate / cross_check 共用）。
from __future__ import annotations

import asyncio
from typing import Dict, Optional, Set


class ChannelLock:
    """同一チャンネルで debate と cross_check が同時に走らないようにする。"""

    def __init__(self) -> None:
        # channel_id -> 占有モード名
        self._owners: Dict[int, str] = {}
        # 内部同期用ロック
        self._lock = asyncio.Lock()
        # 討論中チャンネル（並行チャット可否の判定用。新規 debate 排他とは別）
        self._debate_channels: Set[int] = set()

    async def try_acquire(self, channel_id: int, mode: str) -> bool:
        """空きなら mode で占有する。成功なら True。"""
        # 排他区間に入る
        async with self._lock:
            # 既に占有されていれば失敗
            if channel_id in self._owners:
                return False
            # 占有を記録する
            self._owners[channel_id] = mode
            # debate なら抑制セットにも入れる
            if mode == "debate":
                self._debate_channels.add(channel_id)
            # 成功
            return True

    async def release(self, channel_id: int) -> None:
        """占有を解放する。"""
        # 排他区間に入る
        async with self._lock:
            # 所有者を消す
            self._owners.pop(channel_id, None)
            # 抑制セットからも外す
            self._debate_channels.discard(channel_id)

    def owner(self, channel_id: int) -> Optional[str]:
        """現在の占有モード。無ければ None。"""
        # 辞書から返す
        return self._owners.get(channel_id)

    def is_debate_active(self, channel_id: int) -> bool:
        """討論中チャンネルかどうか。"""
        # セット membership
        return channel_id in self._debate_channels


# プロセス共通ロック
channel_lock = ChannelLock()
