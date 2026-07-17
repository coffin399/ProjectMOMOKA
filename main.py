import asyncio
import discord
from discord.ext import commands, tasks
import yaml
import logging
import os
import shutil
import sys
import json
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox
from pathlib import Path
import ctypes
import platform
from io import StringIO
import aiohttp
from typing import Optional

from MOMOKA.utilities.restart_notice import SHUTDOWN_USER_ID

# Python 3.11 未満では依存パッケージ（discord.py 2.7 / torch 等）の動作保証外のため起動を拒否する
if sys.version_info < (3, 11):
    # 現在のインタプリタバージョンをユーザー向けに表示する
    _current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    # 標準エラーへ要件を明示して終了する
    sys.stderr.write(
        f"MOMOKA requires Python 3.11 or higher (detected {_current}).\n"
        "Please recreate the virtual environment with Python 3.11 "
        "(e.g. `py -3.11 -m venv .venv` or startMOMOKA.bat).\n"
    )
    # 非ゼロ終了コードでプロセスを終了する
    sys.exit(1)


def _preload_torch() -> None:
    """venv の torch を最優先で読み込み、C10 二重登録を防ぐ。

    PATH 上に別環境の ``torch\\lib``（例: グローバル Python 3.11 の torch 2.7）が
    残っていると、c10.dll が二重ロードされ
    ``Key already registered with the same priority: C10`` で即終了することがある。
    Discord ログイン直後の Cog（TTS / 画像）読込より前に確定させる。
    """
    # 現在の PATH を分割する
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    # 他環境の torch\\lib を除外した PATH を組み立てる
    filtered_parts = []
    for part in path_parts:
        # パス比較用に区切りを正規化する
        norm = part.replace("/", "\\").lower()
        # site-packages\\torch\\lib を含むエントリは衝突源なので捨てる
        if "\\torch\\lib" in norm:
            continue
        # 残すパスを追加する
        filtered_parts.append(part)
    # 浄化した PATH を書き戻す
    os.environ["PATH"] = os.pathsep.join(filtered_parts)
    try:
        # TTS / diffusers より先に torch 本体をロードする
        import torch  # noqa: F401
    except ImportError:
        # 画像・TTS 無効構成でも起動できるよう欠落は無視する
        return


# Cog 読込より前に torch を確定する（C10 衝突回避）
_preload_torch()

# --- グローバル変数 ---
log_viewer_thread = None
# GUI スレッドから参照する Bot インスタンス（起動前は None）
bot_ref: Optional["Momoka"] = None


# モバイルアプリとして識別するための関数
async def mobile_identify(self):
    """Discordのモバイルアプリとして識別するための関数"""
    # 通常のidentifyペイロードを取得
    payload = {
        'op': self.IDENTIFY,
        'd': {
            'token': self.token,
            'properties': {
                '$os': 'iOS',  # モバイルアプリとして識別
                '$browser': 'Discord iOS',
                '$device': 'iPhone',
                '$referrer': '',
                '$referring_domain': ''
            },
            'compress': True,
            'large_threshold': 250,
            'v': 3
        }
    }

    # 必要に応じてintentsを追加
    if hasattr(self._connection, 'intents') and self._connection.intents is not None:
        payload['d']['intents'] = self._connection.intents.value

    # プレゼンス情報を追加（存在する場合）
    if hasattr(self._connection, '_activity') or hasattr(self._connection, '_status'):
        presence = {}
        if hasattr(self._connection, '_status'):
            presence['status'] = self._connection._status or 'online'
        if hasattr(self._connection, '_activity'):
            presence['game'] = self._connection._activity

        if presence:
            presence.update({
                'since': 0,
                'afk': False
            })
            payload['d']['presence'] = presence

    # 識別情報を送信
    if hasattr(self, 'call_hooks'):
        await self.call_hooks('before_identify', self.shard_id, initial=getattr(self, '_initial_identify', False))
    await self.send_as_json(payload)


def set_dark_mode():
    """Windowsのダークモードを有効化"""
    try:
        if os.name == 'nt':  # Windowsのみ
            # ダークモードを有効化
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # DPI認識を有効化

            # テーマカラーをダークモードに設定
            try:
                import darkdetect
                if darkdetect.isDark():
                    from ctypes import wintypes

                    # ウィンドウのテーマカラーをダークに設定
                    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                    hwnd = ctypes.windll.user32.GetForegroundWindow()
                    value = 1  # ダークモード
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd,
                        DWMWA_USE_IMMERSIVE_DARK_MODE,
                        ctypes.byref(ctypes.c_int(value)),
                        ctypes.sizeof(ctypes.c_int(value))
                    )
            except ImportError:
                pass  # darkdetectが利用できない場合はスキップ
    except Exception as e:
        print(f"ダークモードの設定中にエラーが発生しました: {e}")


# ダークモードを有効化
set_dark_mode()

# --- ロギング設定の初期化 ---
# ルートロガーの設定
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# 特定のロガーのログレベル設定
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('openai').setLevel(logging.WARNING)
logging.getLogger('google.generativeai').setLevel(logging.WARNING)
logging.getLogger('google.ai').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('PIL').setLevel(logging.WARNING)

# ログキューの作成（GUIログビューアと共有）
log_queue = queue.Queue()


class QueueHandler(logging.Handler):
    """ログをキューに送信するハンドラ"""

    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue
        self.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

    def emit(self, record):
        try:
            self.log_queue.put((record.name, record.levelname, self.format(record)))
        except Exception:
            self.handleError(record)


class StdoutCapture:
    """標準出力をキャプチャしてログキューに送信するクラス"""
    
    def __init__(self, log_queue, original_stdout):
        self.log_queue = log_queue
        self.original_stdout = original_stdout
        self.buffer = StringIO()
    
    def write(self, text):
        """標準出力への書き込みをキャプチャ"""
        # 元の標準出力にも書き込む（コンソールにも表示）
        self.original_stdout.write(text)
        self.original_stdout.flush()
        
        # 空行や改行のみの場合はスキップ
        if not text.strip():
            return
        
        # ログキューに送信
        try:
            # 各行を個別に処理
            for line in text.rstrip().split('\n'):
                if line.strip():
                    # 標準出力のログとして扱う
                    log_entry = f"{line}"
                    self.log_queue.put(("stdout", "INFO", log_entry))
        except Exception:
            pass  # エラーが発生しても元の標準出力は動作させる
    
    def flush(self):
        """フラッシュ処理"""
        self.original_stdout.flush()
        if hasattr(self.buffer, 'flush'):
            self.buffer.flush()


# キューにログを送信するハンドラを追加（GUIログビューア用）
queue_handler = QueueHandler(log_queue)
root_logger.addHandler(queue_handler)
# 注意: DiscordLogHandlerはsetup_hook内で追加されるため、GUIとDiscordの両方にログが送信されます

# 標準出力をキャプチャしてGUIにも表示
original_stdout = sys.stdout
stdout_capture = StdoutCapture(log_queue, original_stdout)
sys.stdout = stdout_capture

from MOMOKA.services.discord_handler import DiscordLogHandler, DiscordLogFormatter
from MOMOKA.utilities.error.errors import InvalidDiceNotationError, DiceValueError


class Momoka(commands.Bot):
    """MOMOKA Botのメインクラス"""

    # /shutdown を実行できるユーザー ID（ハードコード定数）
    SHUTDOWN_USER_ID = SHUTDOWN_USER_ID

    def __init__(
        self,
        *args,
        config: dict,
        bot_id: str,
        bot_role: str,
        persona_key: str,
        display_name: str,
        cogs_to_load: list,
        enable_discord_logging: bool = True,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        # マージ済み設定辞書を保持する
        self.config = config
        # ボット識別子（例: "plana", "arona"）
        self.bot_id = bot_id
        # ボット役割（"primary" or "companion"）
        self.bot_role = bot_role
        # ペルソナキー（例: "plana", "arona"）
        self.persona_key = persona_key
        # 表示名（例: "[PLANA]", "[ARONA]"）
        self.display_name = display_name
        # Discord ログ出力を有効化するか（primary のみ True）
        self.enable_discord_logging = enable_discord_logging
        # ステータスローテーション用テンプレートリスト
        self.status_templates = []
        # ステータスローテーション用インデックス
        self.status_index = 0
        # ロードする Cog のリスト（primary / companion で異なる）
        self.cogs_to_load = cogs_to_load

    def is_admin(self, user_id: int) -> bool:
        """ユーザーが管理者かどうかをチェック"""
        admin_ids = self.config.get('admin_user_ids', [])
        return user_id in admin_ids

    async def notify_active_users_of_restart(self) -> None:
        """利用中ユーザー（音楽・LLM）へ再起動通知を送る。"""
        # 並列通知用のコルーチン一覧
        tasks = []
        # 音楽 Cog を名前で取得する
        music_cog = self.get_cog("music_cog")
        # 通知メソッドがある場合のみキューに入れる
        if music_cog is not None and hasattr(music_cog, "notify_admin_restart"):
            # MusicCog の再起動通知をキューへ追加する
            tasks.append(music_cog.notify_admin_restart())
        # LLM Cog を名前で取得する
        llm_cog = self.get_cog("LLM")
        # 通知メソッドがある場合のみキューに入れる
        if llm_cog is not None and hasattr(llm_cog, "notify_admin_restart"):
            # LLMCog の再起動通知をキューへ追加する
            tasks.append(llm_cog.notify_admin_restart())
        # 対象が無ければ何もしない
        if not tasks:
            # 早期リターン
            return
        # 各 Cog の通知を並行実行し、例外は個別に収集する
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 失敗した通知だけログに残す
        for result in results:
            # 例外オブジェクトかどうかを判定する
            if isinstance(result, Exception):
                # 通知失敗を警告ログへ出す
                logging.warning("Failed to notify active users of restart: %s", result)

    async def close(self) -> None:
        """終了前に利用中ユーザーへ通知してから接続を閉じる。"""
        try:
            # Ctrl+C /shutdown 共通で再起動通知を送る
            await self.notify_active_users_of_restart()
        except Exception as e:
            # 通知失敗でもシャットダウン自体は継続する
            logging.warning(
                "%s notify_active_users_of_restart failed during close: %s",
                self.display_name,
                e
            )
        # discord.py 本来のクローズ処理へ進む
        await super().close()

    async def setup_hook(self):
        """Botの初期セットアップ（ログイン後、接続準備完了前）"""
        # 実行中の Python バージョンを起動ログへ残す（3.11 前提の切り分け用）
        logging.info(
            "%s Runtime Python %s.%s.%s (MOMOKA requires 3.11.x)",
            self.display_name,
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        )

        # self.config は __init__ で渡されているため、ファイル読み込みは不要
        logging.info("%s マージ済み設定を使用して起動します。", self.display_name)

        # ステータスローテーションの設定を取得する
        self.status_templates = self.config.get('status_rotation', [
            "operating on {guild_count} servers",
            "prjMOMOKA Ver.2026-07-17",
        ])
        # ステータスローテーションタスクを開始する
        self.rotate_status.start()

        # Discord ログ出力は primary のみ有効化する
        if self.enable_discord_logging:
            # ロギング設定用の JSON ファイルパス
            logging_json_path = "data/logging_channels.json"
            # 設定辞書からログチャンネル ID リストを取得する
            log_channel_ids_from_config = self.config.get('log_channel_ids', [])
            # リスト形式でなければ空リストに戻す
            if not isinstance(log_channel_ids_from_config, list):
                log_channel_ids_from_config = []
                logging.warning(
                    "%s configs/core_config.yaml の 'log_channel_ids' はリスト形式である必要があります。",
                    self.display_name
                )

            # JSON ファイルから追加のチャンネル ID を読み込む
            log_channel_ids_from_file = []
            try:
                # JSON ファイルのディレクトリを作成する
                dir_path = os.path.dirname(logging_json_path)
                os.makedirs(dir_path, exist_ok=True)
                # JSON ファイルが無ければ空リストで生成する
                if not os.path.exists(logging_json_path):
                    with open(logging_json_path, 'w') as f:
                        json.dump([], f)
                    logging.info("%s %s が見つからなかったため、新規作成しました。", self.display_name, logging_json_path)

                # JSON ファイルからチャンネル ID を読み込む
                with open(logging_json_path, 'r') as f:
                    data = json.load(f)
                    # リスト形式かつ全要素が int であれば採用する
                    if isinstance(data, list) and all(isinstance(i, int) for i in data):
                        log_channel_ids_from_file = data
            except (json.JSONDecodeError, IOError) as e:
                logging.error("%s %s の処理中にエラーが発生しました: %s", self.display_name, logging_json_path, e)

            # 設定ファイルと JSON ファイルのチャンネル ID を統合する
            all_log_channel_ids = list(set(log_channel_ids_from_config + log_channel_ids_from_file))

            # チャンネル ID が設定されていれば Discord ログハンドラを追加する
            if all_log_channel_ids:
                try:
                    # Discord ログハンドラを作成する
                    discord_handler = DiscordLogHandler(bot=self, channel_ids=all_log_channel_ids, interval=6.0)
                    # ログレベルを INFO に設定する
                    discord_handler.setLevel(logging.INFO)
                    # フォーマッタを設定する
                    discord_formatter = DiscordLogFormatter('%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')
                    discord_handler.setFormatter(discord_formatter)
                    # ルートロガーへハンドラを追加する
                    root_logger.addHandler(discord_handler)
                    logging.info(
                        "%s Discord へのロギングをチャンネル ID %s で有効化しました。",
                        self.display_name,
                        all_log_channel_ids
                    )
                except Exception as e:
                    logging.error("%s DiscordLogHandler の初期化中にエラーが発生しました: %s", self.display_name, e)
            else:
                logging.warning("%s ログ送信先の Discord チャンネルが設定されていません。", self.display_name)

        # Cog のロードを開始する
        logging.info("%s Cog のロードを開始します...", self.display_name)
        loaded_cogs_count = 0
        # Cog リストを順番にロードする
        for module_path in self.cogs_to_load:
            try:
                # Cog をロードする
                await self.load_extension(module_path)
                logging.info("%s   > Cog '%s' のロードに成功しました。", self.display_name, module_path)
                # ロード成功数を加算する
                loaded_cogs_count += 1
            except commands.ExtensionAlreadyLoaded:
                logging.debug("%s Cog '%s' は既にロードされています。", self.display_name, module_path)
            except commands.ExtensionNotFound:
                logging.error(
                    "%s   > Cog '%s' が見つかりません。ファイルパスを確認してください。",
                    self.display_name,
                    module_path
                )
            except commands.NoEntryPointError:
                logging.error(
                    "%s   > Cog '%s' に setup 関数が見つかりません。Cog として正しく実装されていますか？",
                    self.display_name,
                    module_path
                )
            except Exception as e:
                logging.error(
                    "%s   > Cog '%s' のロード中に予期しないエラーが発生しました: %s",
                    self.display_name,
                    module_path,
                    e,
                    exc_info=True
                )
        logging.info(
            "%s Cog のロードが完了しました。合計 %d 個の Cog をロードしました。",
            self.display_name,
            loaded_cogs_count
        )

        # companion ボットは同期タイミングをずらす（rate limit 回避）
        if self.bot_role == "companion":
            # 2秒待機して primary の同期を先に行わせる
            await asyncio.sleep(2)

        # スラッシュコマンドの同期を行う
        if self.config.get('sync_slash_commands', True):
            try:
                # テストギルド ID が設定されているか確認する
                test_guild_id = self.config.get('test_guild_id')
                if test_guild_id:
                    # テストギルドへ同期する
                    guild_obj = discord.Object(id=int(test_guild_id))
                    synced_commands = await self.tree.sync(guild=guild_obj)
                    logging.info(
                        "%s %d 個のスラッシュコマンドをテストギルド %s に同期しました。",
                        self.display_name,
                        len(synced_commands),
                        test_guild_id
                    )
                else:
                    # グローバルに同期する
                    synced_commands = await self.tree.sync()
                    logging.info(
                        "%s %d 個のグローバルスラッシュコマンドを同期しました。",
                        self.display_name,
                        len(synced_commands)
                    )
            except Exception as e:
                logging.error(
                    "%s スラッシュコマンドの同期中にエラーが発生しました: %s",
                    self.display_name,
                    e,
                    exc_info=True
                )
        else:
            logging.info("%s スラッシュコマンドの同期は設定で無効化されています。", self.display_name)

        # エラーハンドラを設定する
        self.tree.on_error = self.on_app_command_error

    @tasks.loop(seconds=15)
    async def rotate_status(self):
        """ボットのステータスを定期的に変更する"""
        if not self.status_templates:
            return

        # 次のステータスを選択
        status_template = self.status_templates[self.status_index]
        self.status_index = (self.status_index + 1) % len(self.status_templates)

        # プレースホルダーを置換
        try:
            status_text = status_template.format(guild_count=len(self.guilds))
        except KeyError:
            status_text = status_template  # プレースホルダーがない場合はそのまま使用

        # ステータスを更新
        try:
            await self.change_presence(activity=discord.Game(name=status_text))
        except (aiohttp.client_exceptions.ClientConnectionResetError, ConnectionResetError) as e:
            logging.warning(f"Failed to rotate status due to connection reset: {e}")
        except Exception as e:
            logging.error(f"Failed to rotate status: {e}")

    @rotate_status.before_loop
    async def before_rotate_status(self):
        """ステータスローテーションタスクの開始を待機"""
        await self.wait_until_ready()

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """通常コマンド（プレフィックスコマンド）のエラーハンドリング"""
        # CommandNotFoundエラーは無視（コマンドを探すモードを無効化）
        if isinstance(error, commands.CommandNotFound):
            return  # エラーを無視して何もしない
        
        # その他のエラーはログに記録（必要に応じて処理）
        logging.debug(f"コマンドエラー: {error}")

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        """スラッシュコマンドのエラーハンドリング"""
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return  # 無視するエラー

        if isinstance(error, commands.MissingPermissions):
            await interaction.response.send_message("❌ このコマンドを実行する権限がありません。", ephemeral=True)
        elif isinstance(error, (commands.BotMissingPermissions, discord.Forbidden)):
            await interaction.response.send_message("❌ ボットに必要な権限がありません。管理者に連絡してください。",
                                                    ephemeral=True)
        elif isinstance(error, commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ このコマンドは {error.retry_after:.1f} 秒後に再試行できます。",
                                                    ephemeral=True)
        elif isinstance(error, (InvalidDiceNotationError, DiceValueError)):
            await interaction.response.send_message(f"❌ {str(error)}", ephemeral=True)
        else:
            # その他のエラーはログに記録
            logging.error(f"コマンドエラー: {error}", exc_info=error)
            if interaction.response.is_done():
                await interaction.followup.send("❌ コマンドの実行中にエラーが発生しました。", ephemeral=True)
            else:
                await interaction.response.send_message("❌ コマンドの実行中にエラーが発生しました。", ephemeral=True)


# CONFIG_FILE / DEFAULT_CONFIG_FILE は削除済み — configs/*.yaml を使用

# ===============================================================
# ===== Cog ロードリスト ========================================
# ===============================================================
# プライマリボット（PLANA）が読み込む全 Cog
PRIMARY_COGS = [
    'MOMOKA.images.image_commands_cog',
    'MOMOKA.llm.llm_cog',
    'MOMOKA.media_downloader.ytdlp_downloader_cog',
    'MOMOKA.music.music_cog',
    'MOMOKA.notifications.earthquake_notification_cog',
    'MOMOKA.notifications.twitch_notification_cog',
    'MOMOKA.scheduler.match_time_cog',
    'MOMOKA.timer.timer_cog',
    'MOMOKA.tracker.r6s_tracker_cog',
    'MOMOKA.tracker.valorant_tracker_cog',
    'MOMOKA.tts.tts_cog',
    'MOMOKA.utilities.slash_command_cog',
]

# コンパニオンボット（ARONA）が読み込む軽量 Cog（LLM / 音楽 / スラッシュコマンド）
COMPANION_COGS = [
    'MOMOKA.llm.llm_cog',
    'MOMOKA.music.music_cog',
    'MOMOKA.utilities.slash_command_cog',
]

# ===============================================================
# ===== ログビューアGUI関連の関数とクラス ======================
# ===============================================================

def is_dark_mode():
    """OSのダークモード設定を検出"""
    try:
        if platform.system() == 'Windows':
            import darkdetect
            return darkdetect.isDark()
        return False
    except Exception as e:
        print(f"ダークモード検出エラー: {e}")
        return False

# ダークモードの色設定
DARK_BG = '#1e1e1e'
DARK_FG = '#e0e0e0'
DARK_SELECTION_BG = '#264f78'
DARK_SELECTION_FG = '#ffffff'
DARK_INSERT_BG = '#3c3c3c'
DARK_INSERT_FG = '#ffffff'
DARK_SCROLLBAR_BG = '#2d2d2d'
DARK_SCROLLBAR_TROUGH = '#1e1e1e'

# ライトモードの色設定
LIGHT_BG = '#f0f0f0'
LIGHT_FG = '#000000'
LIGHT_SELECTION_BG = '#cce8ff'
LIGHT_SELECTION_FG = '#000000'
LIGHT_INSERT_BG = '#ffffff'
LIGHT_INSERT_FG = '#000000'
LIGHT_SCROLLBAR_BG = '#e0e0e0'
LIGHT_SCROLLBAR_TROUGH = '#f0f0f0'

# 現在のテーマを決定
DARK_THEME = is_dark_mode()

def get_theme_colors():
    """現在のテーマに応じた色を返す"""
    if DARK_THEME:
        return {
            'bg': DARK_BG,
            'fg': DARK_FG,
            'select_bg': DARK_SELECTION_BG,
            'select_fg': DARK_SELECTION_FG,
            'insert_bg': DARK_INSERT_BG,
            'insert_fg': DARK_INSERT_FG,
            'scrollbar_bg': DARK_SCROLLBAR_BG,
            'scrollbar_trough': DARK_SCROLLBAR_TROUGH,
            'button_bg': '#2d2d2d',
            'button_fg': DARK_FG,
            'button_active_bg': '#3c3c3c',
            'button_active_fg': DARK_FG,
            'frame_bg': DARK_BG,
            'label_bg': DARK_BG,
            'label_fg': DARK_FG,
            'entry_bg': DARK_INSERT_BG,
            'entry_fg': DARK_INSERT_FG,
            'text_bg': DARK_INSERT_BG,
            'text_fg': DARK_INSERT_FG,
            'border': '#3c3c3c',
            'error': '#ff6b6b',
            'warning': '#ffd93d',
            'info': '#4dabf7',
            'debug': '#adb5bd'
        }
    else:
        return {
            'bg': LIGHT_BG,
            'fg': LIGHT_FG,
            'select_bg': LIGHT_SELECTION_BG,
            'select_fg': LIGHT_SELECTION_FG,
            'insert_bg': LIGHT_INSERT_BG,
            'insert_fg': LIGHT_INSERT_FG,
            'scrollbar_bg': LIGHT_SCROLLBAR_BG,
            'scrollbar_trough': LIGHT_SCROLLBAR_TROUGH,
            'button_bg': '#e0e0e0',
            'button_fg': LIGHT_FG,
            'button_active_bg': '#d0d0d0',
            'button_active_fg': LIGHT_FG,
            'frame_bg': LIGHT_BG,
            'label_bg': LIGHT_BG,
            'label_fg': LIGHT_FG,
            'entry_bg': LIGHT_INSERT_BG,
            'entry_fg': LIGHT_INSERT_FG,
            'text_bg': LIGHT_INSERT_BG,
            'text_fg': LIGHT_INSERT_FG,
            'border': '#c0c0c0',
            'error': '#dc3545',
            'warning': '#ffc107',
            'info': '#0d6efd',
            'debug': '#6c757d'
        }


class LogViewerApp:
    def __init__(self, root, log_queue):
        self.root = root
        self.log_queue = log_queue
        self.root.title("MOMOKA ログビューア")
        self.root.withdraw()  # ウィンドウを非表示にする
        self.apply_windows_dark_mode()  # 先にダークモードを適用
        self.root.geometry("1200x800")
        
        # テーマカラーを取得
        self.theme = get_theme_colors()
        
        # メインウィンドウの背景色を設定
        self.root.configure(bg=self.theme['bg'])
        
        # 設定ファイルの読み込み
        self.config_file = "data/log_viewer_config.json"
        self.load_config()
        
        # スタイルの設定を初期化（self.styleとして保存）
        self.style = ttk.Style()
        self.setup_styles()
        
        # メニューバーの作成
        self.create_menu()
        
        # GUIの作成
        self.setup_gui()
        
        # キューを定期的にチェック
        self.poll_log_queue()
        # VC / LLM 稼働数を定期更新する
        self.poll_status()
        
        # ウィンドウクローズ時の処理
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.root.deiconify()  # ウィンドウを再表示
    
    def apply_windows_dark_mode(self):
        """Windowsのダークモード設定を適用"""
        if DARK_THEME and platform.system() == 'Windows':
            try:
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                value = 1
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    ctypes.byref(ctypes.c_int(value)),
                    ctypes.sizeof(ctypes.c_int(value))
                )
            except Exception as e:
                print(f"ダークモードの適用中にエラーが発生しました: {e}")
    
    def create_menu(self):
        """メニューバーの作成"""
        self.menubar = tk.Menu(self.root, 
                             bg=self.theme['bg'], 
                             fg=self.theme['fg'],
                             activebackground=self.theme['select_bg'],
                             activeforeground=self.theme['select_fg'],
                             relief='flat',
                             bd=0)
        
        # ファイルメニュー
        file_menu = tk.Menu(self.menubar, 
                          tearoff=0, 
                          bg=self.theme['bg'], 
                          fg=self.theme['fg'],
                          activebackground=self.theme['select_bg'],
                          activeforeground=self.theme['select_fg'],
                          bd=1,
                          relief='solid')
        file_menu.add_command(label="終了", 
                            command=self.root.quit,
                            activebackground=self.theme['select_bg'],
                            activeforeground=self.theme['select_fg'])
        self.menubar.add_cascade(label="ファイル", menu=file_menu)
        
        # 表示メニュー
        view_menu = tk.Menu(self.menubar, 
                          tearoff=0,
                          bg=self.theme['bg'],
                          fg=self.theme['fg'],
                          activebackground=self.theme['select_bg'],
                          activeforeground=self.theme['select_fg'],
                          bd=1,
                          relief='solid')
        
        # 自動スクロールの状態変数を初期化
        self.auto_scroll_var = tk.BooleanVar(value=self.config.get("auto_scroll", True))
        view_menu.add_checkbutton(label="自動スクロール", 
                                variable=self.auto_scroll_var,
                                command=self.toggle_auto_scroll,
                                activebackground=self.theme['select_bg'],
                                activeforeground=self.theme['select_fg'])
        
        self.menubar.add_cascade(label="表示", menu=view_menu)
        
        # ヘルプメニュー
        help_menu = tk.Menu(self.menubar, 
                           tearoff=0,
                           bg=self.theme['bg'],
                           fg=self.theme['fg'],
                           activebackground=self.theme['select_bg'],
                           activeforeground=self.theme['select_fg'],
                           bd=1,
                           relief='solid')
        help_menu.add_command(label="バージョン情報",
                            command=self.show_about,
                            activebackground=self.theme['select_bg'],
                            activeforeground=self.theme['select_fg'])
        self.menubar.add_cascade(label="ヘルプ", menu=help_menu)
        
        self.root.config(menu=self.menubar)
        
        # メニューのスタイルオプション
        self.root.option_add('*Menu*background', self.theme['bg'])
        self.root.option_add('*Menu*foreground', self.theme['fg'])
        self.root.option_add('*Menu*activeBackground', self.theme['select_bg'])
        self.root.option_add('*Menu*activeForeground', self.theme['select_fg'])
    
    def setup_styles(self):
        """スタイルの初期化のみを行う"""
        # テーマの設定
        self.style.theme_use('clam')
        
        # フレームのスタイル
        self.style.configure('TFrame', 
                      background=self.theme['bg'],
                      borderwidth=0)
        
        # ラベルのスタイル
        self.style.configure('TLabel', 
                      background=self.theme['bg'], 
                      foreground=self.theme['fg'],
                      font=('Meiryo UI', 9),
                      padding=2)
        
        # ボタンのスタイル
        self.style.configure('TButton',
                      background=self.theme['button_bg'],
                      foreground=self.theme['button_fg'],
                      borderwidth=1,
                      relief='raised',
                      padding=5)
        
        self.style.map('TButton',
                 background=[('active', self.theme['button_active_bg']),
                           ('pressed', self.theme['select_bg'])],
                 foreground=[('active', self.theme['button_active_fg']),
                           ('pressed', self.theme['select_fg'])],
                 relief=[('pressed', 'sunken'), ('!pressed', 'raised')])
        
        # エントリーのスタイル
        self.style.configure('TEntry',
                      fieldbackground=self.theme['entry_bg'],
                      foreground=self.theme['entry_fg'],
                      insertcolor=self.theme['insert_fg'],
                      borderwidth=1,
                      relief='solid')
        
        # コンボボックスのスタイル
        self.style.configure('TCombobox',
                      fieldbackground=self.theme['entry_bg'],
                      background=self.theme['entry_bg'],
                      foreground=self.theme['entry_fg'],
                      selectbackground=self.theme['select_bg'],
                      selectforeground=self.theme['select_fg'],
                      arrowcolor=self.theme['fg'],
                      borderwidth=1,
                      relief='solid')
        
        self.style.map('TCombobox',
                      fieldbackground=[('readonly', self.theme['entry_bg'])],
                      selectbackground=[('readonly', self.theme['select_bg'])],
                      selectforeground=[('readonly', self.theme['select_fg'])])
        
        # スクロールバーのスタイル
        self.style.configure('Vertical.TScrollbar',
                      background=self.theme['scrollbar_bg'],
                      troughcolor=self.theme['scrollbar_trough'],
                      arrowcolor=self.theme['fg'],
                      bordercolor=self.theme['bg'],
                      darkcolor=self.theme['bg'],
                      lightcolor=self.theme['bg'],
                      gripcount=0,
                      arrowsize=12)
        
        self.style.configure('Horizontal.TScrollbar',
                      background=self.theme['scrollbar_bg'],
                      troughcolor=self.theme['scrollbar_trough'],
                      arrowcolor=self.theme['fg'],
                      bordercolor=self.theme['bg'],
                      darkcolor=self.theme['bg'],
                      lightcolor=self.theme['bg'],
                      gripcount=0,
                      arrowsize=12)
        
        self.style.map('Vertical.TScrollbar',
                      background=[('active', self.theme['scrollbar_bg'])])
        
        # ラベルフレームのスタイル
        self.style.configure('TLabelframe',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'],
                      relief='groove',
                      borderwidth=2)
        
        self.style.configure('TLabelframe.Label',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'])
                      
        # チェックボタンのスタイル
        self.style.configure('TCheckbutton',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'],
                      indicatorbackground=self.theme['bg'],
                      indicatorcolor=self.theme['fg'],
                      selectcolor=self.theme['bg'])
        
        self.style.map('TCheckbutton',
                 background=[('active', self.theme['bg'])],
                 foreground=[('active', self.theme['fg'])])
        
        # ラジオボタンのスタイル
        self.style.configure('TRadiobutton',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'],
                      indicatorbackground=self.theme['bg'],
                      indicatorcolor=self.theme['fg'],
                      selectcolor=self.theme['bg'])
        
        self.style.map('TRadiobutton',
                 background=[('active', self.theme['bg'])],
                 foreground=[('active', self.theme['fg'])])
        
        # メニューボタンのスタイル
        self.style.configure('TMenubutton',
                           borderwidth=2)
    
    def load_config(self):
        """設定ファイルの読み込み"""
        self.config = {
            "font": ("Meiryo UI", 9),
            "max_lines": 1000,
            "auto_scroll": True,
            "log_levels": {
                "general": "INFO",
                "llm": "INFO",
                "tts": "INFO",
                "error": "WARNING"
            }
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    self.config.update(saved_config)
        except Exception as e:
            print(f"設定ファイルの読み込み中にエラーが発生しました: {e}")
    
    def save_config(self):
        """設定ファイルの保存"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"設定ファイルの保存中にエラーが発生しました: {e}")
        
    def setup_gui(self):
        """GUIの作成"""
        # メインフレーム
        main_frame = ttk.Frame(self.root, padding="5", style='TFrame')
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # コントロールフレーム
        control_frame = ttk.Frame(main_frame, padding="5", style='TFrame')
        control_frame.pack(fill=tk.X, padx=5, pady=5)
        
        # ログレベル選択
        log_level_frame = ttk.LabelFrame(control_frame, text="ログレベル", padding=5)
        log_level_frame.pack(side=tk.LEFT, padx=5, pady=5)
        
        # ログレベル用の変数を初期化
        self.general_level_var = tk.StringVar(value=self.config["log_levels"].get("general", "INFO"))
        self.llm_level_var = tk.StringVar(value=self.config["log_levels"].get("llm", "INFO"))
        self.tts_level_var = tk.StringVar(value=self.config["log_levels"].get("tts", "INFO"))
        self.error_level_var = tk.StringVar(value=self.config["log_levels"].get("error", "WARNING"))
        
        # 一般ログレベル
        ttk.Label(log_level_frame, text="一般:").grid(row=0, column=0, padx=2, pady=2, sticky=tk.W)
        general_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.general_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        general_level.grid(row=0, column=1, padx=2, pady=2)
        general_level.bind("<<ComboboxSelected>>", 
                          lambda e: self.update_log_level("general", self.general_level_var.get()))
        
        # LLMログレベル
        ttk.Label(log_level_frame, text="LLM:").grid(row=0, column=2, padx=2, pady=2, sticky=tk.W)
        llm_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.llm_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        llm_level.grid(row=0, column=3, padx=2, pady=2)
        llm_level.bind("<<ComboboxSelected>>", 
                      lambda e: self.update_log_level("llm", self.llm_level_var.get()))
        
        # TTSログレベル
        ttk.Label(log_level_frame, text="TTS:").grid(row=0, column=4, padx=2, pady=2, sticky=tk.W)
        tts_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.tts_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        tts_level.grid(row=0, column=5, padx=2, pady=2)
        tts_level.bind("<<ComboboxSelected>>", 
                      lambda e: self.update_log_level("tts", self.tts_level_var.get()))
        
        # エラーログレベル
        ttk.Label(log_level_frame, text="エラー:").grid(row=0, column=6, padx=2, pady=2, sticky=tk.W)
        error_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.error_level_var,
            values=["WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        error_level.grid(row=0, column=7, padx=2, pady=2)
        error_level.bind("<<ComboboxSelected>>", 
                        lambda e: self.update_log_level("error", self.error_level_var.get()))
        
        # ボタンフレーム
        button_frame = ttk.Frame(control_frame, style='TFrame')
        button_frame.pack(side=tk.RIGHT, padx=5, pady=5)

        # VC / LLM 稼働数表示（起動前はプレースホルダ）
        self.vc_status_var = tk.StringVar(value="VC: -")
        self.llm_status_var = tk.StringVar(value="LLM: -")
        # VC 接続中ギルド数ラベル
        ttk.Label(
            button_frame,
            textvariable=self.vc_status_var,
            style='TLabel',
        ).pack(side=tk.LEFT, padx=6)
        # LLM 生成中ギルド数ラベル
        ttk.Label(
            button_frame,
            textvariable=self.llm_status_var,
            style='TLabel',
        ).pack(side=tk.LEFT, padx=6)
        
        # クリアボタン
        clear_button = ttk.Button(
            button_frame,
            text="ログをクリア",
            command=self.clear_all_logs
        )
        clear_button.pack(side=tk.LEFT, padx=2)
        
        # 自動スクロールチェックボタン
        auto_scroll = ttk.Checkbutton(
            button_frame,
            text="自動スクロール",
            variable=self.auto_scroll_var,
            command=self.toggle_auto_scroll
        )
        auto_scroll.pack(side=tk.LEFT, padx=2)

        # シャットダウンボタン（二重押し防止のため参照を保持）
        self.shutdown_button = ttk.Button(
            button_frame,
            text="シャットダウン",
            command=self.request_shutdown,
        )
        # 危険操作なので右端に置く
        self.shutdown_button.pack(side=tk.LEFT, padx=6)
        # シャットダウン要求中フラグ
        self._shutdown_requested = False
        
        # ログ表示エリアのフレーム
        log_frame = ttk.Frame(main_frame, style='TFrame')
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # グリッドの設定
        log_frame.columnconfigure(0, weight=1)
        log_frame.columnconfigure(1, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        
        # 左上: 一般ログ
        general_frame = ttk.LabelFrame(log_frame, text="一般ログ", padding="2", style='TLabelframe')
        general_frame.grid(row=0, column=0, padx=2, pady=2, sticky="nsew")
        self.general_log = scrolledtext.ScrolledText(
            general_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['text_fg'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.general_log.pack(fill=tk.BOTH, expand=True)
        
        # 右上: LLMログ
        llm_frame = ttk.LabelFrame(log_frame, text="LLMログ", padding="2", style='TLabelframe')
        llm_frame.grid(row=0, column=1, padx=2, pady=2, sticky="nsew")
        self.llm_log = scrolledtext.ScrolledText(
            llm_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['text_fg'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.llm_log.pack(fill=tk.BOTH, expand=True)
        
        # 左下: TTSログ
        tts_frame = ttk.LabelFrame(log_frame, text="TTSログ", padding="2", style='TLabelframe')
        tts_frame.grid(row=1, column=0, padx=2, pady=2, sticky="nsew")
        self.tts_log = scrolledtext.ScrolledText(
            tts_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['text_fg'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.tts_log.pack(fill=tk.BOTH, expand=True)
        
        # 右下: エラーログ
        error_frame = ttk.LabelFrame(log_frame, text="エラーログ", padding="2", style='TLabelframe')
        error_frame.grid(row=1, column=1, padx=2, pady=2, sticky="nsew")
        self.error_log = scrolledtext.ScrolledText(
            error_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['error'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.error_log.pack(fill=tk.BOTH, expand=True)
        
        # ステータスバー
        self.status_var = tk.StringVar()
        self.status_var.set("準備完了")
        status_bar = ttk.Label(
            self.root, 
            textvariable=self.status_var, 
            relief=tk.SUNKEN, 
            anchor=tk.W,
            style='TLabel'
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, ipady=2)
        
        # コンテキストメニューの設定
        self.setup_context_menu(self.general_log)
        self.setup_context_menu(self.llm_log)
        self.setup_context_menu(self.tts_log)
        self.setup_context_menu(self.error_log)
        
        # キーバインドの設定
        self.root.bind_all("<Control-c>", lambda e: self.copy_text(self.root.focus_get()))
        self.root.bind_all("<Control-a>", lambda e: self.select_all(self.root.focus_get()))
        
        # 初期フォーカスを設定
        self.general_log.focus_set()
    
    def setup_context_menu(self, widget):
        """コンテキストメニューの設定"""
        def show_menu(event):
            menu = tk.Menu(self.root, tearoff=0,
                         bg=self.theme['bg'],
                         fg=self.theme['fg'],
                         activebackground=self.theme['select_bg'],
                         activeforeground=self.theme['select_fg'])
            menu.add_command(label="コピー", command=lambda: self.copy_text(widget))
            menu.add_separator()
            menu.add_command(label="すべて選択", command=lambda: self.select_all(widget))
            menu.add_command(label="クリア", command=lambda: self.clear_log(widget))
            
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        
        widget.bind("<Button-3>", show_menu)
    
    def copy_text(self, widget):
        """選択されたテキストをコピー"""
        try:
            selected_text = widget.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected_text)
            self.status_var.set("選択されたテキストをクリップボードにコピーしました")
        except tk.TclError:
            self.status_var.set("コピーするテキストが選択されていません")
    
    def select_all(self, widget):
        """すべてのテキストを選択"""
        widget.tag_add(tk.SEL, "1.0", tk.END)
        widget.mark_set(tk.INSERT, "1.0")
        widget.see(tk.INSERT)
        return 'break'
    
    def clear_log(self, widget):
        """指定されたウィジェットのログをクリア"""
        widget.config(state='normal')
        widget.delete(1.0, tk.END)
        widget.config(state='disabled')
        self.status_var.set("ログをクリアしました")
    
    def clear_all_logs(self):
        """すべてのログをクリア"""
        for widget in [self.general_log, self.llm_log, self.tts_log, self.error_log]:
            self.clear_log(widget)
        self.status_var.set("すべてのログをクリアしました")
    
    def toggle_auto_scroll(self):
        """自動スクロールの切り替え"""
        self.config["auto_scroll"] = self.auto_scroll_var.get()
        self.save_config()
        status = "有効" if self.auto_scroll_var.get() else "無効"
        self.status_var.set(f"自動スクロールを{status}にしました")
    
    def update_log_level(self, log_type, level):
        """ログレベルの更新"""
        self.config["log_levels"][log_type] = level
        self.save_config()
        self.status_var.set(f"{log_type}のログレベルを{level}に設定しました")
    
    def poll_log_queue(self):
        """ログキューを定期的にチェック"""
        try:
            while True:
                name, level, log_entry = self.log_queue.get_nowait()
                self.process_log_entry(name, level, log_entry)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.poll_log_queue)
    
    def process_log_entry(self, name, level, log_entry):
        """ログエントリを処理 - すべてのログを確実にGUIに表示"""
        # ログレベルに基づいてフィルタリング
        log_levels = {
            "DEBUG": 10,
            "INFO": 20,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50
        }
        
        # エラーログは常にエラーログウィジェットに表示
        if level in ["ERROR", "CRITICAL"]:
            widget = self.error_log
            min_level = log_levels.get(self.error_level_var.get(), 30)
            # エラーログは常に表示（フィルタリングを緩和）
            if log_levels.get(level, 0) >= min_level:
                self.append_to_log(widget, log_entry, level)
            return
        
        # 標準出力からのログは一般ログに表示
        if name == "stdout":
            widget = self.general_log
            min_level = log_levels.get(self.general_level_var.get(), 20)
            # 標準出力は常にINFOレベルとして扱う
            if log_levels.get("INFO", 20) >= min_level:
                self.append_to_log(widget, log_entry, "INFO")
            return
        
        # ログの種類を判定（MOMOKAモジュールのログを分類）
        if "MOMOKA.llm" in name:
            widget = self.llm_log
            min_level = log_levels.get(self.llm_level_var.get(), 20)
        elif "MOMOKA.tts" in name:
            widget = self.tts_log
            min_level = log_levels.get(self.tts_level_var.get(), 20)
        else:
            # その他のすべてのログは一般ログに表示
            widget = self.general_log
            min_level = log_levels.get(self.general_level_var.get(), 20)
        
        # ログレベルが閾値以上の場合のみ表示
        # すべてのログを確実に表示するため、レベルチェックを実行
        if log_levels.get(level, 0) >= min_level:
            self.append_to_log(widget, log_entry, level)
    
    def append_to_log(self, text_widget, message, level=None):
        """ログをテキストウィジェットに追加"""
        text_widget.config(state='normal')
        
        # 行数制限を適用する
        lines = int(text_widget.index('end-1c').split('.')[0])
        if lines > self.config["max_lines"]:
            # 古いログを削除する
            text_widget.delete(1.0, f"{lines - self.config['max_lines']}.0")
        
        # ボット識別タグ（[PLANA] / [ARONA]）の色付けを設定する
        text_widget.tag_config("plana_tag", foreground="#4dabf7", font=("Meiryo UI", 9, "bold"))
        text_widget.tag_config("arona_tag", foreground="#ff6b9d", font=("Meiryo UI", 9, "bold"))
        
        # レベルに応じた色付けを設定する
        if level == "ERROR" or level == "CRITICAL":
            text_widget.tag_config("error", foreground=self.theme['error'])
            tag = "error"
        elif level == "WARNING":
            text_widget.tag_config("warning", foreground=self.theme['warning'])
            tag = "warning"
        elif level == "INFO":
            text_widget.tag_config("info", foreground=self.theme['info'])
            tag = "info"
        elif level == "DEBUG":
            text_widget.tag_config("debug", foreground=self.theme['debug'])
            tag = "debug"
        else:
            tag = None
        
        # メッセージ内に [PLANA] または [ARONA] タグがあるか検出する
        if "[PLANA]" in message or "[LLM_RESPONSE][PLANA]" in message:
            # [PLANA] タグ部分を色付けして挿入する
            parts = message.split("[PLANA]")
            for i, part in enumerate(parts):
                if i > 0:
                    # タグを色付けして挿入する
                    text_widget.insert(tk.END, "[PLANA]", "plana_tag")
                # 残りのテキストを通常タグで挿入する
                if tag:
                    text_widget.insert(tk.END, part, tag)
                else:
                    text_widget.insert(tk.END, part)
            text_widget.insert(tk.END, "\n")
        elif "[ARONA]" in message or "[LLM_RESPONSE][ARONA]" in message:
            # [ARONA] タグ部分を色付けして挿入する
            parts = message.split("[ARONA]")
            for i, part in enumerate(parts):
                if i > 0:
                    # タグを色付けして挿入する
                    text_widget.insert(tk.END, "[ARONA]", "arona_tag")
                # 残りのテキストを通常タグで挿入する
                if tag:
                    text_widget.insert(tk.END, part, tag)
                else:
                    text_widget.insert(tk.END, part)
            text_widget.insert(tk.END, "\n")
        else:
            # タグ無しの場合は通常通り挿入する
            if tag:
                text_widget.insert(tk.END, message + "\n", tag)
            else:
                text_widget.insert(tk.END, message + "\n")
        
        # 自動スクロールが有効なら末尾へスクロールする
        if self.config["auto_scroll"]:
            text_widget.see(tk.END)
        
        text_widget.config(state='disabled')
    
    def show_about(self):
        """バージョン情報を表示"""
        about_window = tk.Toplevel(self.root)
        about_window.title("バージョン情報")
        about_window.transient(self.root)
        about_window.resizable(False, False)
        about_window.configure(bg=self.theme['bg'])
        
        # 中央に配置
        window_width = 300
        window_height = 150
        screen_width = about_window.winfo_screenwidth()
        screen_height = about_window.winfo_screenheight()
        x = (screen_width // 2) - (window_width // 2)
        y = (screen_height // 2) - (window_height // 2)
        about_window.geometry(f'{window_width}x{window_height}+{x}+{y}')
        
        # バージョン情報
        version_label = ttk.Label(
            about_window,
            text="MOMOKA ログビューア\nバージョン 1.0.0\n\n© 2025 MOMOKA Project",
            justify=tk.CENTER,
            style='TLabel'
        )
        version_label.pack(expand=True, padx=20, pady=20)
        
        # OKボタン
        ok_button = ttk.Button(
            about_window,
            text="OK",
            command=about_window.destroy,
            style='TButton'
        )
        ok_button.pack(pady=(0, 20))
        
        # モーダルダイアログとして表示
        about_window.grab_set()
        about_window.focus_set()
        about_window.wait_window()
    
    def poll_status(self):
        """VC / LLM 稼働ギルド数を約1秒ごとに更新する。"""
        try:
            # Bot 未生成ならプレースホルダを維持する
            if bot_ref is None:
                # 起動前表示
                self.vc_status_var.set("VC: -")
                # 起動前表示
                self.llm_status_var.set("LLM: -")
            else:
                # MusicCog から接続中ギルド数を取得する
                music_cog = bot_ref.get_cog("music_cog")
                # ゲッターがあれば件数を表示し、無ければプレースホルダ
                if music_cog is not None and hasattr(music_cog, "get_active_vc_guild_count"):
                    # VC 接続数をラベルへ反映する
                    self.vc_status_var.set(f"VC: {music_cog.get_active_vc_guild_count()}")
                else:
                    # Cog 未ロード時
                    self.vc_status_var.set("VC: -")
                # LLMCog から生成中ギルド数を取得する
                llm_cog = bot_ref.get_cog("LLM")
                # ゲッターがあれば件数を表示し、無ければプレースホルダ
                if llm_cog is not None and hasattr(llm_cog, "get_active_llm_guild_count"):
                    # LLM 稼働数をラベルへ反映する
                    self.llm_status_var.set(f"LLM: {llm_cog.get_active_llm_guild_count()}")
                else:
                    # Cog 未ロード時
                    self.llm_status_var.set("LLM: -")
        except Exception:
            # GUI スレッドを落とさないため表示はそのまま維持する
            pass
        finally:
            # 1秒後に再スケジュールする
            self.root.after(1000, self.poll_status)

    def request_shutdown(self):
        """確認後に Bot を /shutdown と同じ経路で閉じる。"""
        # 二重押しを無視する
        if self._shutdown_requested:
            # 早期リターン
            return
        # 誤操作防止の確認ダイアログ
        confirmed = messagebox.askyesno(
            "シャットダウン確認",
            "ボットをシャットダウンしますか？\n利用中ユーザーへ再起動通知が送られます。",
            parent=self.root,
        )
        # キャンセルなら何もしない
        if not confirmed:
            # 早期リターン
            return
        # Bot 未生成なら実行できない
        if bot_ref is None:
            # ステータスバーへ理由を出す
            self.status_var.set("ボット未起動のためシャットダウンできません")
            # 早期リターン
            return
        try:
            # discord.py のイベントループを取得する
            loop = bot_ref.loop
            # ループ未稼働なら安全に中断する
            if loop is None or not loop.is_running():
                # ステータスバーへ理由を出す
                self.status_var.set("イベントループが利用できないためシャットダウンできません")
                # 早期リターン
                return
            # 二重実行を防ぐ
            self._shutdown_requested = True
            # ボタンを無効化する
            self.shutdown_button.config(state=tk.DISABLED)
            # 進行状況をステータスバーへ出す
            self.status_var.set("シャットダウン中...")
            # GUI スレッドから asyncio へ close() を投げる
            # 両 Bot をレジストリ経由で閉じる（GUI シャットダウン）
            from MOMOKA.bots.registry import registry
            asyncio.run_coroutine_threadsafe(registry.close_all(), loop)
        except Exception as e:
            # 失敗したら再試行できるように戻す
            self._shutdown_requested = False
            # ボタンを再び有効化する
            self.shutdown_button.config(state=tk.NORMAL)
            # 失敗理由をステータスバーへ出す
            self.status_var.set(f"シャットダウン失敗: {e}")

    def on_closing(self):
        """ウィンドウを閉じる時の処理"""
        # 設定を保存
        self.save_config()
        # ウィンドウを閉じる（ボットは継続実行）
        self.root.destroy()


def run_log_viewer_thread(log_queue):
    """ログビューアをスレッドで起動"""
    def run_gui():
        try:
            root = tk.Tk()
            app = LogViewerApp(root, log_queue)
            root.mainloop()
        except Exception as e:
            print(f"ログビューアでエラーが発生しました: {e}")
            import traceback
            traceback.print_exc()
    
    thread = threading.Thread(target=run_gui, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    momoka_art = r"""
███╗   ███╗ ██████╗ ███╗   ███╗ ██████╗ ██╗  ██╗ █████╗ 
████╗ ████║██╔═══██╗████╗ ████║██╔═══██╗██║ ██╔╝██╔══██╗
██╔████╔██║██║   ██║██╔████╔██║██║   ██║█████╔╝ ███████║
██║╚██╔╝██║██║   ██║██║╚██╔╝██║██║   ██║██╔═██╗ ██╔══██║
██║ ╚═╝ ██║╚██████╔╝██║ ╚═╝ ██║╚██████╔╝██║  ██╗██║  ██║
╚═╝     ╚═╝ ╚═════╝ ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
    """
    print(momoka_art)
    
    # ログビューアをスレッドで起動する
    log_viewer_thread = run_log_viewer_thread(log_queue)
    print("ログビューアを起動しました。")

    # --- 設定の読み込みとバリデーション ---
    from MOMOKA.config.loader import load_merged_config, validate_bot_tokens
    from MOMOKA.bots.registry import registry
    from MOMOKA.llm.debate.orchestrator import init_orchestrator

    # configs/*.yaml を統合した設定辞書を読み込む
    try:
        # マージ済み設定辞書を取得する
        merged_config = load_merged_config()
        print("INFO: configs/bots_config.yaml などの設定を統合しました。")
    except Exception as e_load:
        print(f"CRITICAL: 設定ファイルの読み込み中にエラーが発生しました: {e_load}")
        sys.exit(1)

    # ボットトークンの存在確認を行う
    try:
        # PLANA / ARONA のトークンをバリデーションする
        validate_bot_tokens(merged_config)
        print("INFO: ボットトークンのバリデーションに成功しました。")
    except ValueError as e_token:
        print(f"CRITICAL: {e_token}")
        sys.exit(1)

    # Debate オーケストレータを初期化する
    try:
        # グローバルオーケストレータを初期化する
        init_orchestrator(merged_config)
        print("INFO: Debate オーケストレータを初期化しました。")
    except Exception as e_orch:
        print(f"CRITICAL: Debate オーケストレータの初期化中にエラーが発生しました: {e_orch}")
        sys.exit(1)

    # --- Discord Intents / AllowedMentions の設定 ---
    # 両ボット共通の Intents を作成する
    intents = discord.Intents.default()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.voice_states = True
    intents.message_content = True  # 特権インテント（申請済み前提）
    intents.members = False
    intents.presences = False

    # メンション設定を作成する
    allowed_mentions = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=True)

    # モバイル識別関数をパッチする
    discord.gateway.DiscordWebSocket.identify = mobile_identify

    # --- PLANA（プライマリボット）の作成 ---
    # PLANA の設定ブロックを取得する
    plana_bot_config = merged_config['bots']['plana']
    # PLANA のトークンを取得する
    plana_token = plana_bot_config['token']
    # PLANA のペルソナキーを取得する
    plana_persona = plana_bot_config.get('persona', 'plana')
    # PLANA の表示名を取得する
    plana_display = plana_bot_config.get('display_name', 'PLANA')

    # PLANA ボットインスタンスを作成する
    plana_bot = Momoka(
        command_prefix=commands.when_mentioned,
        intents=intents,
        help_command=None,
        allowed_mentions=allowed_mentions,
        config=merged_config,
        bot_id='plana',
        bot_role='primary',
        persona_key=plana_persona,
        display_name=plana_display,
        cogs_to_load=PRIMARY_COGS,
        enable_discord_logging=True,  # primary のみ Discord へログ出力
    )
    # レジストリへ PLANA を登録する
    registry.register('plana', plana_bot, plana_display)
    print(f"INFO: PLANA ボットを作成し、レジストリへ登録しました。")

    # --- ARONA（コンパニオンボット）の作成 ---
    # ARONA の設定ブロックを取得する
    arona_bot_config = merged_config['bots']['arona']
    # ARONA のトークンを取得する
    arona_token = arona_bot_config['token']
    # ARONA のペルソナキーを取得する
    arona_persona = arona_bot_config.get('persona', 'arona')
    # ARONA の表示名を取得する
    arona_display = arona_bot_config.get('display_name', 'ARONA')

    # ARONA ボットインスタンスを作成する
    arona_bot = Momoka(
        command_prefix=commands.when_mentioned,
        intents=intents,
        help_command=None,
        allowed_mentions=allowed_mentions,
        config=merged_config,
        bot_id='arona',
        bot_role='companion',
        persona_key=arona_persona,
        display_name=arona_display,
        cogs_to_load=COMPANION_COGS,
        enable_discord_logging=False,  # companion は Discord ログ出力しない
    )
    # レジストリへ ARONA を登録する
    registry.register('arona', arona_bot, arona_display)
    print(f"INFO: ARONA ボットを作成し、レジストリへ登録しました。")

    # GUI 稼働モニタ / シャットダウンボタンから参照できるように PLANA を共有する
    bot_ref = plana_bot

    # ===============================================================
    # ===== シャットダウン / Cogリロードコマンド（PLANA のみ） =======
    # ===============================================================
    @plana_bot.tree.command(
        name="shutdown",
        description="Shut down both bots after notifying active users (owner only).",
    )
    async def shutdown_command(interaction: discord.Interaction):
        # ハードコード UID 以外は拒否する
        if interaction.user.id != Momoka.SHUTDOWN_USER_ID:
            # 権限不足を ephemeral で返す
            await interaction.response.send_message(
                "❌ You are not allowed to use this command.",
                ephemeral=True,
            )
            # 処理終了
            return
        # シャットダウン開始を応答する
        await interaction.response.send_message(
            "Shutting down both bots... Active users will be notified.",
            ephemeral=False,
        )
        # 実行者をログに残す
        logging.info(
            "/shutdown was executed by user %s (%s)",
            interaction.user,
            interaction.user.id,
        )
        # レジストリの全ボットをクローズする（再起動通知を含む）
        await registry.close_all()

    @plana_bot.tree.command(name="reload_plana", description="🔄 PLANAのCogをリロードします（管理者専用）")
    async def reload_plana_cog(interaction: discord.Interaction, cog_name: str = None):
        # 管理者でなければ拒否する
        if not plana_bot.is_admin(interaction.user.id):
            await interaction.response.send_message("❌ このコマンドは管理者のみ実行できます。", ephemeral=False)
            return

        # リロード処理を開始する
        await interaction.response.defer(ephemeral=False)

        if cog_name:
            # 特定の Cog をリロードする
            if not cog_name.startswith('MOMOKA.'):
                # プレフィックスを補完する
                cog_name = f'MOMOKA.{cog_name}'

            try:
                # Cog をリロードする
                await plana_bot.reload_extension(cog_name)
                await interaction.followup.send(f"✅ Cog `{cog_name}` をリロードしました。", ephemeral=False)
                logging.info(f"Cog '{cog_name}' がユーザー {interaction.user} によってリロードされました。")
            except commands.ExtensionNotLoaded:
                # 未ロードの場合は新規ロードする
                try:
                    await plana_bot.load_extension(cog_name)
                    await interaction.followup.send(f"✅ Cog `{cog_name}` をロードしました（未ロードでした）。",
                                                    ephemeral=False)
                    logging.info(f"Cog '{cog_name}' がユーザー {interaction.user} によってロードされました。")
                except Exception as e:
                    await interaction.followup.send(f"❌ Cog `{cog_name}` のロードに失敗しました: {e}", ephemeral=False)
                    logging.error(f"Cog '{cog_name}' のロードに失敗しました: {e}")
            except Exception as e:
                await interaction.followup.send(f"❌ Cog `{cog_name}` のリロードに失敗しました: {e}", ephemeral=False)
                logging.error(f"Cog '{cog_name}' のリロードに失敗しました: {e}")
        else:
            # 全 Cog をリロードする
            reloaded = []
            failed = []

            # Cog リストをループする
            for module_path in plana_bot.cogs_to_load:
                try:
                    # Cog をリロードする
                    await plana_bot.reload_extension(module_path)
                    reloaded.append(module_path)
                except commands.ExtensionNotLoaded:
                    # 未ロードの場合は新規ロードする
                    try:
                        await plana_bot.load_extension(module_path)
                        reloaded.append(f"{module_path} (新規ロード)")
                    except Exception as e:
                        failed.append(f"{module_path}: {e}")
                except Exception as e:
                    failed.append(f"{module_path}: {e}")

            # 結果メッセージを作成する
            result_msg = f"✅ {len(reloaded)}個のCogをリロード/ロードしました。"
            if failed:
                result_msg += f"\n❌ {len(failed)}個のCogでエラーが発生しました。"

            await interaction.followup.send(result_msg, ephemeral=False)
            logging.info(
                f"全Cogリロードがユーザー {interaction.user} によって実行されました。成功: {len(reloaded)}, 失敗: {len(failed)}"
            )

    @plana_bot.tree.command(name="list_plana_cogs", description="📋 PLANAのロード済みCog一覧を表示します")
    async def list_plana_cogs(interaction: discord.Interaction):
        # ロード済み拡張機能のリストを取得する
        loaded_extensions = list(plana_bot.extensions.keys())
        if not loaded_extensions:
            await interaction.response.send_message("現在ロードされているCogはありません。", ephemeral=False)
            return

        # Cog リストを整形する
        cog_list = "\n".join([f"• `{ext}`" for ext in sorted(loaded_extensions)])
        await interaction.response.send_message(
            f"**PLANA ロード済みCog一覧** ({len(loaded_extensions)}個):\n{cog_list}",
            ephemeral=False
        )

    # --- 両ボットの起動 ---
    async def run_bots():
        """PLANA と ARONA の両方を並行起動する。"""
        try:
            # 両ボットを並行起動する
            await asyncio.gather(
                plana_bot.start(plana_token),
                arona_bot.start(arona_token),
            )
        except KeyboardInterrupt:
            # Ctrl+C でシャットダウンする
            print("\nINFO: KeyboardInterrupt を検出しました。シャットダウンを開始します...")
            # レジストリの全ボットをクローズする
            await registry.close_all()
        except Exception as e_run:
            # 実行中のエラーをログに残す
            logging.critical(f"ボットの実行中に致命的なエラーが発生しました: {e_run}", exc_info=True)
            print(f"CRITICAL: ボットの実行中に致命的なエラーが発生しました: {e_run}")
            # レジストリの全ボットをクローズする
            await registry.close_all()
            sys.exit(1)

    # asyncio イベントループで両ボットを起動する
    try:
        asyncio.run(run_bots())
    except KeyboardInterrupt:
        # 既に run_bots 内で処理済み
        print("INFO: シャットダウン完了。")
    except Exception as e_main:
        logging.critical(f"メインループで致命的なエラーが発生しました: {e_main}", exc_info=True)
        print(f"CRITICAL: メインループで致命的なエラーが発生しました: {e_main}")
        sys.exit(1)
