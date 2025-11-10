# MOMOKA/llm/llm_cog.py
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import List, Dict, Any, Tuple, Optional, AsyncGenerator, Union

import aiohttp
import discord
import openai
from discord import app_commands
from discord.ext import commands

from MOMOKA.llm.error.errors import (
    LLMExceptionHandler,
    SearchAgentError,
    SearchAPIRateLimitError,
    SearchAPIServerError
)
from MOMOKA.llm.plugins import (
    SearchAgent,
    BioManager,
    MemoryManager,
    CommandInfoManager,
    ImageGenerator,
    CommandAgent,
    DeepResearchAgent,
    ScheduledReporter
)

try:
    from MOMOKA.llm.utils.tips import TipsManager
except ImportError:
    logging.error("Could not import TipsManager. Tips functionality will be disabled.")
    TipsManager = None

try:
    import aiofiles
except ImportError:
    aiofiles = None
    logging.warning("aiofiles library not found. Channel model settings will be saved synchronously. "
                    "Install with: pip install aiofiles")

logger = logging.getLogger(__name__)

# Constants
SUPPORTED_IMAGE_EXTENSIONS = ('.png', '.jpeg', '.jpg', '.gif', '.webp')
IMAGE_URL_PATTERN = re.compile(
    r'https?://[^\s]+\.(?:' + '|'.join(ext.lstrip('.') for ext in SUPPORTED_IMAGE_EXTENSIONS) + r')(?:\?[^\s]*)?',
    re.IGNORECASE
)
DISCORD_MESSAGE_MAX_LENGTH = 2000
SAFE_MESSAGE_LENGTH = 1990  # 安全マージン


def _split_message_smartly(text: str, max_length: int) -> List[str]:
    if len(text) <= max_length: return [text]
    chunks, remaining = [], text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        chunk = remaining[:max_length]
        split_point = _find_best_split_point(chunk)
        if split_point == -1: split_point = max_length - 20
        chunk_text = remaining[:split_point].rstrip()
        if chunk_text: chunks.append(chunk_text)
        remaining = remaining[split_point:].lstrip()
    return chunks


def _find_best_split_point(chunk: str) -> int:
    code_block_end = chunk.rfind('```\n')
    if code_block_end > len(chunk) * 0.5: return code_block_end + 4
    paragraph_break = chunk.rfind('\n\n')
    if paragraph_break > len(chunk) * 0.5: return paragraph_break + 2
    newline = chunk.rfind('\n')
    if newline > len(chunk) * 0.6: return newline + 1
    japanese_period = max(chunk.rfind('。'), chunk.rfind('！'), chunk.rfind('？'))
    if japanese_period > len(chunk) * 0.7: return japanese_period + 1
    english_period = max(chunk.rfind('. '), chunk.rfind('! '), chunk.rfind('? '))
    if english_period > len(chunk) * 0.7: return english_period + 2
    comma = max(chunk.rfind('、'), chunk.rfind(', '))
    if comma > len(chunk) * 0.7: return comma + 1
    space = chunk.rfind(' ')
    if space > len(chunk) * 0.7: return space + 1
    return -1


class ThreadCreationView(discord.ui.View):
    """スレッド作成ボタンのViewクラス"""
    
    def __init__(self, llm_cog, original_message: discord.Message):
        super().__init__(timeout=300)  # 5分でタイムアウト
        self.llm_cog = llm_cog
        self.original_message = original_message
    
    @discord.ui.button(label="スレッドを作成する / Create Thread", style=discord.ButtonStyle.primary, emoji="🧵")
    async def create_thread(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # スレッドを作成
            thread = await self.original_message.create_thread(
                name=f"AI Chat - {interaction.user.display_name}",
                auto_archive_duration=60,  # 1時間でアーカイブ
                reason="AI conversation thread created by user"
            )
            
            # 元のチャンネルの会話履歴を取得（スレッド作成前の履歴）
            messages = []
            try:
                # 元のメッセージから遡って会話履歴を収集
                current_msg = self.original_message
                visited_ids = set()
                message_count = 0
                
                while current_msg and message_count < 40:
                    if current_msg.id in visited_ids:
                        break
                    visited_ids.add(current_msg.id)
                    
                    if current_msg.author != self.llm_cog.bot.user:
                        # ユーザーメッセージを処理
                        image_contents, text_content = await self.llm_cog._prepare_multimodal_content(current_msg)
                        text_content = text_content.replace(f'<@!{self.llm_cog.bot.user.id}>', '').replace(f'<@{self.llm_cog.bot.user.id}>', '').strip()
                        
                        if text_content or image_contents:
                            user_content_parts = []
                            if text_content:
                                user_content_parts.append({
                                    "type": "text",
                                    "text": f"{current_msg.created_at.astimezone(self.llm_cog.jst).strftime('[%H:%M]')} {text_content}"
                                })
                            user_content_parts.extend(image_contents)
                            messages.append({"role": "user", "content": user_content_parts})
                            message_count += 1
                    
                    # 前のメッセージを取得
                    if current_msg.reference and current_msg.reference.message_id:
                        try:
                            current_msg = current_msg.reference.resolved or await current_msg.channel.fetch_message(current_msg.reference.message_id)
                        except (discord.NotFound, discord.HTTPException):
                            break
                    else:
                        break
                
                # メッセージを逆順にして正しい順序にする
                messages.reverse()
                
            except Exception as e:
                logger.error(f"Failed to collect conversation history for thread: {e}", exc_info=True)
                messages = []
            
            if messages:
                # LLMクライアントを取得
                llm_client = await self.llm_cog._get_llm_client_for_channel(thread.id)
                if not llm_client:
                    await thread.send("❌ LLM client is not available for this thread.\nこのスレッドではLLMクライアントが利用できません。")
                    return
                
                # システムプロンプトを準備
                system_prompt = await self.llm_cog._prepare_system_prompt(
                    thread.id, interaction.user.id, interaction.user.display_name
                )
                
                messages_for_api = [{"role": "system", "content": system_prompt}]
                
                # 言語プロンプト追加
                if self.llm_cog.language_prompt:
                    messages_for_api.append({"role": "system", "content": self.llm_cog.language_prompt})
                
                messages_for_api.extend(messages)
                
                # スレッド内でLLM応答を生成
                model_name = llm_client.model_name_for_api_calls
                waiting_message = f"⏳ Processing conversation history... / 会話履歴を処理中..."
                temp_message = await thread.send(waiting_message)
                
                # スレッド内での会話方法を説明
                await thread.send("💡 **スレッド内での会話方法 / How to chat in this thread:**\n"
                                "• Botのメッセージにリプライして会話を続けられます / Reply to bot messages to continue chatting\n"
                                "• 画像も送信可能です / Images are also supported\n"
                                "• 会話履歴は自動的に保持されます / Conversation history is automatically maintained")
                
                sent_messages, full_response_text, used_key_index = await self.llm_cog._process_streaming_and_send_response(
                    sent_message=temp_message,
                    channel=thread,
                    user=interaction.user,
                    messages_for_api=messages_for_api,
                    llm_client=llm_client
                )
                
                if sent_messages and full_response_text:
                    logger.info(f"✅ Thread conversation completed | model='{model_name}' | response_length={len(full_response_text)} chars")
                    
                    # TTS Cogにカスタムイベントを発火
                    try:
                        self.llm_cog.bot.dispatch("llm_response_complete", sent_messages, full_response_text)
                        logger.info("📢 Dispatched 'llm_response_complete' event for TTS from thread.")
                    except Exception as e:
                        logger.error(f"Failed to dispatch 'llm_response_complete' event from thread: {e}", exc_info=True)
                
                # ボタンを無効化
                button.disabled = True
                button.label = "✅ Thread Created / スレッド作成済み"
                await interaction.edit_original_response(view=self)
                
            else:
                await thread.send("ℹ️ No conversation history found, but you can start chatting!\n"
                                "会話履歴は見つかりませんでしたが、ここから会話を始めることができます！\n\n"
                                "💡 **スレッド内での会話方法 / How to chat in this thread:**\n"
                                "• Botのメッセージにリプライして会話を続けられます / Reply to bot messages to continue chatting\n"
                                "• 画像も送信可能です / Images are also supported\n"
                                "• 会話履歴は自動的に保持されます / Conversation history is automatically maintained")
                
        except Exception as e:
            logger.error(f"Failed to create thread: {e}", exc_info=True)
            await interaction.followup.send("❌ Failed to create thread.\nスレッドの作成に失敗しました。", ephemeral=True)


class LLMCog(commands.Cog, name="LLM"):
    """A cog for interacting with Large Language Models, with tool support."""

    schedule_report_group = app_commands.Group(name="schedule-report",
                                               description="Manage scheduled deep research reports. / 定期リサーチレポートを管理します。")

    def _add_support_footer(self, embed: discord.Embed) -> None:
        current_footer = embed.footer.text if embed.footer and embed.footer.text else ""
        support_text = "\n問題がありますか？開発者にご連絡ください！ / Having issues? Contact the developer!"
        if current_footer:
            embed.set_footer(text=current_footer + support_text)
        else:
            embed.set_footer(text=support_text.strip())

    def _create_support_view(self) -> discord.ui.View:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="サポートサーバー / Support Server", style=discord.ButtonStyle.link,
                                        url="https://discord.gg/H79HKKqx3s", emoji="💬"))
        return view

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(self.bot, 'config') or not self.bot.config: raise commands.ExtensionFailed(self.qualified_name,
                                                                                                  "Bot config not loaded.")
        self.config = self.bot.config
        self.llm_config = self.config.get('llm')
        if not isinstance(self.llm_config, dict): raise commands.ExtensionFailed(self.qualified_name,
                                                                                 "The 'llm' section in config is missing or invalid.")
        self.language_prompt = self.llm_config.get('language_prompt')
        if self.language_prompt: logger.info("Language prompt loaded from config for fallback.")
        self.http_session, self.bot.cfg = aiohttp.ClientSession(), self.llm_config
        self.conversation_threads: Dict[int, Dict[int, List[Dict[str, Any]]]] = {}  # {guild_id: {thread_id: messages}}
        self.message_to_thread: Dict[int, Dict[int, int]] = {}  # {guild_id: {message_id: thread_id}}
        self.llm_clients: Dict[str, openai.AsyncOpenAI] = {}
        self.provider_api_keys: Dict[str, List[str]] = {}
        self.provider_key_index: Dict[str, int] = {}
        self.model_reset_tasks: Dict[int, asyncio.Task] = {}
        self.exception_handler = LLMExceptionHandler(self.llm_config)
        self.channel_settings_path = "data/channel_llm_models.json"
        self.channel_models: Dict[str, str] = self._load_json_data(self.channel_settings_path)
        # Plugin placeholders (populated by _initialize_plugins)
        self.search_agent: Optional[SearchAgent] = None
        self.bio_manager: Optional[BioManager] = None
        self.memory_manager: Optional[MemoryManager] = None
        self.command_manager: Optional[CommandInfoManager] = None
        self.image_generator: Optional[ImageGenerator] = None
        self.command_agent: Optional[CommandAgent] = None
        self.tips_manager: Optional[TipsManager] = None
        self.deep_research_agent: Optional[DeepResearchAgent] = None
        self.reporter_manager: Optional[ScheduledReporter] = None
        logger.info(
            f"Loaded {len(self.channel_models)} channel-specific model settings from '{self.channel_settings_path}'.")
        self.jst = timezone(timedelta(hours=+9))
        (
            self.search_agent,
            self.bio_manager,
            self.memory_manager,
            self.command_manager,
            self.image_generator,
            self.command_agent,
            self.tips_manager,
            self.deep_research_agent,
            self.reporter_manager
        ) = self._initialize_plugins()
        try:
            self.bot.tree.add_command(self.schedule_report_group)
        except app_commands.CommandAlreadyRegistered:
            logger.debug("schedule-report command group already registered; skipping re-add.")
        default_model_string = self.llm_config.get('model')
        if default_model_string:
            main_llm_client = self._initialize_llm_client(default_model_string)
            if main_llm_client:
                self.llm_clients[default_model_string] = main_llm_client
                logger.info(f"Default LLM client '{default_model_string}' initialized and cached.")
            else:
                logger.error("Failed to initialize main LLM client. Core functionality may be disabled.")
        else:
            logger.error("Default LLM model is not configured in config.yaml.")

    def _initialize_plugins(self) -> Tuple[Optional[SearchAgent], Optional[BioManager], Optional[MemoryManager], Optional[CommandInfoManager], Optional[ImageGenerator], Optional[CommandAgent], Optional[TipsManager], Optional[DeepResearchAgent], Optional[ScheduledReporter]]:
        """Initializes and returns all registered plugins."""
        plugins = {
            "SearchAgent": None,
            "BioManager": None,
            "MemoryManager": None,
            "CommandInfoManager": None,
            "ImageGenerator": None,
            "CommandAgent": None,
            "TipsManager": None,
            "DeepResearchAgent": None,
            "ScheduledReporter": None
        }

        # Initialize plugins that are always needed or don't have a specific config toggle
        if BioManager: plugins["BioManager"] = BioManager(self.bot)
        if MemoryManager: plugins["MemoryManager"] = MemoryManager(self.bot)
        if TipsManager: plugins["TipsManager"] = TipsManager()

        # Initialize plugins based on config
        active_tools = self.llm_config.get('active_tools', [])

        logger.info(f"🔍 [TOOLS] Active tools from config: {active_tools}")

        if 'search' in active_tools:
            if SearchAgent:
                plugins["SearchAgent"] = SearchAgent(self.bot)
                #logger.info(f"✅ [TOOLS] Added 'search' tool (name: {self.search_agent.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'search' is in active_tools but search_agent is None")

        if 'user_bio' in active_tools:
            if BioManager:
                plugins["BioManager"] = BioManager(self.bot)
                #logger.info(f"✅ [TOOLS] Added 'user_bio' tool (name: {self.bio_manager.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'user_bio' is in active_tools but bio_manager is None")

        if 'memory' in active_tools:
            if MemoryManager:
                plugins["MemoryManager"] = MemoryManager(self.bot)
                #logger.info(f"✅ [TOOLS] Added 'memory' tool (name: {self.memory_manager.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'memory' is in active_tools but memory_manager is None")

        if 'image_generator' in active_tools:
            if ImageGenerator:
                plugins["ImageGenerator"] = ImageGenerator(self.bot)
                #logger.info(f"✅ [TOOLS] Added 'image_generator' tool (name: {self.image_generator.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'image_generator' is in active_tools but image_generator is None")

        if 'command_executor' in active_tools:
            if CommandAgent:
                plugins["CommandAgent"] = CommandAgent(self.bot)
                #logger.info(f"✅ [TOOLS] Added 'command_executor' tool (name: {self.command_agent.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'command_executor' is in active_tools but command_agent is None")

        try:
            plugins["DeepResearchAgent"] = DeepResearchAgent(self.bot, search_agent=plugins["SearchAgent"])
        except Exception as e:
            logger.error(f"DeepResearchAgent failed to initialize: {e}", exc_info=True)

        try:
            plugins["ScheduledReporter"] = ScheduledReporter(self.bot, deep_research=plugins["DeepResearchAgent"])
        except Exception as e:
            logger.error(f"ScheduledReporter failed to initialize: {e}", exc_info=True)

        # Log initialized plugins
        for name, instance in plugins.items():
            if instance:
                logger.info(f"{name} initialized successfully.")
            else:
                logger.info(f"{name} is not active or failed to initialize.")

        return (
            plugins["SearchAgent"],
            plugins["BioManager"],
            plugins["MemoryManager"],
            plugins["CommandInfoManager"],
            plugins["ImageGenerator"],
            plugins["CommandAgent"],
            plugins["TipsManager"],
            plugins["DeepResearchAgent"],
            plugins["ScheduledReporter"]
        )

    async def cog_unload(self):
        await self.http_session.close()
        for task in self.model_reset_tasks.values(): task.cancel()
        logger.info(f"Cancelled {len(self.model_reset_tasks)} pending model reset tasks.")
        if self.image_generator: await self.image_generator.close()
        if self.reporter_manager: await self.reporter_manager.shutdown()
        try:
            self.bot.tree.remove_command(self.schedule_report_group.name, type=discord.AppCommandType.chat_input)
        except Exception as e:
            logger.debug(f"Failed to remove schedule-report command group: {e}")
        logger.info("LLMCog's aiohttp session has been closed.")

    def _load_json_data(self, path: str) -> Dict[str, Any]:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f: return {str(k): v for k, v in json.load(f).items()}
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load JSON file '{path}': {e}")
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

    async def _save_channel_models(self) -> None:
        await self._save_json_data(self.channel_models, self.channel_settings_path)

    def _initialize_llm_client(self, model_string: Optional[str]) -> Optional[openai.AsyncOpenAI]:
        if not model_string or '/' not in model_string:
            logger.error(f"Invalid model format: '{model_string}'. Expected 'provider_name/model_name'.")
            return None
        try:
            provider_name, model_name = model_string.split('/', 1)
            provider_config = self.llm_config.get('providers', {}).get(provider_name)
            if not provider_config:
                logger.error(f"Configuration for LLM provider '{provider_name}' not found.")
                return None
            
            # KoboldCPP固有の処理
            is_koboldcpp = provider_name.lower() == 'koboldcpp'
            if is_koboldcpp:
                logger.info(f"🔧 [KoboldCPP] Detected KoboldCPP provider. Applying KoboldCPP-specific settings.")
            
            if provider_name not in self.provider_api_keys:
                api_keys, i = [], 1
                while True:
                    if provider_config.get(f'api_key{i}'):
                        api_keys.append(provider_config[f'api_key{i}']); i += 1
                    else:
                        break
                if not api_keys and provider_config.get('api_key'): api_keys.append(provider_config['api_key'])
                if not api_keys:
                    logger.info(
                        f"No API keys found for provider '{provider_name}'. Assuming local model or keyless API.")
                    # KoboldCPPの場合、ダミーキーを使用
                    if is_koboldcpp:
                        self.provider_api_keys[provider_name] = ["koboldcpp-dummy-key"]
                        logger.info(f"🔧 [KoboldCPP] Using dummy API key (KoboldCPP usually doesn't require authentication)")
                    else:
                        self.provider_api_keys[provider_name] = ["no-key-required"]
                else:
                    self.provider_api_keys[provider_name] = api_keys
                    logger.info(f"Loaded {len(api_keys)} API key(s) for provider '{provider_name}'.")
            self.provider_key_index.setdefault(provider_name, 0)
            key_list, current_key_index = self.provider_api_keys[provider_name], self.provider_key_index[provider_name]
            if current_key_index >= len(key_list): current_key_index = 0; self.provider_key_index[provider_name] = 0
            api_key_to_use = key_list[current_key_index]
            
            base_url = provider_config.get('base_url')
            if is_koboldcpp:
                # KoboldCPPのベースURLが正しい形式か確認
                if not base_url.endswith('/v1'):
                    if base_url.endswith('/'):
                        base_url = base_url.rstrip('/') + '/v1'
                    else:
                        base_url = base_url + '/v1'
                    logger.info(f"🔧 [KoboldCPP] Adjusted base_url to: {base_url}")
            
            client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key_to_use, timeout=provider_config.get('timeout', 300.0) if is_koboldcpp else None)
            client.model_name_for_api_calls, client.provider_name = model_name, provider_name
            # KoboldCPP固有のメタデータを設定
            if is_koboldcpp:
                client.supports_tools = provider_config.get('supports_tools', True)
                logger.info(f"🔧 [KoboldCPP] Initialized client with model '{model_name}'")
                logger.info(f"🔧 [KoboldCPP] Base URL: {base_url}")
                logger.info(f"🔧 [KoboldCPP] Tools support: {client.supports_tools}")
                logger.info(f"🔧 [KoboldCPP] Timeout: {provider_config.get('timeout', 300.0)}s")
            else:
                client.supports_tools = True  # 他のプロバイダーはデフォルトでTrue
            
            logger.info(
                f"Initialized LLM client for provider '{provider_name}' with model '{model_name}' using key index {current_key_index}.")
            return client
        except Exception as e:
            logger.error(f"Error initializing LLM client for '{model_string}': {e}", exc_info=True)
            return None

    async def _get_llm_client_for_channel(self, channel_id: int) -> Optional[openai.AsyncOpenAI]:
        model_string = self.channel_models.get(str(channel_id)) or self.llm_config.get('model')
        if not model_string:
            logger.error("No default model is configured.")
            return None
        if model_string in self.llm_clients: return self.llm_clients[model_string]
        logger.info(f"Initializing a new LLM client for model '{model_string}' for channel {channel_id}")
        client = self._initialize_llm_client(model_string)
        if client: self.llm_clients[model_string] = client
        return client

    async def _prepare_system_prompt(self, channel_id: int, user_id: int, user_display_name: str) -> str:
        if not self.bio_manager or not self.memory_manager:
            logger.error("BioManager or MemoryManager is not initialized.")
            return "Error: Core components for prompt generation are missing."
        system_prompt_template = self.bio_manager.get_system_prompt(channel_id=channel_id, user_id=user_id,
                                                                    user_display_name=user_display_name).replace(
            "必ず日本語で応答してください", "").replace("日本語で答えてください", "").replace(
            "Please respond in Japanese", "")
        available_commands = ""
        if self.command_manager:
            await self.bot.wait_until_ready()
            available_commands = self.command_manager.get_all_commands_info()
        else:
            # commands_managerがfalseの場合、またはCommandInfoManagerが利用できない場合
            if not self.llm_config.get('commands_manager', True):
                logger.debug("commands_manager is disabled in config. No commands will be collected.")
            else:
                logger.warning("CommandInfoManager is not available.")
        try:
            now, current_date_str, current_time_str = datetime.now(self.jst), datetime.now(self.jst).strftime(
                '%Y年%m月%d日'), datetime.now(self.jst).strftime('%H:%M')
            if '{available_commands}' in system_prompt_template:
                system_prompt = system_prompt_template.format(current_date=current_date_str,
                                                              current_time=current_time_str,
                                                              available_commands=available_commands)
            else:
                #logger.warning("⚠️ {available_commands} not in template")
                system_prompt = system_prompt_template.format(current_date=current_date_str,
                                                              current_time=current_time_str)
        except (KeyError, ValueError) as e:
            logger.warning(f"Could not format system_prompt: {e}")
            system_prompt = system_prompt_template.replace('{current_date}', current_date_str).replace('{current_time}',
                                                                                                       current_time_str).replace(
                '{available_commands}', available_commands)
        if available_commands and "# 🤖 利用可能なBotコマンド一覧" not in system_prompt: system_prompt += f"\n\n{available_commands}"
        if formatted_memories := self.memory_manager.get_formatted_memories(): system_prompt += f"\n\n{formatted_memories}"
        logger.info(f"🔧 [SYSTEM] System prompt prepared ({len(system_prompt)} chars)")
        return system_prompt

    def get_tools_definition(self) -> Optional[List[Dict[str, Any]]]:
        definitions = []
        active_tools = self.llm_config.get('active_tools', [])

        logger.info(f"🔍 [TOOLS] Active tools from config: {active_tools}")
        logger.debug(f"🔍 [TOOLS] Plugin status: search_agent={self.search_agent is not None}, "
                     f"bio_manager={self.bio_manager is not None}, "
                     f"memory_manager={self.memory_manager is not None}, "
                     f"image_generator={self.image_generator is not None}, "
                     f"command_agent={self.command_agent is not None}")

        if 'search' in active_tools:
            if self.search_agent:
                definitions.append(self.search_agent.tool_spec)
                #logger.info(f"✅ [TOOLS] Added 'search' tool (name: {self.search_agent.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'search' is in active_tools but search_agent is None")

        if 'user_bio' in active_tools:
            if self.bio_manager:
                definitions.append(self.bio_manager.tool_spec)
                #logger.info(f"✅ [TOOLS] Added 'user_bio' tool (name: {self.bio_manager.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'user_bio' is in active_tools but bio_manager is None")

        if 'memory' in active_tools:
            if self.memory_manager:
                definitions.append(self.memory_manager.tool_spec)
                #logger.info(f"✅ [TOOLS] Added 'memory' tool (name: {self.memory_manager.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'memory' is in active_tools but memory_manager is None")

        if 'image_generator' in active_tools:
            if self.image_generator:
                definitions.append(self.image_generator.tool_spec)
                #logger.info(f"✅ [TOOLS] Added 'image_generator' tool (name: {self.image_generator.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'image_generator' is in active_tools but image_generator is None")

        if 'command_executor' in active_tools:
            if self.command_agent:
                definitions.append(self.command_agent.tool_spec)
                #logger.info(f"✅ [TOOLS] Added 'command_executor' tool (name: {self.command_agent.tool_spec['function']['name']})")
            else:
                logger.warning(f"⚠️ [TOOLS] 'command_executor' is in active_tools but command_agent is None")

        if 'deep_research' in active_tools:
            if self.deep_research_agent:
                definitions.append(self.deep_research_agent.tool_spec)
            else:
                logger.warning(f"⚠️ [TOOLS] 'deep_research' is in active_tools but deep_research_agent is None")

        logger.info(f"🔧 [TOOLS] Total tools to return: {len(definitions)}")

        return definitions or None

    def _format_report_datetime(self, iso_str: Optional[str]) -> str:
        try:
            if not iso_str:
                raise ValueError("missing")
            dt = datetime.fromisoformat(iso_str)
        except (ValueError, TypeError):
            return "Unknown"
        return dt.astimezone(self.jst).strftime('%Y-%m-%d %H:%M JST')

    def _resolve_channel_display(self, channel_id: int) -> str:
        channel = self.bot.get_channel(channel_id)
        if channel:
            return channel.mention
        return f"<#{channel_id}>"

    def _reporter_unavailable_embed(self) -> discord.Embed:
        embed = discord.Embed(title="❌ Reporter Not Available / レポーター機能が利用できません",
                              description="ScheduledReporter plugin is not initialized. / ScheduledReporterプラグインが初期化されていません。",
                              color=discord.Color.red())
        self._add_support_footer(embed)
        return embed

    async def _get_conversation_thread_id(self, message: discord.Message) -> int:
        guild_id = message.guild.id if message.guild else 0  # DMの場合は0
        
        # ギルド固有の辞書を初期化
        if guild_id not in self.message_to_thread:
            self.message_to_thread[guild_id] = {}
        
        if message.id in self.message_to_thread[guild_id]: 
            return self.message_to_thread[guild_id][message.id]
        
        current_msg, visited_ids = message, set()
        while current_msg.reference and current_msg.reference.message_id:
            if current_msg.id in visited_ids: break
            visited_ids.add(current_msg.id)
            try:
                parent_msg = current_msg.reference.resolved or await message.channel.fetch_message(
                    current_msg.reference.message_id)
                if parent_msg.author != self.bot.user: break
                current_msg = parent_msg
            except (discord.NotFound, discord.HTTPException):
                break
        thread_id = current_msg.id
        self.message_to_thread[guild_id][message.id] = thread_id
        return thread_id

    async def _collect_conversation_history(self, message: discord.Message) -> List[Dict[str, Any]]:
        guild_id = message.guild.id if message.guild else 0  # DMの場合は0
        
        # ギルド固有の会話履歴を初期化
        if guild_id not in self.conversation_threads:
            self.conversation_threads[guild_id] = {}
        
        history = []
        current_msg = message
        visited_ids = set()
        max_depth = 50  # 無限ループ防止
        
        # リプライチェーンを遡って会話履歴を収集
        depth = 0
        while current_msg.reference and current_msg.reference.message_id and depth < max_depth:
            if current_msg.reference.message_id in visited_ids:
                break
            visited_ids.add(current_msg.reference.message_id)
            depth += 1
            
            try:
                # 参照メッセージを取得
                parent_msg = current_msg.reference.resolved
                if not parent_msg:
                    parent_msg = await message.channel.fetch_message(current_msg.reference.message_id)
                
                if isinstance(parent_msg, discord.DeletedReferencedMessage):
                    logger.debug(f"Encountered deleted referenced message in history collection.")
                    break
                
                # Botのメッセージの場合、保存された会話履歴から取得
                if parent_msg.author == self.bot.user:
                    thread_id = await self._get_conversation_thread_id(parent_msg)
                    if thread_id in self.conversation_threads[guild_id]:
                        # このメッセージIDに対応するassistantメッセージを検索
                        found_assistant = False
                        for msg in reversed(self.conversation_threads[guild_id][thread_id]):
                            if msg.get("role") == "assistant" and msg.get("message_id") == parent_msg.id:
                                history.append({"role": "assistant", "content": msg["content"]})
                                found_assistant = True
                                # このassistantメッセージより前の会話履歴も含める
                                thread_history = self.conversation_threads[guild_id][thread_id]
                                assistant_index = thread_history.index(msg)
                                # assistantより前のメッセージを追加（時系列順に）
                                for prev_msg in thread_history[:assistant_index]:
                                    history.append(prev_msg)
                                break
                        
                        if not found_assistant:
                            # 履歴にない場合は、そのメッセージの内容を直接取得
                            if parent_msg.content:
                                history.append({"role": "assistant", "content": parent_msg.content})
                
                # ユーザーのメッセージの場合
                elif parent_msg.author != self.bot.user:
                    image_contents, text_content = await self._prepare_multimodal_content(parent_msg)
                    text_content = text_content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
                    if text_content or image_contents:
                        user_content_parts = []
                        if text_content:
                            user_content_parts.append({
                                "type": "text",
                                "text": f"{parent_msg.created_at.astimezone(self.jst).strftime('[%H:%M]')} {text_content}"
                            })
                        user_content_parts.extend(image_contents)
                        history.append({"role": "user", "content": user_content_parts})
                
                # 親メッセージに移動
                current_msg = parent_msg
                
            except (discord.NotFound, discord.HTTPException) as e:
                logger.debug(f"Could not fetch parent message: {e}")
                break
            except Exception as e:
                logger.error(f"Error collecting conversation history: {e}", exc_info=True)
                break
        
        # 会話履歴を時系列順に並び替え（古いものから新しいものへ）
        history.reverse()
        
        # 最大履歴数で制限
        max_history_entries = self.llm_config.get('max_messages', 10) * 2
        if len(history) > max_history_entries:
            history = history[-max_history_entries:]
        
        return history

    async def _process_image_url(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    image_bytes = await response.read()
                    if len(image_bytes) > 20 * 1024 * 1024:
                        logger.warning(f"Image too large ({len(image_bytes)} bytes): {url}")
                        return None
                    mime_type = response.content_type
                    if not mime_type or not mime_type.startswith('image/'):
                        ext = url.split('.')[-1].lower().split('?')
                        mime_type = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif',
                                     'webp': 'image/webp'}.get(ext, 'image/jpeg')
                    if mime_type == 'image/gif':
                        try:
                            from PIL import Image
                            gif_image = Image.open(io.BytesIO(image_bytes))
                            if getattr(gif_image, 'is_animated', False):
                                logger.info(
                                    f"🎬 [IMAGE] Detected animated GIF. Converting to static image: {url[:100]}...")
                                gif_image.seek(0)
                                if gif_image.mode != 'RGBA': gif_image = gif_image.convert('RGBA')
                                output_buffer = io.BytesIO()
                                gif_image.save(output_buffer, format='PNG', optimize=True)
                                image_bytes, mime_type = output_buffer.getvalue(), 'image/png'
                                logger.debug(
                                    f"🖼️ [IMAGE] Converted animated GIF to PNG (Size: {len(image_bytes)} bytes)")
                            else:
                                logger.debug(f"🖼️ [IMAGE] Static GIF detected, processing normally")
                        except ImportError:
                            logger.warning(
                                "⚠️ Pillow (PIL) library not found. Cannot process animated GIFs. Skipping image.")
                            return None
                        except Exception as gif_error:
                            logger.error(f"❌ Error processing GIF image: {gif_error}", exc_info=True)
                            return None
                    encoded_image = base64.b64encode(image_bytes).decode('utf-8')
                    logger.debug(
                        f"🖼️ [IMAGE] Successfully processed image: {url[:100]}... (MIME: {mime_type}, Size: {len(image_bytes)} bytes)")
                    return {"type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded_image}", "detail": "auto"}}
                else:
                    logger.warning(f"Failed to download image from {url} (Status: {response.status})")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"Timeout while downloading image: {url}")
            return None
        except Exception as e:
            logger.error(f"Error processing image URL {url}: {e}", exc_info=True)
            return None

    async def _prepare_multimodal_content(self, message: discord.Message) -> Tuple[List[Dict[str, Any]], str]:
        image_inputs, processed_urls, messages_to_scan, visited_ids, current_msg = [], set(), [], set(), message
        for i in range(5):
            if not current_msg or current_msg.id in visited_ids: break
            if isinstance(current_msg, discord.DeletedReferencedMessage): break
            messages_to_scan.append(current_msg)
            visited_ids.add(current_msg.id)
            if current_msg.reference and current_msg.reference.message_id:
                try:
                    current_msg = current_msg.reference.resolved or await message.channel.fetch_message(
                        current_msg.reference.message_id)
                except (discord.NotFound, discord.HTTPException):
                    break
            else:
                break
        source_urls, text_parts = [], []
        for msg in reversed(messages_to_scan):
            if msg.author != self.bot.user:
                if text_content_part := IMAGE_URL_PATTERN.sub('', msg.content).strip(): text_parts.append(
                    text_content_part)
            for url in IMAGE_URL_PATTERN.findall(msg.content):
                if url not in processed_urls: source_urls.append(url); processed_urls.add(url)
            for attachment in msg.attachments:
                if attachment.content_type and attachment.content_type.startswith(
                    'image/') and attachment.url not in processed_urls: source_urls.append(
                    attachment.url); processed_urls.add(attachment.url)
            for embed in msg.embeds:
                if embed.image and embed.image.url and embed.image.url not in processed_urls: source_urls.append(
                    embed.image.url); processed_urls.add(embed.image.url)
                if embed.thumbnail and embed.thumbnail.url and embed.thumbnail.url not in processed_urls: source_urls.append(
                    embed.thumbnail.url); processed_urls.add(embed.thumbnail.url)
        max_images = self.llm_config.get('max_images', 1)
        for url in source_urls[:max_images]:
            if image_data := await self._process_image_url(url): image_inputs.append(image_data)
        if len(source_urls) > max_images:
            try:
                await message.channel.send(self.llm_config.get('error_msg', {}).get('msg_max_image_size',
                                                                                    "⚠️ Max images ({max_images}) reached.\n⚠️ 一度に処理できる画像の最大枚数({max_images}枚)を超えました。").format(
                    max_images=max_images), delete_after=10, silent=True)
            except discord.HTTPException:
                pass
        return image_inputs, "\n".join(text_parts)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        
        # スレッド内ではBotのメッセージへのリプライのみに反応
        is_thread = isinstance(message.channel, discord.Thread)
        is_mentioned = self.bot.user.mentioned_in(message) and not message.mention_everyone
        is_reply_to_bot = (message.reference and message.reference.resolved and 
                           isinstance(message.reference.resolved, discord.Message) and 
                           message.reference.resolved.author == self.bot.user)
        
        # リプライの場合はメンション必須、通常チャンネルではメンションのみ
        if is_reply_to_bot:
            # リプライの場合はメンションが必要
            if not is_mentioned:
                return
        elif not is_mentioned:
            # リプライでない場合もメンションが必要
            return
        try:
            llm_client = await self._get_llm_client_for_channel(message.channel.id)
            if not llm_client:
                # 修正点：デフォルトのエラーメッセージを一度変数に格納する
                default_error_msg = 'LLM client is not available for this channel.\nこのチャンネルではLLMクライアントが利用できません。'
                error_msg = self.llm_config.get('error_msg', {}).get('general_error', default_error_msg)

                await message.reply(
                    content=f"❌ **Error / エラー** ❌\n\n{error_msg}",  # 修正点：変数を使ってf-stringを構成する
                    view=self._create_support_view(), silent=True)
                return
        except Exception as e:
            logger.error(f"Failed to get LLM client for channel {message.channel.id}: {e}", exc_info=True)
            await message.reply(content=f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}",
                                view=self._create_support_view(), silent=True)
            return
        guild_log = f"guild='{message.guild.name}({message.guild.id})'" if message.guild else "guild='DM'"
        user_log = f"user='{message.author.name}({message.author.id})'"
        model_in_use = llm_client.model_name_for_api_calls
        image_contents, text_content = await self._prepare_multimodal_content(message)
        text_content = text_content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not text_content and not image_contents:
            error_key = 'empty_reply' if is_reply_to_bot and not is_mentioned else 'empty_mention_reply'
            await message.reply(content=self.llm_config.get('error_msg', {}).get(error_key,
                                                                                 "Please say something.\n何かお話しください。" if error_key == 'empty_reply' else "Yes, how can I help you?\nはい、何か御用でしょうか?"),
                                view=self._create_support_view(), silent=True)
            return
        logger.info(
            f"📨 Received LLM request | {guild_log} | {user_log} | model='{model_in_use}' | text_length={len(text_content)} chars | images={len(image_contents)}")
        if text_content: logger.info(
            f"[on_message] {message.guild.name if message.guild else 'DM'}({message.guild.id if message.guild else 0}),{message.author.name}({message.author.id})💬 [USER_INPUT] {((text_content[:200] + '...') if len(text_content) > 203 else text_content).replace(chr(10), ' ')}")
        thread_id = await self._get_conversation_thread_id(message)
        if not self.bio_manager or not self.memory_manager:
            await message.reply(
                content="❌ **Error / エラー** ❌\n\nCannot respond because required plugins are not initialized.\n必要なプラグインが初期化されていないため、応答できません。",
                view=self._create_support_view(), silent=True)
            return
        system_prompt = await self._prepare_system_prompt(message.channel.id, message.author.id,
                                                          message.author.display_name)
        messages_for_api: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if self.language_prompt:
            messages_for_api.append({"role": "system", "content": self.language_prompt})
            logger.info("🌐 [LANG] Using language prompt from config")
        conversation_history = await self._collect_conversation_history(message)
        messages_for_api.extend(conversation_history)
        user_content_parts = []
        if text_content: user_content_parts.append(
            {"type": "text", "text": f"{message.created_at.astimezone(self.jst).strftime('[%H:%M]')} {text_content}"})
        user_content_parts.extend(image_contents)
        if image_contents: logger.debug(f"Including {len(image_contents)} image(s) in request")
        user_message_for_api = {"role": "user", "content": user_content_parts}
        messages_for_api.append(user_message_for_api)
        logger.info(f"🔵 [API] Sending {len(messages_for_api)} messages to LLM")
        logger.debug(
            # FIX IS HERE
            f"Messages structure: system={len(messages_for_api[0]['content'])} chars, lang_override={'present' if len(messages_for_api) > 1 and 'CRITICAL' in str(messages_for_api) else 'absent'}")
        try:
            # スレッド作成ボタンは削除（常にFalse）
            is_first_response = False
            sent_messages, llm_response, used_key_index = await self._handle_llm_streaming_response(message,
                                                                                                    messages_for_api,
                                                                                                    llm_client,
                                                                                                    is_first_response)
            if sent_messages and llm_response:
                logger.info(
                    f"✅ LLM response completed | model='{model_in_use}' | response_length={len(llm_response)} chars")
                log_response = (llm_response[:200] + '...') if len(llm_response) > 203 else llm_response
                key_log_str = f" [key{used_key_index + 1}]" if used_key_index is not None else ""
                logger.info(f"🤖 [LLM_RESPONSE]{key_log_str} {log_response.replace(chr(10), ' ')}")
                logger.debug(f"LLM full response (length: {len(llm_response)} chars):\n{llm_response}")
                guild_id = message.guild.id if message.guild else 0  # DMの場合は0
                
                # ギルド固有の会話履歴を初期化
                if guild_id not in self.conversation_threads:
                    self.conversation_threads[guild_id] = {}
                if thread_id not in self.conversation_threads[guild_id]: 
                    self.conversation_threads[guild_id][thread_id] = []
                
                self.conversation_threads[guild_id][thread_id].append(user_message_for_api)
                assistant_message = {"role": "assistant", "content": llm_response, "message_id": sent_messages[0].id}
                self.conversation_threads[guild_id][thread_id].append(assistant_message)
                for msg in sent_messages: 
                    guild_id_for_msg = msg.guild.id if msg.guild else 0
                    if guild_id_for_msg not in self.message_to_thread:
                        self.message_to_thread[guild_id_for_msg] = {}
                    self.message_to_thread[guild_id_for_msg][msg.id] = thread_id
                self._cleanup_old_threads()

                # TTS Cogにカスタムイベントを発火させる
                try:
                    self.bot.dispatch("llm_response_complete", sent_messages, llm_response)
                    logger.info("📢 Dispatched 'llm_response_complete' event for TTS.")
                except Exception as e:
                    logger.error(f"Failed to dispatch 'llm_response_complete' event: {e}", exc_info=True)

        except Exception as e:
            await message.reply(content=f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}",
                                view=self._create_support_view(), silent=True)

    def _cleanup_old_threads(self):
        for guild_id in list(self.conversation_threads.keys()):
            guild_threads = self.conversation_threads[guild_id]
            if len(guild_threads) > 100:
                threads_to_remove = list(guild_threads.keys())[:len(guild_threads) - 100]
                for thread_id in threads_to_remove:
                    del guild_threads[thread_id]
                    if guild_id in self.message_to_thread:
                        self.message_to_thread[guild_id] = {
                            k: v for k, v in self.message_to_thread[guild_id].items() 
                            if v != thread_id
                        }

    async def _handle_llm_streaming_response(self, message: discord.Message, initial_messages: List[Dict[str, Any]],
                                             client: openai.AsyncOpenAI, is_first_response: bool = False) -> Tuple[
        Optional[List[discord.Message]], str, Optional[int]]:
        sent_message = None
        try:
            model_name = client.model_name_for_api_calls
            if self.tips_manager:
                waiting_embed = self.tips_manager.get_waiting_embed(model_name)
                try:
                    sent_message = await message.reply(embed=waiting_embed, silent=True)
                except discord.HTTPException:
                    sent_message = await message.channel.send(embed=waiting_embed, silent=True)
            else:
                waiting_message = f"-# :incoming_envelope: waiting response for '{model_name}' :incoming_envelope:"
                try:
                    sent_message = await message.reply(waiting_message, silent=True)
                except discord.HTTPException:
                    sent_message = await message.channel.send(waiting_message, silent=True)
            return await self._process_streaming_and_send_response(sent_message=sent_message, channel=message.channel,
                                                                   user=message.author,
                                                                   messages_for_api=initial_messages, llm_client=client,
                                                                   is_first_response=is_first_response)
        except Exception as e:
            logger.error(f"❌ Error during LLM streaming response: {e}", exc_info=True)
            error_msg = f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}"
            if sent_message:
                try:
                    await sent_message.edit(content=error_msg, embed=None, view=self._create_support_view())
                except discord.HTTPException:
                    pass
            else:
                await message.reply(content=error_msg, view=self._create_support_view(), silent=True)
            return None, "", None

    async def _process_streaming_and_send_response(self, sent_message: discord.Message,
                                                   channel: discord.abc.Messageable,
                                                   user: Union[discord.User, discord.Member],
                                                   messages_for_api: List[Dict[str, Any]],
                                                   llm_client: openai.AsyncOpenAI,
                                                   is_first_response: bool = False) -> Tuple[
        Optional[List[discord.Message]], str, Optional[int]]:
        full_response_text, last_update, last_displayed_length, chunk_count = "", 0.0, 0, 0
        update_interval, min_update_chars, retry_sleep_time = 0.5, 15, 2.0
        emoji_prefix, emoji_suffix = ":incoming_envelope: ", " :incoming_envelope:"
        max_final_retries, final_retry_delay = 3, 2.0
        is_first_update = True
        logger.debug(f"Starting LLM stream for message {sent_message.id}")
        stream_generator = self._llm_stream_and_tool_handler(messages_for_api, llm_client, channel.id, user.id)
        async for content_chunk in stream_generator:
            if not content_chunk:
                continue
            chunk_count += 1
            full_response_text += content_chunk
            if chunk_count % 100 == 0: logger.debug(
                f"Stream chunk #{chunk_count}, total length: {len(full_response_text)} chars")
            current_time, chars_accumulated = time.time(), len(full_response_text) - last_displayed_length

            should_update = is_first_update or (
                    current_time - last_update > update_interval and chars_accumulated >= min_update_chars)

            if should_update and full_response_text:
                is_first_update = False
                display_length = len(full_response_text)
                if display_length > SAFE_MESSAGE_LENGTH:
                    display_text = f"{emoji_prefix}{full_response_text[:SAFE_MESSAGE_LENGTH - len(emoji_prefix) - len(emoji_suffix) - 100]}\n\n⚠️ (Output is long, will be split...)\n⚠️ (出力が長いため分割します...){emoji_suffix}"
                else:
                    display_text = f"{emoji_prefix}{full_response_text[:SAFE_MESSAGE_LENGTH - len(emoji_prefix) - len(emoji_suffix)]}{emoji_suffix}"
                if display_text != sent_message.content:
                    try:
                        await sent_message.edit(content=display_text)
                        last_update, last_displayed_length = current_time, len(full_response_text)
                        logger.debug(f"Updated Discord message (displayed: {len(display_text)} chars)")
                    except discord.NotFound:
                        logger.warning(f"⚠️ Message deleted during stream (ID: {sent_message.id}). Aborting.")
                        return None, "", None
                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = (e.retry_after or 1.0) + 0.5
                            logger.warning(
                                f"⚠️ Rate limited on message edit (ID: {sent_message.id}). Waiting {retry_after:.2f}s")
                            await asyncio.sleep(retry_after)
                            last_update = time.time()
                        else:
                            logger.warning(
                                f"⚠️ Failed to edit message (ID: {sent_message.id}): {e.status} - {getattr(e, 'text', str(e))}")
                            await asyncio.sleep(retry_sleep_time)
        logger.debug(f"Stream completed | Total chunks: {chunk_count} | Final length: {len(full_response_text)} chars")
        if full_response_text:
            if len(full_response_text) <= SAFE_MESSAGE_LENGTH:
                # スレッド作成ボタンは削除
                view = None
                
                for attempt in range(max_final_retries):
                    try:
                        if full_response_text != sent_message.content:
                            await sent_message.edit(content=full_response_text, embed=None, view=view)
                        logger.debug(f"Final message updated successfully (attempt {attempt + 1})")
                        break
                    except discord.NotFound:
                        logger.error(f"❌ Message was deleted before final update")
                        return None, "", None
                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = (e.retry_after or 1.0) + 0.5
                            logger.warning(
                                f"⚠️ Rate limited on final update (attempt {attempt + 1}/{max_final_retries}). Waiting {retry_after:.2f}s")
                            await asyncio.sleep(retry_after)
                        else:
                            logger.warning(
                                f"⚠️ Failed to update final message (attempt {attempt + 1}/{max_final_retries}): {e.status} - {getattr(e, 'text', str(e))}")
                            if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                return [sent_message], full_response_text, getattr(llm_client, 'last_used_key_index', None)
            else:
                logger.debug(f"Response is {len(full_response_text)} chars, splitting into multiple messages")
                # 修正: タプル作成のバグを修正
                chunks = _split_message_smartly(full_response_text, SAFE_MESSAGE_LENGTH)
                all_messages = []
                first_chunk = chunks[0]  # 最初のチャンクを取得

                # スレッド作成ボタンは削除
                view = None

                for attempt in range(max_final_retries):
                    try:
                        await sent_message.edit(content=first_chunk, embed=None, view=view)
                        all_messages.append(sent_message)
                        logger.debug(f"Updated first message (1/{len(chunks)})")
                        break
                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = (e.retry_after or 1.0) + 0.5
                            logger.warning(f"⚠️ Rate limited on first chunk update, waiting {retry_after:.2f}s")
                            await asyncio.sleep(retry_after)
                        else:
                            logger.error(f"❌ Failed to update first message: {e}")
                            if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                for i, chunk in enumerate(chunks[1:], start=2):
                    for attempt in range(max_final_retries):
                        try:
                            continuation_msg = await channel.send(chunk)
                            all_messages.append(continuation_msg)
                            logger.debug(f"Sent continuation message {i}/{len(chunks)}")
                            break
                        except discord.HTTPException as e:
                            if e.status == 429:
                                retry_after = (e.retry_after or 1.0) + 0.5
                                logger.warning(f"⚠️ Rate limited on continuation {i}, waiting {retry_after:.2f}s")
                                await asyncio.sleep(retry_after)
                            else:
                                logger.error(f"❌ Failed to send continuation message {i}: {e}")
                                if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                return all_messages, full_response_text, getattr(llm_client, 'last_used_key_index', None)
        else:
            finish_reason = getattr(llm_client, 'last_finish_reason', None)
            if finish_reason == 'content_filter':
                error_msg = self.llm_config.get('error_msg', {}).get('content_filter_error',
                                                                     "The response was blocked by the content filter.\nAIの応答がコンテンツフィルターによってブロックされました。");
                logger.warning(
                    f"⚠️ Empty response from LLM due to content filter.")
            else:
                error_msg = self.llm_config.get('error_msg', {}).get('empty_response_error',
                                                                     "There was no response from the AI. Please try rephrasing your message.\nAIから応答がありませんでした。表現を変えてもう一度お試しください。");
                logger.warning(
                    f"⚠️ Empty response from LLM (Finish reason: {finish_reason})")
            await sent_message.edit(content=f"❌ **Error / エラー** ❌\n\n{error_msg}", embed=None,
                                    view=self._create_support_view())
            return None, "", None

    def _convert_messages_for_gemini(self, messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
        system_prompts_content, other_messages, has_system_message = [], [], False
        for message in messages:
            if message.get("role") == "system":
                if isinstance(message.get("content"), str) and message["content"].strip():
                    system_prompts_content.append(message["content"])
                    has_system_message = True
            else:
                other_messages.append(message)
        if not has_system_message: return messages, ""
        combined_system_prompt = "\n\n".join(system_prompts_content)
        converted_messages = [{"role": "user", "content": combined_system_prompt},
                              {"role": "assistant", "content": "承知いたしました。指示に従います。"}]
        converted_messages.extend(other_messages)
        return converted_messages, combined_system_prompt

    async def _llm_stream_and_tool_handler(self, messages: List[Dict[str, Any]], client: openai.AsyncOpenAI,
                                           channel_id: int, user_id: int) -> AsyncGenerator[str, None]:
        model_string = self.channel_models.get(str(channel_id)) or self.llm_config.get('model')
        is_gemini = model_string and 'gemini' in model_string.lower()

        if is_gemini:
            original_messages_for_log = messages
            messages, combined_system_prompt = self._convert_messages_for_gemini(messages)
            if combined_system_prompt:
                logger.info(f"🔄 [GEMINI ADAPTER] Converting system prompts for Gemini model '{model_string}'.")
                logger.debug(
                    f"  - Combined system prompt ({len(combined_system_prompt)} chars): {combined_system_prompt.replace(chr(10), ' ')[:300]}...")
                logger.debug(f"  - Message count changed: {len(original_messages_for_log)} -> {len(messages)}")

        current_messages = messages.copy()
        max_iterations = self.llm_config.get('max_tool_iterations', 5)
        extra_params = self.llm_config.get('extra_api_parameters', {})

        provider_name = getattr(client, 'provider_name', None)
        if not provider_name:
            if model_string and '/' in model_string:
                provider_name = model_string.split('/', 1)[0]
                logger.debug(
                    "Provider name missing on client; inferring from model string as '%s'", provider_name
                )
            else:
                provider_name = "unknown"
                logger.warning(
                    "Provider name missing on client and could not be inferred from model string."
                )
            client.provider_name = provider_name

        for iteration in range(max_iterations):
            logger.debug(f"Starting LLM API call (iteration {iteration + 1}/{max_iterations})")
            tools_def = self.get_tools_definition()

            api_kwargs = {
                "model": client.model_name_for_api_calls,
                "messages": current_messages,
                "stream": True,
                "temperature": extra_params.get('temperature', 0.7),
                "max_tokens": extra_params.get('max_tokens', 4096)
            }

            # ✅ Gemini でも tools を正しく渡す
            # KoboldCPPの場合はツールサポートをチェック
            is_koboldcpp = provider_name.lower() == 'koboldcpp'
            supports_tools = getattr(client, 'supports_tools', True)
            
            if tools_def and supports_tools:
                api_kwargs["tools"] = tools_def
                # Geminiモデルでは tool_choice パラメータを設定しない
                # Geminiは tool_choice をサポートしていないか、異なる形式を要求する可能性がある
                if not is_gemini:
                    api_kwargs["tool_choice"] = "auto"
                else:
                    logger.debug(f"🔧 [GEMINI] Skipping tool_choice parameter for Gemini model")
                # Safely get tool names, handling cases where the structure might be different
                tool_names = []
                for t in tools_def:
                    try:
                        if isinstance(t, dict):
                            if 'function' in t and isinstance(t['function'], dict):
                                tool_names.append(t['function'].get('name', 'unnamed_function'))
                            elif 'name' in t:
                                tool_names.append(t['name'])
                            else:
                                tool_names.append('unnamed_tool')
                        else:
                            tool_names.append(str(t))
                    except Exception as e:
                        logger.warning(f"⚠️ [TOOLS] Error processing tool: {e}")
                        tool_names.append('error_processing_tool')
                
                logger.info(f"🔧 [TOOLS] Passing {len(tools_def)} tools to API: {tool_names}")
                if is_koboldcpp:
                    logger.info(f"🔧 [KoboldCPP] Tools are enabled for this model")
                if is_gemini:
                    logger.info(f"🔧 [GEMINI] Tools are enabled for Gemini model (without tool_choice)")
            elif tools_def and not supports_tools:
                logger.warning(
                    f"⚠️ [TOOLS] Tools are disabled for provider '{provider_name}' (supports_tools=false). Skipping tools.")
                if is_koboldcpp:
                    logger.warning(
                        f"⚠️ [KoboldCPP] This KoboldCPP model may not support tools. Consider enabling 'supports_tools: true' in config if the model supports it.")
            else:
                logger.warning(f"⚠️ [TOOLS] No tools available to pass to API")

            stream = None
            api_keys = self.provider_api_keys.get(client.provider_name, [])
            num_keys = len(api_keys)

            if num_keys == 0:
                raise Exception(f"No API keys available for provider {provider_name}")

            for attempt in range(num_keys):
                try:
                    current_key_index = self.provider_key_index.get(provider_name, 0)
                    client.last_used_key_index = current_key_index
                    logger.debug(
                        f"Attempting API call to '{provider_name}' with key index {current_key_index} (Attempt {attempt + 1}/{num_keys}).")
                    stream = await client.chat.completions.create(**api_kwargs)
                    logger.debug(f"Stream connection established successfully.")
                    break
                except (openai.RateLimitError, openai.InternalServerError) as e:
                    error_type = "Rate limit" if isinstance(e, openai.RateLimitError) else "Server"
                    status_code = getattr(e, 'status_code', 'N/A')
                    logger.warning(
                        f"⚠️ {error_type} error ({status_code}) for provider '{provider_name}' with key index {current_key_index}. Details: {e}")
                    if attempt + 1 >= num_keys:
                        error_msg = f"Tried {num_keys} API key(s), but no response was received."
                        logger.error(f"❌ {error_msg} Provider: '{provider_name}'")
                        raise Exception(error_msg)
                    next_key_index = (current_key_index + 1) % num_keys
                    self.provider_key_index[provider_name] = next_key_index
                    next_key = api_keys[next_key_index]
                    logger.info(
                        f"🔄 Switching to next API key for provider '{provider_name}' (index: {next_key_index}) and retrying.")
                    provider_config = self.llm_config.get('providers', {}).get(provider_name, {})
                    is_koboldcpp = provider_name.lower() == 'koboldcpp'
                    timeout = provider_config.get('timeout', 300.0) if is_koboldcpp else None
                    new_client = openai.AsyncOpenAI(base_url=client.base_url, api_key=next_key, timeout=timeout)
                    new_client.model_name_for_api_calls = client.model_name_for_api_calls
                    new_client.provider_name = client.provider_name
                    # KoboldCPPメタデータを保持
                    if is_koboldcpp:
                        new_client.supports_tools = getattr(client, 'supports_tools', provider_config.get('supports_tools', True))
                    else:
                        new_client.supports_tools = getattr(client, 'supports_tools', True)
                    client = new_client
                    self.llm_clients[f"{provider_name}/{client.model_name_for_api_calls}"] = new_client
                    await asyncio.sleep(1)
                except (openai.BadRequestError, openai.APIStatusError) as e:
                    status_code = getattr(e, 'status_code', None)
                    # デバッグ用にapi_kwargsの内容をログに記録（機密情報を除外）
                    debug_kwargs = {k: v for k, v in api_kwargs.items() if k != 'messages'}
                    debug_kwargs['messages_count'] = len(api_kwargs.get('messages', []))
                    if 'messages' in api_kwargs and api_kwargs['messages']:
                        # 最初と最後のメッセージの概要のみ記録
                        first_msg = api_kwargs['messages'][0]
                        last_msg = api_kwargs['messages'][-1]
                        debug_kwargs['first_message'] = {
                            'role': first_msg.get('role'),
                            'content_preview': str(first_msg.get('content', ''))[:100] if isinstance(first_msg.get('content'), str) else type(first_msg.get('content')).__name__
                        }
                        debug_kwargs['last_message'] = {
                            'role': last_msg.get('role'),
                            'content_preview': str(last_msg.get('content', ''))[:100] if isinstance(last_msg.get('content'), str) else type(last_msg.get('content')).__name__
                        }
                    logger.error(f"❌ [API ERROR] Provider: '{provider_name}', Status: {status_code}")
                    logger.error(f"❌ [API ERROR] Request parameters: {debug_kwargs}")
                    logger.error(f"❌ [API ERROR] Full error: {e}")
                    
                    if isinstance(status_code, int) and status_code >= 500:
                        logger.warning(
                            f"⚠️ Server-like status error ({status_code}) for provider '{provider_name}' with key index {current_key_index}. Details: {e}")
                    elif isinstance(status_code, int) and status_code >= 400:
                        logger.warning(
                            f"⚠️ Client error ({status_code}) for provider '{provider_name}' with key index {current_key_index}. Details: {e}")
                    else:
                        logger.warning(
                            f"⚠️ Bad request/API status error for provider '{provider_name}' with key index {current_key_index}. Details: {e}")

                    if attempt + 1 >= num_keys:
                        error_msg = f"Tried {num_keys} API key(s), but no response was received."
                        logger.error(f"❌ {error_msg} Provider: '{provider_name}'")
                        raise Exception(error_msg)

                    next_key_index = (current_key_index + 1) % num_keys
                    self.provider_key_index[provider_name] = next_key_index
                    next_key = api_keys[next_key_index]
                    logger.info(
                        f"🔄 Switching to next API key for provider '{provider_name}' (index: {next_key_index}) after error and retrying.")
                    provider_config = self.llm_config.get('providers', {}).get(provider_name, {})
                    is_koboldcpp = provider_name.lower() == 'koboldcpp'
                    timeout = provider_config.get('timeout', 300.0) if is_koboldcpp else None
                    new_client = openai.AsyncOpenAI(base_url=client.base_url, api_key=next_key, timeout=timeout)
                    new_client.model_name_for_api_calls = client.model_name_for_api_calls
                    new_client.provider_name = client.provider_name
                    if is_koboldcpp:
                        new_client.supports_tools = getattr(client, 'supports_tools', provider_config.get('supports_tools', True))
                    else:
                        new_client.supports_tools = getattr(client, 'supports_tools', True)
                    client = new_client
                    self.llm_clients[f"{provider_name}/{client.model_name_for_api_calls}"] = new_client
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"❌ Unhandled error calling LLM API: {e}", exc_info=True)
                    raise

            if stream is None:
                error_msg = f"Tried {num_keys} API key(s), but no response was received."
                logger.error(f"❌ {error_msg} Provider: '{provider_name}'")
                raise Exception(error_msg)

            tool_calls_buffer = []
            assistant_response_content = ""
            finish_reason = None

            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta and delta.content:
                    assistant_response_content += delta.content
                    yield delta.content
                if delta and delta.tool_calls:
                    for tool_call_chunk in delta.tool_calls:
                        chunk_index = tool_call_chunk.index if tool_call_chunk.index is not None else 0
                        if len(tool_calls_buffer) <= chunk_index:
                            tool_calls_buffer.append(
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        buffer = tool_calls_buffer[chunk_index]
                        if tool_call_chunk.id:
                            buffer["id"] = tool_call_chunk.id
                        if tool_call_chunk.function:
                            if tool_call_chunk.function.name:
                                buffer["function"]["name"] = tool_call_chunk.function.name
                            if tool_call_chunk.function.arguments:
                                buffer["function"]["arguments"] += tool_call_chunk.function.arguments

            client.last_finish_reason = finish_reason
            assistant_message = {"role": "assistant", "content": assistant_response_content or None}
            if tool_calls_buffer:
                assistant_message["tool_calls"] = tool_calls_buffer
            current_messages.append(assistant_message)

            if not tool_calls_buffer:
                logger.debug(f"No tool calls, returning final response (Finish reason: {finish_reason})")
                return

            logger.info(f"🔧 [TOOL] LLM requested {len(tool_calls_buffer)} tool call(s)")
            for tc in tool_calls_buffer:
                logger.debug(
                    f"Tool call details: {tc['function']['name']} with args: {tc['function']['arguments'][:200]}")

            tool_calls_obj = [
                SimpleNamespace(
                    id=tc['id'],
                    function=SimpleNamespace(
                        name=tc['function']['name'],
                        arguments=tc['function']['arguments']
                    )
                ) for tc in tool_calls_buffer
            ]
            await self._process_tool_calls(tool_calls_obj, current_messages, channel_id, user_id)

        logger.warning(f"⚠️ Tool processing exceeded max iterations ({max_iterations})")
        yield self.llm_config.get('error_msg', {}).get('tool_loop_timeout',
                                                       "Tool processing exceeded max iterations.\nツールの処理が最大反復回数を超えました.")

    async def _process_tool_calls(self, tool_calls: List[Any], messages: List[Dict[str, Any]], channel_id: int,
                                  user_id: int) -> None:
        for tool_call in tool_calls:
            raw_function_name = tool_call.function.name
            error_content = None
            tool_response_content = ""
            search_result = None
            function_args = {}

            # ✅ Gemini の "default_api.search" → "search" に正規化
            function_name = raw_function_name.split('.')[-1] if '.' in raw_function_name else raw_function_name

            try:
                function_args = json.loads(tool_call.function.arguments)
                logger.info(f"🔧 [TOOL] Executing {raw_function_name} (normalized: {function_name})")
                logger.debug(f"🔧 [TOOL] Arguments: {json.dumps(function_args, ensure_ascii=False, indent=2)}")

                if self.search_agent and function_name == self.search_agent.name:
                    search_result = await self.search_agent.run(arguments=function_args, bot=self.bot,
                                                                channel_id=channel_id)
                    # search_resultはresponseオブジェクトまたは文字列
                    if hasattr(search_result, 'text'):
                        # レスポンスオブジェクトからテキストを取得
                        tool_response_content = search_result.text
                    else:
                        # 文字列の場合（フォールバック）
                        tool_response_content = str(search_result)
                    logger.debug(
                        f"🔧 [TOOL] Result (length: {len(str(tool_response_content))} chars):\n{str(tool_response_content)[:1000]}")
                elif self.bio_manager and function_name == self.bio_manager.name:
                    tool_response_content = await self.bio_manager.run_tool(arguments=function_args, user_id=user_id)
                    logger.debug(f"🔧 [TOOL] Result:\n{tool_response_content}")
                elif self.memory_manager and function_name == self.memory_manager.name:
                    tool_response_content = await self.memory_manager.run_tool(arguments=function_args)
                    logger.debug(f"🔧 [TOOL] Result:\n{tool_response_content}")
                elif self.image_generator and function_name == self.image_generator.name:
                    tool_response_content = await self.image_generator.run(arguments=function_args,
                                                                           channel_id=channel_id)
                    logger.debug(f"🔧 [TOOL] Result:\n{tool_response_content}")
                elif self.command_agent and function_name == self.command_agent.name:
                    logger.info(f"🔧 [TOOL] CommandAgent called with arguments: {function_args}")
                    tool_response_content = await self.command_agent.run(arguments=function_args,
                                                                          bot=self.bot,
                                                                          channel_id=channel_id,
                                                                          user_id=user_id)
                    logger.info(f"🔧 [TOOL] CommandAgent result: {tool_response_content[:200] if tool_response_content else 'None'}...")
                    logger.debug(f"🔧 [TOOL] Full result:\n{tool_response_content}")
                elif self.deep_research_agent and function_name == self.deep_research_agent.name:
                    tool_response_content = await self.deep_research_agent.run_tool(arguments=function_args,
                                                                                   channel_id=channel_id)
                    logger.debug(f"🔧 [TOOL] DeepResearch result (len={len(tool_response_content)}):\n{tool_response_content[:500]}")
                else:
                    logger.warning(f"⚠️ Unsupported tool called: {raw_function_name} (normalized: {function_name})")
                    error_content = f"Error: Tool '{function_name}' is not available."
            except json.JSONDecodeError as e:
                logger.error(f"❌ Error decoding tool arguments for {function_name}: {e}", exc_info=True)
                error_content = f"Error: Invalid JSON arguments - {str(e)}"
            except SearchAPIRateLimitError as e:
                logger.warning(f"⚠️ SearchAgent rate limit hit: {e}")
                error_content = "[Google Search Error]\nThe Google Search API rate limit has been reached. Please tell the user to try again later."
            except SearchAPIServerError as e:
                logger.error(f"❌ SearchAgent server error: {e}")
                error_content = "[Google Search Error]\nA temporary server error occurred with the search service. Please tell the user to try again later."
            except SearchAgentError as e:
                logger.error(f"❌ Error during SearchAgent execution for {function_name}: {e}", exc_info=True)
                error_content = f"[Google Search Error]\nAn error occurred during the search execution: {str(e)}"
            except Exception as e:
                logger.error(f"❌ Unexpected error during tool call for {function_name}: {e}", exc_info=True)
                error_content = f"[Tool Error]\nAn unexpected error occurred: {str(e)}"

            final_content = error_content if error_content else tool_response_content
            logger.debug(f"🔧 [TOOL] Sending tool response back to LLM (length: {len(final_content)} chars)")
            messages.append(
                {"tool_call_id": tool_call.id, "role": "tool", "name": function_name, "content": final_content})
            
            # 検索が成功し、レスポンスオブジェクトが存在する場合、ソースをembedで表示
            if search_result and hasattr(search_result, 'candidates'):
                await self._send_search_sources_embed(search_result, channel_id, function_args.get('query', ''))

    async def _send_search_sources_embed(self, response, channel_id: int, query: str) -> None:
        """検索結果のソースをembedで表示"""
        try:
            # チャンネルを取得
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"Channel {channel_id} not found")
                return

            # レスポンスから引用情報を抽出
            sources = []
            try:
                # candidatesからgrounding metadataを取得
                for candidate in response.candidates:
                    if hasattr(candidate, 'grounding_metadata'):
                        grounding = candidate.grounding_metadata
                        if hasattr(grounding, 'grounding_chunks') and grounding.grounding_chunks:
                            for chunk in grounding.grounding_chunks:
                                if hasattr(chunk, 'web'):
                                    web_info = chunk.web
                                    if hasattr(web_info, 'uri'):
                                        sources.append({
                                            'uri': web_info.uri,
                                            'title': getattr(web_info, 'title', ''),
                                        })
            except Exception as e:
                logger.error(f"Error extracting search sources: {e}", exc_info=True)
                return

            # ソースが見つからない場合は何もしない
            if not sources:
                logger.debug("No sources found in search response")
                return

            # Embedを作成して送信
            embed = discord.Embed(
                title="📚 Search Sources / 検索ソース",
                description=f"**Query / クエリ:** {query}",
                color=discord.Color.blue()
            )

            # ソースを最大10個表示
            sources_text = ""
            for i, source in enumerate(sources[:10], 1):
                title = source.get('title', 'No Title') or 'No Title'
                uri = source.get('uri', '')
                if len(title) > 50:
                    title = title[:47] + "..."
                sources_text += f"{i}. [{title}]({uri})\n"

            if sources_text:
                embed.description += f"\n\n**Sources / ソース一覧:**\n{sources_text}"

            # サポートフッターを追加
            self._add_support_footer(embed)

            # メッセージを送信
            await channel.send(embed=embed, silent=True)
            logger.info(f"✅ Search sources embed sent to channel {channel_id}")

        except Exception as e:
            logger.error(f"Error sending search sources embed: {e}", exc_info=True)

    async def _schedule_model_reset(self, channel_id: int):
        try:
            await asyncio.sleep(3 * 60 * 60)
            logger.info(f"Executing scheduled model reset for channel {channel_id}.")
            channel_id_str = str(channel_id)
            if channel_id_str in self.channel_models:
                default_model, current_model = self.llm_config.get('model'), self.channel_models.get(channel_id_str)
                if current_model and current_model != default_model:
                    del self.channel_models[channel_id_str]
                    await self._save_channel_models()
                    logger.info(f"Model for channel {channel_id} automatically reset to default '{default_model}'.")
                    channel = self.bot.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel):
                        try:
                            embed = discord.Embed(title="ℹ️ AI Model Reset / AIモデルをリセットしました",
                                                  description=f"The AI model for this channel has been reset to the default (`{default_model}`) after 3 hours.\n3時間が経過したため、このチャンネルのAIモデルをデフォルト (`{default_model}`) に戻しました。",
                                                  color=discord.Color.blue())
                            self._add_support_footer(embed)
                            await channel.send(embed=embed, view=self._create_support_view())
                        except discord.HTTPException as e:
                            logger.warning(f"Failed to send model reset notification to channel {channel_id}: {e}")
        except asyncio.CancelledError:
            logger.info(f"Model reset task for channel {channel_id} was cancelled.")
        except Exception as e:
            logger.error(f"An error occurred in the model reset task for channel {channel_id}: {e}", exc_info=True)
        finally:
            self.model_reset_tasks.pop(channel_id, None)

    @app_commands.command(name="chat",
                          description="Chat with the AI without needing to mention.\nAIと対話します。メンション不要で会話できます。")
    @app_commands.describe(message="The message you want to send to the AI.\nAIに送信したいメッセージ",
                           image_url="URL of an image (optional).\n画像のURL（オプション）")
    async def chat_slash(self, interaction: discord.Interaction, message: str, image_url: str = None):
        await interaction.response.defer(ephemeral=False)
        temp_message = None
        try:
            llm_client = await self._get_llm_client_for_channel(interaction.channel_id)
            if not llm_client:
                # 修正点：デフォルトのエラーメッセージを一度変数に格納する
                default_error_msg = 'LLM client is not available for this channel.\nこのチャンネルではLLMクライアントが利用できません。'
                error_msg = self.llm_config.get('error_msg', {}).get('general_error', default_error_msg)

                await interaction.followup.send(
                    content=f"❌ **Error / エラー** ❌\n\n{error_msg}",  # 修正点：変数を使ってf-stringを構成する
                    view=self._create_support_view())
                return
            if not message.strip():
                await interaction.followup.send(
                    content="⚠️ **Input Required / 入力が必要です** ⚠️\n\nPlease enter a message.\nメッセージを入力してください。",
                    view=self._create_support_view())
                return
            model_in_use, image_contents = llm_client.model_name_for_api_calls, []
            if image_url:
                if image_data := await self._process_image_url(image_url):
                    image_contents.append(image_data)
                else:
                    await interaction.followup.send(
                        content="⚠️ **Image Error / 画像エラー** ⚠️\n\nFailed to process the specified image URL.\n指定された画像URLの処理に失敗しました。",
                        view=self._create_support_view())
                    return
            guild_log, user_log = f"guild='{interaction.guild.name}({interaction.guild.id})'" if interaction.guild else "guild='DM'", f"user='{interaction.user.name}({interaction.user.id})'"
            logger.info(
                f"📨 Received /chat request | {guild_log} | {user_log} | model='{model_in_use}' | text_length={len(message)} chars | images={len(image_contents)}")
            logger.info(
                f"[/chat] {interaction.guild.name if interaction.guild else 'DM'}({interaction.guild.id if interaction.guild else 0}),{interaction.user.name}({interaction.user.id})💬 [USER_INPUT] {((message[:200] + '...') if len(message) > 203 else message).replace(chr(10), ' ')}")
            if not self.bio_manager or not self.memory_manager:
                await interaction.followup.send(
                    content="❌ **Plugin Error / プラグインエラー** ❌\n\nCannot respond because required plugins are not initialized.\n必要なプラグインが初期化されていないため、応答できません。",
                    view=self._create_support_view())
                return
            system_prompt = await self._prepare_system_prompt(interaction.channel_id, interaction.user.id,
                                                              interaction.user.display_name)
            messages_for_api: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
            user_content_parts = [{"type": "text",
                                   "text": f"{interaction.created_at.astimezone(self.jst).strftime('[%H:%M]')} {message}"}]
            user_content_parts.extend(image_contents)
            if self.language_prompt:
                messages_for_api.append({"role": "system", "content": self.language_prompt})
                logger.info("🌐 [LANG] Using language prompt from config")
            messages_for_api.append({"role": "user", "content": user_content_parts})
            logger.info(f"🔵 [API] Sending {len(messages_for_api)} messages to LLM")
            model_name = llm_client.model_name_for_api_calls
            if self.tips_manager:
                waiting_embed = self.tips_manager.get_waiting_embed(model_name)
                temp_message = await interaction.followup.send(embed=waiting_embed, ephemeral=False, wait=True)
            else:
                waiting_message = f"-# :incoming_envelope: waiting response for '{model_name}' :incoming_envelope:"
                temp_message = await interaction.followup.send(waiting_message, ephemeral=False, wait=True)
            # スレッド作成ボタンは削除（常にFalse）
            sent_messages, full_response_text, used_key_index = await self._process_streaming_and_send_response(
                sent_message=temp_message, channel=interaction.channel, user=interaction.user,
                messages_for_api=messages_for_api, llm_client=llm_client, is_first_response=False)
            if sent_messages and full_response_text:
                logger.info(
                    f"✅ LLM response completed | model='{model_in_use}' | response_length={len(full_response_text)} chars")
                log_response, key_log_str = (full_response_text[:200] + '...') if len(
                    full_response_text) > 203 else full_response_text, f" [key{used_key_index + 1}]" if used_key_index is not None else ""
                logger.info(f"🤖 [LLM_RESPONSE]{key_log_str} {log_response.replace(chr(10), ' ')}")
                logger.debug(
                    f"LLM full response for /chat (length: {len(full_response_text)} chars):\n{full_response_text}")

                # TTS Cogにカスタムイベントを発火させる
                try:
                    self.bot.dispatch("llm_response_complete", sent_messages, full_response_text)
                    logger.info("📢 Dispatched 'llm_response_complete' event for TTS from /chat command.")
                except Exception as e:
                    logger.error(f"Failed to dispatch 'llm_response_complete' event from /chat: {e}", exc_info=True)

            elif not sent_messages:
                logger.warning("LLM response for /chat was empty or an error occurred.")
        except Exception as e:
            logger.error(f"❌ Error during /chat command execution: {e}", exc_info=True)
            error_msg = f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}"
            try:
                if temp_message:
                    await temp_message.edit(content=error_msg, embed=None, view=self._create_support_view())
                else:
                    await interaction.followup.send(content=error_msg, view=self._create_support_view())
            except discord.HTTPException:
                pass

    # --- (以降のコマンドは変更なし) ---
    @app_commands.command(name="set-ai-bio",
                          description="Set the AI's personality/role (bio) for this channel.\nこのチャンネルのAIの性格や役割(bio)を設定します。")
    async def set_ai_bio_slash(self, interaction: discord.Interaction, bio: str):
        await interaction.response.defer(ephemeral=False)
        if not self.bio_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="BioManager is not available.\nBioManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        if len(bio) > 1024:
            embed = discord.Embed(title="⚠️ Input Too Long / 入力が長すぎます",
                                  description="The AI bio is too long. Please set it within 1024 characters.\nAIのbioが長すぎます。1024文字以内で設定してください。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        try:
            await self.bio_manager.set_channel_bio(interaction.channel_id, bio)
            logger.info(f"AI bio for channel {interaction.channel_id} set by {interaction.user.name}")
            embed = discord.Embed(title="✅ AI Bio Set / AIのbioを設定しました",
                                  description=f"The AI's role in this channel has been set as follows.\nこのチャンネルでのAIの役割が以下のように設定されました。\n\n**New AI Bio / 新しいAIのbio:**\n```\n{bio}\n```",
                                  color=discord.Color.green())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
        except Exception as e:
            logger.error(f"Failed to save channel AI bio settings: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save AI bio settings.\nAIのbio設定の保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="show-ai-bio",
                          description="Show the AI's current bio for this channel.\nこのチャンネルのAIに現在設定されているbioを表示します。")
    async def show_ai_bio_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self.bio_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="BioManager is not available.\nBioManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        current_bio = self.bio_manager.get_channel_bio(interaction.channel_id)
        if current_bio:
            title, description, color = "Current AI Bio / 現在のAIのbio", f"In this channel, the AI has the following role set.\nこのチャンネルでは、AIに以下の役割が設定されています。\n\n**AI Bio / AIのbio:**\n```\n{current_bio}\n```", discord.Color.blue()
        else:
            default_prompt = self.llm_config.get('system_prompt', "Not set. / 設定されていません。")
            try:
                formatted_prompt = default_prompt.format(current_date=datetime.now(self.jst).strftime('%Y年%m月%d日'),
                                                         current_time=datetime.now(self.jst).strftime('%H:%M'))
            except (KeyError, ValueError):
                formatted_prompt = default_prompt
            title, description, color = "Current AI Bio / 現在のAIのbio", f"No specific AI bio is set for this channel. The server's default setting is used.\nこのチャンネルには専用のAI bioが設定されていません。サーバーのデフォルト設定が使用されます。\n\n**Default Setting / デフォルト設定:**\n```\n{formatted_prompt}\n```", discord.Color.greyple()
        embed = discord.Embed(title=title, description=description, color=color)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="reset-ai-bio",
                          description="Reset the AI's bio to default for this channel.\nこのチャンネルのAIのbioをデフォルト設定に戻します。")
    async def reset_ai_bio_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self.bio_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="BioManager is not available.\nBioManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        try:
            if await self.bio_manager.reset_channel_bio(interaction.channel_id):
                logger.info(f"AI bio for channel {interaction.channel_id} reset by {interaction.user.name}")
                default_prompt = self.llm_config.get('system_prompt', 'Not set / 未設定')
                try:
                    formatted_prompt = default_prompt.format(
                        current_date=datetime.now(self.jst).strftime('%Y年%m月%d日'),
                        current_time=datetime.now(self.jst).strftime('%H:%M'))
                except (KeyError, ValueError):
                    formatted_prompt = default_prompt
                display_prompt = (formatted_prompt[:100] + '...') if len(formatted_prompt) > 103 else formatted_prompt
                embed = discord.Embed(title="✅ AI Bio Reset / AIのbioをリセットしました",
                                      description=f"The AI bio for this channel has been reset to the default.\nこのチャンネルのAIのbioをデフォルト設定に戻しました。\n> Current Default / 現在のデフォルト: `{display_prompt}`",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            else:
                embed = discord.Embed(title="ℹ️ No Custom AI Bio / 専用のAI bioはありません",
                                      description="No custom AI bio is set for this channel.\nこのチャンネルには専用のAI bioが設定されていません。",
                                      color=discord.Color.blue())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
        except Exception as e:
            logger.error(f"Failed to save channel AI bio settings after reset: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save AI bio settings.\nAIのbio設定の保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="set-user-bio",
                          description="Save your information for the AI to remember.\nAIにあなたの情報を記憶させます。")
    @app_commands.describe(
        bio="Information about you for the AI to remember (e.g., My name is Tanaka. My hobby is reading.).\nAIに覚えてほしいあなたの情報を記述してください。(例: 私の名前は田中です。趣味は読書です。)",
        mode="Select save mode. 'Overwrite' or 'Append' is available.\n保存モードを選択してください。'上書き'または'追記'が可能です。")
    @app_commands.choices(mode=[app_commands.Choice(name="Overwrite / 上書き", value="overwrite"),
                                app_commands.Choice(name="Append / 追記", value="append"), ])
    async def set_user_bio_slash(self, interaction: discord.Interaction, bio: str, mode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=False)
        if not self.bio_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="BioManager is not available.\nBioManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        if len(bio) > 1024:
            embed = discord.Embed(title="⚠️ Input Too Long / 入力が長すぎます",
                                  description="User bio is too long. Please set it within 1024 characters.\nユーザー情報(bio)が長すぎます。1024文字以内で設定してください。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        try:
            await self.bio_manager.set_user_bio(interaction.user.id, bio, mode=mode.value)
            logger.info(
                f"User bio for {interaction.user.name} ({interaction.user.id}) was set with mode '{mode.value}'.")
            updated_bio = self.bio_manager.get_user_bio(interaction.user.id)
            embed = discord.Embed(
                title=f"✅ Your information has been saved ({mode.name}).\n✅ あなたの情報を記憶しました ({mode.name})",
                description=f"The AI has stored your information as follows.\nAIはあなたの情報を以下のように記憶しました。\n\n**Your Bio / あなたのbio:**\n```\n{updated_bio}\n```",
                color=discord.Color.green())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
        except Exception as e:
            logger.error(f"Failed to save user bio settings: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save your information.\nあなたの情報の保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="show-user-bio",
                          description="Show the information the AI has stored about you.\nAIが記憶しているあなたの情報を表示します。")
    async def show_user_bio_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self.bio_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="BioManager is not available.\nBioManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        current_bio = self.bio_manager.get_user_bio(interaction.user.id)
        if current_bio:
            embed = discord.Embed(
                title=f"💡 {interaction.user.display_name}'s Information / {interaction.user.display_name}さんの情報",
                description=f"**Bio:**\n```\n{current_bio}\n```", color=discord.Color.blue())
        else:
            embed = discord.Embed(
                title=f"💡 {interaction.user.display_name}'s Information / {interaction.user.display_name}さんの情報",
                description="Currently, no information is stored about you.\nYou can set it using the `/set-user-bio` command or by asking the AI to remember it in conversation.\n現在、あなたに関する情報は何も記憶されていません。\n`/set-user-bio` コマンドか、会話の中でAIに記憶を頼むことで設定できます。",
                color=discord.Color.greyple())
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="reset-user-bio",
                          description="Delete all information the AI has stored about you.\nAIが記憶しているあなたの情報をすべて削除します。")
    async def reset_user_bio_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self.bio_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="BioManager is not available.\nBioManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        try:
            if await self.bio_manager.reset_user_bio(interaction.user.id):
                logger.info(f"User bio for {interaction.user.name} ({interaction.user.id}) was reset.")
                embed = discord.Embed(title="✅ Information Deleted / 情報を削除しました",
                                      description=f"All information about {interaction.user.display_name} has been deleted.\n{interaction.user.display_name}さんに関する情報をすべて削除しました。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            else:
                embed = discord.Embed(title="ℹ️ No Information Stored / 情報はありません",
                                      description="No information is stored about you.\nあなたに関する情報は何も記憶されていません。",
                                      color=discord.Color.blue())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
        except Exception as e:
            logger.error(f"Failed to save user bio settings after reset: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Deletion Error / 削除エラー",
                                  description="Failed to delete your information.\nあなたの情報の削除に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="memory-save",
                          description="Save information to the global shared memory.\nグローバル共有メモリに情報を保存します。")
    @app_commands.describe(
        key="The key for the information (e.g., 'Developer Announcement').\n情報のキー（項目名） 例: '開発者からのお知らせ'",
        value="The content of the information (e.g., 'Next maintenance is...').\n情報の内容 例: '次回のメンテナンスは...'")
    async def memory_save_slash(self, interaction: discord.Interaction, key: str, value: str):
        await interaction.response.defer(ephemeral=False)
        if not self.memory_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="MemoryManager is not available.\nMemoryManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        try:
            await self.memory_manager.save_memory(key, value)
            embed = discord.Embed(title="✅ Saved to Global Shared Memory / グローバル共有メモリに保存しました",
                                  color=discord.Color.green())
            embed.add_field(name="Key / キー", value=f"```{key}```", inline=False)
            embed.add_field(name="Value / 値", value=f"```{value}```", inline=False)
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
        except Exception as e:
            logger.error(f"Failed to save global memory via command: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save to global shared memory.\nグローバル共有メモリへの保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="memory-list",
                          description="List all global shared memories.\nグローバル共有メモリの情報を一覧表示します。")
    async def memory_list_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self.memory_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="MemoryManager is not available.\nMemoryManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        memories = self.memory_manager.list_memories()
        if not memories:
            embed = discord.Embed(title="ℹ️ No Memories / メモリに情報はありません",
                                  description="Nothing is saved in the global shared memory.\nグローバル共有メモリには何も保存されていません。",
                                  color=discord.Color.blue())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        embed = discord.Embed(title="🌐 Global Shared Memory / グローバル共有メモリ", color=discord.Color.blue())
        description = ""
        for key, value in memories.items():
            field_text = f"**{key}**: {value}\n"
            if len(description) + len(field_text) > 4000:
                description += "\n... (partially omitted due to display limit / 表示制限のため一部省略)"
                break
            description += field_text
        embed.description = description
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    async def memory_key_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        if not self.memory_manager: return []
        keys = self.memory_manager.list_memories().keys()
        return [app_commands.Choice(name=key, value=key) for key in keys if current.lower() in key.lower()][:25]

    @app_commands.command(name="memory-delete",
                          description="Delete a global shared memory.\nグローバル共有メモリから情報を削除します。")
    @app_commands.describe(key="The key of the memory to delete.\n削除したい情報のキー")
    @app_commands.autocomplete(key=memory_key_autocomplete)
    async def memory_delete_slash(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=False)
        if not self.memory_manager:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="MemoryManager is not available.\nMemoryManagerが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            return
        try:
            if await self.memory_manager.delete_memory(key):
                embed = discord.Embed(title="✅ Memory Deleted / メモリを削除しました",
                                      description=f"Deleted key '{key}' from global shared memory.\nグローバル共有メモリからキー '{key}' を削除しました。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            else:
                embed = discord.Embed(title="⚠️ Key Not Found / キーが見つかりません",
                                      description=f"Key '{key}' does not exist in global shared memory.\nキー '{key}' はグローバル共有メモリに存在しません。",
                                      color=discord.Color.gold())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
        except Exception as e:
            logger.error(f"Failed to delete global memory via command: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Deletion Error / 削除エラー",
                                  description="Failed to delete from global shared memory.\nグローバル共有メモリからの削除に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    async def model_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        available_models = self.llm_config.get('available_models', [])
        return [app_commands.Choice(name=model, value=model) for model in available_models if
                current.lower() in model.lower()][:25]

    @app_commands.command(name="switch-models",
                          description="Switches the AI model used for this channel.\nこのチャンネルで使用するAIモデルを切り替えます。")
    @app_commands.describe(model="Select the model you want to use.\n使用したいモデルを選択してください。")
    @app_commands.autocomplete(model=model_autocomplete)
    async def switch_model_slash(self, interaction: discord.Interaction, model: str):
        await interaction.response.defer(ephemeral=False)
        available_models = self.llm_config.get('available_models', [])
        if model not in available_models:
            embed = discord.Embed(title="⚠️ Invalid Model / 無効なモデル",
                                  description=f"The specified model '{model}' is not available.\n指定されたモデル '{model}' は利用できません。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        channel_id, channel_id_str, default_model = interaction.channel_id, str(
            interaction.channel_id), self.llm_config.get('model')
        if channel_id in self.model_reset_tasks:
            self.model_reset_tasks[channel_id].cancel()
            self.model_reset_tasks.pop(channel_id, None)
            logger.info(f"Cancelled previous model reset task for channel {channel_id}.")
        self.channel_models[channel_id_str] = model
        try:
            await self._save_channel_models()
            await self._get_llm_client_for_channel(interaction.channel_id)
            if model != default_model:
                task = asyncio.create_task(self._schedule_model_reset(channel_id))
                self.model_reset_tasks[channel_id] = task
                embed = discord.Embed(title="✅ Model Switched / モデルを切り替えました",
                                      description=f"The AI model for this channel has been switched to `{model}`.\nIt will automatically revert to the default model (`{default_model}`) **after 3 hours**.\nこのチャンネルのAIモデルが `{model}` に切り替えられました。\n**3時間後**にデフォルトモデル (`{default_model}`) に自動的に戻ります。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(
                    f"Model for channel {channel_id} switched to '{model}' by {interaction.user.name}. Reset scheduled in 3 hours.")
            else:
                embed = discord.Embed(title="✅ Model Reset to Default / モデルをデフォルトに戻しました",
                                      description=f"The AI model for this channel has been reset to the default `{model}`.\nこのチャンネルのAIモデルがデフォルトの `{model}` に戻されました。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(f"Model for channel {channel_id} switched to default '{model}' by {interaction.user.name}.")
        except Exception as e:
            logger.error(f"Failed to save channel model settings: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save settings.\n設定の保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())

    @app_commands.command(name="switch-models-default-server",
                          description="Resets the AI model for this channel to the server default.\nこのチャンネルのAIモデルをサーバーのデフォルト設定に戻します。")
    async def reset_model_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        channel_id, channel_id_str = interaction.channel_id, str(interaction.channel_id)
        if channel_id in self.model_reset_tasks:
            self.model_reset_tasks[channel_id].cancel()
            self.model_reset_tasks.pop(channel_id, None)
            logger.info(f"Cancelled scheduled model reset for channel {channel_id} due to manual reset.")
        if channel_id_str in self.channel_models:
            del self.channel_models[channel_id_str]
            try:
                await self._save_channel_models()
                default_model = self.llm_config.get('model', 'Not set / 未設定')
                embed = discord.Embed(title="✅ Model Reset to Default / モデルをデフォルトに戻しました",
                                      description=f"The AI model for this channel has been reset to the default (`{default_model}`).\nこのチャンネルのAIモデルをデフォルト (`{default_model}`) に戻しました。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(f"Model for channel {interaction.channel_id} reset to default by {interaction.user.name}")
            except Exception as e:
                logger.error(f"Failed to save channel model settings after reset: {e}", exc_info=True)
                embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                      description="Failed to save settings.\n設定の保存に失敗しました。",
                                      color=discord.Color.red())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view())
        else:
            embed = discord.Embed(title="ℹ️ No Custom Model Set / 専用モデルはありません",
                                  description="No custom model is set for this channel.\nこのチャンネルには専用のモデルが設定されていません。",
                                  color=discord.Color.blue())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @switch_model_slash.error
    async def switch_model_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Error in /switch-model command: {error}", exc_info=True)
        error_message = f"An unexpected error occurred: {error}\n予期せぬエラーが発生しました: {error}"
        embed = discord.Embed(title="❌ Unexpected Error / 予期せぬエラー", description=error_message,
                              color=discord.Color.red())
        self._add_support_footer(embed)
        view = self._create_support_view()
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    async def image_model_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        if not self.image_generator: return []
        available_models, current_lower = self.image_generator.get_available_models(), current.lower()
        filtered = [model for model in available_models if current_lower in model.lower()]
        if len(filtered) > 25:
            models_by_provider, choices = self.image_generator.get_models_by_provider(), []
            for provider, models in sorted(models_by_provider.items()):
                if current_lower in provider.lower():
                    for model in models[:5]:
                        if len(choices) >= 25: break
                        choices.append(app_commands.Choice(name=model, value=model))
                    if len(choices) >= 25: break
            return choices[:25]
        return [app_commands.Choice(name=model, value=model) for model in filtered][:25]

    @app_commands.command(name="switch-image-model",
                          description="Switch the image generation model for this channel. / このチャンネルの画像生成モデルを切り替えます。")
    @app_commands.describe(
        model="Select the image generation model you want to use. / 使用したい画像生成モデルを選択してください。")
    @app_commands.autocomplete(model=image_model_autocomplete)
    async def switch_image_model_slash(self, interaction: discord.Interaction, model: str):
        await interaction.response.defer(ephemeral=False)
        if not self.image_generator:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="ImageGenerator is not available.\nImageGeneratorが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        # プロバイダー付き形式（provider/model_name）の場合は実際のモデル名を抽出
        actual_model = model.split('/', 1)[1] if '/' in model else model
        available_models = self.image_generator.get_available_models()
        if actual_model not in available_models:
            embed = discord.Embed(title="⚠️ Invalid Model / 無効なモデル",
                                  description=f"The specified model `{model}` is not available.\n指定されたモデル `{model}` は利用できません。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        try:
            await self.image_generator.set_model_for_channel(interaction.channel_id, actual_model)
            default_model = self.image_generator.default_model
            try:
                provider, model_name = model.split('/', 1)
            except ValueError:
                provider, model_name = "local", model

            if model != default_model:
                embed = discord.Embed(title="✅ Image Model Switched / 画像生成モデルを切り替えました",
                                      description="The image generation model for this channel has been switched.\nこのチャンネルの画像生成モデルを切り替えました。",
                                      color=discord.Color.green())
                embed.add_field(name="New Model / 新しいモデル", value=f"```\n{model}\n```", inline=False)
                embed.add_field(name="Provider / プロバイダー", value=f"`{provider}`", inline=True)
                embed.add_field(name="Model Name / モデル名", value=f"`{model_name}`", inline=True)
                embed.add_field(name="💡 Tip / ヒント",
                                value=f"To reset to default (`{default_model}`), use `/reset-image-model`\nデフォルト (`{default_model}`) に戻すには `/reset-image-model`",
                                inline=False)
            else:
                embed = discord.Embed(title="✅ Image Model Set to Default / 画像生成モデルをデフォルトに設定しました",
                                      description="The image generation model for this channel is now the default.\nこのチャンネルの画像生成モデルがデフォルトになりました。",
                                      color=discord.Color.green())
                embed.add_field(name="Model / モデル", value=f"```\n{model}\n```", inline=False)
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            logger.info(
                f"Image model for channel {interaction.channel_id} switched to '{model}' by {interaction.user.name}")
        except Exception as e:
            logger.error(f"Failed to save channel image model settings: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save settings.\n設定の保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())

    @app_commands.command(name="show-image-model",
                          description="Show the current image generation model for this channel. / このチャンネルの現在の画像生成モデルを表示します。")
    async def show_image_model_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self.image_generator:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="ImageGenerator is not available.\nImageGeneratorが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        current_model, default_model, is_default = self.image_generator.get_model_for_channel(
            interaction.channel_id), self.image_generator.default_model, self.image_generator.get_model_for_channel(
            interaction.channel_id) == self.image_generator.default_model
        try:
            provider, model_name = current_model.split('/', 1)
        except ValueError:
            provider, model_name = "local", current_model

        embed = discord.Embed(title="🎨 Current Image Generation Model / 現在の画像生成モデル",
                              color=discord.Color.blue() if is_default else discord.Color.purple())
        embed.add_field(name="Current Model / 現在のモデル", value=f"```\n{current_model}\n```", inline=False)
        embed.add_field(name="Provider / プロバイダー", value=f"`{provider}`", inline=True)
        embed.add_field(name="Status / 状態", value='`Default / デフォルト`' if is_default else '`Custom / カスタム`',
                        inline=True)
        models_by_provider = self.image_generator.get_models_by_provider()
        for provider_name, models in sorted(models_by_provider.items()):
            model_list = "\n".join([f"• `{m.split('/', 1)[1]}`" for m in models[:5]])
            if len(models) > 5: model_list += f"\n• ... and {len(models) - 5} more"
            embed.add_field(name=f"📦 {provider_name.title()} Models", value=model_list or "None", inline=True)
        embed.add_field(name="💡 Commands / コマンド",
                        value="• `/switch-image-model` - Change model / モデル変更\n• `/reset-image-model` - Reset to default / デフォルトに戻す",
                        inline=False)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="list-image-models",
                          description="List all available image generation models. / 利用可能な画像生成モデルの一覧を表示します。")
    @app_commands.describe(provider="Filter by provider (optional). / プロバイダーで絞り込み（オプション）")
    async def list_image_models_slash(self, interaction: discord.Interaction, provider: str = None):
        await interaction.response.defer(ephemeral=False)
        if not self.image_generator:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="ImageGenerator is not available.\nImageGeneratorが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        models_by_provider = self.image_generator.get_models_by_provider()
        if provider:
            provider_lower = provider.lower()
            models_by_provider = {k: v for k, v in models_by_provider.items() if provider_lower in k.lower()}
            if not models_by_provider:
                embed = discord.Embed(title="⚠️ No Models Found / モデルが見つかりません",
                                      description=f"No models found for provider: `{provider}`\nプロバイダー `{provider}` のモデルが見つかりません。",
                                      color=discord.Color.gold())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view())
                return
        total_models = sum(len(models) for models in models_by_provider.values())
        embed = discord.Embed(title="🎨 Available Image Generation Models / 利用可能な画像生成モデル",
                              description=f"Total: {total_models} models across {len(models_by_provider)} provider(s)\n合計: {len(models_by_provider)}プロバイダー、{total_models}モデル",
                              color=discord.Color.blue())
        for provider_name, models in sorted(models_by_provider.items()):
            # モデル名からプロバイダー部分を除去（表示用）
            model_names = [m.split('/', 1)[1] if '/' in m else m for m in models]
            if len(model_names) > 10:
                model_text = "\n".join([f"{i + 1}. `{m}`" for i, m in enumerate(model_names[:10])])
                model_text += f"\n... and {len(model_names) - 10} more"
            else:
                model_text = "\n".join([f"{i + 1}. `{m}`" for i, m in enumerate(model_names)])
            embed.add_field(name=f"📦 {provider_name.title()} ({len(models)} models)", value=model_text or "None",
                            inline=False)
        embed.add_field(name="💡 How to Use / 使い方",
                        value="Use `/switch-image-model` to change the model for this channel.\n`/switch-image-model` でこのチャンネルのモデルを変更できます。",
                        inline=False)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @switch_image_model_slash.error
    async def switch_image_model_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Error in /switch-image-model command: {error}", exc_info=True)
        error_message = f"An unexpected error occurred: {error}\n予期せぬエラーが発生しました: {error}"
        embed = discord.Embed(title="❌ Unexpected Error / 予期せぬエラー", description=error_message,
                              color=discord.Color.red())
        self._add_support_footer(embed)
        view = self._create_support_view()
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    @app_commands.command(name="llm_help",
                          description="Displays help and usage guidelines for LLM (AI Chat) features.\nLLM (AI対話) 機能のヘルプと利用ガイドラインを表示します。")
    async def llm_help_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        bot_user = self.bot.user or interaction.client.user
        bot_name = bot_user.name if bot_user else "This Bot / 当Bot"
        embed = discord.Embed(title=f"💡 {bot_name} AI Chat Help & Guidelines / AI対話機能ヘルプ＆ガイドライン",
                              description=f"Explanation and terms of use for the AI chat features.\n{bot_name}のAI対話機能についての説明と利用規約です。",
                              color=discord.Color.purple())
        if bot_user and bot_user.avatar: embed.set_thumbnail(url=bot_user.avatar.url)
        embed.add_field(name="Basic Usage / 基本的な使い方",
                        value=f"• Mention the bot (`@{bot_name}`) to get a response from the AI.\n  Botにメンション (`@{bot_name}`) して話しかけると、AIが応答します。\n• **You can also continue the conversation by replying to the bot's messages (no mention needed).**\n  **Botのメッセージに返信することでも会話を続けられます（メンション不要）。**\n• If you ask the AI to remember something, it will try to store that information.\n  「私の名前は〇〇です。覚えておいて」のように話しかけると、AIがあなたの情報を記憶しようとします。\n• Attach images or paste image URLs with your message, and the AI will try to understand them.\n  画像と一緒に話しかけると、AIが画像の内容も理解しようとします。",
                        inline=False)

        # Split "Useful Commands" into multiple fields to avoid character limits
        embed.add_field(name="Commands - AI/Channel Settings / コマンド - AI/チャンネル設定",
                        value="• `/switch-models`: Change the AI model used in this channel. / このチャンネルで使うAIモデルを変更します。\n"
                              "• `/set-ai-bio`: Set a custom personality/role for the AI in this channel. / このチャンネル専用のAIの性格や役割を設定します。\n"
                              "• `/show-ai-bio`: Check the current AI bio setting. / 現在のAIのbio設定を確認します。\n"
                              "• `/reset-ai-bio`: Reset the AI bio to the default. / AIのbio設定をデフォルトに戻します。",
                        inline=False)

        embed.add_field(name="Commands - Image Generation / コマンド - 画像生成",
                        value="• `/switch-image-model`: Switch the image generation model for this channel. / このチャンネルの画像生成モデルを切り替えます。\n"
                              "• `/reset-image-model`: Reset the image generation model to default. / 画像生成モデルをデフォルトに戻します。\n"
                              "• `/show-image-model`: Show the current image generation model. / 現在の画像生成モデルを表示します。\n"
                              "• `/list-image-models`: List all available image generation models. / 利用可能な全画像生成モデルを一覧表示します。",
                        inline=False)

        embed.add_field(name="Commands - User Info / コマンド - ユーザー情報",
                        value="• `/set-user-bio`: Set information about you for the AI to remember. / AIに覚えてほしいあなたの情報を設定します。\n"
                              "• `/show-user-bio`: Check the information the AI has stored about you. / AIが記憶しているあなたの情報を確認します。\n"
                              "• `/reset-user-bio`: Delete your information from the AI's memory. / あなたの情報をAIの記憶から削除します。",
                        inline=False)

        embed.add_field(name="Commands - Global Memory / コマンド - グローバルメモリ",
                        value="• `/memory-save`: Save information to the global shared memory. / 全サーバー共通のメモリに情報を保存します。\n"
                              "• `/memory-list`: List all information in the global memory. / グローバルメモリの情報を一覧表示します。\n"
                              "• `/memory-delete`: Delete information from the global memory. / グローバルメモリから情報を削除します。",
                        inline=False)

        embed.add_field(name="Commands - Other / コマンド - その他",
                        value="• `/chat`: Chat with the AI without needing to mention. / AIとメンションなしで対話します。\n"
                              "• `/clear_history`: Reset the conversation history. / 会話履歴をリセットします。",
                        inline=False)
                        
        channel_model_str = self.channel_models.get(str(interaction.channel_id))
        model_display = f"`{channel_model_str}` (Channel-specific / このチャンネル専用)" if channel_model_str else f"`{self.llm_config.get('model', 'Not set / 未設定')}` (Default / デフォルト)"
        ai_bio_display, user_bio_display = "N/A", "N/A"
        if self.bio_manager:
            ai_bio_display = "✅ (Custom / 専用設定あり)" if self.bio_manager.get_channel_bio(
                interaction.channel_id) else "Default / デフォルト"
            user_bio_display = "✅ (Stored / 記憶あり)" if self.bio_manager.get_user_bio(
                interaction.user.id) else "None / なし"
        active_tools = self.llm_config.get('active_tools', [])
        tools_info = "• None / なし" if not active_tools else "• " + ", ".join(active_tools)
        embed.add_field(name="Current AI Settings / 現在のAI設定",
                        value=f"• **Model in Use / 使用モデル:** {model_display}\n• **AI Role (Channel) / AIの役割(チャンネル):** {ai_bio_display} (see `/show-ai-bio`)\n• **Your Info / あなたの情報:** {user_bio_display} (see `/show-user-bio`)\n• **Max Conversation History / 会話履歴の最大保持数:** {self.llm_config.get('max_messages', 'Not set / 未設定')} pairs\n• **Max Images at Once / 一度に処理できる最大画像枚数:** {self.llm_config.get('max_images', 'Not set / 未設定')} image(s)\n• **Available Tools / 利用可能なツール:** {tools_info}",
                        inline=False)
        embed.add_field(name="--- 📜 AI Usage Guidelines / AI利用ガイドライン ---",
                        value="Please review the following to ensure safe use of the AI features.\nAI機能を安全にご利用いただくため、以下の内容を必ずご確認ください。",
                        inline=False)
        embed.add_field(name="⚠️ 1. Data Input Precautions / データ入力時の注意",
                        value="**NEVER include personal or confidential information** such as your name, contact details, or passwords.\nAIに記憶させる情報には、氏名、連絡先、パスワードなどの**個人情報や秘密情報を絶対に含めないでください。**",
                        inline=False)
        embed.add_field(name="✅ 2. Precautions for Using Generated Output / 生成物利用時の注意",
                        value="The AI's responses may contain inaccuracies or biases. **Always fact-check and use them at your own risk.**\nAIの応答には虚偽や偏見が含まれる可能性があります。**必ずファクトチェックを行い、自己の責任で利用してください。**",
                        inline=False)
        embed.set_footer(
            text="These guidelines are subject to change without notice.\nガイドラインは予告なく変更される場合があります。")
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="clear_history",
                          description="Clears the history of the current conversation thread.\n現在の会話スレッドの履歴をクリアします。")
    async def clear_history_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        guild_id = interaction.guild.id if interaction.guild else 0  # DMの場合は0
        cleared_count, threads_to_clear = 0, set()
        
        try:
            async for msg in interaction.channel.history(limit=200):
                if guild_id in self.message_to_thread and msg.id in self.message_to_thread[guild_id]: 
                    threads_to_clear.add(self.message_to_thread[guild_id][msg.id])
        except (discord.Forbidden, discord.HTTPException):
            embed = discord.Embed(title="⚠️ Permission Error / 権限エラー",
                                  description="Could not read the channel's message history.\nチャンネルのメッセージ履歴を読み取れませんでした。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        
        for thread_id in threads_to_clear:
            if guild_id in self.conversation_threads and thread_id in self.conversation_threads[guild_id]:
                del self.conversation_threads[guild_id][thread_id]
                if guild_id in self.message_to_thread:
                    self.message_to_thread[guild_id] = {
                        k: v for k, v in self.message_to_thread[guild_id].items() 
                        if v != thread_id
                    }
                cleared_count += 1
        
        if cleared_count > 0:
            embed = discord.Embed(title="✅ History Cleared / 履歴をクリアしました",
                                  description=f"Cleared the history of {cleared_count} conversation thread(s) related to this channel.\nこのチャンネルに関連する {cleared_count} 個の会話スレッドの履歴をクリアしました。",
                                  color=discord.Color.green())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
        else:
            embed = discord.Embed(title="ℹ️ No History Found / 履歴がありません",
                                  description="No conversation history to clear was found.\nクリア対象の会話履歴が見つかりませんでした。",
                                  color=discord.Color.blue())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())


async def setup(bot: commands.Bot):
    """Sets up the LLMCog."""
    try:
        await bot.add_cog(LLMCog(bot))
        logger.info("LLMCog loaded successfully.")
    except Exception as e:
        logger.critical(f"Failed to set up LLMCog: {e}", exc_info=True)
        raise