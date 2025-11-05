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