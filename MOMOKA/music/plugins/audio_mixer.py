# MOMOKA/music/audio_mixer.py
import discord
import struct
import asyncio
import io
import threading
from typing import Dict, Optional, List, Tuple
import logging

logger = logging.getLogger(__name__)


class AudioMixer(discord.AudioSource):
    def __init__(self, on_source_removed_callback=None):
        # ソース辞書: ソース名→AudioSourceオブジェクト
        self.sources: Dict[str, discord.AudioSource] = {}
        # ボリューム辞書: ソース名→音量(float)
        self.volumes: Dict[str, float] = {}
        # asyncio用ロック（非同期メソッド間の排他制御）
        self.lock = asyncio.Lock()
        # スレッド間の排他制御用ロック（read()はオーディオスレッドから呼ばれる）
        self._thread_lock = threading.Lock()
        # ミキサーの終了フラグ
        self._is_done = False
        # ミキサーのアクティブ状態
        self.active = True
        # ソース削除時に呼ばれるコールバック（各ソース削除ごとに発火）
        self.on_source_removed_callback = on_source_removed_callback

    def is_done(self) -> bool:
        """ミキサーが終了したかどうかを返す"""
        return self._is_done

    def stop(self):
        """ミキサーを停止し、全リソースをクリーンアップ"""
        self.active = False
        self._is_done = True
        self.cleanup()

    def read(self) -> bytes:
        """
        オーディオフレームを読み取り、全ソースをミキシングして返す。
        注意: このメソッドはDiscordのオーディオスレッドから呼ばれるため、
        asyncio APIは使用不可。threading.Lockで排他制御する。
        """
        # 停止済みなら空バイトを返してプレイヤーを終了させる
        if not self.active or self._is_done:
            return b''

        # 3840バイト = 960サンプル * 2バイト(16bit) * 2チャンネル(ステレオ) = 20ms分のPCMデータ
        final_frame = bytearray(3840)
        # 終了したソースを (名前, ソース参照) のタプルで記録
        finished_sources: List[Tuple[str, discord.AudioSource]] = []

        # スレッド安全にソース一覧のスナップショットを取得
        with self._thread_lock:
            sources_to_process = list(self.sources.items())

        # 各ソースからフレームを読み取り、ミキシング
        for name, source in sources_to_process:
            try:
                # ソースからPCMフレームを読み取る
                frame = source.read()
                # 空フレーム＝ソース終了
                if not frame:
                    finished_sources.append((name, source))
                    continue

                # フレームが3840バイト未満の場合はゼロパディング
                if len(frame) < 3840:
                    frame += b'\x00' * (3840 - len(frame))

                # 16bitリトルエンディアンPCMサンプルをイテレート
                source_samples = struct.iter_unpack('<h', frame)
                final_samples = struct.iter_unpack('<h', final_frame)

                mixed_frame_data = bytearray()
                # このソースの音量を取得
                volume = self.volumes.get(name, 1.0)

                # サンプルごとにミキシング（加算合成）
                for source_sample, final_sample in zip(source_samples, final_samples):
                    s_val = source_sample[0]
                    f_val = final_sample[0]
                    # ボリューム適用してサンプルを加算
                    mixed_sample = f_val + int(s_val * volume)
                    # クリッピング（16bit範囲に制限）
                    mixed_sample = max(-32768, min(32767, mixed_sample))
                    mixed_frame_data.extend(struct.pack('<h', mixed_sample))

                final_frame = mixed_frame_data

            except Exception:
                # 読み取りエラーが発生したソースは終了扱い
                finished_sources.append((name, source))

        # 終了したソースを削除
        actually_removed: List[str] = []
        if finished_sources:
            with self._thread_lock:
                for name, finished_source in finished_sources:
                    # ソースが置き換えられていないか確認（seek等で別ソースに差し替わっている場合はスキップ）
                    current_source = self.sources.get(name)
                    if current_source is not finished_source:
                        # 別のソースに差し替わっている→削除しない（新しいソースを保持）
                        continue
                    # ソース辞書から削除
                    self.sources.pop(name, None)
                    # ボリューム辞書からも削除
                    self.volumes.pop(name, None)
                    # FFmpegプロセス等のリソースをクリーンアップ
                    if finished_source and hasattr(finished_source, 'cleanup'):
                        try:
                            finished_source.cleanup()
                        except Exception as e:
                            logger.error(f"Error cleaning up finished source '{name}': {e}")
                    # 実際に削除されたソース名を記録
                    actually_removed.append(name)

            # コールバックはロック外で実行（デッドロック防止）
            # 各ソースの削除ごとにコールバックを発火
            if self.on_source_removed_callback and actually_removed:
                for name in actually_removed:
                    try:
                        self.on_source_removed_callback(name)
                    except Exception as e:
                        logger.error(f"Error in on_source_removed_callback for '{name}': {e}")

        return bytes(final_frame)

    async def add_source(self, name: str, source: discord.AudioSource, volume: float = 1.0):
        """
        ソースをミキサーに追加する。同名のソースが存在する場合は置き換え。
        """
        async with self.lock:
            with self._thread_lock:
                # 同名ソースが存在する場合は先にクリーンアップ
                if name in self.sources:
                    old_source = self.sources.get(name)
                    if old_source and hasattr(old_source, 'cleanup'):
                        old_source.cleanup()

                # 新しいソースを登録
                self.sources[name] = source
                # 音量を設定（0.0以上に制限）
                self.volumes[name] = max(0.0, volume)

    async def remove_source(self, name: str) -> Optional[discord.AudioSource]:
        """
        指定名のソースをミキサーから削除する。
        """
        removed_source = None
        async with self.lock:
            with self._thread_lock:
                # ソース辞書から削除
                removed_source = self.sources.pop(name, None)
                # ボリューム辞書からも削除
                self.volumes.pop(name, None)
            # FFmpegプロセス等のリソースをクリーンアップ（thread_lock外で実行）
            if removed_source and hasattr(removed_source, 'cleanup'):
                removed_source.cleanup()
        # ソース削除コールバックを発火（asyncio lock外で実行）
        if removed_source and self.on_source_removed_callback:
            try:
                self.on_source_removed_callback(name)
            except Exception as e:
                logger.error(f"Error in on_source_removed_callback for '{name}': {e}")
        return removed_source

    async def set_volume(self, name: str, volume: float):
        """指定ソースの音量を変更する"""
        async with self.lock:
            with self._thread_lock:
                if name in self.volumes:
                    self.volumes[name] = max(0.0, volume)

    def get_source(self, name: str) -> Optional[discord.AudioSource]:
        """指定ソースを取得する"""
        with self._thread_lock:
            return self.sources.get(name)

    def has_sources(self) -> bool:
        """ミキサーにソースが存在するかどうかを返す"""
        with self._thread_lock:
            return bool(self.sources)

    def get_source_names(self) -> list:
        """現在登録されているソース名のリストを返す"""
        with self._thread_lock:
            return list(self.sources.keys())

    def cleanup(self):
        """全ソースのリソースをクリーンアップ"""
        # スレッド安全にコピーを作成してからイテレート
        with self._thread_lock:
            sources_copy = list(self.sources.values())
            self.sources.clear()
            self.volumes.clear()
        # クリーンアップはロック外で実行
        for source in sources_copy:
            if hasattr(source, 'cleanup'):
                try:
                    source.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up source: {e}")


class MusicAudioSource(discord.FFmpegPCMAudio):
    """音楽再生用のFFmpegオーディオソース"""
    def __init__(self, source, *, title: str = "Unknown Track", guild_id: int, **kwargs):
        super().__init__(source, **kwargs)
        # トラックのタイトル（ログ用）
        self.title = title
        # ギルドID（ログ用）
        self.guild_id = guild_id

    def cleanup(self):
        """FFmpegプロセスのクリーンアップ"""
        logger.info(f"Guild {self.guild_id}: Music FFmpeg process for '{self.title}' is being cleaned up.")
        super().cleanup()


class TTSAudioSource(discord.FFmpegPCMAudio):
    """TTS読み上げ用のFFmpegオーディオソース"""
    def __init__(self, source, *, text: str, guild_id: int, **kwargs):
        # BytesIOの場合はpipe=Trueを強制
        if isinstance(source, io.BytesIO):
            kwargs['pipe'] = True

        # BytesIOの参照を保持してクリーンアップ時にクローズ
        self._source_buffer = source if isinstance(source, io.BytesIO) else None
        # テキスト（ログ用、30文字以上は切り詰め）
        self.text = text if len(text) < 30 else text[:27] + "..."
        # ギルドID（ログ用）
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
        """FFmpegプロセスとバッファのクリーンアップ"""
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
