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
from tkinter import scrolledtext, ttk
from pathlib import Path
import ctypes
import platform

# --- グローバル変数 ---
log_viewer_thread = None


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
                    logging.info(
                        f"{CONFIG_FILE} が見つからなかったため、{DEFAULT_CONFIG_FILE} からコピーして生成しました。")
                    logging.warning(f"生成された {CONFIG_FILE} を確認し、ボットトークンやAPIキーを設定してください。")
                except Exception as e_copy:
                    print(
                        f"CRITICAL: {DEFAULT_CONFIG_FILE} から {CONFIG_FILE} のコピー中にエラーが発生しました: {e_copy}")
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

        # ステータスローテーションの設定
        self.status_templates = self.config.get('status_rotation', [
            "Ask @PLANA for command help",
            "operating on {guild_count} servers",
            "PLANA Ver.2025-11-03",
            "Ask @PLANA <image generation>",
            "/say <audio generation>"
        ])
        self.rotate_status.start()

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
                logging.error(
                    f"  > Cog '{module_path}' に setup 関数が見つかりません。Cogとして正しく実装されていますか？")
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
                    logging.info(
                        f"{len(synced_commands)}個のスラッシュコマンドをテストギルド {test_guild_id} に同期しました。")
                else:
                    synced_commands = await self.tree.sync()
                    logging.info(f"{len(synced_commands)}個のグローバルスラッシュコマンドを同期しました。")
            except Exception as e:
                logging.error(f"スラッシュコマンドの同期中にエラーが発生しました: {e}", exc_info=True)
        else:
            logging.info("スラッシュコマンドの同期は設定で無効化されています。")

        # エラーハンドラの設定
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
        await self.change_presence(activity=discord.Game(name=status_text))

    @rotate_status.before_loop
    async def before_rotate_status(self):
        """ステータスローテーションタスクの開始を待機"""
        await self.wait_until_ready()

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


CONFIG_FILE = 'config.yaml'
DEFAULT_CONFIG_FILE = 'config.default.yaml'


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
        """ログエントリを処理"""
        # ログレベルに基づいてフィルタリング
        log_levels = {
            "DEBUG": 10,
            "INFO": 20,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50
        }
        
        # ログの種類を判定
        if "MOMOKA.llm" in name:
            log_type = "llm"
            widget = self.llm_log
            min_level = log_levels.get(self.llm_level_var.get(), 20)
        elif "MOMOKA.tts" in name:
            log_type = "tts"
            widget = self.tts_log
            min_level = log_levels.get(self.tts_level_var.get(), 20)
        elif level in ["ERROR", "CRITICAL"]:
            log_type = "error"
            widget = self.error_log
            min_level = log_levels.get(self.error_level_var.get(), 30)
        else:
            log_type = "general"
            widget = self.general_log
            min_level = log_levels.get(self.general_level_var.get(), 20)
        
        # ログレベルが閾値以上の場合のみ表示
        if log_levels.get(level, 0) >= min_level:
            self.append_to_log(widget, log_entry, level)
    
    def append_to_log(self, text_widget, message, level=None):
        """ログをテキストウィジェットに追加"""
        text_widget.config(state='normal')
        
        # 行数制限
        lines = int(text_widget.index('end-1c').split('.')[0])
        if lines > self.config["max_lines"]:
            text_widget.delete(1.0, f"{lines - self.config['max_lines']}.0")
        
        # レベルに応じた色付け
        if level == "ERROR" or level == "CRITICAL":
            text_widget.tag_config("error", foreground=self.theme['error'])
            text_widget.insert(tk.END, message + "\n", "error")
        elif level == "WARNING":
            text_widget.tag_config("warning", foreground=self.theme['warning'])
            text_widget.insert(tk.END, message + "\n", "warning")
        elif level == "INFO":
            text_widget.tag_config("info", foreground=self.theme['info'])
            text_widget.insert(tk.END, message + "\n", "info")
        elif level == "DEBUG":
            text_widget.tag_config("debug", foreground=self.theme['debug'])
            text_widget.insert(tk.END, message + "\n", "debug")
        else:
            text_widget.insert(tk.END, message + "\n")
        
        # 自動スクロール
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
    
    # ログビューアをスレッドで起動
    log_viewer_thread = run_log_viewer_thread(log_queue)
    print("ログビューアを起動しました。")

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
    intents.message_content = True  # 特権インテントの申請が受理されたらTrueに変更
    intents.members = False
    intents.presences = False
    allowed_mentions = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=True)
    discord.gateway.DiscordWebSocket.identify = mobile_identify
    bot_instance = Momoka(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                          allowed_mentions=allowed_mentions)


    # ===============================================================
    # ===== Cogリロードコマンド =====================================
    # ===============================================================
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
