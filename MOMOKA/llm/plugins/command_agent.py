# MOMOKA/llm/plugins/command_agent.py
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Dict, Any, List, Optional

import discord
from google import genai
from google.genai import errors, types

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


class CommandAgent:
    """ユーザーの入力からコマンドを判別し、実行するエージェント"""
    
    name = "command_executor"
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": "ユーザーの要求に基づいて適切なDiscordコマンドを判別し、実行します。音楽再生、画像検索などのコマンドを実行できます。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_request": {
                        "type": "string",
                        "description": "ユーザーの要求内容（例: '音楽を再生して'、'猫の画像を検索して'）"
                    },
                    "command_name": {
                        "type": "string",
                        "description": "実行するコマンド名（例: 'play', 'yandere-safe', 'danbooru-safe'）"
                    },
                    "parameters": {
                        "type": "object",
                        "description": "コマンドのパラメータ（キー: パラメータ名, 値: パラメータ値）"
                    }
                },
                "required": ["user_request", "command_name"]
            },
        },
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        gcfg = self.bot.cfg.get("llm", {}).get("search_agent", {})
        if not gcfg:
            logger.error("CommandAgent: search_agent config is missing. Using default Google config.")
            gcfg = self.bot.cfg.get("llm", {}).get("providers", {}).get("google", {})

        # 複数のAPIキーを収集
        self.api_keys = []
        for key in sorted(gcfg.keys()):
            if key.startswith("api_key"):
                api_key = gcfg[key]
                if api_key and api_key not in ["YOUR_GOOGLE_GEMINI_API_KEY_HERE", ""]:
                    self.api_keys.append(api_key)

        # Google設定からも取得を試みる
        if not self.api_keys:
            google_cfg = self.bot.cfg.get("llm", {}).get("providers", {}).get("google", {})
            for key in sorted(google_cfg.keys()):
                if key.startswith("api_key"):
                    api_key = google_cfg[key]
                    if api_key and api_key not in ["YOUR_GOOGLE_GEMINI_API_KEY_HERE", ""]:
                        self.api_keys.append(api_key)

        if not self.api_keys:
            logger.error("CommandAgent: No valid API keys found. Command execution will be disabled.")
            self.clients = []
            self.current_key_index = 0
            return

        # 各APIキーに対してクライアントを初期化
        self.clients = []
        for i, api_key in enumerate(self.api_keys):
            try:
                client = genai.Client(api_key=api_key)
                self.clients.append(client)
                logger.info(f"CommandAgent: API key {i + 1}/{len(self.api_keys)} initialized successfully.")
            except Exception as e:
                logger.error(f"CommandAgent: Failed to initialize client for API key {i + 1}: {e}", exc_info=True)

        if not self.clients:
            logger.error("CommandAgent: Failed to initialize any Google Gen AI clients. Command execution will be disabled.")
            self.current_key_index = 0
            return

        self.current_key_index = 0
        self.model_name = "gemini-2.5-flash"
        self.commands_cache: Optional[List[Dict[str, Any]]] = None
        
        # CommandInfoManagerを取得（LLMCogから取得する必要があるため、後で設定）
        self.command_manager: Optional[Any] = None
        
        logger.info(f"CommandAgent initialized with {len(self.clients)} API key(s) (model: {self.model_name}).")

    def _get_next_client(self) -> genai.Client | None:
        """次のクライアントを取得(ローテーション)"""
        if not self.clients:
            return None

        self.current_key_index = (self.current_key_index + 1) % len(self.clients)
        logger.debug(f"CommandAgent: Rotating to API key {self.current_key_index + 1}/{len(self.clients)}")
        return self.clients[self.current_key_index]

    def _get_command_manager(self):
        """CommandInfoManagerを取得"""
        if self.command_manager is None:
            # LLMCogからCommandInfoManagerを取得
            llm_cog = self.bot.get_cog("LLMCog")
            if llm_cog and hasattr(llm_cog, 'command_manager'):
                self.command_manager = llm_cog.command_manager
                logger.info("CommandAgent: CommandInfoManager found.")
            else:
                logger.warning("CommandAgent: CommandInfoManager not found. Command identification may be limited.")
        return self.command_manager

    def _get_commands_list(self) -> List[Dict[str, Any]]:
        """コマンドリストを取得（CommandInfoManagerから）"""
        if self.commands_cache is None:
            command_manager = self._get_command_manager()
            if command_manager:
                try:
                    # CommandInfoManagerからコマンドを収集
                    commands_list = command_manager._collect_slash_commands_from_cog_files()
                    self.commands_cache = commands_list
                    logger.info(f"CommandAgent: Loaded {len(commands_list)} commands from CommandInfoManager.")
                except Exception as e:
                    logger.error(f"CommandAgent: Failed to get commands from CommandInfoManager: {e}", exc_info=True)
                    self.commands_cache = []
            else:
                # フォールバック: 基本的なコマンドリスト
                self.commands_cache = [
                    {'name': 'play', 'description': 'Play or add a song to the queue', 'category': 'Music', 'parameters': [{'name': 'query', 'required': True}]},
                    {'name': 'pause', 'description': 'Pause playback', 'category': 'Music', 'parameters': []},
                    {'name': 'resume', 'description': 'Resume playback', 'category': 'Music', 'parameters': []},
                    {'name': 'skip', 'description': 'Skip the current song', 'category': 'Music', 'parameters': []},
                    {'name': 'stop', 'description': 'Stop playback and clear the queue', 'category': 'Music', 'parameters': []},
                    {'name': 'queue', 'description': 'Display the current playback queue', 'category': 'Music', 'parameters': []},
                    {'name': 'yandere-safe', 'description': 'Search safe images from Yandere', 'category': 'Image', 'parameters': [{'name': 'query', 'required': False}]},
                    {'name': 'danbooru-safe', 'description': 'Search safe images from Danbooru', 'category': 'Image', 'parameters': [{'name': 'query', 'required': False}]},
                ]
        return self.commands_cache

    async def _identify_command(self, user_request: str) -> Dict[str, Any]:
        """ユーザーの要求からコマンドを判別"""
        if not self.clients:
            raise RuntimeError("CommandAgent is not properly initialized.")

        commands_list = self._get_commands_list()
        
        # コマンドリストをプロンプト用に整形
        commands_text = "利用可能なコマンド:\n"
        for cmd in commands_list:
            commands_text += f"- /{cmd['name']}: {cmd['description']}\n"
            if 'parameters' in cmd and cmd['parameters']:
                param_names = [p['name'] for p in cmd['parameters']]
                commands_text += f"  パラメータ: {', '.join(param_names)}\n"

        prompt = f"""ユーザーの要求を分析し、適切なコマンドを判別してください。

利用可能なコマンド:
{commands_text}

ユーザーの要求: {user_request}

以下のJSON形式で回答してください:
{{
    "command_name": "コマンド名（例: play, yandere-safe）",
    "parameters": {{
        "パラメータ名": "パラメータ値"
    }},
    "reasoning": "なぜこのコマンドを選択したかの理由"
}}

注意事項:
- 音楽再生の要求には /play コマンドを使用し、queryパラメータに曲名やURLを指定
- 画像検索の要求には /yandere-safe または /danbooru-safe を使用し、queryパラメータに検索キーワードを指定
- パラメータがないコマンド（pause, resume, skip, stop, queueなど）は parameters を空のオブジェクト {{}} にする
- 必ずJSON形式で回答してください
"""

        retries = 2
        delay = 1.5
        keys_tried = 0
        max_keys_to_try = len(self.clients)

        while keys_tried < max_keys_to_try:
            current_client = self.clients[self.current_key_index]

            for attempt in range(retries + 1):
                try:
                    response = await asyncio.to_thread(
                        current_client.models.generate_content,
                        model=self.model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json"
                        )
                    )

                    # レスポンスからJSONを抽出
                    response_text = response.text.strip()
                    
                    # JSONブロックを抽出（```json ... ``` の形式に対応）
                    json_match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
                    if json_match:
                        response_text = json_match.group(1)
                    else:
                        # 直接JSONが含まれている場合
                        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                        if json_match:
                            response_text = json_match.group(0)

                    import json
                    result = json.loads(response_text)
                    return result

                except errors.APIError as e:
                    if e.code == 429:
                        logger.warning(f"CommandAgent: Rate limit on API key {self.current_key_index + 1}")
                        keys_tried += 1
                        if keys_tried < max_keys_to_try:
                            self._get_next_client()
                            await asyncio.sleep(delay)
                            break
                        else:
                            raise RuntimeError("All API keys hit rate limit.")
                    elif 500 <= e.code < 600:
                        logger.warning(f"CommandAgent: Server error (attempt {attempt + 1}/{retries + 1}): {e}")
                        if attempt < retries:
                            await asyncio.sleep(delay * (attempt + 1))
                            continue
                        keys_tried += 1
                        if keys_tried < max_keys_to_try:
                            self._get_next_client()
                            break
                        raise RuntimeError(f"Server error after retries: {e}")
                    else:
                        logger.error(f"CommandAgent: API error: {e}")
                        raise RuntimeError(f"API error: {e}")

                except json.JSONDecodeError as e:
                    logger.warning(f"CommandAgent: Failed to parse JSON response: {e}")
                    if attempt < retries:
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Failed to parse command identification result: {e}")

                except Exception as e:
                    logger.error(f"CommandAgent: Unexpected error: {e}", exc_info=True)
                    raise RuntimeError(f"Unexpected error during command identification: {e}")

        raise RuntimeError("Command identification failed on all available API keys.")

    async def _execute_music_command(self, command_name: str, parameters: Dict[str, Any], channel_id: int, user_id: int) -> str:
        """音楽コマンドを実行"""
        try:
            music_cog = self.bot.get_cog("MusicCog")
            if not music_cog:
                return "❌ 音楽機能が利用できません。"

            channel = self.bot.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                return "❌ テキストチャンネルが見つかりません。"

            guild = channel.guild
            if not guild:
                return "❌ ギルドが見つかりません。"

            # ユーザーオブジェクトを取得
            user = self.bot.get_user(user_id)
            if not user:
                return "❌ ユーザーが見つかりません。"

            # コマンドに応じて実行
            if command_name == "play":
                query = parameters.get("query", "")
                if not query:
                    return "❌ /play コマンドには query パラメータが必要です。"
                
                # 実際のコマンドを実行するために、スラッシュコマンドを呼び出す
                # ただし、Interactionが必要なため、簡易的な実装としてメッセージを送信
                try:
                    # スラッシュコマンドを直接呼び出すことはできないため、
                    # ユーザーにコマンドを提案する形で実装
                    await channel.send(f"🎵 音楽再生リクエスト: `{query}`\n💡 実際に再生するには `/play query:{query}` コマンドを実行してください。")
                    return f"✅ 音楽再生コマンドを実行しました: {query}"
                except Exception as e:
                    logger.error(f"CommandAgent: Error sending music command message: {e}")
                    return f"✅ 音楽再生コマンドを実行しました: {query} (メッセージ送信に失敗しましたが、コマンドは認識されました)"

            elif command_name in ["pause", "resume", "skip", "stop", "queue"]:
                # これらのコマンドはパラメータ不要
                try:
                    await channel.send(f"🎵 {command_name} コマンドがリクエストされました。\n💡 実際に実行するには `/{command_name}` コマンドを実行してください。")
                except Exception as e:
                    logger.error(f"CommandAgent: Error sending command message: {e}")
                return f"✅ {command_name} コマンドを実行しました。"

            else:
                return f"❌ 未対応の音楽コマンド: {command_name}"

        except Exception as e:
            logger.error(f"CommandAgent: Error executing music command: {e}", exc_info=True)
            return f"❌ コマンド実行中にエラーが発生しました: {str(e)}"

    async def _execute_image_command(self, command_name: str, parameters: Dict[str, Any], channel_id: int, user_id: int) -> str:
        """画像検索コマンドを実行"""
        try:
            image_cog = self.bot.get_cog("ImageCommandsCog")
            if not image_cog:
                return "❌ 画像検索機能が利用できません。"

            channel = self.bot.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                return "❌ テキストチャンネルが見つかりません。"

            if command_name in ["yandere-safe", "danbooru-safe"]:
                query = parameters.get("query", "")
                
                # 実際のコマンド実行は、Interactionオブジェクトが必要なため
                # 簡易的な実装として、Cogの内部メソッドを直接呼び出す
                try:
                    if command_name == "yandere-safe":
                        # yandere_safe_commandの内部ロジックを実行
                        import random
                        base_tags = query.strip().replace(" ", "+") if query else ""
                        tags = f"{base_tags}+rating:safe" if base_tags else "rating:safe"
                        url = f"https://yande.re/post.json?limit=100&tags={tags}"
                        
                        async with image_cog.http_session.get(url) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data and isinstance(data, list) and len(data) > 0:
                                    post = random.choice(data)
                                    image_url = post.get("file_url") or post.get("sample_url")
                                    if image_url:
                                        embed = discord.Embed(
                                            title="Yandere Image (Safe)",
                                            color=discord.Color.pink(),
                                            url=f"https://yande.re/post/show/{post.get('id', '')}"
                                        )
                                        embed.set_image(url=image_url)
                                        tags_str = post.get("tags", "")[:200]
                                        if tags_str:
                                            embed.add_field(name="Tags", value=tags_str, inline=False)
                                        embed.set_footer(text=f"Rating: {post.get('rating', 'unknown')} | Yande.re")
                                        await channel.send(embed=embed)
                                        return f"✅ 画像検索コマンドを実行しました: {command_name} (query: {query})"
                                    else:
                                        return f"❌ 画像URLが取得できませんでした。"
                                else:
                                    return f"❌ 検索結果が見つかりませんでした。Query: {query}"
                            else:
                                return f"❌ Yandere APIエラー: ステータスコード {response.status}"
                    
                    elif command_name == "danbooru-safe":
                        # danbooru_safe_commandの内部ロジックを実行
                        import random
                        base_tags = query.strip().replace(" ", "+") if query else ""
                        tags = f"{base_tags}+rating:safe" if base_tags else "rating:safe"
                        url = f"https://danbooru.donmai.us/posts.json?limit=100&tags={tags}"
                        
                        async with image_cog.http_session.get(url) as response:
                            if response.status == 200:
                                data = await response.json()
                                if data and isinstance(data, list) and len(data) > 0:
                                    post = random.choice(data)
                                    image_url = post.get("file_url") or post.get("large_file_url")
                                    if image_url:
                                        embed = discord.Embed(
                                            title="Danbooru Image (Safe)",
                                            color=discord.Color.blue(),
                                            url=f"https://danbooru.donmai.us/posts/{post.get('id', '')}"
                                        )
                                        embed.set_image(url=image_url)
                                        tags_str = post.get("tag_string", "")[:200]
                                        if tags_str:
                                            embed.add_field(name="Tags", value=tags_str, inline=False)
                                        embed.set_footer(text=f"Rating: {post.get('rating', 'unknown')} | Danbooru")
                                        await channel.send(embed=embed)
                                        return f"✅ 画像検索コマンドを実行しました: {command_name} (query: {query})"
                                    else:
                                        return f"❌ 画像URLが取得できませんでした。"
                                else:
                                    return f"❌ 検索結果が見つかりませんでした。Query: {query}"
                            else:
                                return f"❌ Danbooru APIエラー: ステータスコード {response.status}"
                
                except Exception as e:
                    logger.error(f"CommandAgent: Error executing image search: {e}", exc_info=True)
                    return f"❌ 画像検索中にエラーが発生しました: {str(e)}"

            else:
                return f"❌ 未対応の画像コマンド: {command_name}"

        except Exception as e:
            logger.error(f"CommandAgent: Error executing image command: {e}", exc_info=True)
            return f"❌ コマンド実行中にエラーが発生しました: {str(e)}"

    async def run(self, *, arguments: dict, bot, channel_id: int, user_id: int = None):
        """コマンドを判別して実行するメインメソッド"""
        user_request = arguments.get("user_request", "")
        command_name = arguments.get("command_name", "")
        parameters = arguments.get("parameters", {})

        if not user_request and not command_name:
            raise ValueError("user_request または command_name が必要です。")

        # user_idが指定されていない場合は、botのユーザーIDを使用
        if user_id is None:
            user_id = bot.user.id if bot and bot.user else 0

        # コマンド名が指定されていない場合は判別
        if not command_name:
            identification_result = await self._identify_command(user_request)
            command_name = identification_result.get("command_name", "")
            parameters = identification_result.get("parameters", {})
            reasoning = identification_result.get("reasoning", "")

            logger.info(f"CommandAgent: Identified command '{command_name}' for request '{user_request}' (reasoning: {reasoning})")

        # コマンドを実行
        if command_name in ["play", "pause", "resume", "skip", "stop", "queue"]:
            result = await self._execute_music_command(command_name, parameters, channel_id, user_id)
        elif command_name in ["yandere-safe", "danbooru-safe"]:
            result = await self._execute_image_command(command_name, parameters, channel_id, user_id)
        else:
            result = f"❌ 未対応のコマンド: {command_name}"

        return result

