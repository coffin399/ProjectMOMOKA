# MOMOKA/llm/plugins/bio_manager.py
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Any, List, TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext import commands

try:
    import aiofiles
except ImportError:
    aiofiles = None

logger = logging.getLogger(__name__)


class BioManager:
    """
    AIの役割(チャンネルbio)とユーザー情報(ユーザーbio)を管理するプラグイン。
    ファイルI/O、ツール定義、ツール実行、システムプロンプト生成を担当する。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.llm_config = bot.config.get('llm', {})

        # ファイルパスの定義
        self.channel_bios_path = "data/channel_bios.json"
        self.user_bios_path = "data/user_bios.json"

        # データのロード
        self.channel_bios: Dict[str, str] = self._load_json_data(self.channel_bios_path)
        self.user_bios: Dict[str, str] = self._load_json_data(self.user_bios_path)
        logger.info(
            f"BioManager initialized: Loaded {len(self.channel_bios)} channel bios and {len(self.user_bios)} user bios.")

    @property
    def name(self) -> str:
        """このプラグインが提供するツールの名前"""
        return "user_bio"

    @property
    def tool_spec(self) -> Dict[str, Any]:
        """Function Calling用のツール定義を返す"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Remember or update information about a user. Proactively call this when a user introduces themselves or asks you to remember something. Existing information is overwritten with new information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "bio_text": {
                            "type": "string",
                            "description": "The full text of information to remember about the user. (e.g., 'My name is Tanaka. My favorite food is ramen.')"
                        }
                    },
                    "required": ["bio_text"]
                }
            }
        }

    # --- データ操作メソッド (コマンドから使用) ---
    async def set_channel_bio(self, channel_id: int, bio: str) -> None:
        self.channel_bios[str(channel_id)] = bio
        await self._save_channel_bios()

    def get_channel_bio(self, channel_id: int) -> str | None:
        return self.channel_bios.get(str(channel_id))

    async def reset_channel_bio(self, channel_id: int) -> bool:
        if str(channel_id) in self.channel_bios:
            del self.channel_bios[str(channel_id)]
            await self._save_channel_bios()
            return True
        return False

    async def set_user_bio(self, user_id: int, bio: str, mode: str = 'overwrite') -> None:
        user_id_str = str(user_id)
        if mode == 'append':
            existing_bio = self.user_bios.get(user_id_str, "")
            if existing_bio:
                self.user_bios[user_id_str] = f"{existing_bio}\n{bio}"
            else:
                self.user_bios[user_id_str] = bio
        else:  # 'overwrite' or default
            self.user_bios[user_id_str] = bio

        await self._save_user_bios()

    def get_user_bio(self, user_id: int) -> str | None:
        return self.user_bios.get(str(user_id))

    async def reset_user_bio(self, user_id: int) -> bool:
        if str(user_id) in self.user_bios:
            del self.user_bios[str(user_id)]
            await self._save_user_bios()
            return True
        return False

    # --- ツール実行メソッド (LLMCogから使用) ---
    async def run_tool(self, arguments: Dict[str, Any], user_id: int) -> str:
        """AIからのツール呼び出しを処理する"""
        bio_text = arguments.get('bio_text')
        if not bio_text:
            logger.warning(f"[run_tool] user_bio tool called without bio_text for user {user_id}")
            return "Error: bio_text is missing."

        try:
            await self.set_user_bio(user_id, bio_text)
            logger.info(f"[run_tool] User bio for {user_id} saved via tool call. Content: '{bio_text[:150]}'")
            return "ユーザー情報を正常に記憶しました。"
        except Exception as e:
            logger.error(f"[run_tool] Failed to save user bio via tool for user {user_id}: {e}", exc_info=True)
            return f"Error: ユーザー情報の保存に失敗しました - {e}"

    # --- プロンプト生成メソッド (LLMCogから使用) ---
    def get_system_prompt(self, channel_id: int, user_id: int, user_display_name: str) -> str:
        """最終的なシステムプロンプトを組み立てる"""
        system_parts = [self.llm_config.get('system_prompt', "You are a helpful assistant.")]

        if channel_bio := self.get_channel_bio(channel_id):
            logger.info(
                f"[get_system_prompt] Loaded channel bio for channel {channel_id}. Content: '{channel_bio[:150]}'")
            system_parts.append(f"\n# Additional Role for This Channel:\n{channel_bio}")

        if user_bio := self.get_user_bio(user_id):
            logger.info(
                f"[get_system_prompt] Loaded user bio for user {user_id} ({user_display_name}). Content: '{user_bio[:150]}'")
            system_parts.append(f"\n# Information About the User ({user_display_name}):\n{user_bio}")

        if 'user_bio' in self.llm_config.get('active_tools', []):
            system_parts.append(
                "\n# Remembering User Information:\nWhen a user introduces themselves or asks you to remember something, proactively use the `user_bio` tool to store that information.")

        return "\n".join(system_parts)

    # --- ファイルI/O (プライベートメソッド) ---
    def _load_json_data(self, path: str) -> Dict[str, Any]:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 読み込んだデータを辞書として返し、キーを文字列に変換する
                    return {str(k): v for k, v in data.items()}
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load JSON file '{path}': {e}")
        # ファイルが存在しない、またはエラーの場合は空の辞書を返す
        return {}

    async def _save_json_data(self, data: Dict[str, Any], path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if aiofiles:
                async with aiofiles.open(path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(data, indent=4, ensure_ascii=False))
            else:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Failed to save JSON file '{path}': {e}")
            raise

    async def _save_channel_bios(self) -> None:
        await self._save_json_data(self.channel_bios, self.channel_bios_path)

    async def _save_user_bios(self) -> None:
        await self._save_json_data(self.user_bios, self.user_bios_path)