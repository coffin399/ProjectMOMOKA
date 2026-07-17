# MOMOKA/music/music_cog.py
import asyncio
import collections
import gc
import io
import itertools
import logging
import math
import random
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks

from MOMOKA.utilities.donation import donation_from_bot, make_subtle_link_button

try:
    from MOMOKA.music.plugins.ytdlp_wrapper import (
        Track,
        extract as extract_audio_data,
        ensure_stream,
        set_youtube_cookie_path,
        UnsupportedMediaError,
    )
    from MOMOKA.music.error.errors import MusicCogExceptionHandler
    from MOMOKA.music.plugins.audio_mixer import AudioMixer, MusicAudioSource
    from MOMOKA.music.plugins.voice_dave_patch import apply_dave_patch
except ImportError as e:
    print(f"[CRITICAL] MusicCog: 必須コンポーネントのインポートに失敗しました。エラー: {e}")
    Track = None
    extract_audio_data = None
    ensure_stream = None
    set_youtube_cookie_path = None
    UnsupportedMediaError = None
    MusicCogExceptionHandler = None
    AudioMixer = None
    MusicAudioSource = None
    apply_dave_patch = None

logger = logging.getLogger(__name__)

# Now Playing プログレスバーの更新間隔（秒）。Discord rate limit を考慮して 10 秒にする
PROGRESS_UPDATE_INTERVAL = 10


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
        self.last_now_playing_message: Optional[discord.Message] = None
        # プログレスバー定期更新タスク（未起動時は None）
        self.progress_update_task: Optional[asyncio.Task] = None

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

    def stop_progress_updater(self):
        # プログレス更新タスクが存在し、まだ完了していないか判定する
        if self.progress_update_task and not self.progress_update_task.done():
            # 定期更新ループをキャンセルする
            self.progress_update_task.cancel()
        # タスク参照をクリアする
        self.progress_update_task = None

    async def cleanup_voice_client(self):
        if self.cleanup_in_progress:
            return
        self.cleanup_in_progress = True
        try:
            # 切断時はプログレスバー更新を止める
            self.stop_progress_updater()
            # Now Playing のグレーアウト表示は MusicCog 側で行うため、ここでは参照のみクリアする
            self.last_now_playing_message = None

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
        # YouTube クッキーパスを yt-dlp ラッパーへ反映する
        self._apply_youtube_cookie_config()
        self.auto_leave_timeout = self.music_config.get('auto_leave_timeout', 10)
        self.max_queue_size = self.music_config.get('max_queue_size', 9000)
        self.max_guilds = self.music_config.get('max_guilds', 100000000)
        self.inactive_timeout_minutes = self.music_config.get('inactive_timeout_minutes', 30)
        self.global_connection_lock = asyncio.Lock()
        self.cleanup_task = None

    async def cog_load(self):
        # DAVE: 2.6 系向けモンキーパッチ / 2.7+ は discord.py ネイティブ + davey（voice_dave_patch 内で分岐）
        if apply_dave_patch:
            try:
                # ボイスWebSocketの IDENTIFY/RESUME/received_message をパッチ
                result = apply_dave_patch()
                if result:
                    logger.info("DAVE protocol patch applied successfully")
            except Exception as e:
                logger.warning(f"DAVE protocol patch failed (non-fatal): {e}")

        if not self.cleanup_task or self.cleanup_task.done():
            self.cleanup_task = self.cleanup_task_loop.start()
        logger.info("MusicCog loaded and cleanup task started")

    def _apply_youtube_cookie_config(self) -> None:
        """config の youtube_cookie_file を yt-dlp ラッパーへ反映する"""
        # ラッパーが未インポートの場合は何もしない
        if set_youtube_cookie_path is None:
            # 早期リターンする
            return
        # 未指定時は youtube_cookie.txt を既定として渡す（自動検出の優先候補になる）
        set_youtube_cookie_path(self.music_config.get('youtube_cookie_file', 'youtube_cookie.txt'))

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

    async def notify_admin_restart(self) -> None:
        """再起動前に Now Playing UI を管理者再起動メッセージへ切り替える。"""
        # 共有の再起動文言を遅延インポートする（循環参照回避）
        from MOMOKA.utilities.restart_notice import RESTART_NOTICE_MUSIC
        # Now Playing があるギルドだけを対象にする
        target_guild_ids = [
            guild_id
            for guild_id, state in list(self.guild_states.items())
            if state.last_now_playing_message is not None
        ]
        # 対象が無ければ何もしない
        if not target_guild_ids:
            # 早期リターン
            return
        # ギルドごとに UI を再起動表示へ更新する
        for guild_id in target_guild_ids:
            # 最新のギルド状態を取得する
            state = self._get_guild_state(guild_id)
            # 状態が消えていればスキップする
            if not state:
                # 次のギルドへ
                continue
            try:
                # プログレス更新を止めて編集競合を避ける
                state.stop_progress_updater()
                # LayoutView の終了表示分岐に入るため再生中トラックをクリアする
                state.current_track = None
                # 再生中フラグも下ろす
                state.is_playing = False
                # Now Playing を再起動文言付きグレーアウト UI に更新する
                await self._update_now_playing_message_ui(
                    guild_id,
                    finished_message=RESTART_NOTICE_MUSIC,
                )
            except Exception as e:
                # 1ギルドの失敗で他ギルド通知を止めない
                logger.warning(
                    "Guild %s: failed to notify admin restart on Now Playing: %s",
                    guild_id,
                    e,
                )

    @tasks.loop(minutes=5)
    async def cleanup_task_loop(self):
        try:
            current_time = datetime.now()
            inactive_threshold = timedelta(minutes=self.inactive_timeout_minutes)
            guilds_to_cleanup = []
            for gid, state in list(self.guild_states.items()):
                # 接続済みだが人間がいなくなっているギルドは自動退出（Bot残留の保険）
                if (
                    state.voice_client
                    and state.voice_client.is_connected()
                    and not self._vc_has_humans(state.voice_client.channel)
                ):
                    # タイマー無しでも取り残されないようスケジュールする
                    if not state.auto_leave_task or state.auto_leave_task.done():
                        # 無人なので自動退出を予約
                        self._schedule_auto_leave(gid)
                    # このギルドは disconnect 待ちなのでメモリ掃除リストには入れない
                    continue
                # 長時間非アクティブかつ未接続の状態を破棄対象にする
                if (
                    current_time - state.last_activity > inactive_threshold
                    and not state.is_playing
                    and (not state.voice_client or not state.voice_client.is_connected())
                ):
                    # 破棄リストへ追加
                    guilds_to_cleanup.append(gid)
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

    def get_active_vc_guild_count(self) -> int:
        """VC に接続中のギルド数を返す（GUI 稼働モニタ用）。"""
        # 接続済み voice_client を持つギルドだけを数える
        return sum(
            1
            for s in self.guild_states.values()
            if s.voice_client and s.voice_client.is_connected()
        )

    @staticmethod
    async def _to_durable_message(
        message: Optional[discord.Message],
    ) -> Optional[discord.Message]:
        """Interaction応答メッセージをチャンネル編集可能な通常Messageへ変換する。

        InteractionMessage.edit は webhook token（約15分で失効）に依存するため、
        長期更新する Now Playing は必ず channel Message 経由で編集する。
        """
        # メッセージが無い場合はそのまま返す
        if message is None:
            # 変換対象なし
            return None
        try:
            # Interaction/Webhook由来のメッセージは fetch で通常Messageに置き換える
            # （どちらも webhook token 依存で約15分後に 50027 になる）
            if isinstance(message, (discord.InteractionMessage, discord.WebhookMessage)):
                # GET /channels/.../messages/... で永続編集可能なMessageを取得する
                return await message.fetch()
            # 既に通常Messageならそのまま使う
            return message
        except Exception as e:
            # fetch失敗時は元メッセージを残し、呼び出し側でフォールバックする
            logger.warning(f"Failed to convert interaction message to durable message: {e}")
            # 失敗時は元オブジェクトを返す
            return message

    async def _send_ctx_message(
            self,
            ctx: commands.Context,
            *,
            content: Optional[str] = None,
            embed: Optional[discord.Embed] = None,
            view: Optional[discord.ui.View] = None,
            ephemeral: bool = False,
            **kwargs,
    ) -> Optional[discord.Message]:
        # ContextオブジェクトからInteractionを取得する（スラッシュコマンドの場合は存在する）
        interaction = getattr(ctx, "interaction", None)
        try:
            # Interactionが存在する場合の処理
            if interaction:
                # 送信用のパラメータ辞書を構築する
                kwargs_to_send = {
                    "content": content,
                    "embed": embed,
                    "ephemeral": ephemeral,
                    **kwargs,
                }
                # 表示するView（ボタンなど）が指定されているか判定する
                if view is not None:
                    # 送信用パラメータにViewを追加する
                    kwargs_to_send["view"] = view

                # インタラクションに対する最初の応答が完了していないか判定する
                if not interaction.response.is_done():
                    # 最初のレスポンスメッセージを送信する
                    await interaction.response.send_message(**kwargs_to_send)
                    try:
                        # スラッシュコマンドのオリジナル応答メッセージオブジェクトを取得して返す
                        return await interaction.original_response()
                    # メッセージ取得中に例外が発生した場合のハンドリング
                    except Exception:
                        # 取得できなかった場合はNoneを返す
                        return None
                else:
                    # すでに一度応答している場合は、followupを使ってメッセージを送信して返す
                    return await interaction.followup.send(**kwargs_to_send)
            # Interactionが存在しない（通常のテキストコマンドなどの）場合の処理
            else:
                # メッセージがephemeral（一時表示）指定されているか判定する
                if ephemeral:
                    # プレフィックスコマンドでは一時表示ができないため、ログを出力する
                    logger.debug("Ephemeral messages are not supported for prefix commands; sending normally.")
                # 通常のメッセージ送信を行い、そのメッセージオブジェクトを返す
                return await ctx.send(content=content, embed=embed, view=view, **kwargs)
        # 送信処理中にエラーが発生した場合のハンドリングを行う
        except Exception as e:
            # エラーが発生したギルド（サーバー）の情報を取得する
            guild = ctx.guild
            # ギルド情報がある場合は「ID (名称)」、ない場合は「Unknown guild」として文字列を構築する
            guild_info = f"{guild.id} ({guild.name})" if guild else "Unknown guild"
            # エラー内容をログに出力する
            logger.error(f"Guild {guild_info}: Response error: {e}")
            # エラー時はNoneを返す
            return None

    async def _send_response(self, ctx: commands.Context, message_key: str, ephemeral: bool = False,
                             **kwargs):
        content = self.exception_handler.get_message(message_key, **kwargs)
        await self._send_ctx_message(ctx, content=content, ephemeral=ephemeral)

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
        await self._send_ctx_message(ctx, content=error_message, ephemeral=True)

    async def _ensure_voice(self, ctx: commands.Context, connect_if_not_in: bool = True) -> Optional[
        discord.VoiceClient]:
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="サーバーの上限に達しています。", ephemeral=True)
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
                    # 接続前の短い待機で前回切断との競合を避ける
                    await asyncio.sleep(0.3)
                    # 自己deafは使わず接続し、直後にサーバー側スピーカーミュートへ切り替え
                    state.voice_client = await asyncio.wait_for(
                        user_voice.channel.connect(
                            timeout=30.0, reconnect=True, self_deaf=False),
                        timeout=35.0
                    )
                    # 緑アイコンのサーバー側スピーカーミュートを適用（権限不足時は自己deafへ）
                    await self._apply_server_deafen(ctx.guild)
                    # 接続成功をログに残す
                    logger.info(
                        f"Guild {ctx.guild.id} ({ctx.guild.name}): Connected to {user_voice.channel.name}")
                    # 接続時点ですでに人間がいなければ自動退出を予約する
                    if not self._vc_has_humans(state.voice_client.channel):
                        # 無人VCに留まらないようタイマーを開始する
                        self._schedule_auto_leave(ctx.guild.id)
                    # 接続済み VoiceClient を返す
                    return state.voice_client
                except Exception as e:
                    await self._handle_error(ctx, e)
                    state.voice_client = None
                    return None
            elif not vc:
                await self._send_response(ctx, "bot_not_in_voice_channel", ephemeral=True)
                return None
            return vc

    def mixer_finished_callback(self, error: Optional[Exception], guild_id: int,
                                expected_mixer=None):
        """
        ミキサー（voice_client.play()のafter）が終了した際のコールバック。
        mixer.stop()やvoice_client.stop()で呼ばれる。
        注意: オーディオスレッドから呼ばれるため、asyncio APIはrun_coroutine_threadsafeで実行。

        Args:
            error: 再生中に発生したエラー（あれば）
            guild_id: ギルドID
            expected_mixer: このコールバックが紐づくミキサー参照。
                旧ミキサーのコールバックが新ミキサーのstateを破壊するのを防止する。
        """
        if error:
            logger.error(f"Guild {guild_id}: Mixer unexpectedly finished with error: {error}")
        logger.info(f"Guild {guild_id}: Mixer has finished.")
        state = self._get_guild_state(guild_id)
        if not state or state._playing_next:
            return

        # 旧ミキサーのコールバックが新ミキサーのstateを破壊するのを防止
        # state.mixerが別のミキサーに差し替わっている場合はスキップ
        if expected_mixer is not None and state.mixer is not expected_mixer:
            logger.info(f"Guild {guild_id}: Mixer callback ignored (stale mixer, "
                        f"expected={id(expected_mixer)}, current={id(state.mixer) if state.mixer else 'None'})")
            return

        # ミキサーが既にクリーンアップ済み（意図的な停止）ならスキップ
        # _cleanup_idle_mixer等で事前にmixer=Noneにされている場合
        if state.mixer is None and not state.is_playing:
            logger.info(f"Guild {guild_id}: Mixer callback ignored (already cleaned up)")
            return

        state._playing_next = True

        # 終了時のトラック情報を保存
        finished_track = state.current_track
        # ミキサー参照をクリア
        state.mixer = None
        state.is_playing = False

        # LoopMode.ONEの場合は current_track を保持、それ以外は None にする
        if state.loop_mode != LoopMode.ONE:
            state.current_track = None

        state.reset_playback_tracking()

        # エラーがあればテキストチャンネルに通知
        if error:
            guild = self.bot.get_guild(guild_id)
            error_message = self.exception_handler.handle_error(error, guild)
            if state.last_text_channel_id:
                asyncio.run_coroutine_threadsafe(
                    self._send_background_message(state.last_text_channel_id, "error_message_wrapper",
                                                  error=error_message),
                    self.bot.loop
                )

        # LoopMode.ALLの場合はキューに再追加
        if finished_track and state.loop_mode == LoopMode.ALL:
            asyncio.run_coroutine_threadsafe(state.queue.put(finished_track), self.bot.loop)

        # 次の曲を再生（非同期タスクとしてスケジュール）
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
        """
        音楽ソースがミキサーから削除されたときに呼ばれる。
        ループモードやキューを考慮して次の曲を再生する。
        AudioMixerのon_source_removed_callbackから各ソース削除ごとに発火される。
        """
        state = self._get_guild_state(guild_id)
        # シーク中や既に次曲処理中の場合はスキップ
        if not state or state.is_seeking or state._playing_next:
            return

        state._playing_next = True

        try:
            # 終了したトラック情報を保存
            finished_track = state.current_track
            state.is_playing = False

            # LoopMode.ONEの場合は current_track を保持、それ以外は None にする
            if state.loop_mode != LoopMode.ONE:
                state.current_track = None

            state.reset_playback_tracking()

            # LoopMode.ALLの場合はキューに再追加
            if finished_track and state.loop_mode == LoopMode.ALL:
                await state.queue.put(finished_track)

            # 次の曲を再生（キューが空の場合はミキサーの停止も行う）
            await self._play_next_song(guild_id)
        finally:
            state._playing_next = False

    async def _cleanup_idle_mixer(self, state: GuildState):
        """
        ミキサーにソースが残っていない場合、ミキサーを停止してクリーンアップする。
        TTS等のソースが残っている場合はミキサーを維持する。
        これにより voice_client.is_playing() が False になり、TTS直接再生が可能になる。
        """
        if not state.mixer:
            return
        # ミキサーにソースが残っているか確認
        if state.mixer.has_sources():
            return
        # ソースなし→ミキサーを停止
        logger.info(f"Guild {state.guild_id}: Cleaning up idle mixer (no sources remaining)")
        mixer = state.mixer
        # 先にstate.mixerをNoneにしてmixer_finished_callbackでの二重処理を防止
        state.mixer = None
        # ミキサーを停止（read()がb''を返す→voice_clientのプレイヤーが停止）
        mixer.stop()

    async def _play_next_song(self, guild_id: int, seek_seconds: int = 0, play_msg: Optional[discord.Message] = None):
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
            # 再生対象が無いので再生状態をクリアする
            state.current_track = None
            # 再生中フラグを下ろす
            state.is_playing = False
            # 再生位置トラッキングを初期化する
            state.reset_playback_tracking()
            # キュー終了時はプログレスバー更新を停止する
            state.stop_progress_updater()
            # Now Playing メッセージが残っているか判定する
            if state.last_now_playing_message:
                # V2 LayoutView のグレーアウト UI に切り替える（旧 Embed 編集は使わない）
                await self._update_now_playing_message_ui(
                    guild_id,
                    finished_message=(
                        "⏹️ **Queue Finished**\n"
                        "All songs in the queue have been played."
                    ),
                )
            else:
                # メッセージが無い場合のみテキスト通知を送る
                if state.last_text_channel_id:
                    # キュー終了メッセージを送信する
                    await self._send_background_message(state.last_text_channel_id, "queue_ended")
            # キュー終了時：ミキサーにソースが残っていなければ停止してクリーンアップ
            # （TTS等が残っている場合はミキサーを維持する）
            await self._cleanup_idle_mixer(state)
            # 次曲再生処理を終了する
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
                # ストリームURLを最新値へ更新する
                track_to_play.stream_url = updated_track.stream_url
                # FFmpeg が CDN へアクセスできるよう HTTP ヘッダー（Cookie 含む）も同期する
                track_to_play.http_headers = updated_track.http_headers

            ffmpeg_before_opts = self.ffmpeg_before_options
            if seek_seconds > 0:
                # シーク指定位置から再生を開始するための開始オプションを構築する
                ffmpeg_before_opts = f"-ss {seek_seconds} {ffmpeg_before_opts}"

            # YouTube は yt-dlp パイプ再生（googlevideo 直読みは 403 になる）
            source = MusicAudioSource(
                track_to_play.stream_url,
                title=track_to_play.title,
                guild_id=guild_id,
                webpage_url=track_to_play.url,
                http_headers=getattr(track_to_play, "http_headers", None),
                executable=self.ffmpeg_path,
                before_options=ffmpeg_before_opts,
                options=self.ffmpeg_options,
            )

            if state.mixer is None:
                def on_source_removed(name: str):
                    """ソースが削除されたときのコールバック"""
                    if name == 'music':
                        asyncio.run_coroutine_threadsafe(self._on_music_source_removed(guild_id), self.bot.loop)
                
                state.mixer = AudioMixer(on_source_removed_callback=on_source_removed)

            # ミキサーをローカル変数に保持（awaitの間にstate.mixerがNoneに変更されるのを防止）
            # mixer_finished_callbackの旧ミキサー競合でstate.mixer=Noneにされても、
            # ローカル変数はオブジェクトを保持し続ける
            current_mixer = state.mixer

            await current_mixer.add_source('music', source, volume=state.volume)

            if not state.voice_client or not state.voice_client.is_connected():
                # ボイスクライアントが存在しない、あるいは切断されている場合は再生を中断する
                logger.info(f"Guild {guild_id}: voice_client is None or disconnected, aborting playback")
                # 再生中フラグを初期化する
                state.is_playing = False
                # 再生中トラック情報を初期化する
                state.current_track = None
                # 再生時間追跡情報を初期化する
                state.reset_playback_tracking()
                # アイドルミキサーをクリーンアップする
                await self._cleanup_idle_mixer(state)
                # 再生中メッセージのUIを最新化する（停止状態に更新する）
                await self._update_now_playing_message_ui(guild_id)
                # 切断時はプログレスバー更新も停止する
                state.stop_progress_updater()
                # 処理を正常終了する
                return

            if state.voice_client.source is not current_mixer:
                # 旧AudioPlayerが残留している場合（_cleanup_idle_mixer後のレース等）は
                # 明示的に停止して新しいミキサーで再生開始
                if state.voice_client.is_playing():
                    logger.info(f"Guild {guild_id}: Stopping stale AudioPlayer before starting new mixer")
                    state.voice_client.stop()
                # lambdaにミキサー参照をキャプチャし、mixer_finished_callbackで照合する
                # これにより旧ミキサーのコールバックが新ミキサーのstateを破壊するのを防止
                state.voice_client.play(
                    current_mixer,
                    after=lambda e, m=current_mixer: self.mixer_finished_callback(e, guild_id, m)
                )
                logger.info(f"Guild {guild_id}: Started new AudioPlayer with mixer {id(current_mixer)}")

            # 旧ミキサーのコールバックでstateが破壊された場合の復元処理
            # （mixer_finished_callbackのミキサーID照合で防止されるが、念のため）
            if state.mixer is None and current_mixer is not None:
                logger.warning(f"Guild {guild_id}: state.mixer was cleared during playback setup, restoring")
                state.mixer = current_mixer
            if not state.is_playing:
                logger.warning(f"Guild {guild_id}: state.is_playing was cleared during playback setup, restoring")
                state.is_playing = True
            if state.current_track is None and track_to_play is not None and not is_seek_operation:
                logger.warning(f"Guild {guild_id}: state.current_track was cleared during playback setup, restoring")
                state.current_track = track_to_play

            if is_seek_operation:
                state.is_seeking = False

            # シーク操作以外では Now Playing UI を更新する
            # 次曲移行時も最初の /play 応答メッセージを編集して維持し、誰が再生開始したか分かるようにする
            if state.last_text_channel_id and not is_seek_operation:
                # 再生コントロール一体型UI（LayoutView）を構築する
                view = MusicControllerView(self, guild_id)
                # 送信先チャンネルを取得する
                channel = self.bot.get_channel(state.last_text_channel_id)
                # チャンネルが取れた場合のみ UI を更新する
                if channel:
                    try:
                        # 初回 /play の応答メッセージが渡されているか判定する
                        if play_msg:
                            # webhook期限切れ回避のためチャンネル経由 Message へ変換する
                            play_msg = await self._to_durable_message(play_msg)
                            # /play 応答を Now Playing UI に編集する
                            await play_msg.edit(content=None, embed=None, view=view)
                            # 編集したメッセージを最新の Now Playing として保存する
                            state.last_now_playing_message = play_msg
                        elif state.last_now_playing_message:
                            # 次曲など: 既存の /play 起点メッセージを編集して返信関係を維持する
                            target_message = await self._to_durable_message(
                                state.last_now_playing_message
                            )
                            # 同じメッセージ上で次曲の UI に差し替える
                            await target_message.edit(content=None, embed=None, view=view)
                            # 参照を最新の Message オブジェクトへ更新する
                            state.last_now_playing_message = target_message
                        else:
                            # 既存メッセージが無い場合のみ新規投稿する（フォールバック）
                            state.last_now_playing_message = await channel.send(view=view)
                        # Now Playing 表示後にプログレスバーの定期更新を開始する
                        self._start_progress_updater(guild_id)
                    # 送信または編集処理中に例外が発生した場合のハンドリング
                    except Exception as e:
                        # 編集失敗時は新規送信で復旧を試みる
                        logger.error(f"Failed to update now playing message: {e}")
                        try:
                            # 壊れた参照を捨てる
                            state.last_now_playing_message = None
                            # チャンネルへ新規 Now Playing を投稿する
                            state.last_now_playing_message = await channel.send(view=view)
                            # 復旧後もプログレス更新を開始する
                            self._start_progress_updater(guild_id)
                        except Exception as send_error:
                            # 復旧にも失敗した旨をログへ残す
                            logger.error(
                                f"Failed to recover now playing message: {send_error}"
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
            # 再生エラー時はプログレスバー更新を停止する
            state.stop_progress_updater()
            asyncio.create_task(self._play_next_song(guild_id))

    @staticmethod
    def _vc_has_humans(channel: Optional[discord.abc.Connectable]) -> bool:
        # チャンネル未取得時は人間なしとして扱う
        if channel is None:
            # 退出判定側で「無人」とみなす
            return False
        # members を持つチャンネルのみ人間有無を判定する
        members = getattr(channel, "members", None)
        # members が取れない場合も無人扱い（安全側に倒す）
        if members is None:
            # 退出してハング回避を優先する
            return False
        # Bot以外（人間）が1人でもいれば True
        return any(not m.bot for m in members)

    async def _apply_server_deafen(self, guild: discord.Guild) -> None:
        """サーバー側スピーカーミュート（緑）を適用。権限が無ければ自己deafへフォールバック。"""
        # 自Botの Member を取得する
        me = guild.me
        # Member が取れない場合は何もしない
        if me is None:
            # 早期リターン
            return
        try:
            # サーバー側 deafen（緑色アイコン）で自身をスピーカーミュートする
            await me.edit(deafen=True, reason="Music bot: server deafen while connected")
            # 成功時は自己deafが残っていても問題ないが、見た目をサーバー側に寄せる
            logger.debug("Guild %s: Applied server deafen to bot", guild.id)
        except (discord.Forbidden, discord.HTTPException) as e:
            # Mute/Deafen Members 権限不足などで失敗した場合は自己deafへフォールバック
            logger.warning(
                "Guild %s: Server deafen failed (%s); falling back to self_deaf",
                guild.id,
                e,
            )
            try:
                # 接続中チャンネルに対して自己スピーカーミュートを立てる
                if me.voice and me.voice.channel:
                    # Voice Identify 相当の自己deafフラグを送る
                    await guild.change_voice_state(
                        channel=me.voice.channel, self_mute=False, self_deaf=True)
            except Exception as fallback_error:
                # フォールバック失敗もログのみ（再生自体は継続させる）
                logger.warning(
                    "Guild %s: self_deaf fallback also failed: %s",
                    guild.id,
                    fallback_error,
                )

    def _schedule_auto_leave(self, guild_id: int):
        # 対象ギルドの再生状態を取得する
        state = self._get_guild_state(guild_id)
        # 状態が無ければスケジュールできない
        if not state:
            # 早期リターン
            return
        # 既存タイマーがあればキャンセルして差し替える（再スケジュール漏れ防止）
        if state.auto_leave_task and not state.auto_leave_task.done():
            # 進行中の自動退出タスクを中断する
            state.auto_leave_task.cancel()
        # まだVCに接続しているときだけ新しいタイマーを起動する
        if state.voice_client and state.voice_client.is_connected():
            # 無人確認付きの自動退出コルーチンを起動する
            state.auto_leave_task = asyncio.create_task(self._auto_leave_coroutine(guild_id))

    async def _auto_leave_coroutine(self, guild_id: int):
        try:
            # 設定された猶予秒だけ待機する（直後の再入室に対応）
            await asyncio.sleep(self.auto_leave_timeout)
        except asyncio.CancelledError:
            # 人間が戻った等でキャンセルされた場合はそのまま終了
            raise
        # 待機後に最新のギルド状態を再取得する
        state = self._get_guild_state(guild_id)
        # 状態または接続が無い場合は何もしない
        if not state or not state.voice_client or not state.voice_client.is_connected():
            # 既に切断済み
            return
        # 人間がいまだ居ない場合のみ退出する（Bot同士だけの残留を防ぐ）
        if self._vc_has_humans(state.voice_client.channel):
            # 人間が戻っているので退出不要
            return
        # テキストチャンネルがあれば退出メッセージを送る
        if state.last_text_channel_id:
            # バックグラウンド通知を送る
            await self._send_background_message(state.last_text_channel_id, "auto_left_empty_channel")
        # disconnect単体ではなく状態ごとクリーンアップして再接続ハングを防ぐ
        await self._cleanup_guild_state(guild_id)

    async def _cleanup_guild_state(self, guild_id: int):
        # 破棄前に状態を取得する（UI 更新に必要）
        state = self._get_guild_state(guild_id)
        # 状態が存在するか判定する
        if state:
            # ギルド破棄前にプログレスバー更新を停止する
            state.stop_progress_updater()
            # 切断前に再生中トラックをクリアしてグレーアウト表示できるようにする
            state.current_track = None
            # 再生中フラグも下ろす
            state.is_playing = False
            # Now Playing メッセージがある場合は切断用のグレーアウト UI に更新する
            if state.last_now_playing_message:
                # V2 LayoutView で Playback Ended 表示に切り替える
                await self._update_now_playing_message_ui(
                    guild_id,
                    finished_message=(
                        "⏹️ **Playback Ended**\n"
                        "The bot has disconnected from the voice channel."
                    ),
                )
        # ギルド状態を辞書から取り出す
        state = self.guild_states.pop(guild_id, None)
        # 取り出した状態が存在するか判定する
        if state:
            # ボイス接続とミキサーをクリーンアップする
            await state.cleanup_voice_client()
            # 自動退出タスクが動いていればキャンセルする（自分自身以外）
            if (
                state.auto_leave_task
                and not state.auto_leave_task.done()
                and state.auto_leave_task is not asyncio.current_task()
            ):
                # 他経路から呼ばれた場合のみタイマーを止める
                state.auto_leave_task.cancel()
            # キューを空にする
            await state.clear_queue()
            # ギルド名解決用にギルドオブジェクトを取得する
            guild = self.bot.get_guild(guild_id)
            # クリーンアップ完了をログに残す
            logger.info(f"Guild {guild_id} ({guild.name if guild else ''}): State cleaned up")

    def _start_progress_updater(self, guild_id: int):
        # 対象ギルドの再生状態を取得する
        state = self._get_guild_state(guild_id)
        # 状態が無ければ開始できないので終了する
        if not state:
            # 早期リターン
            return
        # 既存の更新タスクがあれば先に止める（二重起動防止）
        state.stop_progress_updater()
        # 10秒間隔のプログレス更新ループをバックグラウンドで開始する
        state.progress_update_task = asyncio.create_task(
            self._progress_updater_loop(guild_id),
            name=f"music_progress_{guild_id}",
        )

    async def _progress_updater_loop(self, guild_id: int):
        try:
            # 再生中は一定間隔で Now Playing UI を更新し続ける
            while True:
                # Discord rate limit を避けるため更新間隔だけ待機する
                await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
                # 待機後に最新のギルド状態を再取得する
                state = self._get_guild_state(guild_id)
                # 状態・再生・メッセージのいずれかが無効ならループを終了する
                if (
                    not state
                    or not state.is_playing
                    or not state.current_track
                    or not state.last_now_playing_message
                ):
                    # 更新対象が無いのでループを抜ける
                    break
                # 一時停止中は位置が変わらないため API 呼び出しをスキップする
                if state.is_paused:
                    # 次の間隔まで待つ
                    continue
                # プログレスバー込みで Now Playing UI を再描画する
                await self._update_now_playing_message_ui(guild_id)
        except asyncio.CancelledError:
            # タスクキャンセルは正常終了として扱う
            pass
        except Exception as e:
            # 想定外エラーをログに残し、ループは終了する
            logger.error(f"Guild {guild_id}: Progress updater error: {e}", exc_info=True)
        finally:
            # ループ終了時にタスク参照をクリアする（生存中の state がある場合のみ）
            state = self._get_guild_state(guild_id)
            # 状態が残っており、かつ自分自身のタスク参照ならクリアする
            if state and state.progress_update_task is asyncio.current_task():
                # 参照を None にして再利用可能にする
                state.progress_update_task = None

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"{self.bot.user.name} の MusicCog が正常にロードされました。")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        # 自BotがVCから切断されたらギルド状態を破棄する
        if member.id == self.bot.user.id and before.channel and not after.channel:
            # 再生状態・VoiceClient・自動退出タスクをまとめて掃除
            await self._cleanup_guild_state(member.guild.id)
            # 以降の無人判定は不要
            return

        # 対象ギルドIDを取得する
        guild_id = member.guild.id
        # Music 状態が無いギルドは無視する
        if guild_id not in self.guild_states:
            # 早期リターン
            return

        # ギルド再生状態を取得する
        state = self._get_guild_state(guild_id)
        # 未接続なら自動退出判定の対象外
        if not state or not state.voice_client or not state.voice_client.is_connected():
            # 早期リターン
            return

        # Botが現在いるVCを基準にする
        current_vc_channel = state.voice_client.channel
        # 自BotのVCと無関係な移動は無視する
        if before.channel != current_vc_channel and after.channel != current_vc_channel:
            # 早期リターン
            return

        # 人間がいなければ自動退出を（再）スケジュールする
        # NOTE: 以前は「タスク未完了なら再スケジュールしない」条件があり、
        # cancel直後に done() が遅れるとBot同士残留バグになっていた
        if not self._vc_has_humans(current_vc_channel):
            # タイムアウト付きで退出コルーチンを起動／差し替え
            self._schedule_auto_leave(guild_id)
        elif state.auto_leave_task and not state.auto_leave_task.done():
            # 人間が戻ったので予定されていた自動退出を取り消す
            state.auto_leave_task.cancel()

    @commands.hybrid_command(name="play", description="曲を再生またはキューに追加します。")
    @app_commands.describe(query="再生したい曲のタイトル、またはURL")
    async def play(self, ctx: commands.Context, *, query: str):
        # レスポンス送信を保留（defer）にし、処理がタイムアウトしないようにする
        await ctx.defer()

        # ギルド固有の再生状態クラスを取得する
        state = self._get_guild_state(ctx.guild.id)
        # 再生状態クラスが正しく取得できたか（上限に達していないか）判定する
        if not state:
            # 取得に失敗した場合は、エラーメッセージを送信して処理を終了する
            await self._send_ctx_message(ctx, content="サーバーの上限に達しています。", ephemeral=True)
            # コマンドの実行を終了する
            return

        # ボイスチャンネルへの接続を確認、または新規接続を行い、ボイスクライアントを取得する
        vc = await self._ensure_voice(ctx, connect_if_not_in=True)
        # ボイスチャンネルに接続できなかったか判定する
        if not vc:
            # 接続失敗時はこれ以上処理を進めず終了する
            return

        # キューのサイズが設定上の上限値に達しているか判定する
        if state.queue.qsize() >= self.max_queue_size:
            # キュー上限到達のエラーレスポンスを返信して終了する
            await self._send_response(ctx, "max_queue_size_reached",
                                      max_size=self.max_queue_size)
            # コマンドの実行を終了する
            return

        # コマンド開始時点での再生状態、または読み込み状態を取得してフラグに保持する
        was_playing = state.is_playing or state.is_loading
        # 読み込み状態フラグをTrueにする
        state.is_loading = True

        # 検索中のメッセージオブジェクトを初期化する
        searching_msg = None
        try:
            # 検索開始した事実を示す一時的メッセージを送信し、オブジェクトを保持する
            searching_msg = await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("searching_for_song", query=query),
            )

            # yt-dlp等を用いて検索クエリから音声情報を抽出する
            extracted_media = await extract_audio_data(query, shuffle_playlist=False)

            # 音声情報の抽出に失敗した（結果が空だった）か判定する
            if not extracted_media:
                # 検索メッセージが取得できていれば、検索結果なしのテキストに書き換える
                if searching_msg:
                    # 検索メッセージの内容を更新する
                    await searching_msg.edit(content=self.exception_handler.get_message("search_no_results", query=query))
                else:
                    # メッセージが無い場合は、新規に検索結果なしメッセージを送信する
                    await self._send_ctx_message(
                        ctx,
                        content=self.exception_handler.get_message("search_no_results", query=query),
                    )

                # コマンドの実行を終了する
                return

            # 抽出されたデータがリスト（プレイリスト）であるか判定し、トラックリストに変換する
            tracks = extracted_media if isinstance(extracted_media, list) else [extracted_media]
            # 追加された曲数と、最初のトラックへの参照を初期化する
            added_count, first_track = 0, None

            # 抽出されたトラックのリストを走査する
            for track in tracks:
                # キューが上限数に達していないか走査のたびに判定する
                if state.queue.qsize() < self.max_queue_size:
                    # トラックのリクエストユーザーIDを設定する
                    track.requester_id = ctx.author.id
                    # ストリームURLを初期状態としてNoneにする
                    track.stream_url = None
                    # トラックオブジェクトを非同期の再生キューに追加する
                    await state.queue.put(track)
                    # 最初のトラックである（added_countが0）か判定する
                    if added_count == 0:
                        # 最初のトラックへの参照を保持する
                        first_track = track
                    # 追加曲数のカウントを1加算する
                    added_count += 1
                # キュー上限に達したため走査を終了する
                else:
                    # すでに曲追加中の場合、キュー上限エラーメッセージを送信する
                    await self._send_ctx_message(
                        ctx,
                        content=self.exception_handler.get_message("max_queue_size_reached",
                                                                  max_size=self.max_queue_size)
                    )

                    # ループ処理を終了する
                    break

            # すでに何らかの音楽が再生中であったか判定する
            if was_playing:
                # 複数曲（プレイリスト）がキューに追加されたか判定する
                if added_count > 1:
                    # 追加完了の文言を取得する
                    added_playlist_content = self.exception_handler.get_message("added_playlist_to_queue", count=added_count)
                    # 検索開始メッセージが保持されているか判定する
                    if searching_msg:
                        # 既存の検索開始メッセージをキュー追加完了メッセージに編集する
                        await searching_msg.edit(content=added_playlist_content)
                    else:
                        # メッセージがない場合は新規にキュー追加メッセージを送信する
                        await self._send_ctx_message(ctx, content=added_playlist_content)

                # 1曲だけが追加され、かつそのトラックオブジェクトが有効か判定する
                elif added_count == 1 and first_track:
                    # 1曲追加完了の文言をフォーマットして取得する
                    added_song_content = self.exception_handler.get_message("added_to_queue",
                                                                           title=first_track.title,
                                                                           duration=format_duration(first_track.duration),
                                                                           requester_display_name=ctx.author.display_name)
                    # 検索開始メッセージが保持されているか判定する
                    if searching_msg:
                        # 既存の検索開始メッセージを1曲追加メッセージに編集する
                        await searching_msg.edit(content=added_song_content)
                    else:
                        # メッセージがない場合は新規に1曲追加メッセージを送信する
                        await self._send_ctx_message(ctx, content=added_song_content)

            # 再生中ではない（この play コマンドで新規再生を開始する）か判定する
            if not was_playing:
                # _play_next_songを実行し、searching_msgを再生メッセージとして流用・編集する
                await self._play_next_song(ctx.guild.id, play_msg=searching_msg)

        # 検索または追加処理中に例外が発生した場合のハンドリングを行う
        except Exception as e:
            # 例外内容を解析し、ギルド用のエラーメッセージを取得する
            error_message = self.exception_handler.handle_error(e, ctx.guild)
            # エラー文言をフォーマットして取得する
            wrapped_error_msg = self.exception_handler.get_message("error_message_wrapper", error=error_message)
            # 検索中メッセージが存在するか判定する
            if searching_msg:
                # 検索メッセージをエラーメッセージに編集する
                await searching_msg.edit(content=wrapped_error_msg)
            else:
                # メッセージがない場合は、新規にエラーメッセージを送信する
                await self._send_ctx_message(ctx, content=wrapped_error_msg)

        # 最終的に必ず実行するクリーンアップ処理
        finally:
            # 読み込み状態フラグをFalseに戻す
            state.is_loading = False

    @commands.hybrid_command(name="seek", description="再生位置を指定した時刻に移動します。")
    @app_commands.describe(time="移動先の時刻 (例: 1:30 または 90 秒)")
    async def seek(self, ctx: commands.Context, *, time: str):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

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

        # シーク操作: is_seekingフラグでコールバックからの二重処理を防止
        state.is_seeking = True
        try:
            # _play_next_songがseek_seconds > 0で呼ばれると、同じトラックをシーク位置から再生
            # add_source('music', new_source) が旧ソースを自動的に置き換えるため、
            # 旧ソースの明示的な削除は不要
            await self._play_next_song(ctx.guild.id, seek_seconds=seek_seconds)
            await self._send_response(ctx, "seeked_to_position", position=format_duration(seek_seconds))
        finally:
            state.is_seeking = False

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
        await self._update_now_playing_message_ui(ctx.guild.id)

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
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="skip", description="再生中の曲をスキップします。")
    async def skip(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)
            return

        await ctx.defer()
        vc = await self._ensure_voice(ctx, connect_if_not_in=False)
        if not vc or not state.current_track:
            await self._send_response(ctx, "nothing_to_skip", ephemeral=True)
            return

        skipped_title = state.current_track.title
        await self._send_response(ctx, "skipped_song", title=skipped_title)

        # ミキサーから音楽ソースを削除
        # remove_sourceのコールバックで_on_music_source_removedが呼ばれ、次の曲が自動再生される
        if state.mixer:
            await state.mixer.remove_source('music')
        elif state.voice_client and state.voice_client.is_playing():
            # ミキサーなしで再生中の場合（フォールバック）
            state.voice_client.stop()

    @commands.hybrid_command(name="stop", description="再生を停止し、キューをクリアします。")
    async def stop(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

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
        # stop 時はプログレスバー更新を停止する
        state.stop_progress_updater()
        await self._send_response(ctx, "stopped_playback")
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="leave", description="ボットをボイスチャンネルから切断します。")
    async def leave(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

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
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        state.update_last_text_channel(ctx.channel.id)
        if state.queue.empty() and not state.current_track:
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("queue_empty"),
                ephemeral=True,
            )

            return

        items_per_page = 10
        queue_list = list(state.queue._queue)
        total_items = len(queue_list)
        total_pages = math.ceil(len(queue_list) / items_per_page) if len(queue_list) > 0 else 1

        async def get_page_embed(page_num: int):
            embed = discord.Embed(
                title=self.exception_handler.get_message("queue_title",
                                                         count=total_items + (1 if state.current_track else 0)),
                color=discord.Color.from_rgb(79, 194, 255)
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
            await self._send_ctx_message(ctx, embed=await get_page_embed(current_page))

        else:
            view = get_queue_view(current_page, total_pages, ctx.author.id)
            await self._send_ctx_message(ctx, embed=await get_page_embed(current_page), view=view)

    @commands.hybrid_command(name="nowplaying", description="現在再生中の曲の情報を表示します。")
    async def nowplaying(self, ctx: commands.Context):
        # ギルドの再生状態オブジェクトを取得する
        state = self._get_guild_state(ctx.guild.id)
        # 再生状態オブジェクトが存在しない、または現在再生中のトラックが無いか判定する
        if not state or not state.current_track:
            # 再生中の曲がない旨を示すエラーレスポンスを送信する
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("now_playing_nothing"),
                ephemeral=True,
            )

            # コマンドの実行を終了する
            return

        # コマンドのレスポンス保留（defer）を開始する
        await ctx.defer()
        
        # 既に Now Playing コントロールメッセージが存在しているか判定する
        if state.last_now_playing_message:
            try:
                # チャット上の古いコントロールメッセージを削除する
                await state.last_now_playing_message.delete()
            # メッセージ削除中に発生した例外のハンドリング
            except Exception:
                # 削除失敗時はパスする
                pass
            # 古いメッセージへの参照を初期化する
            state.last_now_playing_message = None

        # 一体型UIが統合された LayoutView オブジェクトを構築する
        view = MusicControllerView(self, ctx.guild.id)
        
        # 新しい Now Playing リッチUIを送信する（embedは指定しない）
        msg = await self._send_ctx_message(ctx, view=view)
        # 送信が成功してメッセージオブジェクトが返ってきたか判定する
        if msg:
            # webhook期限切れ回避のため、チャンネル経由のMessageへ変換して保存する
            state.last_now_playing_message = await self._to_durable_message(msg)
            # nowplaying 再表示時もプログレスバー定期更新を開始する
            self._start_progress_updater(ctx.guild.id)

    def _create_now_playing_embed(self, state: GuildState, track: Track) -> discord.Embed:
        # 再生が一時停止中であるか判定して、表示用アイコンを設定する
        status_icon = "⏸️" if state.is_paused else "▶️"
        # 再生が一時停止中であるか判定して、表示用のステータス文字列を設定する
        status_text = "Paused" if state.is_paused else "Playing"
        
        # リクエストユーザーのメンション文字列を初期化する
        requester_mention = "Unknown"
        # トラック情報にリクエストユーザーIDが存在するか判定する
        if track.requester_id:
            # ユーザーIDをDiscordのメンション形式に変換して設定する
            requester_mention = f"<@{track.requester_id}>"
            
        # Embedオブジェクトを作成し、タイトルとブランドカラー（水色）を設定する
        embed = discord.Embed(
            title=f"{status_icon} Now {status_text}",
            color=discord.Color.from_rgb(79, 194, 255)
        )
        # Embedのメイン説明文として、再生中のトラックタイトルをリンク付きで設定する
        embed.description = f"**[{track.title}]({track.url})**"
        
        # 現在の再生位置（秒）を取得する
        current_pos = state.get_current_position()
        # 再生位置と曲の長さからプログレスバー文字列を生成する
        progress_bar = self._create_progress_bar(current_pos, track.duration)
        # 現在の再生時間と曲の総時間をフォーマットした文字列を構築する
        duration_str = f"`{format_duration(current_pos)}` / `{format_duration(track.duration)}`"
        # プログレスバーと再生時間を表示するフィールドをEmbedに追加する
        embed.add_field(name="Progress", value=f"{progress_bar}\n{duration_str}", inline=False)
        
        # アップローダー/チャンネル名が定義されているか判定し、無ければ「Unknown」にする
        uploader_val = track.uploader if track.uploader else "Unknown"
        # アップローダー名を記載するフィールドをEmbedに追加する
        embed.add_field(name="Channel / Uploader", value=uploader_val, inline=True)
        # リクエストユーザーを記載するフィールドをEmbedに追加する
        embed.add_field(name="Requested By", value=requester_mention, inline=True)
        # 現在有効になっているループモード名を表示するフィールドをEmbedに追加する
        embed.add_field(name="Loop Mode", value=f"`{state.loop_mode.name.lower()}`", inline=True)
        
        # 現在のキューに残っている曲数を取得する
        remaining = state.queue.qsize()
        # 残り曲数を表示するフィールドをEmbedに追加する
        embed.add_field(name="Queue Status", value=f"{remaining} songs in queue", inline=True)
        
        # サムネイル画像のURLが有効であり、かつ文字列「None」ではないか判定する
        if track.thumbnail and track.thumbnail.strip() and track.thumbnail != "None":
            # サムネイルURLをEmbedの右上サムネイル画像として登録する
            embed.set_thumbnail(url=track.thumbnail)
            
        # フッターにMOMOKAミュージックプレイヤーのクレジットを設定する
        embed.set_footer(text="MOMOKA Music Player")
        # 構築完了したEmbedオブジェクトを返す
        return embed

    async def _update_now_playing_message_ui(
        self,
        guild_id: int,
        finished_message: Optional[str] = None,
    ):
        # ギルドの再生状態オブジェクトを取得する
        state = self._get_guild_state(guild_id)
        # 再生状態、または直前の再生中メッセージが存在しない場合は処理を中断する
        if not state or not state.last_now_playing_message:
            # 早期リターン
            return

        # 最新の再生状態を元に一体型UI（LayoutView）を新規構築する
        view = MusicControllerView(
            self,
            guild_id,
            finished_message=finished_message,
        )
        # 編集対象メッセージをローカル変数に保持する
        target_message = state.last_now_playing_message

        try:
            # InteractionMessageのままならチャンネル経由Messageへ変換する
            target_message = await self._to_durable_message(target_message)
            # 変換結果を状態へ反映する
            state.last_now_playing_message = target_message
            # 古いメッセージの Embed をクリアしつつ、新しい V2レイアウトでメッセージを上書き編集する
            await target_message.edit(embed=None, view=view)
        except discord.HTTPException as e:
            # Invalid Webhook Token（50027）は期限切れInteraction応答の典型なので復旧を試みる
            if e.code == 50027:
                recovered = await self._recover_now_playing_message(state, view)
                # 復旧できなければ参照を捨ててエラー連打を防ぐ
                if not recovered:
                    # 期限切れ参照を破棄する
                    state.last_now_playing_message = None
                    # プログレス更新も止めて無駄なAPI呼び出しを防ぐ
                    state.stop_progress_updater()
                    # 復旧失敗を警告ログに残す
                    logger.warning(
                        f"Guild {guild_id}: Now Playing webhook token expired; "
                        "cleared message reference to stop update errors."
                    )
            else:
                # それ以外のHTTPエラーは通常どおり記録する
                logger.error(f"Failed to update now playing message UI: {e}")
        # 編集処理中に例外が発生した場合のハンドリング
        except Exception as e:
            # エラーログを出力する
            logger.error(f"Failed to update now playing message UI: {e}")

        # 再生中の曲がなくなっている（再生が終了または停止している）か判定する
        if not state.current_track:
            # メッセージの参照をクリアして、次の再生に備える
            state.last_now_playing_message = None

    async def _recover_now_playing_message(
        self,
        state: "GuildState",
        view: "MusicControllerView",
    ) -> bool:
        """期限切れwebhookのNow Playingメッセージをチャンネル再送で復旧する。"""
        # 送信先チャンネルIDが無ければ復旧不可
        if not state.last_text_channel_id:
            # 復旧失敗
            return False
        # チャンネルオブジェクトを取得する
        channel = self.bot.get_channel(state.last_text_channel_id)
        # テキストチャンネル以外は復旧対象外
        if not isinstance(channel, discord.TextChannel):
            # 復旧失敗
            return False
        try:
            # 古い期限切れメッセージは削除を試みる（失敗しても続行）
            if state.last_now_playing_message:
                try:
                    # 期限切れメッセージを削除する
                    await state.last_now_playing_message.delete()
                except Exception:
                    # 削除失敗は無視して再送へ進む
                    pass
            # チャンネル経由で新しい Now Playing を送信する（webhook非依存）
            state.last_now_playing_message = await channel.send(view=view)
            # 復旧成功
            return True
        except Exception as e:
            # 再送失敗をログに残す
            logger.error(f"Failed to recover now playing message: {e}")
            # 復旧失敗
            return False

    def _create_progress_bar(self, current: int, total: int, length: int = 20) -> str:
        # 総時間が無効な場合は空のバーを返す
        if total <= 0:
            # 未確定長の曲向けにプレースホルダを返す
            return "─" * length
        # 0.0〜1.0 に正規化した進捗率を計算する
        progress = min(max(current, 0) / total, 1.0)
        # バー末尾に ○ を残すため、塗りつぶしは最大 length-1 にする
        filled = min(int(length * progress), length - 1)
        # 塗りつぶし・現在位置・残りを結合してプログレスバー文字列を作る
        bar = "━" * filled + "○" + "─" * (length - filled - 1)
        # 生成したバー文字列を返す
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
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        if index < 1:
            await self._send_response(ctx, "invalid_queue_number", ephemeral=True)
            return

        if state.queue.empty():
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("queue_empty"),
                ephemeral=True,
            )

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
            await self._send_ctx_message(ctx, content="音量は0から200の間で指定してください。", ephemeral=True)

            return

        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

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
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        await ctx.defer()
        mode_map = {"off": LoopMode.OFF, "one": LoopMode.ONE, "all": LoopMode.ALL}
        mode_val = mode.lower()
        if mode_val not in mode_map:
            await self._send_ctx_message(
                ctx,
                content="無効なモードです。`off`, `one`, `all`のいずれかを指定してください。",
                ephemeral=True,
            )

            return
        state.loop_mode = mode_map.get(mode_val, LoopMode.OFF)
        state.update_activity()
        await self._send_response(ctx, f"loop_{mode_val}")

    @commands.hybrid_command(name="join", description="ボットをあなたのいるボイスチャンネルに接続します。")
    async def join(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if await self._ensure_voice(ctx, connect_if_not_in=True):
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("already_connected"),
                ephemeral=True,
            )

    @commands.hybrid_group(name="reload", description="各種機能を再読み込みします。 / Reloads various features.")
    async def reload(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await self._send_ctx_message(
                ctx,
                content='このコマンドにはサブコマンドが必要です。 (例: `reload music_cog`)',
                ephemeral=True,
            )

    @reload.command(name="music_cog", description="音楽Cogを再読み込みして、問題をリセットします。/ Reloads the music cog to fix issues.")
    async def reload_music_cog(self, ctx: commands.Context):
        # 処理がタイムアウトしないようレスポンス送信を保留にし、他者に見えないエフェメラルに設定する
        await ctx.defer(ephemeral=True)
        # 現在のモジュール（拡張機能）名を取得する
        module_name = self.__module__
        # Cogの再読み込み試行をログに出力する
        logger.info(f"音楽Cog ({module_name}) の再読み込みを試みます。リクエスト者: {ctx.author}")

        try:
            # ボットに対して拡張機能（このモジュール自身）の再読み込みを実行する
            await self.bot.reload_extension(module_name)
            # 再読み込み成功の旨をログに出力する
            logger.info(f"音楽Cog ({module_name}) の再読み込みに成功しました。")
            # 完了のメッセージをエフェメラル（一時表示）設定で送信する
            await ctx.send(
                "🎵 音楽機能の再読み込みが完了しました。\n🎵 Music feature has been successfully reloaded.",
                ephemeral=True
            )
        # 再読み込み処理中に何らかの例外が発生した場合のハンドリングを行う
        except Exception as e:
            # エラーの詳細をログ（スタックトレース付き）に出力する
            logger.error(f"音楽Cog ({module_name}) の再読み込み中にエラーが発生しました: {e}", exc_info=True)
            # エラーが発生した旨とエラーの詳細内容をエフェメラル設定で送信する
            await ctx.send(
                f"❌ 音楽機能の再読み込み中にエラーが発生しました。\n❌ An error occurred while reloading the music feature.\n```py\n{type(e).__name__}: {e}\n```",
                ephemeral=True
            )

    @reload_music_cog.error
    async def reload_music_cog_error(self, ctx: commands.Context, error: commands.CommandError):
        await self.exception_handler.handle_generic_command_error(ctx, error)
class MusicControllerView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: MusicCog,
        guild_id: int,
        finished_message: Optional[str] = None,
    ):
        # タイムアウトなしで初期化する
        super().__init__(timeout=None)
        # 親のMusicCogインスタンスを保持する
        self.cog = cog
        # 対象のギルドIDを保持する
        self.guild_id = guild_id
        # 停止/終了時に表示するカスタム文言（無ければデフォルト文）
        self.finished_message = finished_message
        # UI（V2コンポーネント）の構築処理を実行する
        self.rebuild_ui()

    def rebuild_ui(self):
        # 既存のビューアイテムをすべてクリアする
        self.clear_items()

        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 再生状態、または再生中のトラックが存在しないか判定する
        if not state or not state.current_track:
            # グレーのアクセントカラーでコンテナを生成する
            container = discord.ui.Container(accent_color=discord.Color.light_grey())
            # 呼び出し元指定の終了文言があれば使い、無ければ停止用のデフォルト文を使う
            stopped_text = self.finished_message or (
                "⏹️ **Playback Stopped**\n"
                "Playback was stopped or the queue has finished."
            )
            # Section は accessory 必須のため、停止メッセージは TextDisplay のみ使う
            container.add_item(discord.ui.TextDisplay(stopped_text))

            # 無効化されたボタンを配置するアクション行を作成する
            action_row = discord.ui.ActionRow()
            # 一時停止ボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="⏸️ Pause", style=discord.ButtonStyle.secondary, disabled=True))
            # スキップボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, disabled=True))
            # 停止ボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="⏹️ Stop", style=discord.ButtonStyle.secondary, disabled=True))
            # コンテナにアクション行を追加する
            container.add_item(action_row)

            # ビュー自体にコンテナを追加して完了する
            self.add_item(container)
            # 処理を終了する
            return

        # 再生中のトラック情報を取得する
        track = state.current_track
        # 一時停止状態であるか取得する
        is_paused = state.is_paused

        # 再起アイコンと再生ステータスの文言を一時停止状態に合わせて決定する
        status_icon = "⏸️" if is_paused else "▶️"
        # ステータス文字列を設定する
        status_text = "Paused" if is_paused else "Playing"

        # 水色のアクセント色でV2コンテナを初期化する
        container = discord.ui.Container(accent_color=discord.Color.from_rgb(79, 194, 255))

        # タイトル本文を構築する
        title_text = f"### {status_icon} Now {status_text}\n**[{track.title}]({track.url})**"
        # サムネイルがあるときだけ Section（accessory 必須）を使い、無いときは TextDisplay
        if track.thumbnail and track.thumbnail.strip() and track.thumbnail != "None":
            # 右上サムネイル付きセクションを追加する
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(title_text),
                    accessory=discord.ui.Thumbnail(track.thumbnail),
                )
            )
        else:
            # サムネイル無しはテキストのみ追加する
            container.add_item(discord.ui.TextDisplay(title_text))

        # 現在の再生位置（秒）を取得する
        current_pos = state.get_current_position()
        # 進行状況バー（テキストアート）を生成する
        progress_bar = self.cog._create_progress_bar(current_pos, track.duration)
        # 再生時間と総再生時間の文字列フォーマットを生成する
        duration_str = f"`{format_duration(current_pos)}` / `{format_duration(track.duration)}`"
        # 進行状況は TextDisplay のみ（Section は accessory 必須のため使わない）
        container.add_item(
            discord.ui.TextDisplay(f"**Progress**\n{progress_bar}\n{duration_str}")
        )

        # チャンネル名（アップローダー）のデフォルトフォールバックを設定する
        uploader_val = track.uploader if track.uploader else "Unknown"
        # リクエストユーザーのメンション文字列を設定する
        requester_mention = f"<@{track.requester_id}>" if track.requester_id else "Unknown"
        # 残りのキューの数を取得する
        remaining = state.queue.qsize()

        # 詳細メタデータ用のテキスト文字列を構築する
        info_text = (
            f"**Channel:** {uploader_val}  |  **Requested By:** {requester_mention}\n"
            f"**Loop Mode:** `{state.loop_mode.name.lower()}`  |  **Queue:** {remaining} songs"
        )
        # メタデータも TextDisplay のみで追加する
        container.add_item(discord.ui.TextDisplay(info_text))

        # 一時停止/再生ボタンを初期化する
        self.pause_resume_btn = discord.ui.Button(
            # 一時停止中なら緑色、再生中ならグレーでボタンのカラーを設定する
            style=discord.ButtonStyle.success if is_paused else discord.ButtonStyle.secondary,
            # 一時停止中なら「再開」、再生中なら「一時停止」でラベルを設定する
            label="▶️ Resume" if is_paused else "⏸️ Pause",
            # カスタムIDを設定する
            custom_id=f"music_pause_resume_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.pause_resume_btn.callback = self.pause_resume_callback

        # スキップボタンをプライマリカラーで初期化する
        self.skip_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="⏭️ Skip",
            custom_id=f"music_skip_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.skip_btn.callback = self.skip_callback

        # 停止ボタンをレッドカラーで初期化する
        self.stop_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="⏹️ Stop",
            custom_id=f"music_stop_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.stop_btn.callback = self.stop_callback

        # アクション行（ボタン配置用）を作成する
        action_row = discord.ui.ActionRow()
        # ボタンを追加する
        action_row.add_item(self.pause_resume_btn)
        # ボタンを追加する
        action_row.add_item(self.skip_btn)
        # ボタンを追加する
        action_row.add_item(self.stop_btn)
        # 控えめな寄付リンク（enabled 時のみ）
        donation_btn = make_subtle_link_button(donation_from_bot(self.cog.bot))
        if donation_btn is not None:
            action_row.add_item(donation_btn)
        # コンテナにボタンを格納したアクション行を追加する
        container.add_item(action_row)

        # ビューに構築したコンテナをアタッチする
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 再生状態、またはボイス接続が存在しないか判定する
        if not state or not state.voice_client:
            # エフェメラルエラーを送信する
            await interaction.response.send_message("The bot is not in a voice channel.", ephemeral=True)
            # チェック失敗
            return False

        # 操作を実行したユーザーのボイス接続状態を取得する
        user_voice = interaction.user.voice
        # ユーザーがボイスチャンネルに入っていない、またはボットと異なるチャンネルか判定する
        if not user_voice or not user_voice.channel or user_voice.channel != state.voice_client.channel:
            # エフェメラルエラーを送信する
            await interaction.response.send_message("You must be in the same voice channel as the bot to use the controls.", ephemeral=True)
            # チェック失敗
            return False
        # チェックパス
        return True

    async def pause_resume_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 現在一時停止中であるか判定する
        if state.is_paused:
            # ボイス接続が存在するか判定する
            if state.voice_client:
                # 音声再生を再開する
                state.voice_client.resume()
            # 一時停止フラグをFalseに設定する
            state.is_paused = False
            # 一時停止開始のタイムスタンプが存在するか判定する
            if state.paused_at and state.playback_start_time:
                # 一時停止されていた実経過時間を算出する
                pause_duration = time.time() - state.paused_at
                # 総再生開始時刻に一時停止時間を加算して進行位置を補正する
                state.playback_start_time += pause_duration
            # 一時停止開始時刻を初期化する
            state.paused_at = None
            # 再開ログを記録する
            logger.info(f"Guild {self.guild_id}: playback resumed via UI button")
        else:
            # ボイス接続が存在するか判定する
            if state.voice_client:
                # 音声再生を一時停止する
                state.voice_client.pause()
            # 一時停止フラグをTrueに設定する
            state.is_paused = True
            # 現在の時刻を一時停止開始時刻として記録する
            state.paused_at = time.time()
            # 一時停止ログを記録する
            logger.info(f"Guild {self.guild_id}: playback paused via UI button")

        # 変更された再生状態に基づいてUIを再構築する
        self.rebuild_ui()
        # コンポーネントinteractionのトークンで編集し、webhook期限切れを避ける
        try:
            # defer後は edit_original_response がコンポーネント用の有効トークンを使う
            await interaction.edit_original_response(embed=None, view=self)
        except Exception:
            # フォールバック: チャンネル経由Messageへ変換して編集する
            durable = await self.cog._to_durable_message(interaction.message)
            if durable is not None:
                # 通常Messageとして編集する
                await durable.edit(embed=None, view=self)
                # 最新の参照を状態へ保存する
                state.last_now_playing_message = durable

    async def skip_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 再生状態、または再生中のトラックが存在しない場合は終了する
        if not state or not state.current_track:
            # 処理終了
            return

        # スキップ開始をログに記録する
        logger.info(f"Guild {self.guild_id}: skipping song via UI button")
        # オーディオミキサーが存在するか判定する
        if state.mixer:
            # ミキサーから対象の音源を削除してスキップをトリガーする
            await state.mixer.remove_source('music')
        # ミキサーがなく、ボイスクライアントが直接再生中であるか判定する
        elif state.voice_client and state.voice_client.is_playing():
            # 再生を停止してスキップをトリガーする
            state.voice_client.stop()

    async def stop_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 停止処理の開始をログに記録する
        logger.info(f"Guild {self.guild_id}: stopping playback via UI button")
        # ループモードをOFFに設定する
        state.loop_mode = LoopMode.OFF
        # キューの内容をすべて消去する
        await state.clear_queue()
        # オーディオミキサーが存在するか判定する
        if state.mixer:
            # ミキサーを完全に停止する
            state.mixer.stop()
            # ミキサーオブジェクトを破棄する
            state.mixer = None
        # ボイスクライアントが直接再生中であるか判定する
        if state.voice_client and state.voice_client.is_playing():
            # 再生を停止する
            state.voice_client.stop()
        # 再生中フラグを初期化する
        state.is_playing = False
        # 一時停止フラグを初期化する
        state.is_paused = False
        # 再生中トラック情報を初期化する
        state.current_track = None
        # 再生時間計測情報を初期化する
        state.reset_playback_tracking()
        # UI 停止時はプログレスバー更新も止める
        state.stop_progress_updater()

        # 停止した状態に基づいてUIを再構築する
        self.rebuild_ui()
        # コンポーネントinteractionのトークンで編集し、webhook期限切れを避ける
        try:
            # defer後は edit_original_response がコンポーネント用の有効トークンを使う
            await interaction.edit_original_response(embed=None, view=self)
        except Exception:
            # フォールバック: チャンネル経由Messageへ変換して編集する
            durable = await self.cog._to_durable_message(interaction.message)
            if durable is not None:
                # 通常Messageとして編集する
                await durable.edit(embed=None, view=self)

        # 直前の Now Playing メッセージへの参照が存在するか判定する
        if state.last_now_playing_message:
            # 参照を初期化する
            state.last_now_playing_message = None


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(MusicCog(bot))
        logger.info("MusicCog successfully loaded")
    except Exception as e:
        logger.error(f"MusicCogのセットアップ中にエラー: {e}", exc_info=True)
        raise
