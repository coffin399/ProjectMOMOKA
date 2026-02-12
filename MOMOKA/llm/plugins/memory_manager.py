# MOMOKA/llm/plugins/memory_manager.py
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext import commands

try:
    import aiofiles
except ImportError:
    aiofiles = None

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    サーバーをまたいで共有されるグローバルメモリを管理するプラグイン。
    キーと値のペアで情報を保存し、Botが参加している全ての場所から参照できる。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.memories_path = "data/global_memories.json"
        self.memories: Dict[str, str] = self._load_json_data(self.memories_path)
        logger.info(f"MemoryManager initialized: Loaded {len(self.memories)} global memories.")

    @property
    def name(self) -> str:
        """このプラグインが提供するツールの名前"""
        return "memory"

    @property
    def tool_spec(self) -> Dict[str, Any]:
        """Function Calling用のツール定義を返す"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Save or update information in global shared memory as key-value pairs. Use this to remember universal information that should be shared across the entire bot (e.g., developer announcements).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "The key (item name) for the information to remember. e.g., 'Developer Announcement'"
                        },
                        "value": {
                            "type": "string",
                            "description": "The content of the information to remember. e.g., 'Next maintenance is scheduled for next Monday.'"
                        }
                    },
                    "required": ["key", "value"]
                }
            }
        }

    # --- データ操作メソッド (コマンドから使用) ---
    async def save_memory(self, key: str, value: str) -> None:
        self.memories[key] = value
        await self._save_memories()
        logger.info(f"[save_memory] Saved global memory: key='{key}'")

    def list_memories(self) -> Dict[str, str]:
        return self.memories

    async def delete_memory(self, key: str) -> bool:
        if key in self.memories:
            del self.memories[key]
            await self._save_memories()
            logger.info(f"[delete_memory] Deleted global memory: key='{key}'")
            return True
        return False

    # --- ツール実行メソッド (LLMCogから使用) ---
    async def run_tool(self, arguments: Dict[str, Any]) -> str:
        key = arguments.get('key')
        value = arguments.get('value')
        if not key or not value:
            logger.warning(f"[run_tool] memory tool called with missing key/value")
            return "Error: keyとvalueの両方が必要です。"

        try:
            await self.save_memory(key, value)
            return f"グローバル共有メモリにキー'{key}'で情報を記憶しました。"
        except Exception as e:
            logger.error(f"[run_tool] Failed to save global memory: {e}", exc_info=True)
            return f"Error: グローバル共有メモリへの保存に失敗しました - {e}"

    # --- プロンプト生成メソッド (LLMCogから使用) ---
    def get_formatted_memories(self) -> str | None:
        """システムプロンプトに注入するための整形済みメモリ文字列を返す"""
        if not self.memories:
            return None

        header = "# Global Shared Memory"
        items = [f"- {key}: {value}" for key, value in self.memories.items()]

        logger.info(f"[get_formatted_memories] Loaded {len(items)} global memories.")

        return "\n".join([header] + items)

    # --- ファイルI/O (プライベートメソッド) ---
    def _load_json_data(self, path: str) -> Dict[str, Any]:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load JSON file '{path}': {e}")
        return {}

    async def _save_memories(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.memories_path), exist_ok=True)
            if aiofiles:
                async with aiofiles.open(self.memories_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(self.memories, indent=4, ensure_ascii=False))
            else:
                with open(self.memories_path, 'w', encoding='utf-8') as f:
                    json.dump(self.memories, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Failed to save memories file '{self.memories_path}': {e}")
            raise