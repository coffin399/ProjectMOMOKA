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
from tkinter import messagebox
import subprocess
import atexit
from pathlib import Path
import ctypes

# モバイルアプリとして識別するための関数
async def mobile_identify(self):
    """Discordのモバイルアプリとして識別するための関数"""
    # 通常のidentifyペイロードを取得
    payload = {
        'op': self.IDENTIFY,
        'd': {
            'token': self.token,
            'properties': {
                '$os': sys.platform,
                '$browser': 'Discord iOS',  # モバイルアプリとして識別
                '$device': 'discord.py',
                '$referrer': '',
                '$referring_domain': ''
            },
            'compress': True,
            'large_threshold': 250,
            'v': 3
        }
    }
    
    # 必要に応じてintentsを追加
    if self._connection.intents is not None:
        payload['d']['intents'] = self._connection.intents.value
    
    # セッションIDを追加（存在する場合）
    if self._connection.session_id is not None:
        payload['d']['session_id'] = self._connection.session_id
    
    # セッションIDを追加（存在する場合）
    if self._connection._activity is not None or self._connection._status is not None:
        payload['d']['presence'] = {
            'status': self._connection._status or 'online',
            'game': self._connection._activity,
            'since': 0,
            'afk': False
        }
    
    # プロパティを上書きしてモバイルアプリとして識別
    payload['d']['properties']['$os'] = 'iOS'
    payload['d']['properties']['$browser'] = 'Discord iOS'
    payload['d']['properties']['$device'] = 'iPhone'
    payload['d']['properties']['$referrer'] = ''
    payload['d']['properties']['$referring_domain'] = ''
    
    # 識別情報を送信
    await self.call_hooks('before_identify', self.shard_id, initial=self._initial_identify)
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

# キューにログを送信するハンドラを追加
queue_handler = QueueHandler(log_queue)
root_logger.addHandler(queue_handler)

from MOMOKA.services.discord_handler import DiscordLogHandler, DiscordLogFormatter
from MOMOKA.utilities.error.errors import InvalidDiceNotationError, DiceValueError

class Momoka(commands.Bot):
    """MOMOKA Botのメインクラス"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = None
        self.status_templates = []
        self.status_index = 0
        # ロードするCogのリスト
        self.cogs_to_load = [
            'MOMOKA.images.image_commands_cog',
            'MOMOKA.llm.llm_cog',
            'MOMOKA.media_downloader.ytdlp_downloader_cog',
            'MOMOKA.music.music_cog',
            'MOMOKA.notifications.earthquake_notification_cog',
            'MOMOKA.notifications.twitch_notification_cog',
            'MOMOKA.timer.timer_cog',
            'MOMOKA.tracker.r6s_tracker_cog',
            'MOMOKA.tracker.valorant_tracker_cog',
            'MOMOKA.tts.tts_cog',
            'MOMOKA.utilities.slash_command_cog',
        ]
    
    def is_admin(self, user_id: int) -> bool:
        """ユーザーが管理者かどうかをチェック"""
        admin_ids = self.config.get('admin_user_ids', [])
        return user_id in admin_ids
    
    async def setup_hook(self):
        """Botの初期セットアップ（ログイン後、接続準備完了前）"""
        # 設定ファイルの読み込み
        if not os.path.exists(CONFIG_FILE):
            if os.path.exists(DEFAULT_CONFIG_FILE):
                try:
                    shutil.copyfile(DEFAULT_CONFIG_FILE, CONFIG_FILE)
                    logging.info(f"{CONFIG_FILE} が見つからなかったため、{DEFAULT_CONFIG_FILE} からコピーして生成しました。")
                    logging.warning(f"生成された {CONFIG_FILE} を確認し、ボットトークンやAPIキーを設定してください。")
                except Exception as e_copy:
                    print(f"CRITICAL: {DEFAULT_CONFIG_FILE} から {CONFIG_FILE} のコピー中にエラーが発生しました: {e_copy}")
                    raise RuntimeError(f"{CONFIG_FILE} の生成に失敗しました。")
            else:
                print(f"CRITICAL: {CONFIG_FILE} も {DEFAULT_CONFIG_FILE} も見つかりません。設定ファイルがありません。")
                raise FileNotFoundError(f"{CONFIG_FILE} も {DEFAULT_CONFIG_FILE} も見つかりません。")
        
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
                if not self.config:
                    print(f"CRITICAL: {CONFIG_FILE} が空または無効です。ボットを起動できません。")
                    raise RuntimeError(f"{CONFIG_FILE} が空または無効です。")
            logging.info(f"{CONFIG_FILE} を正常に読み込みました。")
        except Exception as e:
            print(f"CRITICAL: {CONFIG_FILE} の読み込みまたは解析中にエラーが発生しました: {e}")
            raise
        
        # ロギング設定
        logging_json_path = "data/logging_channels.json"
        log_channel_ids_from_config = self.config.get('log_channel_ids', [])
        if not isinstance(log_channel_ids_from_config, list):
            log_channel_ids_from_config = []
            logging.warning("config.yaml の 'log_channel_ids' はリスト形式である必要があります。")
        
        log_channel_ids_from_file = []
        try:
            dir_path = os.path.dirname(logging_json_path)
            os.makedirs(dir_path, exist_ok=True)
            if not os.path.exists(logging_json_path):
                with open(logging_json_path, 'w') as f:
                    json.dump([], f)
                logging.info(f"{logging_json_path} が見つからなかったため、新規作成しました。")
            
            with open(logging_json_path, 'r') as f:
                data = json.load(f)
                if isinstance(data, list) and all(isinstance(i, int) for i in data):
                    log_channel_ids_from_file = data
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"{logging_json_path} の処理中にエラーが発生しました: {e}")
        
        all_log_channel_ids = list(set(log_channel_ids_from_config + log_channel_ids_from_file))
        
        if all_log_channel_ids:
            try:
                discord_handler = DiscordLogHandler(bot=self, channel_ids=all_log_channel_ids, interval=6.0)
                discord_handler.setLevel(logging.INFO)
                discord_formatter = DiscordLogFormatter('%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')
                discord_handler.setFormatter(discord_formatter)
                root_logger.addHandler(discord_handler)
                logging.info(f"DiscordへのロギングをチャンネルID {all_log_channel_ids} で有効化しました。")
            except Exception as e:
                logging.error(f"DiscordLogHandler の初期化中にエラーが発生しました: {e}")
        else:
            logging.warning("ログ送信先のDiscordチャンネルが設定されていません。")
        
        # Cogのロード
        logging.info("Cogのロードを開始します...")
        loaded_cogs_count = 0
        for module_path in self.cogs_to_load:
            try:
                await self.load_extension(module_path)
                logging.info(f"  > Cog '{module_path}' のロードに成功しました。")
                loaded_cogs_count += 1
            except commands.ExtensionAlreadyLoaded:
                logging.debug(f"Cog '{module_path}' は既にロードされています。")
            except commands.ExtensionNotFound:
                logging.error(f"  > Cog '{module_path}' が見つかりません。ファイルパスを確認してください。")
            except commands.NoEntryPointError:
                logging.error(f"  > Cog '{module_path}' に setup 関数が見つかりません。Cogとして正しく実装されていますか？")
            except Exception as e:
                logging.error(f"  > Cog '{module_path}' のロード中に予期しないエラーが発生しました: {e}", exc_info=True)
        logging.info(f"Cogのロードが完了しました。合計 {loaded_cogs_count} 個のCogをロードしました。")
        
        # スラッシュコマンドの同期
        if self.config.get('sync_slash_commands', True):
            try:
                test_guild_id = self.config.get('test_guild_id')
                if test_guild_id:
                    guild_obj = discord.Object(id=int(test_guild_id))
                    synced_commands = await self.tree.sync(guild=guild_obj)
                    logging.info(f"{len(synced_commands)}個のスラッシュコマンドをテストギルド {test_guild_id} に同期しました。")
                else:
                    synced_commands = await self.tree.sync()
                    logging.info(f"{len(synced_commands)}個のグローバルスラッシュコマンドを同期しました。")
            except Exception as e:
                logging.error(f"スラッシュコマンドの同期中にエラーが発生しました: {e}", exc_info=True)
        else:
            logging.info("スラッシュコマンドの同期は設定で無効化されています。")
        
        # エラーハンドラの設定
        self.tree.on_error = self.on_app_command_error
    
    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        """スラッシュコマンドのエラーハンドリング"""
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return  # 無視するエラー
            
        if isinstance(error, commands.MissingPermissions):
            await interaction.response.send_message("❌ このコマンドを実行する権限がありません。", ephemeral=True)
        elif isinstance(error, (commands.BotMissingPermissions, discord.Forbidden)):
            await interaction.response.send_message("❌ ボットに必要な権限がありません。管理者に連絡してください。", ephemeral=True)
        elif isinstance(error, commands.CommandOnCooldown):
            await interaction.response.send_message(f"⏳ このコマンドは {error.retry_after:.1f} 秒後に再試行できます。", ephemeral=True)
        elif isinstance(error, (InvalidDiceNotationError, DiceValueError)):
            await interaction.response.send_message(f"❌ {str(error)}", ephemeral=True)
        else:
            # その他のエラーはログに記録
            logging.error(f"コマンドエラー: {error}", exc_info=error)
            if interaction.response.is_done():
                await interaction.followup.send("❌ コマンドの実行中にエラーが発生しました。", ephemeral=True)
            else:
                await interaction.response.send_message("❌ コマンドの実行中にエラーが発生しました。", ephemeral=True)

CONFIG_FILE = 'config.yaml'
DEFAULT_CONFIG_FILE = 'config.default.yaml'

# ... (以下のコードは変更なし)

def run_log_viewer():
    """ログビューアを別プロセスで起動"""
    try:
        # ログビューアのパスを取得
        log_viewer_path = Path(__file__).parent / "log_viewer.py"
        if not log_viewer_path.exists():
            print("ログビューアが見つかりません。")
            return None
        
        # 別プロセスでログビューアを起動
        process = subprocess.Popen(
            [sys.executable, str(log_viewer_path)],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        
        # 終了時にプロセスを確実に終了させる
        def cleanup():
            try:
                if process.poll() is None:  # プロセスがまだ実行中の場合
                    process.terminate()
                    process.wait(timeout=3)
            except Exception as e:
                print(f"ログビューアの終了中にエラーが発生しました: {e}")
        
        atexit.register(cleanup)
        return process
    except Exception as e:
        print(f"ログビューアの起動中にエラーが発生しました: {e}")
        return None

if __name__ == "__main__":
    # ログビューアを起動
    log_viewer_process = run_log_viewer()
    
    momoka_art = r"""
███╗   ███╗ ██████╗ ███╗   ███╗ ██████╗ ██╗  ██╗ █████╗ 
████╗ ████║██╔═══██╗████╗ ████║██╔═══██╗██║ ██╔╝██╔══██╗
██╔████╔██║██║   ██║██╔████╔██║██║   ██║█████╔╝ ███████║
██║╚██╔╝██║██║   ██║██║╚██╔╝██║██║   ██║██╔═██╗ ██╔══██║
██║ ╚═╝ ██║╚██████╔╝██║ ╚═╝ ██║╚██████╔╝██║  ██╗██║  ██║
╚═╝     ╚═╝ ╚═════╝ ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
    """
    print(momoka_art)
    initial_config = {}
    try:
        if not os.path.exists(CONFIG_FILE) and os.path.exists(DEFAULT_CONFIG_FILE):
            try:
                shutil.copyfile(DEFAULT_CONFIG_FILE, CONFIG_FILE)
                print(f"INFO: メイン実行: {CONFIG_FILE} が見つからず、{DEFAULT_CONFIG_FILE} からコピー生成しました。")
            except Exception as e_copy_main:
                print(
                    f"CRITICAL: メイン実行: {DEFAULT_CONFIG_FILE} から {CONFIG_FILE} のコピー中にエラー: {e_copy_main}")
                sys.exit(1)
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f_main_init:
            initial_config = yaml.safe_load(f_main_init)
            if not initial_config or not isinstance(initial_config, dict):
                print(f"CRITICAL: メイン実行: {CONFIG_FILE} が空または無効な形式です。")
                sys.exit(1)
    except Exception as e_main:
        print(f"CRITICAL: メイン実行: {CONFIG_FILE} の読み込みまたは解析中にエラー: {e_main}。")
        sys.exit(1)
    bot_token_val = initial_config.get('bot_token')
    if not bot_token_val or bot_token_val == "YOUR_BOT_TOKEN_HERE":
        print(f"CRITICAL: {CONFIG_FILE}にbot_tokenが未設定か無効、またはプレースホルダのままです。")
        sys.exit(1)
    intents = discord.Intents.default()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.voice_states = True
    intents.message_content = True #特権インテントの申請が受理されたらTrueに変更
    intents.members = False
    intents.presences = False
    allowed_mentions = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=True)
    discord.gateway.DiscordWebSocket.identify = mobile_identify
    bot_instance = Momoka(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                           allowed_mentions=allowed_mentions)


    # ================================================================
    # ===== Cogリロードコマンド ======================================
    # ================================================================
    @bot_instance.tree.command(name="reload_plana", description="🔄 Cogをリロードします（管理者専用）")
    async def reload_cog(interaction: discord.Interaction, cog_name: str = None):
        if not bot_instance.is_admin(interaction.user.id):
            await interaction.response.send_message("❌ このコマンドは管理者のみ実行できます。", ephemeral=False)
            return

        await interaction.response.defer(ephemeral=False)

        if cog_name:
            # 特定のCogをリロード
            if not cog_name.startswith('MOMOKA.'):
                cog_name = f'MOMOKA.{cog_name}'

            try:
                await bot_instance.reload_extension(cog_name)
                await interaction.followup.send(f"✅ Cog `{cog_name}` をリロードしました。", ephemeral=False)
                logging.info(f"Cog '{cog_name}' がユーザー {interaction.user} によってリロードされました。")
            except commands.ExtensionNotLoaded:
                try:
                    await bot_instance.load_extension(cog_name)
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
            reloaded = []
            failed = []

            for module_path in bot_instance.cogs_to_load:
                try:
                    await bot_instance.reload_extension(module_path)
                    reloaded.append(module_path)
                except commands.ExtensionNotLoaded:
                    try:
                        await bot_instance.load_extension(module_path)
                        reloaded.append(f"{module_path} (新規ロード)")
                    except Exception as e:
                        failed.append(f"{module_path}: {e}")
                except Exception as e:
                    failed.append(f"{module_path}: {e}")

            result_msg = f"✅ {len(reloaded)}個のCogをリロード/ロードしました。"
            if failed:
                result_msg += f"\n❌ {len(failed)}個のCogでエラーが発生しました。"

            await interaction.followup.send(result_msg, ephemeral=False)
            logging.info(
                f"全Cogリロードがユーザー {interaction.user} によって実行されました。成功: {len(reloaded)}, 失敗: {len(failed)}")


    @bot_instance.tree.command(name="list_plana_cogs", description="📋 ロード済みのCog一覧を表示します")
    async def list_cogs(interaction: discord.Interaction):
        loaded_extensions = list(bot_instance.extensions.keys())
        if not loaded_extensions:
            await interaction.response.send_message("現在ロードされているCogはありません。", ephemeral=False)
            return

        cog_list = "\n".join([f"• `{ext}`" for ext in sorted(loaded_extensions)])
        await interaction.response.send_message(f"**ロード済みCog一覧** ({len(loaded_extensions)}個):\n{cog_list}",
                                                ephemeral=False)


    try:
        bot_instance.run(bot_token_val)
    except Exception as e:
        logging.critical(f"ボットの実行中に致命的なエラーが発生しました: {e}", exc_info=True)
        print(f"CRITICAL: ボットの実行中に致命的なエラーが発生しました: {e}")
        sys.exit(1)