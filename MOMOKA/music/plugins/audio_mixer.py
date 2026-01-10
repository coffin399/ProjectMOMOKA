# MOMOKA/music/audio_mixer.py
import discord
import struct
import asyncio
import io
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class AudioMixer(discord.AudioSource):
    def __init__(self, on_source_removed_callback=None):
        self.sources: Dict[str, discord.AudioSource] = {}
        self.volumes: Dict[str, float] = {}
        self.lock = asyncio.Lock()
        self._is_done = False
        self.active = True
        self.on_source_removed_callback = on_source_removed_callback

    def is_done(self) -> bool:
        return self._is_done

    def stop(self):
        self.active = False
        self._is_done = True
        self.cleanup()

    def read(self) -> bytes:
        if not self.active or self._is_done:
            return b''

        final_frame = bytearray(3840)
        finished_sources = []
        sources_to_process = list(self.sources.items())

        for name, source in sources_to_process:
            try:
                frame = source.read()
                if not frame:
                    finished_sources.append(name)
                    continue

                if len(frame) < 3840:
                    frame += b'\x00' * (3840 - len(frame))

                source_samples = struct.iter_unpack('<h', frame)
                final_samples = struct.iter_unpack('<h', final_frame)

                mixed_frame_data = bytearray()
                volume = self.volumes.get(name, 1.0)

                for source_sample, final_sample in zip(source_samples, final_samples):
                    s_val = source_sample[0]
                    f_val = final_sample[0]
                    mixed_sample = f_val + int(s_val * volume)
                    mixed_sample = max(-32768, min(32767, mixed_sample))
                    mixed_frame_data.extend(struct.pack('<h', mixed_sample))

                final_frame = mixed_frame_data

            except Exception:
                finished_sources.append(name)

        if finished_sources:
            try:
                loop = asyncio.get_running_loop()
                for name in finished_sources:
                    asyncio.run_coroutine_threadsafe(self.remove_source(name), loop)
            except RuntimeError:
                for name in finished_sources:
                    source = self.sources.pop(name, None)
                    self.volumes.pop(name, None)
                    if source and hasattr(source, 'cleanup'):
                        source.cleanup()
                    # ソースが削除されてミキサーが空になった場合にコールバックを呼ぶ
                    if not self.sources and self.on_source_removed_callback:
                        try:
                            self.on_source_removed_callback(name)
                        except Exception as e:
                            logger.error(f"Error in on_source_removed_callback: {e}")

        return bytes(final_frame)

    async def add_source(self, name: str, source: discord.AudioSource, volume: float = 1.0):
        async with self.lock:
            if name in self.sources:
                old_source = self.sources.get(name)
                if old_source and hasattr(old_source, 'cleanup'):
                    old_source.cleanup()

            self.sources[name] = source
            self.volumes[name] = max(0.0, volume)

    async def remove_source(self, name: str) -> Optional[discord.AudioSource]:
        async with self.lock:
            source = self.sources.pop(name, None)
            self.volumes.pop(name, None)
            if source and hasattr(source, 'cleanup'):
                source.cleanup()
            # ソースが削除されてミキサーが空になった場合にコールバックを呼ぶ
            if not self.sources and self.on_source_removed_callback:
                try:
                    self.on_source_removed_callback(name)
                except Exception as e:
                    logger.error(f"Error in on_source_removed_callback: {e}")
            return source

    async def set_volume(self, name: str, volume: float):
        async with self.lock:
            if name in self.volumes:
                self.volumes[name] = max(0.0, volume)

    def get_source(self, name: str) -> Optional[discord.AudioSource]:
        return self.sources.get(name)

    def cleanup(self):
        # コピーを作成してからイテレート（イテレーション中の辞書変更を防止）
        sources_copy = list(self.sources.values())
        for source in sources_copy:
            if hasattr(source, 'cleanup'):
                try:
                    source.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up source: {e}")
        self.sources.clear()
        self.volumes.clear()


class MusicAudioSource(discord.FFmpegPCMAudio):
    def __init__(self, source, *, title: str = "Unknown Track", guild_id: int, **kwargs):
        super().__init__(source, **kwargs)
        self.title = title
        self.guild_id = guild_id

    def cleanup(self):
        logger.info(f"Guild {self.guild_id}: Music FFmpeg process for '{self.title}' is being cleaned up.")
        super().cleanup()


class TTSAudioSource(discord.FFmpegPCMAudio):
    def __init__(self, source, *, text: str, guild_id: int, **kwargs):
        # BytesIOの場合はpipe=Trueを強制
        if isinstance(source, io.BytesIO):
            kwargs['pipe'] = True
        
        # BytesIOの参照を保持してクリーンアップ時にクローズ
        self._source_buffer = source if isinstance(source, io.BytesIO) else None
        self.text = text if len(text) < 30 else text[:27] + "..."
        self.guild_id = guild_id

        try:
            super().__init__(source, **kwargs)
        except Exception as e:
            logger.error(f"Guild {guild_id}: Failed to initialize TTSAudioSource: {e}")
            # 初期化失敗時にもバッファをクローズ
            if self._source_buffer:
                try:
                    self._source_buffer.close()
                except Exception:
                    pass
            raise

    def cleanup(self):
        logger.info(f"Guild {self.guild_id}: TTS FFmpeg process for '{self.text}' is being cleaned up.")
        try:
            super().cleanup()
        finally:
            # BytesIOバッファを明示的にクローズしてメモリを解放
            if self._source_buffer:
                try:
                    self._source_buffer.close()
                except Exception as e:
                    logger.warning(f"Guild {self.guild_id}: Failed to close TTS buffer: {e}")
                finally:
                    self._source_buffer = None