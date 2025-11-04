# MOMOKA/music/music_cog.py
import asyncio
import gc
import logging
import math
import random
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Dict, Optional
import time
import subprocess
import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks

try:
    from MOMOKA.music.plugins.ytdlp_wrapper import Track, extract as extract_audio_data, ensure_stream
    from MOMOKA.music.error.errors import MusicCogExceptionHandler
    from MOMOKA.music.plugins.audio_mixer import AudioMixer, MusicAudioSource
except ImportError as e:
    print(f"[CRITICAL] MusicCog: 必須コンポーネントのインポートに失敗しました。エラー: {e}")
    Track = None
    extract_audio_data = None
    ensure_stream = None
    MusicCogExceptionHandler = None
    AudioMixer = None
    MusicAudioSource = None

logger = logging.getLogger(__name__)


def format_duration(duration_seconds: int) -> str:
    if duration_seconds is None or duration_seconds < 0:
        return "N/A"
    hours, remainder = divmod(duration_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}" if hours > 0 else f"{int(minutes):02}:{int(seconds):02}"


def parse_time_to_seconds(time_str: str) -> Optional[int]:
    try:
        time_str = time_str.strip()

        if ':' not in time_str:
            return max(0, int(time_str))

        time_str = time_str.rstrip(':')
        parts = [int(p) for p in time_str.split(':')]

        if not parts or any(p < 0 for p in parts):
            return None

        if len(parts) == 2:
            return max(0, parts[0] * 60 + parts[1])
        elif len(parts) == 3:
            return max(0, parts[0] * 3600 + parts[1] * 60 + parts[2])
        else:
            return None
    except (ValueError, AttributeError):
        pass
    return None


class LoopMode(Enum):
    OFF = auto()
    ONE = auto()
    ALL = auto()


class GuildState:
    def __init__(self, bot: commands.Bot, guild_id: int, cog_config: dict):
        self.bot = bot
        self.guild_id = guild_id
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_track: Optional[Track] = None
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.volume: float = cog_config.get('music', {}).get('default_volume', 20) / 100.0
        self.loop_mode: LoopMode = LoopMode.OFF
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.auto_leave_task: Optional[asyncio.Task] = None
        self.last_text_channel_id: Optional[int] = None
        self.connection_lock = asyncio.Lock()
        self.last_activity = datetime.now()
        self.cleanup_in_progress = False
        self.playback_start_time: Optional[float] = None
        self.seek_position: int = 0
        self.paused_at: Optional[float] = None
        self.is_seeking: bool = False
        self.is_loading: bool = False
        self.mixer: Optional[AudioMixer] = None
        self._playing_next: bool = False  # 次の曲を再生中かどうかのフラグ

    def update_activity(self):
        self.last_activity = datetime.now()

    def update_last_text_channel(self, channel_id: int):
        self.last_text_channel_id = channel_id
        self.update_activity()

    def get_current_position(self) -> int:
        if not self.is_playing:
            return self.seek_position

        if self.is_paused and self.paused_at:
            elapsed = self.paused_at - self.playback_start_time
            return self.seek_position + int(elapsed)

        if self.playback_start_time:
            elapsed = time.time() - self.playback_start_time
            return self.seek_position + int(elapsed)

        return self.seek_position

    def reset_playback_tracking(self):
        self.playback_start_time = None
        self.seek_position = 0
        self.paused_at = None

    async def clear_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.queue = asyncio.Queue()

    async def cleanup_voice_client(self):
        if self.cleanup_in_progress:
            return
        self.cleanup_in_progress = True
        try:
            if self.mixer:
                self.mixer.stop()
                self.mixer = None
            if self.voice_client:
                try:
                    if self.voice_client.is_playing():
                        self.voice_client.stop()
                    if self.voice_client.is_connected():
                        await asyncio.wait_for(self.voice_client.disconnect(force=True), timeout=5.0)
                except Exception as e:
                    guild = self.bot.get_guild(self.guild_id)
                    logger.warning(f"Guild {self.guild_id} ({guild.name if guild else ''}): Voice cleanup error: {e}")
                finally:
                    self.voice_client = None
        finally:
            self.cleanup_in_progress = False


class MusicCog(commands.Cog, name="music_cog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not all((Track, extract_audio_data, ensure_stream, MusicCogExceptionHandler, AudioMixer, MusicAudioSource)):
            raise commands.ExtensionFailed(self.qualified_name, "必須コンポーネントのインポート失敗")
        self.config = self._load_bot_config()
        self.music_config = self.config.get('music', {})
        self.guild_states: Dict[int, GuildState] = {}
        self.exception_handler = MusicCogExceptionHandler(self.music_config)
        self.ffmpeg_path = self.music_config.get('ffmpeg_path', 'ffmpeg')
        self.ffmpeg_before_options = self.music_config.get('ffmpeg_before_options',
                                                           "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
        self.ffmpeg_options = self.music_config.get('ffmpeg_options', "-vn")
        self.auto_leave_timeout = self.music_config.get('auto_leave_timeout', 10)
        self.max_queue_size = self.music_config.get('max_queue_size', 9000)
        self.max_guilds = self.music_config.get('max_guilds', 100000000)
        self.inactive_timeout_minutes = self.music_config.get('inactive_timeout_minutes', 30)
        self.global_connection_lock = asyncio.Lock()
        self.cleanup_task = None

    async def cog_load(self):
        if not self.cleanup_task or self.cleanup_task.done():
            self.cleanup_task = self.cleanup_task_loop.start()
        logger.info("MusicCog loaded and cleanup task started")

    def _load_bot_config(self) -> dict:
        if hasattr(self.bot, 'config') and self.bot.config:
            return self.bot.config
        try:
            with open('config.yaml', 'r', encoding='utf-8') as f:
                loaded_config = yaml.safe_load(f)
                self.bot.config = loaded_config
                return loaded_config
        except Exception:
            return {}

    def cog_unload(self):
        logger.info("Unloading MusicCog...")
        if hasattr(self, 'cleanup_task') and self.cleanup_task:
            self.cleanup_task.cancel()
        if hasattr(self, 'cleanup_task_loop') and self.cleanup_task_loop.is_running():
            self.cleanup_task_loop.cancel()
        for guild_id in list(self.guild_states.keys()):
            try:
                state = self.guild_states[guild_id]
                if state.mixer:
                    state.mixer.stop()
                if state.voice_client and state.voice_client.is_connected():
                    asyncio.create_task(state.voice_client.disconnect(force=True))
                if state.auto_leave_task and not state.auto_leave_task.done():
                    state.auto_leave_task.cancel()
            except Exception as e:
                guild = self.bot.get_guild(guild_id)
                logger.warning(f"Guild {guild_id} ({guild.name if guild else ''}) unload cleanup error: {e}")
        self.guild_states.clear()
        logger.info("MusicCog unloaded.")

    @tasks.loop(minutes=5)
    async def cleanup_task_loop(self):
        try:
            current_time = datetime.now()
            inactive_threshold = timedelta(minutes=self.inactive_timeout_minutes)
            guilds_to_cleanup = [
                gid for gid, state in self.guild_states.items()
                if (current_time - state.last_activity > inactive_threshold and
                    not state.is_playing and
                    (not state.voice_client or not state.voice_client.is_connected()))
            ]
            for guild_id in guilds_to_cleanup:
                guild = self.bot.get_guild(guild_id)
                logger.info(f"Cleaning up inactive guild: {guild_id} ({guild.name if guild else ''})")
                await self._cleanup_guild_state(guild_id)
            if guilds_to_cleanup:
                gc.collect()
        except Exception as e:
            logger.error(f"Cleanup task error: {e}", exc_info=True)

    @cleanup_task_loop.before_loop
    async def before_cleanup_task(self):
        await self.bot.wait_until_ready()

    def _get_guild_state(self, guild_id: int) -> Optional[GuildState]:
        if guild_id not in self.guild_states:
            if len(self.guild_states) >= self.max_guilds:
                oldest_guild, oldest_time = None, datetime.now()
                for gid, state in self.guild_states.items():
                    if not state.is_playing and state.last_activity < oldest_time:
                        oldest_guild, oldest_time = gid, state.last_activity
                if oldest_guild:
                    asyncio.create_task(self._cleanup_guild_state(oldest_guild))
                    guild = self.bot.get_guild(oldest_guild)
                    logger.info(
                        f"Removed oldest inactive guild {oldest_guild} ({guild.name if guild else ''}) to make room")
            self.guild_states[guild_id] = GuildState(self.bot, guild_id, self.config)
        self.guild_states[guild_id].update_activity()
        return self.guild_states[guild_id]

    async def _send_response(self, ctx: commands.Context, message_key: str, ephemeral: bool = False,
                             **kwargs):
        content = self.exception_handler.get_message(message_key, **kwargs)
        try:
            await ctx.send(content, ephemeral=ephemeral)
        except Exception as e:
            logger.error(f"Guild {ctx.guild.id} ({ctx.guild.name}): Response error: {e}")

    async def _send_background_message(self, channel_id: int, message_key: str, **kwargs):
        try:
            channel = self.bot.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(self.exception_handler.get_message(message_key, **kwargs))
        except discord.Forbidden:
            logger.debug(f"No permission to send to channel {channel_id}")
        except Exception as e:
            logger.error(f"Background message error: {e}")

    async def _handle_error(self, ctx: commands.Context, error: Exception):
        error_message = self.exception_handler.handle_error(error, ctx.guild)
        await ctx.send(error_message, ephemeral=True)

    async def _ensure_voice(self, ctx: commands.Context, connect_if_not_in: bool = True) -> Optional[
        discord.VoiceClient]:
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("サーバーの上限に達しています。", ephemeral=True)
            return None
        state.update_last_text_channel(ctx.channel.id)
        user_voice = ctx.author.voice
        if not user_voice or not user_voice.channel:
            await self._send_response(ctx, "join_voice_channel_first", ephemeral=True)
            return None

        async with state.connection_lock:
            async with self.global_connection_lock:
                active_connections = sum(
                    1 for s in self.guild_states.values() if s.voice_client and s.voice_client.is_connected())
                if active_connections >= self.max_guilds and not state.voice_client:
                    await self._send_response(ctx, "error_playing", ephemeral=True,
                                              error="現在接続数が上限に達しています。")
                    return None

            vc = state.voice_client
            if vc:
                if not vc.is_connected():
                    await state.cleanup_voice_client()
                    vc = None
                elif vc.channel == user_voice.channel:
                    return vc
                else:
                    await state.cleanup_voice_client()
                    await asyncio.sleep(0.5)
                    vc = None

            for voice_client in list(self.bot.voice_clients):
                if voice_client.guild.id == ctx.guild.id and voice_client != state.voice_client:
                    try:
                        await asyncio.wait_for(voice_client.disconnect(force=True), timeout=3.0)
                    except:
                        pass

            if not vc and connect_if_not_in:
                try:
                    await asyncio.sleep(0.3)
                    state.voice_client = await asyncio.wait_for(
                        user_voice.channel.connect(timeout=30.0, reconnect=True, self_deaf=True),
                        timeout=35.0
                    )
                    logger.info(
                        f"Guild {ctx.guild.id} ({ctx.guild.name}): Connected to {user_voice.channel.name}")
                    return state.voice_client
                except Exception as e:
                    await self._handle_error(ctx, e)
                    state.voice_client = None
                    return None
            elif not vc:
                await self._send_response(ctx, "bot_not_in_voice_channel", ephemeral=True)
                return None
            return vc

    def mixer_finished_callback(self, error: Optional[Exception], guild_id: int):
        if error:
            logger.error(f"Guild {guild_id}: Mixer unexpectedly finished with error: {error}")
        logger.info(f"Guild {guild_id}: Mixer has finished.")
        state = self._get_guild_state(guild_id)
        if state and not state._playing_next:
            state._playing_next = True
            
            finished_track = state.current_track
            state.mixer = None
            state.is_playing = False
            
            # LoopMode.ONEの場合は current_track を保持、それ以外は None にする
            if state.loop_mode != LoopMode.ONE:
                state.current_track = None
            
            state.reset_playback_tracking()
            
            if error:
                guild = self.bot.get_guild(guild_id)
                error_message = self.exception_handler.handle_error(error, guild)
                if state.last_text_channel_id:
                    asyncio.run_coroutine_threadsafe(
                        self._send_background_message(state.last_text_channel_id, "error_message_wrapper",
                                                      error=error_message),
                        self.bot.loop
                    )
            
            if finished_track and state.loop_mode == LoopMode.ALL:
                asyncio.run_coroutine_threadsafe(state.queue.put(finished_track), self.bot.loop)
            
            def play_next_and_reset_flag():
                async def _play():
                    try:
                        await self._play_next_song(guild_id)
                    finally:
                        if state:
                            state._playing_next = False
                asyncio.run_coroutine_threadsafe(_play(), self.bot.loop)
            
            play_next_and_reset_flag()

    async def _on_music_source_removed(self, guild_id: int):
        """音楽ソースが削除されたときに呼ばれる（ループモードやキューを考慮して次の曲を再生）"""
        state = self._get_guild_state(guild_id)
        if not state or state.is_seeking or state._playing_next:
            return
        
        state._playing_next = True
        
        try:
            finished_track = state.current_track
            state.is_playing = False
            
            # LoopMode.ONEの場合は current_track を保持、それ以外は None にする
            if state.loop_mode != LoopMode.ONE:
                state.current_track = None
            
            state.reset_playback_tracking()
            
            if finished_track and state.loop_mode == LoopMode.ALL:
                await state.queue.put(finished_track)
            
            await self._play_next_song(guild_id)
        finally:
            state._playing_next = False

    async def _play_next_song(self, guild_id: int, seek_seconds: int = 0):
        state = self._get_guild_state(guild_id)
        if not state:
            return

        if state.is_playing and not seek_seconds > 0:
            return

        is_seek_operation = seek_seconds > 0
        track_to_play: Optional[Track] = None

        if is_seek_operation and state.current_track:
            track_to_play = state.current_track
        elif state.loop_mode == LoopMode.ONE and state.current_track and not is_seek_operation:
            track_to_play = state.current_track
        elif not state.is_playing and not state.queue.empty() and not is_seek_operation:
            try:
                track_to_play = await state.queue.get()
                state.queue.task_done()
            except:
                pass

        if not track_to_play:
            state.current_track = None
            state.is_playing = False
            state.reset_playback_tracking()
            if state.last_text_channel_id:
                await self._send_background_message(state.last_text_channel_id, "queue_ended")
            return

        if not is_seek_operation:
            state.current_track = track_to_play

        state.is_playing = True
        state.is_paused = False
        state.update_activity()

        state.seek_position = seek_seconds
        state.playback_start_time = time.time()
        state.paused_at = None

        try:
            is_local_file = False
            if track_to_play.stream_url:
                try:
                    is_local_file = Path(track_to_play.stream_url).is_file()
                except Exception:
                    pass

            if not is_local_file:
                updated_track = await ensure_stream(track_to_play)
                if not updated_track or not updated_track.stream_url:
                    raise RuntimeError(f"'{track_to_play.title}' の有効なストリームURLを取得できませんでした。")
                track_to_play.stream_url = updated_track.stream_url

            ffmpeg_before_opts = self.ffmpeg_before_options
            if seek_seconds > 0:
                ffmpeg_before_opts = f"-ss {seek_seconds} {ffmpeg_before_opts}"

            source = MusicAudioSource(
                track_to_play.stream_url,
                title=track_to_play.title,
                guild_id=guild_id,
                executable=self.ffmpeg_path,
                before_options=ffmpeg_before_opts,
                options=self.ffmpeg_options,
                stderr=subprocess.PIPE
            )

            if state.mixer is None:
                def on_source_removed(name: str):
                    """ソースが削除されたときのコールバック"""
                    if name == 'music':
                        asyncio.run_coroutine_threadsafe(self._on_music_source_removed(guild_id), self.bot.loop)
                
                state.mixer = AudioMixer(on_source_removed_callback=on_source_removed)

            await state.mixer.add_source('music', source, volume=state.volume)

            if state.voice_client and state.voice_client.source is not state.mixer:
                state.voice_client.play(state.mixer, after=lambda e: self.mixer_finished_callback(e, guild_id))

            if is_seek_operation:
                state.is_seeking = False

            if state.last_text_channel_id and track_to_play.requester_id and not is_seek_operation:
                try:
                    requester = self.bot.get_user(track_to_play.requester_id) or await self.bot.fetch_user(
                        track_to_play.requester_id)
                except:
                    requester = None
                await self._send_background_message(
                    state.last_text_channel_id, "now_playing", title=track_to_play.title,
                    duration=format_duration(track_to_play.duration),
                    requester_display_name=requester.display_name if requester else "不明"
                )
        except Exception as e:
            guild = self.bot.get_guild(guild_id)
            logger.error(f"Guild {guild_id} ({guild.name if guild else ''}): Playback error: {e}", exc_info=True)
            error_message = self.exception_handler.handle_error(e, guild)
            if state.last_text_channel_id:
                await self._send_background_message(state.last_text_channel_id, "error_message_wrapper",
                                                    error=error_message)
            if state.loop_mode == LoopMode.ALL and track_to_play and not is_seek_operation:
                await state.queue.put(track_to_play)
            state.current_track = None
            state.is_seeking = False
            state.is_playing = False
            state.reset_playback_tracking()
            asyncio.create_task(self._play_next_song(guild_id))

    def _song_finished_callback(self, error: Optional[Exception], guild_id: int):
        state = self._get_guild_state(guild_id)
        if not state or state.is_seeking:
            return

        if state.mixer:
            asyncio.run_coroutine_threadsafe(state.mixer.remove_source('music'), self.bot.loop)

        finished_track = state.current_track
        state.is_playing = False
        state.current_track = None
        state.reset_playback_tracking()

        if error:
            guild = self.bot.get_guild(guild_id)
            error_message = self.exception_handler.handle_error(error, guild)
            if state.last_text_channel_id:
                asyncio.run_coroutine_threadsafe(
                    self._send_background_message(state.last_text_channel_id, "error_message_wrapper",
                                                  error=error_message),
                    self.bot.loop
                )

        if finished_track and state.loop_mode == LoopMode.ALL:
            asyncio.run_coroutine_threadsafe(state.queue.put(finished_track), self.bot.loop)

        asyncio.run_coroutine_threadsafe(self._play_next_song(guild_id), self.bot.loop)

    def _schedule_auto_leave(self, guild_id: int):
        state = self._get_guild_state(guild_id)
        if not state:
            return
        if state.auto_leave_task and not state.auto_leave_task.done():
            state.auto_leave_task.cancel()
        if state.voice_client and state.voice_client.is_connected():
            state.auto_leave_task = asyncio.create_task(self._auto_leave_coroutine(guild_id))

    async def _auto_leave_coroutine(self, guild_id: int):
        await asyncio.sleep(self.auto_leave_timeout)
        state = self._get_guild_state(guild_id)
        if state and state.voice_client and state.voice_client.is_connected():
            if not [m for m in state.voice_client.channel.members if not m.bot]:
                if state.last_text_channel_id:
                    await self._send_background_message(state.last_text_channel_id, "auto_left_empty_channel")
                await state.voice_client.disconnect()

    async def _cleanup_guild_state(self, guild_id: int):
        state = self.guild_states.pop(guild_id, None)
        if state:
            await state.cleanup_voice_client()
            if state.auto_leave_task and not state.auto_leave_task.done():
                state.auto_leave_task.cancel()
            await state.clear_queue()
            guild = self.bot.get_guild(guild_id)
            logger.info(f"Guild {guild_id} ({guild.name if guild else ''}): State cleaned up")

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"{self.bot.user.name} の MusicCog が正常にロードされました。")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        if member.id == self.bot.user.id and before.channel and not after.channel:
            await self._cleanup_guild_state(member.guild.id)
            return

        guild_id = member.guild.id
        if guild_id not in self.guild_states:
            return

        state = self._get_guild_state(guild_id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            return

        current_vc_channel = state.voice_client.channel
        if before.channel != current_vc_channel and after.channel != current_vc_channel:
            return

        human_members_in_vc = [m for m in current_vc_channel.members if not m.bot]
        if not human_members_in_vc:
            if not state.auto_leave_task or state.auto_leave_task.done():
                self._schedule_auto_leave(guild_id)
        elif state.auto_leave_task and not state.auto_leave_task.done():
            state.auto_leave_task.cancel()

    @commands.hybrid_command(name="play", description="曲を再生またはキューに追加します。")
    @app_commands.describe(query="再生したい曲のタイトル、またはURL")
    async def play(self, ctx: commands.Context, *, query: str):
        await ctx.defer()

        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("サーバーの上限に達しています。", ephemeral=True)
            return

        vc = await self._ensure_voice(ctx, connect_if_not_in=True)
        if not vc:
            return

        if state.queue.qsize() >= self.max_queue_size:
            await self._send_response(ctx, "max_queue_size_reached",
                                      max_size=self.max_queue_size)
            return

        was_playing = state.is_playing or state.is_loading
        state.is_loading = True

        try:
            await ctx.send(
                self.exception_handler.get_message("searching_for_song", query=query)
            )

            extracted_media = await extract_audio_data(query, shuffle_playlist=False)

            if not extracted_media:
                await ctx.send(
                    self.exception_handler.get_message("search_no_results", query=query)
                )
                return

            tracks = extracted_media if isinstance(extracted_media, list) else [extracted_media]
            added_count, first_track = 0, None

            for track in tracks:
                if state.queue.qsize() < self.max_queue_size:
                    track.requester_id = ctx.author.id
                    track.stream_url = None
                    await state.queue.put(track)
                    if added_count == 0:
                        first_track = track
                    added_count += 1
                else:
                    await ctx.send(
                        self.exception_handler.get_message("max_queue_size_reached",
                                                           max_size=self.max_queue_size)
                    )
                    break

            if added_count > 1:
                await ctx.send(
                    self.exception_handler.get_message("added_playlist_to_queue",
                                                       count=added_count)
                )
            elif added_count == 1 and first_track:
                await ctx.send(
                    self.exception_handler.get_message("added_to_queue",
                                                       title=first_track.title,
                                                       duration=format_duration(first_track.duration),
                                                       requester_display_name=ctx.author.display_name)
                )

            if not was_playing:
                await self._play_next_song(ctx.guild.id)

        except Exception as e:
            error_message = self.exception_handler.handle_error(e, ctx.guild)
            await ctx.send(
                self.exception_handler.get_message("error_message_wrapper", error=error_message)
            )
        finally:
            state.is_loading = False

    @commands.hybrid_command(name="seek", description="再生位置を指定した時刻に移動します。")
    @app_commands.describe(time="移動先の時刻 (例: 1:30 または 90 秒)")
    async def seek(self, ctx: commands.Context, *, time: str):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        await ctx.defer()

        if not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if not state.current_track:
            await self._send_response(ctx, "nothing_to_skip", ephemeral=True)
            return

        seek_seconds = parse_time_to_seconds(time)
        if seek_seconds is None:
            await self._send_response(ctx, "invalid_time_format", ephemeral=True)
            return

        if seek_seconds >= state.current_track.duration:
            await self._send_response(ctx, "seek_beyond_duration", ephemeral=True,
                                      duration=format_duration(state.current_track.duration))
            return

        state.is_seeking = True
        self._song_finished_callback(None, ctx.guild.id)
        await asyncio.sleep(0.5)
        state.is_seeking = False

        await self._send_response(ctx, "seeked_to_position", position=format_duration(seek_seconds))
        await self._play_next_song(ctx.guild.id, seek_seconds=seek_seconds)

    @commands.hybrid_command(name="pause", description="再生を一時停止します。")
    async def pause(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if not state.is_playing:
            await self._send_response(ctx, "error_playing", ephemeral=True, error="再生中ではありません。")
            return

        if state.is_paused:
            await self._send_response(ctx, "error_playing", ephemeral=True, error="既に一時停止中です。")
            return

        state.voice_client.pause()
        state.is_paused = True
        state.paused_at = time.time()
        await self._send_response(ctx, "playback_paused")

    @commands.hybrid_command(name="resume", description="一時停止中の再生を再開します。")
    async def resume(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if not state.is_paused:
            await self._send_response(ctx, "error_playing", ephemeral=True, error="一時停止中ではありません。")
            return

        state.voice_client.resume()
        state.is_paused = False
        if state.paused_at and state.playback_start_time:
            pause_duration = time.time() - state.paused_at
            state.playback_start_time += pause_duration
        state.paused_at = None
        await self._send_response(ctx, "playback_resumed")

    @commands.hybrid_command(name="skip", description="再生中の曲をスキップします。")
    async def skip(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        await ctx.defer()
        vc = await self._ensure_voice(ctx, connect_if_not_in=False)
        if not vc or not state.current_track:
            await self._send_response(ctx, "nothing_to_skip", ephemeral=True)
            return

        await self._send_response(ctx, "skipped_song", title=state.current_track.title)
        self._song_finished_callback(None, ctx.guild.id)

    @commands.hybrid_command(name="stop", description="再生を停止し、キューをクリアします。")
    async def stop(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        await ctx.defer()
        if not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        state.loop_mode = LoopMode.OFF
        await state.clear_queue()
        if state.mixer:
            state.mixer.stop()
            state.mixer = None
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.stop()
        state.is_playing = False
        state.is_paused = False
        state.current_track = None
        state.reset_playback_tracking()
        await self._send_response(ctx, "stopped_playback")

    @commands.hybrid_command(name="leave", description="ボットをボイスチャンネルから切断します。")
    async def leave(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        await ctx.defer()
        async with state.connection_lock:
            if not state.voice_client or not state.voice_client.is_connected():
                await self._send_response(ctx, "bot_not_in_voice_channel", ephemeral=True)
                return
            await self._send_response(ctx, "leaving_voice_channel")
            await self._cleanup_guild_state(ctx.guild.id)

    @commands.hybrid_command(name="queue", description="現在の再生キューを表示します。")
    async def queue(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        state.update_last_text_channel(ctx.channel.id)
        if state.queue.empty() and not state.current_track:
            await ctx.send(self.exception_handler.get_message("queue_empty"), ephemeral=True)
            return

        items_per_page = 10
        queue_list = list(state.queue._queue)
        total_items = len(queue_list)
        total_pages = math.ceil(len(queue_list) / items_per_page) if len(queue_list) > 0 else 1

        async def get_page_embed(page_num: int):
            embed = discord.Embed(
                title=self.exception_handler.get_message("queue_title",
                                                         count=total_items + (1 if state.current_track else 0)),
                color=discord.Color.blue()
            )
            lines = []
            if page_num == 1 and state.current_track:
                track = state.current_track
                try:
                    requester = ctx.guild.get_member(track.requester_id) or await self.bot.fetch_user(
                        track.requester_id)
                except:
                    requester = None
                status_icon = '▶️' if state.is_playing else '⏸️'
                current_pos = state.get_current_position()
                lines.append(
                    f"**{status_icon} {track.title}** (`{format_duration(current_pos)}/{format_duration(track.duration)}`) - Req: **{requester.display_name if requester else '不明'}**\n"
                )

            start = (page_num - 1) * items_per_page
            end = (page_num - 1) * items_per_page + items_per_page
            for i, track in enumerate(queue_list[start:end], start=start + 1):
                try:
                    requester = ctx.guild.get_member(track.requester_id) or await self.bot.fetch_user(
                        track.requester_id)
                except:
                    requester = None
                lines.append(
                    f"`{i}.` **{track.title}** (`{format_duration(track.duration)}`) - Req: **{requester.display_name if requester else '不明'}**"
                )

            embed.description = "\n".join(lines) if lines else "このページには曲がありません。"
            if total_pages > 1:
                embed.set_footer(text=f"ページ {page_num}/{total_pages}")
            return embed

        def get_queue_view(current_page: int, total_pages: int, user_id: int):
            view = discord.ui.View(timeout=60.0)

            first_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                emoji="⏪",
                label="First",
                disabled=(current_page == 1)
            )

            async def first_callback(interaction: discord.Interaction):
                if interaction.user.id != user_id:
                    await interaction.response.send_message("このボタンは使用できません。", ephemeral=True)
                    return
                nonlocal current_page
                current_page = 1
                await interaction.response.edit_message(
                    embed=await get_page_embed(current_page),
                    view=get_queue_view(current_page, total_pages, user_id)
                )

            first_button.callback = first_callback
            view.add_item(first_button)

            prev_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                emoji="◀️",
                label="Previous",
                disabled=(current_page == 1)
            )

            async def prev_callback(interaction: discord.Interaction):
                if interaction.user.id != user_id:
                    await interaction.response.send_message("このボタンは使用できません。", ephemeral=True)
                    return
                nonlocal current_page
                current_page = max(1, current_page - 1)
                await interaction.response.edit_message(
                    embed=await get_page_embed(current_page),
                    view=get_queue_view(current_page, total_pages, user_id)
                )

            prev_button.callback = prev_callback
            view.add_item(prev_button)

            stop_button = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                emoji="⏹️",
                label="Close"
            )

            async def stop_callback(interaction: discord.Interaction):
                if interaction.user.id != user_id:
                    await interaction.response.send_message("このボタンは使用できません。", ephemeral=True)
                    return
                view.stop()
                await interaction.response.edit_message(view=None)

            stop_button.callback = stop_callback
            view.add_item(stop_button)

            next_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                emoji="▶️",
                label="Next",
                disabled=(current_page == total_pages)
            )

            async def next_callback(interaction: discord.Interaction):
                if interaction.user.id != user_id:
                    await interaction.response.send_message("このボタンは使用できません。", ephemeral=True)
                    return
                nonlocal current_page
                current_page = min(total_pages, current_page + 1)
                await interaction.response.edit_message(
                    embed=await get_page_embed(current_page),
                    view=get_queue_view(current_page, total_pages, user_id)
                )

            next_button.callback = next_callback
            view.add_item(next_button)

            last_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                emoji="⏩",
                label="Last",
                disabled=(current_page == total_pages)
            )

            async def last_callback(interaction: discord.Interaction):
                if interaction.user.id != user_id:
                    await interaction.response.send_message("このボタンは使用できません。", ephemeral=True)
                    return
                nonlocal current_page
                current_page = total_pages
                await interaction.response.edit_message(
                    embed=await get_page_embed(current_page),
                    view=get_queue_view(current_page, total_pages, user_id)
                )

            last_button.callback = last_callback
            view.add_item(last_button)

            return view

        current_page = 1
        if total_pages <= 1:
            await ctx.send(embed=await get_page_embed(current_page))
        else:
            view = get_queue_view(current_page, total_pages, ctx.author.id)
            await ctx.send(embed=await get_page_embed(current_page), view=view)

    @commands.hybrid_command(name="nowplaying", description="現在再生中の曲の情報を表示します。")
    async def nowplaying(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not state.current_track:
            await ctx.send(self.exception_handler.get_message("now_playing_nothing"),
                                                    ephemeral=True)
            return

        track = state.current_track
        status_icon = "▶️" if state.is_playing else ("⏸️" if state.is_paused else "⏹️")
        try:
            requester = ctx.guild.get_member(track.requester_id) or await self.bot.fetch_user(
                track.requester_id)
        except:
            requester = None

        current_pos = state.get_current_position()
        progress_bar = self._create_progress_bar(current_pos, track.duration)

        embed = discord.Embed(
            title=f"{status_icon} {track.title}",
            url=track.url,
            description=f"{progress_bar}\n`{format_duration(current_pos)}` / `{format_duration(track.duration)}`\n\nリクエスト: **{requester.display_name if requester else '不明'}**\nURL: {track.url}\nループモード: `{state.loop_mode.name.lower()}`",
            color=discord.Color.green() if state.is_playing else (
                discord.Color.orange() if state.is_paused else discord.Color.light_grey())
        )
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        await ctx.send(embed=embed)

    def _create_progress_bar(self, current: int, total: int, length: int = 20) -> str:
        if total <= 0:
            return "─" * length
        progress = min(current / total, 1.0)
        filled = int(length * progress)
        bar = "━" * filled + "○" + "─" * (length - filled - 1)
        return bar

    @commands.hybrid_command(name="shuffle", description="再生キューをシャッフルします。")
    async def shuffle(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if state.queue.qsize() < 2:
            await self._send_response(ctx, "error_playing", ephemeral=True,
                                      error="シャッフルするにはキューに2曲以上必要です。")
            return

        queue_list = list(state.queue._queue)
        random.shuffle(queue_list)
        state.queue = asyncio.Queue()
        for item in queue_list:
            await state.queue.put(item)
        await self._send_response(ctx, "queue_shuffled")

    @commands.hybrid_command(name="clear", description="再生キューを空にします（再生中の曲は停止しません）。")
    async def clear(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        await state.clear_queue()
        await self._send_response(ctx, "queue_cleared")

    @commands.hybrid_command(name="remove", description="キューから指定した番号の曲を削除します。")
    @app_commands.describe(index="削除したい曲のキュー番号")
    async def remove(self, ctx: commands.Context, index: int):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        if index < 1:
            await self._send_response(ctx, "invalid_queue_number", ephemeral=True)
            return

        if state.queue.empty():
            await ctx.send(self.exception_handler.get_message("queue_empty"), ephemeral=True)
            return

        actual_index = index - 1
        if not (0 <= actual_index < state.queue.qsize()):
            await self._send_response(ctx, "invalid_queue_number", ephemeral=True)
            return

        queue_list = list(state.queue._queue)
        removed_track = queue_list.pop(actual_index)
        state.queue = asyncio.Queue()
        for item in queue_list:
            await state.queue.put(item)
        await self._send_response(ctx, "song_removed", title=removed_track.title)

    @commands.hybrid_command(name="volume", description="音量を変更します (0-200)。")
    @app_commands.describe(level="設定したい音量レベル (0-200)")
    async def volume(self, ctx: commands.Context, level: int):
        if not 0 <= level <= 200:
            await ctx.send("音量は0から200の間で指定してください。", ephemeral=True)
            return

        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        state.volume = level / 100.0
        state.update_activity()
        if state.mixer:
            await state.mixer.set_volume('music', state.volume)
        await self._send_response(ctx, "volume_set", volume=level)

    @commands.hybrid_command(name="loop", description="ループ再生モードを設定します。")
    @app_commands.describe(mode="ループのモードを選択してください。")
    @app_commands.choices(mode=[
        app_commands.Choice(name="オフ (Loop Off)", value="off"),
        app_commands.Choice(name="現在の曲をループ (Loop One)", value="one"),
        app_commands.Choice(name="キュー全体をループ (Loop All)", value="all")
    ])
    async def loop(self, ctx: commands.Context, mode: str):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await ctx.send("エラーが発生しました。", ephemeral=True)
            return

        await ctx.defer()
        mode_map = {"off": LoopMode.OFF, "one": LoopMode.ONE, "all": LoopMode.ALL}
        mode_val = mode.lower()
        if mode_val not in mode_map:
            await ctx.send(f"無効なモードです。`off`, `one`, `all`のいずれかを指定してください。", ephemeral=True)
            return
        state.loop_mode = mode_map.get(mode_val, LoopMode.OFF)
        state.update_activity()
        await self._send_response(ctx, f"loop_{mode_val}")

    @commands.hybrid_command(name="join", description="ボットをあなたのいるボイスチャンネルに接続します。")
    async def join(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if await self._ensure_voice(ctx, connect_if_not_in=True):
            await ctx.send(self.exception_handler.get_message("already_connected"), ephemeral=True)

    @commands.hybrid_command(name="music_help", description="音楽機能のコマンド一覧と使い方を表示します。")
    async def music_help(self, ctx: commands.Context):
        await ctx.defer(ephemeral=False)
        prefix = str(self.bot.command_prefix).strip('"'+"'")
        embed = discord.Embed(
            title="🎵 音楽機能 ヘルプ / Music Feature Help",
            description=f"音楽再生に関するコマンドの一覧です。\nコマンドはスラッシュ (`/`) またはプレフィックス (`{prefix}`) で始まります。",
            color=discord.Color.from_rgb(79, 194, 255)
        )
        command_info = {
            "▶️ 再生コントロール / Playback Control": [
                {"name": "play", "args": "<song name or URL>", "desc_ja": "曲を再生/キュー追加",
                 "desc_en": "Play/add a song"},
                {"name": "pause", "args": "", "desc_ja": "一時停止", "desc_en": "Pause"},
                {"name": "resume", "args": "", "desc_ja": "再生再開", "desc_en": "Resume"},
                {"name": "stop", "args": "", "desc_ja": "再生停止＆キュークリア", "desc_en": "Stop & clear queue"},
                {"name": "skip", "args": "", "desc_ja": "現在の曲をスキップ", "desc_en": "Skip song"},
                {"name": "seek", "args": "<time>", "desc_ja": "指定時刻に移動", "desc_en": "Seek to time"},
                {"name": "volume", "args": "<level 0-200>", "desc_ja": "音量変更", "desc_en": "Change volume"}
            ],
            "💿 キュー管理 / Queue Management": [
                {"name": "queue", "args": "", "desc_ja": "キュー表示", "desc_en": "Display queue"},
                {"name": "nowplaying", "args": "", "desc_ja": "現在再生中の曲", "desc_en": "Show current song"},
                {"name": "shuffle", "args": "", "desc_ja": "キューをシャッフル", "desc_en": "Shuffle queue"},
                {"name": "clear", "args": "", "desc_ja": "キューをクリア", "desc_en": "Clear queue"},
                {"name": "remove", "args": "<queue number>", "desc_ja": "指定番号の曲を削除", "desc_en": "Remove song"},
                {"name": "loop", "args": "<off|one|all>", "desc_ja": "ループモード設定", "desc_en": "Set loop mode"}
            ],
            "🔊 ボイスチャンネル / Voice Channel": [
                {"name": "join", "args": "", "desc_ja": "VCに接続", "desc_en": "Join VC"},
                {"name": "leave", "args": "", "desc_en": "Leave VC", "desc_ja": "VCから切断"}
            ]
        }
        cog_command_names = {cmd.name for cmd in self.get_commands()}
        for category, commands_in_category in command_info.items():
            field_value = "".join(
                f"`{prefix}{c['name']}{' ' + c['args'] if c['args'] else ''}`\n{c['desc_ja']} / {c['desc_en']}\n"
                for c in commands_in_category if c['name'] in cog_command_names
            )
            if field_value:
                embed.add_field(name=f"**{category}**", value=field_value, inline=False)

        active_guilds = len(self.guild_states)
        embed.set_footer(text=f"<> は引数を表します | Active: {active_guilds}/{self.max_guilds} servers")
        await ctx.send(embed=embed)

    @commands.hybrid_group(name="reload", description="各種機能を再読み込みします。 / Reloads various features.")
    async def reload(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send('このコマンドにはサブコマンドが必要です。 (例: `reload music_cog`)', ephemeral=True)

    @reload.command(name="music_cog", description="音楽Cogを再読み込みして、問題をリセットします。/ Reloads the music cog to fix issues.")
    async def reload_music_cog(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        module_name = self.__module__
        logger.info(f"音楽Cog ({module_name}) の再読み込みを試みます。リクエスト者: {ctx.author}")

        try:
            await self.bot.reload_extension(module_name)
            logger.info(f"音楽Cog ({module_name}) の再読み込みに成功しました。")
            await ctx.followup.send(
                "🎵 音楽機能の再読み込みが完了しました。\n🎵 Music feature has been successfully reloaded.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"音楽Cog ({module_name}) の再読み込み中にエラーが発生しました: {e}", exc_info=True)
            await ctx.followup.send(
                f"❌ 音楽機能の再読み込み中にエラーが発生しました。\n❌ An error occurred while reloading the music feature.\n```py\n{type(e).__name__}: {e}\n```",
                ephemeral=True
            )

    @reload_music_cog.error
    async def reload_music_cog_error(self, ctx: commands.Context, error: commands.CommandError):
        await self.exception_handler.handle_generic_command_error(ctx, error)


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(MusicCog(bot))
        logger.info("MusicCog successfully loaded")
    except Exception as e:
        logger.error(f"MusicCogのセットアップ中にエラー: {e}", exc_info=True)
        raise
