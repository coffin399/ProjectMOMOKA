import discord
from discord.ext import commands, tasks
import yaml
import logging
import os
import shutil
import sys
import json

# --- ロギング設定の初期化 ---
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('openai').setLevel(logging.WARNING)
logging.getLogger('google.generativeai').setLevel(logging.WARNING)
logging.getLogger('google.ai').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

from MOMOKA.services.discord_handler import DiscordLogHandler, DiscordLogFormatter
from MOMOKA.utilities.error.errors import InvalidDiceNotationError, DiceValueError

CONFIG_FILE = 'config.yaml'
DEFAULT_CONFIG_FILE = 'config.default.yaml'


async def mobile_identify(self):
    """Botをモバイルとして識別させるためのカスタム関数"""
    payload = {
        'op': self.IDENTIFY,
        'd': {
            'token': self.token,
            'properties': {
                '$os': 'Discord Android',
                '$browser': 'Discord Android',
                '$device': 'Discord Android'
            },
            'compress': True,
            'large_threshold': 250,
            'intents': self._connection.intents.value
        }
    }
    if self.shard_id is not None and self.shard_count is not None:
        payload['d']['shard'] = [self.shard_id, self.shard_count]
    state = self._connection
    if state._activity is not None or state._status is not None:
        payload['d']['presence'] = {
            'status': state._status,
            'game': state._activity,
            'since': 0,
            'afk': False
        }
    await self.call_hooks('before_identify', self.shard_id, initial=self._initial_identify)
    await self.send_as_json(payload)


class Shittim(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = None
        self.status_templates = []
        self.status_index = 0
        #ロードする順序を指定
        self.cogs_to_load = [
            'PLANA.images.image_commands_cog',
            'PLANA.llm.llm_cog',
            'PLANA.media_downloader.ytdlp_downloader_cog',
            'PLANA.music.music_cog',
            'PLANA.notifications.earthquake_notification_cog',
            'PLANA.notifications.twitch_notification_cog',
            'PLANA.timer.timer_cog',
            'PLANA.tracker.r6s_tracker_cog',
            'PLANA.tracker.valorant_tracker_cog',
            'PLANA.tts.tts_cog',
            'PLANA.utilities.slash_command_cog',
        ]

    def is_admin(self, user_id: int) -> bool:
        """ユーザーが管理者かどうかをチェック"""
        admin_ids = self.config.get('admin_user_ids', [])
        return user_id in admin_ids

    async def setup_hook(self):
        """Botの初期セットアップ（ログイン後、接続準備完了前）"""
        if not os.path.exists(CONFIG_FILE):
            if os.path.exists(DEFAULT_CONFIG_FILE):
                try:
                    shutil.copyfile(DEFAULT_CONFIG_FILE, CONFIG_FILE)
                    logging.info(
                        f"{CONFIG_FILE} が見つからなかったため、{DEFAULT_CONFIG_FILE} をコピーして生成しました。")
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

        # ================================================================
        # ===== ロギング設定 =============================================
        # ================================================================
        # コンソール用のフォーマッター
        console_log_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s] %(message)s')

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.handlers = []  # 既存のハンドラをクリア

        # コンソールハンドラの設定
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(console_log_format)
        root_logger.addHandler(console_handler)

        log_channel_ids_from_config = self.config.get('log_channel_ids', [])
        if not isinstance(log_channel_ids_from_config, list):
            log_channel_ids_from_config = []
            logging.warning("config.yaml の 'log_channel_ids' はリスト形式である必要があります。")

        logging_json_path = "data/logging_channels.json"
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

                # Discordに送信するログ用のフォーマッターを新しく作成
                discord_formatter = DiscordLogFormatter('%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')
                discord_handler.setFormatter(discord_formatter)

                root_logger.addHandler(discord_handler)
                logging.info(f"DiscordへのロギングをチャンネルID {all_log_channel_ids} で有効化しました。")
            except Exception as e:
                logging.error(f"DiscordLogHandler の初期化中にエラーが発生しました: {e}")
        else:
            logging.warning("ログ送信先のDiscordチャンネルが設定されていません。")
        # ================================================================
        # ===== ロギング設定ここまで =====================================
        # ================================================================

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
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        original_error = getattr(error, 'original', error)
        logging.error(f"コマンド '{interaction.command.name}' でエラーが発生しました。", exc_info=error)
        if isinstance(original_error, (InvalidDiceNotationError, DiceValueError)):
            error_message = f"エラー: {original_error.message}"
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(error_message, ephemeral=False)
                else:
                    await interaction.followup.send(error_message, ephemeral=False)
            except discord.HTTPException as e:
                logging.error(f"エラーメッセージの送信に失敗しました: {e}")
            return
        if isinstance(error, discord.app_commands.MissingPermissions):
            error_message = "エラー: このコマンドを実行する権限がありません。\nError: You do not have the required permissions to run this command."
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(error_message, ephemeral=False)
                else:
                    await interaction.followup.send(error_message, ephemeral=False)
            except discord.HTTPException as e:
                logging.error(f"権限エラーメッセージの送信に失敗しました: {e}")
            return
        try:
            error_message = ("コマンドの実行中に予期しないエラーが発生しました。開発者に連絡してください。\n"
                             "An unexpected error occurred while executing the command. Please contact the developer.")
            if not interaction.response.is_done():
                await interaction.response.send_message(error_message, ephemeral=False)
            else:
                await interaction.followup.send(error_message, ephemeral=False)
        except discord.HTTPException as e:
            logging.error(f"最終的なエラーメッセージの送信に失敗しました: {e}")

    @tasks.loop(seconds=10)
    async def rotate_status(self):
        # ボットが完全に準備完了しているか、ステータステンプレートが存在するかを確認
        # これにより、再接続中の実行や設定不備を防ぐ
        if not self.is_ready() or not self.status_templates:
            return

        current_template = self.status_templates[self.status_index]
        status_text = current_template.format(guild_count=len(self.guilds), prefix=self.config.get('prefix', '!!'))
        activity_type_str = self.config.get('status_activity_type', 'playing').lower()
        activity_type_map = {
            'playing': discord.ActivityType.playing,
            'streaming': discord.ActivityType.streaming,
            'listening': discord.ActivityType.listening,
            'watching': discord.ActivityType.watching,
            'competing': discord.ActivityType.competing
        }
        selected_activity_type = activity_type_map.get(activity_type_str, discord.ActivityType.streaming)

        if selected_activity_type == discord.ActivityType.streaming:
            stream_url = self.config.get('status_stream_url', 'https://www.twitch.tv/coffinnoob299')
            activity = discord.Streaming(name=status_text, url=stream_url)
        else:
            activity = discord.Activity(type=selected_activity_type, name=status_text)

        try:
            await self.change_presence(activity=activity, status=discord.Status.online)
        except Exception as e:
            logging.warning(f"ステータスの更新中に一時的なエラーが発生しました: {e}")

        self.status_index = (self.status_index + 1) % len(self.status_templates)

    @rotate_status.before_loop
    async def before_rotate_status(self):
        await self.wait_until_ready()

    async def on_ready(self):
        if not self.user:
            logging.error("on_ready: self.user が None です。処理をスキップします。")
            return
        logging.info(f'{self.user.name} ({self.user.id}) としてDiscordにログインし、準備が完了しました！')
        logging.info(f"現在 {len(self.guilds)} サーバーに参加しています。")
        logging.info("📱 モバイルステータスで表示されています")
        self.status_templates = self.config.get('status_rotation', [
                                                                    "Ask @PLANA for command help",
                                                                    "operating on {guild_count} servers",
                                                                    "PLANA Ver.2025-11-03",
                                                                    "Ask @PLANA <image generation>",
                                                                    "/say <audio generation>"
                                                                    ])
        self.rotate_status.start()

    async def on_guild_join(self, guild: discord.Guild):
        logging.info(
            f"新しいサーバー '{guild.name}' (ID: {guild.id}) に参加しました。現在のサーバー数: {len(self.guilds)}")

    async def on_guild_remove(self, guild: discord.Guild):
        logging.info(f"サーバー '{guild.name}' (ID: {guild.id}) から退出しました。現在のサーバー数: {len(self.guilds)}")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"引数が不足しています: `{error.param.name}`\n`{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(
                f"引数の型が正しくありません。\n`{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("このコマンドを実行する権限がありません。")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"このコマンドはクールダウン中です。あと {error.retry_after:.2f} 秒お待ちください。")
        elif isinstance(error, commands.ExtensionError):
            logging.error(
                f"Cog関連のエラーが発生しました ({ctx.command.cog_name if ctx.command else 'UnknownCog'}): {error}",
                exc_info=error)
            await ctx.send("コマンドの処理中にCogエラーが発生しました。管理者に報告してください。")
        else:
            logging.error(
                f"コマンド '{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' の実行中に予期しないエラーが発生しました:",
                exc_info=error)
            try:
                await ctx.send("コマンドの実行中に予期しないエラーが発生しました。")
            except discord.errors.Forbidden:
                logging.warning(f"エラーメッセージを送信できませんでした ({ctx.channel.id}): 権限不足")


if __name__ == "__main__":
    plana_art = r"""
██████╗ ██╗      █████╗ ███╗   ██╗ █████╗ 
██╔══██╗██║     ██╔══██╗████╗  ██║██╔══██╗
██████╔╝██║     ███████║██╔██╗ ██║███████║
██╔═══╝ ██║     ██╔══██║██║╚██╗██║██╔══██║
██║     ███████╗██║  ██║██║ ╚████║██║  ██║
╚═╝     ╚══════╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝
    """
    print(plana_art)
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
    bot_instance = Shittim(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
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
            if not cog_name.startswith('PLANA.'):
                cog_name = f'PLANA.{cog_name}'

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