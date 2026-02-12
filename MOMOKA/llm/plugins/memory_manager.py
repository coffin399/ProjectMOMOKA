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
    ユーザーごとに個別のメモリを管理するプラグイン。
    キーと値のペアで情報を保存し、メンションしたユーザーのメモリのみシステムプロンプトに注入する。
    データ構造: {user_id_str: {key: value, ...}, ...}
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 保存先パスをユーザー別メモリに変更
        self.memories_path = "data/user_memories.json"
        # ユーザーID → {key: value} の二重辞書構造でメモリを管理
        self.memories: Dict[str, Dict[str, str]] = self._load_json_data(self.memories_path)
        # 読み込んだユーザー数とメモリ総数をログ出力
        total_entries = sum(len(v) for v in self.memories.values())
        logger.info(f"MemoryManager initialized: Loaded {total_entries} memories for {len(self.memories)} user(s).")

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
                "description": "Save or update information in your personal memory as key-value pairs. Use this to remember information specific to the current user (e.g., preferences, notes).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "The key (item name) for the information to remember. e.g., 'Favorite Color'"
                        },
                        "value": {
                            "type": "string",
                            "description": "The content of the information to remember. e.g., 'Blue'"
                        }
                    },
                    "required": ["key", "value"]
                }
            }
        }

    # --- データ操作メソッド (コマンドから使用) ---
    async def save_memory(self, user_id: int, key: str, value: str) -> None:
        """指定ユーザーのメモリにキーと値を保存する"""
        user_id_str = str(user_id)
        # ユーザー用の辞書が存在しなければ初期化
        if user_id_str not in self.memories:
            self.memories[user_id_str] = {}
        # キーと値を保存
        self.memories[user_id_str][key] = value
        await self._save_memories()
        logger.info(f"[save_memory] Saved memory for user {user_id}: key='{key}'")

    def list_memories(self, user_id: int) -> Dict[str, str]:
        """指定ユーザーのメモリ一覧を返す"""
        return self.memories.get(str(user_id), {})

    async def delete_memory(self, user_id: int, key: str) -> bool:
        """指定ユーザーのメモリからキーを削除する"""
        user_id_str = str(user_id)
        # ユーザーのメモリが存在し、かつ指定キーが含まれている場合のみ削除
        if user_id_str in self.memories and key in self.memories[user_id_str]:
            del self.memories[user_id_str][key]
            # ユーザーのメモリが空になった場合はエントリ自体を削除
            if not self.memories[user_id_str]:
                del self.memories[user_id_str]
            await self._save_memories()
            logger.info(f"[delete_memory] Deleted memory for user {user_id}: key='{key}'")
            return True
        return False

    # --- ツール実行メソッド (LLMCogから使用) ---
    async def run_tool(self, arguments: Dict[str, Any], user_id: int) -> str:
        """AIからのツール呼び出しを処理する。user_idでメモリのスコープを限定。"""
        key = arguments.get('key')
        value = arguments.get('value')
        # キーと値の両方が必要
        if not key or not value:
            logger.warning(f"[run_tool] memory tool called with missing key/value for user {user_id}")
            return "Error: keyとvalueの両方が必要です。"

        try:
            # ユーザーIDを指定してメモリを保存
            await self.save_memory(user_id, key, value)
            return f"あなたのメモリにキー'{key}'で情報を記憶しました。"
        except Exception as e:
            logger.error(f"[run_tool] Failed to save memory for user {user_id}: {e}", exc_info=True)
            return f"Error: メモリへの保存に失敗しました - {e}"

    # --- プロンプト生成メソッド (LLMCogから使用) ---
    def get_formatted_memories(self, user_id: int) -> str | None:
        """指定ユーザーのメモリのみ整形してシステムプロンプト用文字列として返す"""
        # そのユーザーのメモリだけを取得
        user_memories = self.memories.get(str(user_id), {})
        # メモリが空なら何も返さない
        if not user_memories:
            return None

        # ヘッダーとメモリ項目を整形
        header = "# Your Personal Memory"
        items = [f"- {key}: {value}" for key, value in user_memories.items()]

        logger.info(f"[get_formatted_memories] Loaded {len(items)} memories for user {user_id}.")

        return "\n".join([header] + items)

    # --- ファイルI/O (プライベートメソッド) ---
    def _load_json_data(self, path: str) -> Dict[str, Dict[str, str]]:
        """JSONファイルからユーザー別メモリデータを読み込む"""
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # キーを文字列に正規化して返す
                    return {str(k): v for k, v in data.items()}
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load JSON file '{path}': {e}")
        # ファイルが存在しない、またはエラーの場合は空辞書を返す
        return {}

    async def _save_memories(self) -> None:
        """現在のメモリデータをJSONファイルに保存する"""
        try:
            os.makedirs(os.path.dirname(self.memories_path), exist_ok=True)
            # aiofilesが利用可能な場合は非同期I/Oを使用
            if aiofiles:
                async with aiofiles.open(self.memories_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(self.memories, indent=4, ensure_ascii=False))
            else:
                # aiofilesが無い場合は同期I/Oにフォールバック
                with open(self.memories_path, 'w', encoding='utf-8') as f:
                    json.dump(self.memories, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Failed to save memories file '{self.memories_path}': {e}")
            raise