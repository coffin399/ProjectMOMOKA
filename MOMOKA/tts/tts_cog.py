# PLANA/tts/tts_cog.py
import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import io
import asyncio
import json
from pathlib import Path
import re
from typing import Dict, Optional, List, Any
import time
import logging
import gc  # ガベージコレクションを追加

try:
    from MOMOKA.music.music_cog import MusicCog
    from MOMOKA.music.plugins.audio_mixer import TTSAudioSource, MusicAudioSource
except ImportError:
    MusicCog = None
    TTSAudioSource = None
    MusicAudioSource = None

try:
    from MOMOKA.tts.error.errors import TTSCogExceptionHandler
except ImportError as e:
    logging.getLogger("MOMOKA.tts").critical(
        "TTSCog: 必須コンポーネントのインポートに失敗しました。エラー: %s", e
    )
    TTSCogExceptionHandler = None


class TTSCog(commands.Cog, name="tts_cog"):
    """
    TTS Cog - Style-Bert-VITS2対応
    
    Note: KoboldCPPはLLM推論サーバーであり、TTSとは直接関係ありません。
    このCogはStyle-Bert-VITS2を使用してテキストを音声に変換します。
    KoboldCPPを使用している場合でも、LLM Cogが応答を生成すると、
    このCogが自動的にその応答を読み上げます（設定されている場合）。
    """
    def __init__(self, bot: commands.Bot):
        if TTSCogExceptionHandler is None:
            raise commands.ExtensionFailed(self.qualified_name,
                                           "必須コンポーネントTTSCogExceptionHandlerのインポート失敗")
        if TTSAudioSource is None:
            raise commands.ExtensionFailed(self.qualified_name,
                                           "必須コンポーネントTTSAudioSourceのインポート失敗")
        self.bot = bot
        self.config = bot.config.get('tts', {})

        # Internal TTS synthesizer configuration
        from MOMOKA.generator.tts import StyleBertVITS2Synthesizer, SynthesizerConfig
        tts_cfg = SynthesizerConfig(
            model_root=self.config.get('model_root', 'models/tts-models'),
            model_name=self.config.get('default_model_dir'),
            dictionary_dir=self.config.get('pyopenjtalk_dict_dir'),
            sample_rate=int(self.config.get('sample_rate', 48000)),  # Discord standard: 48kHz
            noise_scale=float(self.config.get('noise_scale', 0.667)),
            noise_w=float(self.config.get('noise_w', 0.8)),
            length_scale=float(self.config.get('length_scale', 1.0)),
        )
        self.synthesizer = StyleBertVITS2Synthesizer(tts_cfg)
        self.api_url = self.config.get('api_server_url')  # optional legacy
        self.api_key = self.config.get('api_key')

        self.default_model_id = self.config.get('default_model_id', 0)
        self.default_style = self.config.get('default_style', 'Neutral')
        self.default_style_weight = self.config.get('default_style_weight', 5.0)
        self.default_speed = self.config.get('default_speed', 1.0)
        self.default_volume = self.config.get('default_volume', 1.0)

        headers = {}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key

        self.session = aiohttp.ClientSession(headers=headers)
        self.exception_handler = TTSCogExceptionHandler()

        self.tts_locks: Dict[int, asyncio.Lock] = {}

        self.available_models: List[Dict] = []
        self.models_loaded: bool = False

        self.settings_file = Path("data/tts_settings.json")
        self.channel_settings: Dict[int, Dict] = {}
        self._load_settings()

        self.speech_settings_file = Path("data/speech_settings.json")
        self.speech_settings: Dict[str, Dict[str, Any]] = {}
        self._load_speech_settings()

        self.dictionary_file = Path("data/speech_dictionary.json")
        self.speech_dictionary: Dict[str, str] = {}
        self._load_dictionary()

        self.llm_bot_ids = [1031673203774464160, 1311866016011124736]

        logging.getLogger(__name__).info(
            "TTSCog loaded (Internal Style-Bert-VITS2 wrapper, AudioMixer enabled)"
        )

    async def cog_load(self):
        logging.getLogger(__name__).info("TTSCog loaded. Preparing internal synthesizer...")
        await self.fetch_available_models()  # still useful for UI and IDs

    async def cog_unload(self):
        """Cogのアンロード時にリソースをクリーンアップ"""
        self._save_settings()
        self._save_speech_settings()
        self._save_dictionary()
        
        # aiohttpセッションのクローズ
        if self.session and not self.session.closed:
            await self.session.close()
        
        # ロック辞書のクリア
        self.tts_locks.clear()
        
        # ガベージコレクションを強制実行
        gc.collect()
        
        logging.getLogger(__name__).info("TTSCog unloaded and session closed.")

    def _load_settings(self):
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.channel_settings = {int(k): v for k, v in data.items()}
                logging.getLogger(__name__).info(
                    "[TTSCog] モデル設定を読み込みました: %dチャンネル", len(self.channel_settings)
                )
            else:
                self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] モデル設定読み込みエラー: %s", e)
            self.channel_settings = {}

    def _save_settings(self):
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                data = {str(k): v for k, v in self.channel_settings.items()}
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] モデル設定保存エラー: %s", e)

    def _load_speech_settings(self):
        try:
            if self.speech_settings_file.exists():
                with open(self.speech_settings_file, 'r', encoding='utf-8') as f:
                    self.speech_settings = json.load(f)
                logging.getLogger(__name__).info(
                    "[TTSCog] 読み上げ設定を読み込みました: %dギルド", len(self.speech_settings)
                )
            else:
                self.speech_settings_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 読み上げ設定読み込みエラー: %s", e)
            self.speech_settings = {}

    def _save_speech_settings(self):
        try:
            self.speech_settings_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.speech_settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.speech_settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 読み上げ設定保存エラー: %s", e)

    def _get_guild_speech_settings(self, guild_id: int) -> Dict[str, Any]:
        guild_id_str = str(guild_id)
        if guild_id_str not in self.speech_settings:
            self.speech_settings[guild_id_str] = {
                "speech_channel_id": None,
                "auto_join_users": [],
                "enable_notifications": True,
                "volume": self.default_volume
            }
        self.speech_settings[guild_id_str].setdefault("enable_notifications", True)
        self.speech_settings[guild_id_str].setdefault("volume", self.default_volume)
        return self.speech_settings[guild_id_str]

    def _get_channel_settings(self, channel_id: int) -> Dict:
        if channel_id not in self.channel_settings:
            return {
                "model_id": self.default_model_id,
                "style": self.default_style,
                "style_weight": self.default_style_weight,
                "speed": self.default_speed
            }
        return self.channel_settings[channel_id]

    def _set_channel_settings(self, channel_id: int, settings: Dict):
        self.channel_settings[channel_id] = settings
        self._save_settings()

    async def fetch_available_models(self) -> bool:
        try:
            async with self.session.get(f"{self.api_url}/models/info") as response:
                if response.status == 200:
                    data = await response.json()
                    self.available_models = data.get('models', []) if isinstance(data, dict) else data
                    self.models_loaded = True
                    print(f"✓ [TTSCog] {len(self.available_models)}個のモデルを検出")
                    return True
                return False
        except aiohttp.ClientConnectorError:
            return False
        except Exception:
            return False

    def get_model_name(self, model_id: int) -> str:
        for model in self.available_models:
            if isinstance(model, dict) and model.get('id') == model_id:
                return model.get('name', f"Model {model_id}")
        return f"Model {model_id}"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or message.embeds:
            return

        guild_settings = self._get_guild_speech_settings(message.guild.id)
        if message.channel.id != guild_settings.get("speech_channel_id"):
            return

        voice_client = message.guild.voice_client
        if not voice_client or not voice_client.is_connected() or not message.clean_content:
            return

        lock = self._get_tts_lock(message.guild.id)
        if lock.locked():
            return

        async with lock:
            channel_settings = self._get_channel_settings(voice_client.channel.id)
            await self._handle_say_logic(
                message.guild, message.clean_content,
                channel_settings["model_id"], channel_settings["style"],
                channel_settings["style_weight"], channel_settings["speed"],
                guild_settings.get("volume", self.default_volume)
            )

    @commands.Cog.listener()
    async def on_llm_response_complete(self, response_messages: list, text_to_speak: str):
        if not response_messages or not (guild := response_messages[0].guild):
            return

        guild_settings = self._get_guild_speech_settings(guild.id)
        if response_messages[0].channel.id != guild_settings.get("speech_channel_id"):
            return

        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected() or not text_to_speak:
            return

        lock = self._get_tts_lock(guild.id)
        if lock.locked():
            return

        async with lock:
            channel_settings = self._get_channel_settings(voice_client.channel.id)
            await self._handle_say_logic(
                guild, text_to_speak,
                channel_settings["model_id"], channel_settings["style"],
                channel_settings["style_weight"], channel_settings["speed"],
                guild_settings.get("volume", self.default_volume)
            )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id == self.bot.user.id:
            return

        guild = member.guild
        guild_settings = self._get_guild_speech_settings(guild.id)
        voice_client = guild.voice_client

        if member.id in guild_settings.get("auto_join_users", []) and not before.channel and after.channel:
            if not voice_client or not voice_client.is_connected():
                try:
                    await after.channel.connect()
                except Exception as e:
                    logging.getLogger(__name__).error("[TTSCog] 自動参加エラー: %s", e)

        if not voice_client:
            return

        if before.channel == voice_client.channel and not any(m for m in voice_client.channel.members if not m.bot):
            await voice_client.disconnect()
            guild_settings["speech_channel_id"] = None
            self._save_speech_settings()
            return

        if not guild_settings.get("enable_notifications", True):
            return

        text_to_say = None
        if before.channel != voice_client.channel and after.channel == voice_client.channel:
            template = self.config.get('join_message_template', "{member_name}さんが参加しました。")
            text_to_say = template.format(member_name=member.display_name)
        elif before.channel == voice_client.channel and after.channel != voice_client.channel:
            template = self.config.get('leave_message_template', "{member_name}さんが退出しました。")
            text_to_say = template.format(member_name=member.display_name)

        if text_to_say:
            await self.trigger_tts_from_event(guild, text_to_say)

    async def trigger_tts_from_event(self, guild: discord.Guild, text: str):
        lock = self._get_tts_lock(guild.id)
        guild_settings = self._get_guild_speech_settings(guild.id)
        async with lock:
            await self._handle_say_logic(
                guild, text, self.default_model_id, self.default_style,
                self.default_style_weight, self.default_speed,
                guild_settings.get("volume", self.default_volume)
            )

    tts_group = app_commands.Group(name="tts", description="TTS関連のコマンド")

    @tts_group.command(name="volume", description="TTSの音量を設定します (0-200%)")
    @app_commands.describe(volume="音量 (0から200の整数)")
    async def tts_volume(self, interaction: discord.Interaction, volume: app_commands.Range[int, 0, 200]):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        float_volume = volume / 100.0
        guild_settings['volume'] = float_volume
        self._save_speech_settings()

        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            music_cog: Optional[MusicCog] = self.bot.get_cog("music_cog")
            music_state = music_cog._get_guild_state(interaction.guild.id) if music_cog else None
            if music_state and music_state.mixer:
                tts_sources = [name for name in music_state.mixer.sources.keys() if name.startswith("tts_")]
                for name in tts_sources:
                    await music_state.mixer.set_volume(name, float_volume)
            elif isinstance(voice_client.source, discord.PCMVolumeTransformer):
                voice_client.source.volume = float_volume

        await interaction.response.send_message(f"🔊 TTSの音量を **{volume}%** に設定しました。")

    speech_group = app_commands.Group(name="speech", description="テキストチャンネルの読み上げに関するコマンド")

    @speech_group.command(name="enable", description="このチャンネルのメッセージ読み上げを有効にします")
    async def enable_speech(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("❌ ボイスチャンネルに接続してから実行してください。", ephemeral=True)
        
        vc = interaction.user.voice.channel
        try:
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.move_to(vc)
            else:
                await vc.connect()
        except Exception as e:
            return await interaction.response.send_message(f"❌ 接続失敗: `{e}`", ephemeral=True)

        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        guild_settings["speech_channel_id"] = interaction.channel.id
        self._save_speech_settings()
        embed = discord.Embed(title="🔊 VC読み上げ開始", description=f"対象: {interaction.channel.mention}, {vc.mention}", color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @speech_group.command(name="disable", description="メッセージ読み上げを無効にします")
    async def disable_speech(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        if guild_settings.get("speech_channel_id") is None:
            return await interaction.response.send_message("ℹ️ 読み上げは無効です。", ephemeral=True)
        
        guild_settings["speech_channel_id"] = None
        self._save_speech_settings()
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("✅ 読み上げを無効にしました。")

    @speech_group.command(name="skip", description="現在の読み上げをスキップします")
    async def skip_speech(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client:
            return await interaction.response.send_message("❌ BotがVCにいません。", ephemeral=True)

        skipped = False
        music_cog: Optional[MusicCog] = self.bot.get_cog("music_cog")
        music_state = music_cog._get_guild_state(interaction.guild.id) if music_cog else None
        if music_state and music_state.mixer:
            tts_sources = [name for name in music_state.mixer.sources.keys() if name.startswith("tts_")]
            if tts_sources:
                for name in tts_sources: await music_state.mixer.remove_source(name)
                skipped = True

        if not skipped and voice_client.is_playing() and isinstance(voice_client.source, (TTSAudioSource, discord.PCMVolumeTransformer)):
            voice_client.stop()
            skipped = True

        await interaction.response.send_message("✅ スキップしました。" if skipped else "❌ スキップ対象がありません。", ephemeral=not skipped)

    autojoin_group = app_commands.Group(name="autojoin", description="VCへの自動参加に関するコマンド")

    @autojoin_group.command(name="enable", description="あなたがVCに参加した際、BOTも自動で参加するようにします")
    async def enable_auto_join(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        auto_join_users = guild_settings.setdefault("auto_join_users", [])
        if interaction.user.id in auto_join_users:
            return await interaction.response.send_message("ℹ️ 自動参加は既に有効です。", ephemeral=True)
        auto_join_users.append(interaction.user.id)
        self._save_speech_settings()
        await interaction.response.send_message("✅ 自動参加を有効にしました。")

    @autojoin_group.command(name="disable", description="BOTの自動参加設定を解除します")
    async def disable_auto_join(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        auto_join_users = guild_settings.get("auto_join_users", [])
        if interaction.user.id not in auto_join_users:
            return await interaction.response.send_message("ℹ️ 自動参加は設定されていません。", ephemeral=True)
        auto_join_users.remove(interaction.user.id)
        self._save_speech_settings()
        await interaction.response.send_message("✅ 自動参加を解除しました。")

    notification_group = app_commands.Group(name="join-leave-notification", description="VCへの入退室通知に関するコマンド")

    @notification_group.command(name="enable", description="VCへの入退室を音声で通知するようにします")
    async def enable_notification(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        if guild_settings.get("enable_notifications", True):
            return await interaction.response.send_message("ℹ️ 通知は既に有効です。", ephemeral=True)
        guild_settings["enable_notifications"] = True
        self._save_speech_settings()
        await interaction.response.send_message("✅ 入退室通知を有効にしました。")

    @notification_group.command(name="disable", description="VCへの入退室通知を無効にします")
    async def disable_notification(self, interaction: discord.Interaction):
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)
        if not guild_settings.get("enable_notifications", True):
            return await interaction.response.send_message("ℹ️ 通知は既に無効です。", ephemeral=True)
        guild_settings["enable_notifications"] = False
        self._save_speech_settings()
        await interaction.response.send_message("✅ 入退室通知を無効にしました。")

    @app_commands.command(name="say", description="テキストを音声で読み上げます")
    @app_commands.describe(text="読み上げるテキスト", model_id="モデルID", style="スタイル名", style_weight="スタイルの強さ", speed="話速")
    async def say(self, interaction: discord.Interaction, text: str, model_id: Optional[int] = None, style: Optional[str] = None, style_weight: Optional[float] = None, speed: Optional[float] = None):
        if not self.config.get('enable_say_command', True):
            return await interaction.response.send_message("読み上げコマンドは無効です。", ephemeral=True)
        if not interaction.guild.voice_client:
            return await self.exception_handler.send_message(interaction, "bot_not_in_voice", ephemeral=True)
        
        lock = self._get_tts_lock(interaction.guild.id)
        if lock.locked():
            return await self.exception_handler.send_message(interaction, "tts_in_progress", ephemeral=True)

        channel_settings = self._get_channel_settings(interaction.guild.voice_client.channel.id)
        guild_settings = self._get_guild_speech_settings(interaction.guild.id)

        final_model_id = model_id if model_id is not None else channel_settings["model_id"]
        final_style = style if style is not None else channel_settings["style"]
        final_style_weight = style_weight if style_weight is not None else channel_settings["style_weight"]
        final_speed = speed if speed is not None else channel_settings["speed"]
        final_volume = guild_settings.get("volume", self.default_volume)

        await interaction.response.defer()
        async with lock:
            success = await self._handle_say_logic(interaction.guild, text, final_model_id, final_style, final_style_weight, final_speed, final_volume, interaction)
            if success:
                await interaction.followup.send(f"🔊 読み上げ中: `{text}`", ephemeral=True)

    def _get_tts_lock(self, guild_id: int) -> asyncio.Lock:
        return self.tts_locks.setdefault(guild_id, asyncio.Lock())

    def _load_dictionary(self):
        try:
            if self.dictionary_file.exists():
                with open(self.dictionary_file, 'r', encoding='utf-8') as f:
                    self.speech_dictionary = json.load(f)
                    logging.getLogger(__name__).info(
                        "[TTSCog] 読み上げ辞書を読み込みました: %d単語", len(self.speech_dictionary)
                    )
            else:
                self.dictionary_file.parent.mkdir(parents=True, exist_ok=True)
                self._save_dictionary()
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 辞書読み込みエラー: %s", e)

    def _save_dictionary(self):
        try:
            with open(self.dictionary_file, 'w', encoding='utf-8') as f:
                json.dump(self.speech_dictionary, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 辞書保存エラー: %s", e)

    def _apply_dictionary(self, text: str) -> str:
        if not self.speech_dictionary:
            return text
        sorted_words = sorted(self.speech_dictionary.keys(), key=len, reverse=True)
        for word in sorted_words:
            text = text.replace(word, self.speech_dictionary[word])
        return text

    dictionary_group = app_commands.Group(name="dictionary", description="読み上げ辞書の管理")

    @dictionary_group.command(name="add", description="読み上げ辞書に単語を追加します")
    @app_commands.describe(word="登録する単語", reading="読み方")
    async def add_dictionary(self, interaction: discord.Interaction, word: str, reading: str):
        is_update = word in self.speech_dictionary
        old_reading = self.speech_dictionary.get(word)
        self.speech_dictionary[word] = reading
        self._save_dictionary()
        
        embed = discord.Embed(title=f"📖 辞書を{'更新' if is_update else '追加'}しました", color=discord.Color.blue() if is_update else discord.Color.green())
        embed.add_field(name="単語", value=f"`{word}`", inline=False)
        if is_update: embed.add_field(name="変更前", value=f"`{old_reading}`", inline=True)
        embed.add_field(name="読み方", value=f"`{reading}`", inline=True)
        await interaction.response.send_message(embed=embed)

    @dictionary_group.command(name="remove", description="読み上げ辞書から単語を削除します")
    @app_commands.describe(word="削除する単語")
    async def remove_dictionary(self, interaction: discord.Interaction, word: str):
        if word not in self.speech_dictionary:
            return await interaction.response.send_message(f"❌ `{word}` は辞書にありません。", ephemeral=True)
        
        reading = self.speech_dictionary.pop(word)
        self._save_dictionary()
        embed = discord.Embed(title="📖 辞書から削除しました", color=discord.Color.orange())
        embed.add_field(name="単語", value=f"`{word}`", inline=True).add_field(name="読み方", value=f"`{reading}`", inline=True)
        await interaction.response.send_message(embed=embed)

    @dictionary_group.command(name="list", description="登録されている辞書の一覧を表示します")
    async def list_dictionary(self, interaction: discord.Interaction):
        if not self.speech_dictionary:
            return await interaction.response.send_message("📖 辞書は空です。", ephemeral=True)
        
        # Simple list for now, pagination can be re-added if needed
        description = "\n".join(f"`{word}` → `{reading}`" for word, reading in sorted(self.speech_dictionary.items()))
        embed = discord.Embed(title="📖 読み上げ辞書", description=description, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @dictionary_group.command(name="search", description="辞書から単語を検索します")
    @app_commands.describe(query="検索する単語（部分一致）")
    async def search_dictionary(self, interaction: discord.Interaction, query: str):
        results = {w: r for w, r in self.speech_dictionary.items() if query.lower() in w.lower()}
        if not results:
            return await interaction.response.send_message(f"❌ `{query}` に一致する単語は見つかりませんでした。", ephemeral=True)

        description = "\n".join(f"`{word}` → `{reading}`" for word, reading in sorted(results.items())[:25])
        embed = discord.Embed(title=f"🔍 検索結果: {query}", description=description, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    async def _handle_say_logic(self, guild: discord.Guild, text: str, model_id: int, style: str, style_weight: float, speed: float, volume: float, interaction: Optional[discord.Interaction] = None) -> bool:
        """TTS音声の再生を制御するメインロジック。音楽再生中はミキサーでオーバーレイする。"""
        voice_client = guild.voice_client
        # ボイス接続がなければ再生不可
        if not voice_client:
            return False

        # テキストの前処理: URLを省略し、辞書変換を適用
        processed_text = re.sub(r'https?://[\S]+', ' URL省略 ', text)
        converted_text = self._apply_dictionary(processed_text)
        # 200文字以上は切り詰め
        if len(converted_text) > 200:
            converted_text = converted_text[:200] + " 以下省略"

        # MusicCogの状態を確認
        music_cog: Optional[MusicCog] = self.bot.get_cog("music_cog")
        music_state = music_cog._get_guild_state(guild.id) if music_cog else None

        # 音楽が再生中（ミキサーあり & is_playing）ならミキサーでオーバーレイ
        if music_state and music_state.mixer and music_state.is_playing:
            return await self._overlay_tts_with_mixer(guild, converted_text, model_id, style, style_weight, speed, volume, interaction)

        # ミキサーが存在するがソースが残っている場合（TTS等）もミキサーを使う
        if music_state and music_state.mixer and music_state.mixer.has_sources():
            return await self._overlay_tts_with_mixer(guild, converted_text, model_id, style, style_weight, speed, volume, interaction)

        # それ以外は直接再生（voice_clientから直接play）
        return await self._play_tts_directly(guild, converted_text, model_id, style, style_weight, speed, volume, interaction)

    async def _api_call_to_audio_data(self, text: str, model_id: int, style: str, style_weight: float, speed: float) -> Optional[bytes]:
        # Prefer internal synthesizer; fall back to legacy HTTP if configured
        try:
            wav = self.synthesizer.synthesize_to_wav(
                text=text,
                style=style,
                style_weight=style_weight,
                speed=speed,
                noise_scale=self.config.get('noise_scale', 0.667),
                noise_w=self.config.get('noise_w', 0.8),
                length_scale=self.config.get('length_scale', 1.0),
            )
            return wav
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 内製TTS処理エラー: %s", e)

        if not self.api_url:
            return None

        endpoint = f"{self.api_url}/voice"
        params = {"text": text, "model_id": model_id, "style": style, "style_weight": style_weight, "speed": speed, "encoding": "wav"}
        try:
            # タイムアウトを設定してメモリリークを防止
            timeout = aiohttp.ClientTimeout(total=30)
            async with self.session.post(endpoint, params=params, timeout=timeout) as response:
                if response.status == 200:
                    audio_data = await response.read()
                    return audio_data
                logging.getLogger(__name__).error(
                    "[TTSCog] 音声生成APIエラー: %s %s", response.status, await response.text()
                )
                return None
        except asyncio.TimeoutError:
            logging.getLogger(__name__).error("[TTSCog] 音声生成APIタイムアウト")
            return None
        except Exception as e:
            logging.getLogger(__name__).error("[TTSCog] 音声生成APIリクエストエラー: %s", e)
            return None

    async def _overlay_tts_with_mixer(self, guild: discord.Guild, text: str, model_id: int, style: str, style_weight: float, speed: float, volume: float, interaction: Optional[discord.Interaction] = None) -> bool:
        music_cog: Optional[MusicCog] = self.bot.get_cog("music_cog")
        music_state = music_cog._get_guild_state(guild.id)
        
        wav_data = await self._api_call_to_audio_data(text, model_id, style, style_weight, speed)
        if not wav_data:
            if interaction: await interaction.followup.send("❌ 音声生成に失敗しました。", ephemeral=True)
            return False

        try:
            # BytesIOをwith文で管理してメモリリークを防止
            audio_buffer = io.BytesIO(wav_data)
            tts_source = TTSAudioSource(audio_buffer, text=text, guild_id=guild.id, pipe=True)
            source_name = f"tts_{int(time.time() * 1000)}"
            await music_state.mixer.add_source(source_name, tts_source, volume=volume)
            # wav_dataの参照を明示的に削除
            del wav_data
            return True
        except Exception as e:
            logging.getLogger(__name__).error(f"[TTSCog] ミキサーへのTTS追加エラー: {e}")
            if interaction: await interaction.followup.send("❌ 音声の再生に失敗しました。", ephemeral=True)
            return False

    async def _play_tts_directly(self, guild: discord.Guild, text: str, model_id: int, style: str, style_weight: float, speed: float, volume: float, interaction: Optional[discord.Interaction] = None) -> bool:
        """TTS音声をvoice_clientから直接再生する（音楽非再生時）"""
        voice_client = guild.voice_client
        # ボイス接続状態を確認
        if not voice_client or not voice_client.is_connected():
            return False
        # 既に別の音声が再生中の場合はスキップ
        # ※ _handle_say_logicでミキサー再生中はオーバーレイに回されるため、
        #   ここに来る場合はミキサー不在 = voice_client.is_playing()で正しく判定できる
        if voice_client.is_playing():
            return False

        wav_data = await self._api_call_to_audio_data(text, model_id, style, style_weight, speed)
        if not wav_data:
            if interaction: await interaction.followup.send("❌ 音声生成に失敗しました。", ephemeral=True)
            return False

        # 再度接続状態を確認（音声生成中に切断された可能性）
        if not voice_client.is_connected():
            return False

        try:
            # BytesIOをメモリ効率的に管理
            audio_buffer = io.BytesIO(wav_data)
            source = TTSAudioSource(audio_buffer, text=text, guild_id=guild.id, pipe=True)
            volume_source = discord.PCMVolumeTransformer(source, volume=volume)
            
            # 再生完了後のクリーンアップコールバック
            def after_playback(error):
                if error:
                    logging.getLogger(__name__).warning(f"[TTSCog] 再生エラー: {error}")
                # メモリ解放を促進
                gc.collect()
            
            voice_client.play(volume_source, after=after_playback)
            # wav_dataの参照を明示的に削除
            del wav_data
            return True
        except discord.errors.ClientException as e:
            logging.getLogger(__name__).warning(f"[TTSCog] 再生エラー: {e}")
            return False
        except Exception as e:
            logging.getLogger(__name__).error(f"[TTSCog] TTS再生中の予期しないエラー: {e}")
            return False


async def setup(bot: commands.Bot):
    if 'tts' not in bot.config:
        logging.getLogger("MOMOKA.tts").warning("'tts' section not found in config.yaml. TTSCog will not be loaded.")
        return
    if not bot.get_cog("music_cog"):
        logging.getLogger("MOMOKA.tts").warning("MusicCog is not loaded. TTSCog may not function correctly with music.")
    
    await bot.add_cog(TTSCog(bot))
