# PLANA/utilities/slash_command_cog.py
import datetime
import logging
import random
import re
import json
import os
from typing import Optional, List, Dict, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ユーザー指定のエラークラスをインポート
from PLANA.utilities.error.errors import InvalidDiceNotationError, DiceValueError

logger = logging.getLogger(__name__)


class SlashCommandsCog(commands.Cog, name="スラッシュコマンド"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        # 保存先を data/json/ に変更
        self.logging_channels_file = "data/logging_channels.json"

        # configから必要な値を取得
        self.arona_repository = self.bot.config.get("arona_repository_url",
                                                    "https://github.com/coffin399/music-bot-arona")
        self.plana_repository = self.bot.config.get("plana_repository_url",
                                                    "https://github.com/coffin399/llmcord-JP-plana")
        self.support_x_url = self.bot.config.get("support_x_url", "https://x.com/coffin299")
        self.support_discord_id = self.bot.config.get("support_discord_id", "coffin299")
        self.bot_invite_url = self.bot.config.get("bot_invite_url")

        if not self.bot_invite_url:
            logger.error(
                "CRITICAL: config.yaml に 'bot_invite_url' が設定されていません。/invite コマンドは機能しません。")
        elif self.bot_invite_url in ["YOUR_BOT_INVITE_LINK_HERE", "HOGE_FUGA_PIYO"]:
            logger.error(
                "CRITICAL: 'bot_invite_url' がプレースホルダのままです。/invite コマンドは正しく機能しません。config.yamlを確認してください。")

        self.generic_help_message_text_ja = self.bot.config.get("generic_help_message_ja", "ヘルプ")
        self.generic_help_message_text_en = self.bot.config.get("generic_help_message_en", "Help")

    async def cog_unload(self) -> None:
        await self.session.close()

    def _load_logging_channels(self) -> List[int]:
        if os.path.exists(self.logging_channels_file):
            try:
                with open(self.logging_channels_file, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list) and all(isinstance(i, int) for i in data):
                        return data
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"ロギングチャンネル設定ファイルの読み込みに失敗しました: {e}")
        return []

    def _save_logging_channels(self, channel_ids: List[int]) -> None:
        try:
            # ディレクトリのパスを取得
            dir_path = os.path.dirname(self.logging_channels_file)
            # ディレクトリが存在しない場合は作成
            os.makedirs(dir_path, exist_ok=True)
            with open(self.logging_channels_file, 'w') as f:
                json.dump(channel_ids, f, indent=4)
        except IOError as e:
            logger.error(f"ロギングチャンネル設定ファイルの保存に失敗しました: {e}")

    def _get_discord_log_handler(self) -> Optional[Any]:
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if handler.__class__.__name__ == 'DiscordLogHandler':
                return handler
        return None

    async def get_prefix_from_config(self) -> str:
        prefix = "!!"
        if hasattr(self.bot, 'config') and self.bot.config:
            cfg_prefix = self.bot.config.get('prefix')
            if isinstance(cfg_prefix, str) and cfg_prefix:
                prefix = cfg_prefix
        return prefix

    def _add_support_footer(self, embed: discord.Embed) -> None:
        """embedにサポートサーバーへのフッターを追加"""
        current_footer = embed.footer.text if embed.footer else ""
        support_text = "\n問題がありますか？開発者にご連絡ください！ / Having issues? Contact the developer!"
        embed.set_footer(text=current_footer + support_text if current_footer else support_text.strip())

    def _create_support_view(self) -> discord.ui.View:
        """サポートサーバーへのリンクボタンを含むViewを作成"""
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="サポートサーバー / Support Server",
            style=discord.ButtonStyle.link,
            url="https://discord.gg/H79HKKqx3s",
            emoji="💬"
        ))
        return view

    def _get_single_recruit(self, guaranteed_star2: bool = False) -> int:
        if guaranteed_star2:
            population = [3, 2]
            weights = [3.0, 18.5]
            return random.choices(population, weights=weights, k=1)[0]
        else:
            population = [3, 2, 1]
            weights = [3.0, 18.5, 78.5]
            return random.choices(population, weights=weights, k=1)[0]

    @app_commands.command(name="gacha",
                          description="ブルーアーカイブ風の生徒募集（ガチャ）を行います。/ Recruits students like in Blue Archive.")
    @app_commands.describe(rolls="募集回数を選択します。/ Select the number of recruitments.")
    @app_commands.choices(rolls=[
        app_commands.Choice(name="10回募集 (10 Rolls)", value=10),
        app_commands.Choice(name="1回募集 (1 Roll)", value=1),
    ])
    async def gacha(self, interaction: discord.Interaction, rolls: app_commands.Choice[int]):
        await interaction.response.defer(ephemeral=False)
        num_rolls = rolls.value
        results = []
        if num_rolls == 10:
            for _ in range(9):
                results.append(self._get_single_recruit())
            results.append(self._get_single_recruit(guaranteed_star2=True))
            random.shuffle(results)
        else:
            results.append(self._get_single_recruit())

        has_star_3 = 3 in results
        embed_color = discord.Color.from_rgb(230, 13, 138) if has_star_3 else discord.Color.gold()

        rarity_to_emoji = {1: "🟦", 2: "🟨", 3: "🟪"}
        emoji_results = [rarity_to_emoji[r] for r in results]

        if num_rolls == 10:
            result_text = "".join(emoji_results[:5]) + "\n" + "".join(emoji_results[5:])
        else:
            result_text = emoji_results[0]

        embed = discord.Embed(title="生徒募集 結果 / Recruitment Results",
                              description=f"{interaction.user.mention} 先生の募集結果です。",
                              color=embed_color)
        embed.add_field(name="結果 / Results", value=result_text, inline=False)
        embed.set_footer(text="提供割合: 🟪(☆3): 3.0%, 🟨(☆2): 18.5%, 🟦(☆1): 78.5%")
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view())
        logger.info(f"/gacha ({num_rolls}回) が実行されました。 (User: {interaction.user.id})")

    @app_commands.command(name="diceroll",
                          description="指定された範囲でダイスを振ります。/ Rolls a dice within the specified range.")
    @app_commands.describe(min_value="ダイスの最小値 / The minimum value of the dice",
                           max_value="ダイスの最大値 / The maximum value of the dice")
    async def diceroll(self, interaction: discord.Interaction, min_value: int, max_value: int):
        if min_value > max_value:
            raise DiceValueError("最小値は最大値より大きくできません。")

        result = random.randint(min_value, max_value)
        embed = discord.Embed(title="🎲 ダイスロール結果 / Dice Roll Result",
                              description=f"{interaction.user.mention} がダイスを振りました！",
                              color=discord.Color.green())
        embed.add_field(name="指定範囲 / Range", value=f"`{min_value}` ～ `{max_value}`", inline=False)
        embed.add_field(name="出た目 / Result", value=f"**{result}**", inline=False)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view())
        logger.info(
            f"/diceroll が実行されました。 (User: {interaction.user.id}, Range: {min_value}-{max_value}, Result: {result})")

    @app_commands.command(name="roll",
                          description="nDn形式でダイスを振ります (例: 2d6+3)。/ Rolls dice in nDn format (e.g., 2d6+3).")
    @app_commands.describe(
        expression="ダイスの表記 (例: 1d100, 2d6+5, 3d8-2) / Dice notation (e.g., 1d100, 2d6+5, 3d8-2)")
    async def roll(self, interaction: discord.Interaction, expression: str):
        match = re.match(r'(\d*)d(\d+)\s*([+-]\s*\d+)?', expression.lower().strip())
        if not match:
            raise InvalidDiceNotationError()

        dice_count_str, dice_sides_str, modifier_str = match.groups()
        dice_count = int(dice_count_str) if dice_count_str else 1
        dice_sides = int(dice_sides_str)
        modifier = int(modifier_str.replace(" ", "")) if modifier_str else 0
        MAX_DICE_COUNT, MAX_DICE_SIDES = 100, 10000
        if not (1 <= dice_count <= MAX_DICE_COUNT and 1 <= dice_sides <= MAX_DICE_SIDES):
            raise DiceValueError(f"ダイスの数(1〜{MAX_DICE_COUNT})または面(1〜{MAX_DICE_SIDES})が不正です。")

        rolls = [random.randint(1, dice_sides) for _ in range(dice_count)]
        total = sum(rolls)
        final_result = total + modifier
        embed = discord.Embed(title="🎲 ダイスロール結果 / Dice Roll Result",
                              description=f"{interaction.user.mention} がダイスを振りました！",
                              color=discord.Color.purple())
        input_expression = f"{dice_count}d{dice_sides}"
        if modifier > 0:
            input_expression += f" + {modifier}"
        elif modifier < 0:
            input_expression += f" - {abs(modifier)}"
        embed.add_field(name="入力 / Input", value=f"`{input_expression}`", inline=False)
        rolls_str = ", ".join(map(str, rolls))
        if len(rolls_str) > 1000: rolls_str = rolls_str[:997] + "..."
        embed.add_field(name="各ダイスの出目 / Individual Rolls", value=f"[{rolls_str}]", inline=False)
        result_str = f"**{final_result}**"
        if modifier != 0 or dice_count > 1:
            details = f" (合計: {total}"
            if modifier > 0:
                details += f" + {modifier}"
            elif modifier < 0:
                details += f" - {abs(modifier)}"
            details += ")"
            result_str += details
        embed.add_field(name="最終結果 / Final Result", value=result_str, inline=False)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view())
        logger.info(
            f"/roll が実行されました。 (User: {interaction.user.id}, Expression: {expression}, Result: {final_result})")

    @app_commands.command(name="check",
                          description="ダイスロールと、任意で条件判定を行います。/ Rolls dice and optionally performs a check.")
    @app_commands.describe(expression="ダイスの表記 (例: 1d100, 2d6+5) / Dice notation (e.g., 1d100, 2d6+5)",
                           condition="[任意] 比較条件 / [Optional] Comparison condition",
                           target="[任意] 目標値 / [Optional] Target number")
    @app_commands.choices(condition=[app_commands.Choice(name="< (より小さい)", value="<"),
                                     app_commands.Choice(name="<= (以下)", value="<="),
                                     app_commands.Choice(name="> (より大きい)", value=">"),
                                     app_commands.Choice(name=">= (以上)", value=">="),
                                     app_commands.Choice(name="= (等しい)", value="==")])
    async def check(self, interaction: discord.Interaction, expression: str, condition: Optional[str] = None,
                    target: Optional[int] = None):
        if (condition is None and target is not None) or (condition is not None and target is None):
            await interaction.response.send_message(
                "エラー: 判定を行うには、`条件`と`目標値`の両方を指定してください。\nError: To perform a check, you must specify both a `condition` and a `target` number.",
                ephemeral=False)
            return

        match = re.match(r'(\d*)d(\d+)\s*([+-]\s*\d+)?', expression.lower().strip())
        if not match:
            raise InvalidDiceNotationError()

        dice_count_str, dice_sides_str, modifier_str = match.groups()
        dice_count = int(dice_count_str) if dice_count_str else 1
        dice_sides = int(dice_sides_str)
        modifier = int(modifier_str.replace(" ", "")) if modifier_str else 0
        MAX_DICE_COUNT, MAX_DICE_SIDES = 100, 10000
        if not (1 <= dice_count <= MAX_DICE_COUNT and 1 <= dice_sides <= MAX_DICE_SIDES):
            raise DiceValueError(f"ダイスの数(1〜{MAX_DICE_COUNT})または面(1〜{MAX_DICE_SIDES})が不正です。")

        rolls = [random.randint(1, dice_sides) for _ in range(dice_count)]
        total = sum(rolls)
        final_result = total + modifier
        is_check = condition is not None and target is not None
        if is_check:
            success = False
            target_val = target or 0
            if condition == "<":
                success = final_result < target_val
            elif condition == "<=":
                success = final_result <= target_val
            elif condition == ">":
                success = final_result > target_val
            elif condition == ">=":
                success = final_result >= target_val
            elif condition == "==":
                success = final_result == target_val
            status_text, status_emoji = ("Success!", "✅") if success else ("Failure!", "❌")
            embed_color = discord.Color.green() if success else discord.Color.red()
            embed = discord.Embed(title=f"{status_emoji} 判定ロール結果 / Check Roll Result",
                                  description=f"{interaction.user.mention} が判定を行いました！", color=embed_color)
            dice_expression = f"{dice_count}d{dice_sides}"
            if modifier > 0:
                dice_expression += f"+{modifier}"
            elif modifier < 0:
                dice_expression += f"{modifier}"
            rolls_str = ", ".join(map(str, rolls))
            display_condition = condition.replace("==", "=")
            result_details = f"**{status_text}** ⟵ `{final_result}` {display_condition} `{target}` ⟵ `[{rolls_str}]` {dice_expression}"
            embed.add_field(name="結果 / Result", value=result_details, inline=False)
        else:
            embed = discord.Embed(title="🎲 ダイスロール結果 / Dice Roll Result",
                                  description=f"{interaction.user.mention} がダイスを振りました！",
                                  color=discord.Color.purple())
            input_expression = f"{dice_count}d{dice_sides}"
            if modifier > 0:
                input_expression += f" + {modifier}"
            elif modifier < 0:
                input_expression += f" - {abs(modifier)}"
            embed.add_field(name="入力 / Input", value=f"`{input_expression}`", inline=False)
            rolls_str = ", ".join(map(str, rolls))
            if len(rolls_str) > 1000: rolls_str = rolls_str[:997] + "..."
            embed.add_field(name="各ダイスの出目 / Individual Rolls", value=f"[{rolls_str}]", inline=False)
            result_str = f"**{final_result}**"
            if modifier != 0 or dice_count > 1:
                details = f" (合計: {total}"
                if modifier > 0:
                    details += f" + {modifier}"
                elif modifier < 0:
                    details += f" - {abs(modifier)}"
                details += ")"
                result_str += details
            embed.add_field(name="最終結果 / Final Result", value=result_str, inline=False)
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view())
        log_message = f"/check が実行されました。 (User: {interaction.user.id}, Expression: {expression}{f' {condition} {target}' if is_check else ''}, Result: {final_result}{f', Success: {success}' if is_check else ''})"
        logger.info(log_message)

    @app_commands.command(name="ping",
                          description="Botの現在のレイテンシを表示します。/ Shows the bot's current latency.")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        embed = discord.Embed(title="Pong! 🏓", description=f"現在のレイテンシ / Current Latency: `{latency_ms}ms`",
                              color=discord.Color.green() if latency_ms < 150 else (
                                  discord.Color.orange() if latency_ms < 300 else discord.Color.red()))
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/ping が実行されました。レイテンシ: {latency_ms}ms (User: {interaction.user.id})")

    @app_commands.command(name="serverinfo",
                          description="現在のサーバーに関する情報を表示します。/ Displays information about the current server.")
    async def serverinfo(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。\nThis command can only be used within a server.",
                ephemeral=False)
            return
        guild = interaction.guild
        embed = discord.Embed(title=f"{guild.name} のサーバー情報 / Server Information", color=discord.Color.blue())
        if guild.icon: embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="サーバーID / Server ID", value=guild.id, inline=True)
        owner_display = "不明 / Unknown"
        if guild.owner:
            owner_display = guild.owner.mention
        elif guild.owner_id:
            try:
                owner_user = await self.bot.fetch_user(guild.owner_id)
                owner_display = owner_user.mention if owner_user else f"ID: {guild.owner_id}"
            except discord.NotFound:
                owner_display = f"ID: {guild.owner_id} (取得不可 / Not found)"
            except Exception as e:
                logger.warning(f"オーナー情報の取得に失敗 (ID: {guild.owner_id}): {e}")
                owner_display = f"ID: {guild.owner_id} (エラー / Error)"
        embed.add_field(name="オーナー / Owner", value=owner_display, inline=True)
        embed.add_field(name="メンバー数 / Member Count", value=guild.member_count, inline=True)
        embed.add_field(name="テキストチャンネル数 / Text Channels", value=len(guild.text_channels), inline=True)
        embed.add_field(name="ボイスチャンネル数 / Voice Channels", value=len(guild.voice_channels), inline=True)
        embed.add_field(name="ロール数 / Roles", value=len(guild.roles), inline=True)
        created_at_text = discord.utils.format_dt(guild.created_at, style='F')
        embed.add_field(name="作成日時 / Created At", value=created_at_text, inline=False)
        verification_level_str_en = guild.verification_level.name.replace('_', ' ').capitalize()
        embed.add_field(name="認証レベル / Verification Level", value=f"{verification_level_str_en}", inline=True)
        if guild.features:
            features_str = ", ".join(f"`{f.replace('_', ' ').title()}`" for f in guild.features)
            embed.add_field(name="サーバー機能 / Server Features", value=features_str, inline=False)
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/serverinfo が実行されました。 (Server: {guild.id}, User: {interaction.user.id})")

    @app_commands.command(name="userinfo",
                          description="指定されたユーザーの情報を表示します。/ Displays information about the specified user.")
    @app_commands.describe(
        user="情報を表示するユーザー（任意、デフォルトはコマンド実行者） / User to display information for (optional, defaults to you)")
    async def userinfo(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target_user = user or interaction.user
        embed = discord.Embed(title=f"{target_user.display_name} のユーザー情報 / User Information",
                              color=target_user.accent_color or discord.Color.blurple())
        if target_user.display_avatar: embed.set_thumbnail(url=target_user.display_avatar.url)
        username_display = f"{target_user.name}#{target_user.discriminator}" if target_user.discriminator != '0' else target_user.name
        embed.add_field(name="ユーザー名 / Username", value=username_display, inline=True)
        embed.add_field(name="ユーザーID / User ID", value=target_user.id, inline=True)
        is_bot, is_bot_en = ("はい", "Yes") if target_user.bot else ("いいえ", "No")
        embed.add_field(name="Botアカウントか / Bot Account?", value=f"{is_bot} / {is_bot_en}", inline=True)
        created_at_text = discord.utils.format_dt(target_user.created_at, style='F')
        embed.add_field(name="アカウント作成日時 / Account Created", value=created_at_text, inline=False)
        if interaction.guild and isinstance(target_user, discord.Member):
            member: discord.Member = target_user
            joined_at_text = discord.utils.format_dt(member.joined_at,
                                                     style='F') if member.joined_at else "不明 / Unknown"
            embed.add_field(name="サーバー参加日時 / Joined Server", value=joined_at_text, inline=False)
            roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
            roles_count = len(roles)
            roles_display_value = "なし / None"
            if roles:
                roles_str = ", ".join(roles)
                roles_display_value = roles_str[:1017] + "..." if len(roles_str) > 1020 else roles_str
            embed.add_field(name=f"ロール ({roles_count}) / Roles ({roles_count})", value=roles_display_value,
                            inline=False)
            if member.bot:
                evaluation_lines = [
                    "✅ **認証済みBot** / Verified Bot" if member.public_flags.verified_bot else "❌ **未認証Bot** / Unverified Bot",
                    "👑 **管理者権限** / Administrator Privileges" if member.guild_permissions.administrator else "🔧 **標準権限** / Standard Privileges"]
                embed.add_field(name="Botの評価 / Bot Evaluation", value="\n".join(evaluation_lines), inline=False)
            else:
                if member.joined_at:
                    sorted_members = sorted(interaction.guild.members,
                                            key=lambda m: m.joined_at or datetime.datetime.max.replace(
                                                tzinfo=datetime.timezone.utc))
                    try:
                        join_position = sorted_members.index(member) + 1
                        embed.add_field(name="参加順位 / Join Rank", value=f"{join_position}番目 / th", inline=True)
                    except ValueError:
                        pass
                perms = member.guild_permissions
                notable_perms_ja = {"管理者": perms.administrator, "サーバー管理": perms.manage_guild,
                                    "ロール管理": perms.manage_roles, "追放": perms.kick_members,
                                    "BAN": perms.ban_members}
                user_perms = [name for name, has_perm in notable_perms_ja.items() if has_perm]
                perms_display = "なし / None"
                if user_perms: perms_display = "✅ **管理者**" if "管理者" in user_perms else ", ".join(user_perms)
                embed.add_field(name="重要な権限 / Key Permissions", value=perms_display, inline=False)
                if member.timed_out_until:
                    timeout_text = discord.utils.format_dt(member.timed_out_until, style='R')
                    embed.add_field(name="⏳ タイムアウト中 / Timed Out", value=f"終了: {timeout_text}", inline=True)
            if member.nick: embed.add_field(name="ニックネーム / Nickname", value=member.nick, inline=True)
            if member.premium_since:
                premium_text = discord.utils.format_dt(member.premium_since, style='R')
                embed.add_field(name="サーバーブースト開始 / Server Boosting Since", value=premium_text, inline=True)
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/userinfo が実行されました。 (TargetUser: {target_user.id}, Requester: {interaction.user.id})")

    @app_commands.command(name="avatar",
                          description="指定されたユーザーのアバター画像URLを表示します。/ Displays the avatar of the specified user.")
    @app_commands.describe(
        user="アバターを表示するユーザー（任意、デフォルトはコマンド実行者） / User whose avatar to display (optional, defaults to you)")
    async def avatar_command(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target_user = user or interaction.user
        avatar_url = target_user.display_avatar.url
        embed = discord.Embed(title=f"{target_user.display_name} のアバター / Avatar",
                              color=target_user.accent_color or discord.Color.default())
        embed.set_image(url=avatar_url)
        embed.add_field(name="画像URL / Image URL", value=f"[リンク / Link]({avatar_url})")
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/avatar が実行されました。 (TargetUser: {target_user.id}, Requester: {interaction.user.id})")

    @app_commands.command(name="arona",
                          description="Arona Music Botのリポジトリを表示します / Shows the Arona Music Bot repository")
    async def arona_repo_slash(self, interaction: discord.Interaction) -> None:
        if self.arona_repository:
            await interaction.response.send_message(
                f"アロナ (Arona Music Bot) のリポジトリはこちらです！\n{self.arona_repository}\n\nHere is the repository for Arona (Arona Music Bot)!\n{self.arona_repository}",
                ephemeral=False)
            logger.info(f"/arona が実行されました。 (User: {interaction.user.id})")
        else:
            await interaction.response.send_message(
                "Arona Music BotのリポジトリURLが設定されていません。\nThe repository URL for Arona Music Bot is not set.",
                ephemeral=False)
            logger.warning(f"/arona が実行されましたが、リポジトリURL未設定。 (User: {interaction.user.id})")

    @app_commands.command(name="plana",
                          description="llmcord-JP-planaのリポジトリを表示します / Shows the llmcord-JP-plana repository")
    async def plana_repo_slash(self, interaction: discord.Interaction) -> None:
        if self.plana_repository:
            await interaction.response.send_message(
                f"プラナ (llmcord-JP-plana) のリポジトリはこちらです！\n{self.plana_repository}\n\nHere is the repository for Plana (llmcord-JP-plana)!\n{self.plana_repository}",
                ephemeral=False)
            logger.info(f"/plana が実行されました。 (User: {interaction.user.id})")
        else:
            await interaction.response.send_message(
                "llmcord-JP-planaのリポジトリURLが設定されていません。\nThe repository URL for llmcord-JP-plana is not set.",
                ephemeral=False)
            logger.warning(f"/plana が実行されましたが、リポジトリURL未設定。 (User: {interaction.user.id})")

    @app_commands.command(name="support",
                          description="開発者へのお問い合わせ方法を表示します / Shows how to contact the developer")
    async def support_contact_slash(self, interaction: discord.Interaction) -> None:
        support_server_invite = "https://discord.gg/H79HKKqx3s"

        embed = discord.Embed(
            title="💬 サポート / Support",
            description="Botに関するご質問・ご要望・不具合報告などは、公式サポートサーバーまたは以下の方法でお気軽にお問い合わせください。\n\nFor questions, requests, or bug reports about the bot, please join our official support server or contact us using the methods below.",
            color=discord.Color.blurple()
        )

        support_server_id = 1176527382755864586
        support_guild = self.bot.get_guild(support_server_id)
        if support_guild and support_guild.icon:
            embed.set_thumbnail(url=support_guild.icon.url)

        embed.add_field(
            name="🏠 公式サポートサーバー / Official Support Server",
            value=f"最も迅速なサポートを受けられます！\nGet the fastest support here!\n\n**サーバー参加は下のボタンから！**\n**Join the server using the button below!**",
            inline=False
        )

        embed.add_field(
            name="🐦 X (Twitter)",
            value=f"[**@coffin299**]({self.support_x_url})\nDMまたはメンションでお問い合わせください。\nContact via DM or mention.",
            inline=True
        )

        embed.add_field(
            name="💬 Discord DM",
            value=f"**`{self.support_discord_id}`**\nDiscordのDMでお問い合わせください。\nContact via Discord DM.",
            inline=True
        )

        embed.add_field(
            name="📝 ご連絡時のお願い / When Contacting",
            value="• Botを使用しているサーバー名\n• 具体的な問題や要望の内容\n• スクリーンショット（あれば）\n\n• Server name where you're using the bot\n• Specific issue or request details\n• Screenshots (if available)",
            inline=False
        )

        embed.set_footer(text="お気軽にお問い合わせください！ / Feel free to contact us!")

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="サポートサーバーに参加 / Join Support Server",
            style=discord.ButtonStyle.link,
            url=support_server_invite,
            emoji="🏠"
        ))
        view.add_item(discord.ui.Button(
            label="X (Twitter)で連絡 / Contact on X",
            style=discord.ButtonStyle.link,
            url=self.support_x_url,
            emoji="🐦"
        ))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        logger.info(f"/support が実行されました。 (User: {interaction.user.id})")

    @app_commands.command(name="invite",
                          description="このBotをあなたのサーバーに招待します。/ Invites this bot to your server.")
    async def invite_bot_slash(self, interaction: discord.Interaction) -> None:
        invite_url_to_display = self.bot_invite_url
        bot_name = self.bot.user.name if self.bot.user else "This Bot"
        if invite_url_to_display and invite_url_to_display not in ["YOUR_BOT_INVITE_LINK_HERE", "HOGE_FUGA_PIYO"]:
            embed = discord.Embed(title=f"{bot_name} をサーバーに招待 / Invite {bot_name} to Your Server",
                                  description=f"下のボタンからPLANAをあなたのサーバーに招待できます！\n\nYou can invite PLANA to your server using the button below!",
                                  color=discord.Color.og_blurple())
            if self.bot.user and self.bot.user.avatar: embed.set_thumbnail(url=self.bot.user.avatar.url)
            embed.set_footer(text=f"{bot_name} をご利用いただきありがとうございます！\nThank you for using {bot_name}!")
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="サーバーに招待 / Invite to Server", style=discord.ButtonStyle.link,
                                            url=invite_url_to_display, emoji="💌"))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
            logger.info(f"/invite が実行されました。 (User: {interaction.user.id})")
        else:
            await interaction.response.send_message(
                "エラー: Botの招待URLが `config.yaml` に正しく設定されていません。\nBotの管理者にご連絡ください。\n\nError: The bot's invitation URL is not set correctly in `config.yaml`.\nPlease contact the bot administrator.",
                ephemeral=False)
            logger.error(
                f"/invite が実行されましたが、招待URLがconfig.yamlに未設定またはプレースホルダです。 (User: {interaction.user.id})")

    @app_commands.command(name="updates",
                          description="Botの最新のアップデート履歴（コミットログ）を表示します。/ Shows the bot's latest update history (commit log).")
    async def updates(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not self.plana_repository:
            await interaction.followup.send(
                "エラー: リポジトリのURLが設定されていません。\nError: The repository URL is not configured.",
                ephemeral=False)
            logger.warning(f"/updates が実行されましたが、リポジトリURLが未設定です。 (User: {interaction.user.id})")
            return

        repo_match = re.match(r"https://github\.com/([^/]+)/([^/]+)", self.plana_repository)
        if not repo_match:
            await interaction.followup.send(
                "エラー: 設定されているリポジトリURLの形式が正しくありません。\nError: The configured repository URL format is invalid.",
                ephemeral=False)
            logger.warning(
                f"/updates が実行されましたが、リポジトリURLの形式が不正です: {self.plana_repository} (User: {interaction.user.id})")
            return

        owner, repo = repo_match.groups()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"

        try:
            async with self.session.get(api_url) as response:
                if response.status == 200:
                    commits: List[Dict[str, Any]] = await response.json()
                    embed = discord.Embed(
                        title="📜 アップデート履歴 / Update History",
                        description=f"最新のコミット25件を表示しています。\nShowing the 25 most recent commits from the [{repo}]({self.plana_repository}) repository.",
                        color=discord.Color.blue()
                    )

                    for commit_data in commits[:25]:
                        sha = commit_data['sha'][:7]
                        message = commit_data['commit']['message'].split('\n')[0]
                        author = commit_data['commit']['author']['name']
                        html_url = commit_data['html_url']

                        date_str = commit_data['commit']['author']['date']
                        commit_date = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))

                        timestamp = discord.utils.format_dt(commit_date, style='R')

                        if len(message) > 80:
                            message = message[:77] + "..."

                        embed.add_field(
                            name=f"📝 `{sha}` by {author} ({timestamp})",
                            value=f"[{message}]({html_url})",
                            inline=False
                        )

                    self._add_support_footer(embed)
                    await interaction.followup.send(embed=embed, view=self._create_support_view())
                    logger.info(f"/updates が正常に実行されました。 (User: {interaction.user.id})")

                else:
                    error_data = await response.json()
                    error_message = error_data.get("message", "Unknown error")
                    await interaction.followup.send(
                        f"エラー: GitHub APIからの情報取得に失敗しました (ステータス: {response.status})。\n`{error_message}`\n\nError: Failed to fetch data from GitHub API (Status: {response.status}).",
                        ephemeral=False)
                    logger.error(
                        f"/updates の実行中にGitHub APIエラーが発生しました (Status: {response.status}): {error_message}")

        except aiohttp.ClientError as e:
            await interaction.followup.send(
                "エラー: GitHub APIへの接続中に問題が発生しました。\nError: An issue occurred while connecting to the GitHub API.",
                ephemeral=False)
            logger.error(f"/updates の実行中に接続エラーが発生しました: {e}")

    @app_commands.command(name="help",
                          description="Botのヘルプ情報を表示します。/ Displays help information for the bot.")
    async def help_slash_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        bot_name_ja = self.bot.user.name if self.bot.user else "当Bot"
        bot_name_en = self.bot.user.name if self.bot.user else "This Bot"
        bot_avatar_url = self.bot.user.avatar.url if self.bot.user and self.bot.user.avatar else None
        prefix = await self.get_prefix_from_config()
        embed = discord.Embed(
            title=f"📜 {bot_name_ja} ヘルプ / {bot_name_en} Help",
            description=f"{self.generic_help_message_text_ja}\n\n{self.generic_help_message_text_en}",
            color=discord.Color.teal()
        )
        if bot_avatar_url:
            embed.set_thumbnail(url=bot_avatar_url)
        desc_ja_detail = "より詳細な情報は、以下のコマンドで確認できます。"
        desc_en_detail = "For more detailed information, please check the following commands:"
        llm_help_cmd_ja = "• **AI対話機能:** `/llm_help` (または `/llm_help_en`)"
        llm_help_cmd_en = "• **AI Chat (LLM):** `/llm_help` (or `/llm_help_en`)"
        music_help_cmd_ja = "• **音楽再生機能:** `/music_help`"
        music_help_cmd_en = "• **Music Playback:** `/music_help` (or `/music_help_en`)"
        prefix_info_ja = f"プレフィックスコマンドも利用可能です (現在のプレフィックス: `none` )。"
        prefix_info_en = f"(Prefix commands are also available. Current prefix: `none` )"
        embed.add_field(
            name="基本情報 / Basic Information",
            value=f"{desc_ja_detail}\n{llm_help_cmd_ja}\n{music_help_cmd_ja}\n{prefix_info_ja}\n\n"
                  f"{desc_en_detail}\n{llm_help_cmd_en}\n{music_help_cmd_en}\n{prefix_info_en}",
            inline=False
        )
        main_features_title_ja = "主な機能"
        main_features_ja_val = (
            "- **AIとの対話 (LLM):** メンションで話しかけるとAIが応答します。画像も認識可能です。\n"
            "- **音楽再生:** ボイスチャンネルで音楽を再生、キュー管理、各種操作ができます。\n"
            "- **画像検索:** 猫の画像を表示できます。\n"
            "- **情報表示:** サーバー情報、ユーザー情報、Botのレイテンシなどを表示します。"
        )
        main_features_en_val = (
            "- **AI Chat (LLM):** Mention the bot to talk with AI. It can also recognize images (if model supports).\n"
            "- **Music Playback:** Play music in voice channels, manage queues, and perform various operations.\n"
            "- **Image Search:** Display cat pictures.\n"
            "- **Information Display:** Show server info, user info, bot latency, etc."
        )
        embed.add_field(
            name=f"{main_features_title_ja} / Main Features",
            value=f"{main_features_ja_val}\n\n{main_features_en_val}",
            inline=False
        )
        utility_cmds_ja = [
            f"`/check <表記> [条件] [目標値]` - ダイスロールと任意での条件判定",
            f"`/roll <表記>` - nDn形式でダイスロール (例: 2d6+3)",
            f"`/diceroll <最小値> <最大値>` - 指定範囲でダイスロール",
            f"`/gacha` - ブルーアーカイブ風ガチャ",
            f"`/earthquake <チャンネル>` - 緊急地震速報の通知チャンネルを設定",
            f"`/test_earthquake` - 地震速報のテスト通知を送信",
            f"`/ping` - Botの応答速度を確認",
            f"`/serverinfo` - サーバー情報を表示",
            f"`/userinfo [ユーザー]` - ユーザー情報を表示",
            f"`/avatar [ユーザー]` - アバター画像を表示",
            f"`/invite` - Botの招待リンクを表示",
            f"`/updates` - Botのアップデート履歴を表示",
            f"`/meow` - ランダムな猫の画像を表示",
            f"`/support` - 開発者への連絡方法を表示"
        ]
        utility_cmds_en = [
            f"`/check <notation> [cond] [target]` - Rolls dice and optionally performs a check",
            f"`/roll <notation>` - Rolls dice in nDn format (e.g., 2d6+3)",
            f"`/diceroll <min> <max>` - Rolls a dice in a specified range",
            f"`/gacha` - Simulates Blue Archive gacha",
            f"`/earthquake <channel>` - Sets channel for Earthquake Early Warnings(JapanOnly)",
            f"`/test_earthquake` - Sends a test Earthquake Early Warning",
            f"`/ping` - Check bot's latency",
            f"`/serverinfo` - Display server info",
            f"`/userinfo [user]` - Display user info",
            f"`/avatar [user]` - Display avatar",
            f"`/invite` - Display bot invite link",
            f"`/updates` - Shows the bot's update history",
            f"`/meow` - Displays a random cat picture",
            f"`/support` - Shows how to contact the developer"
        ]
        if self.plana_repository:
            utility_cmds_ja.append(f"`/plana` - Plana (Bot)リポジトリ")
            utility_cmds_en.append(f"`/plana` - Plana (Bot) repository")
        if self.arona_repository:
            utility_cmds_ja.append(f"`/arona` - Arona (Music)リポジトリ")
            utility_cmds_en.append(f"`/arona` - Arona (Music) repository")
        embed.add_field(name="便利なコマンド (Japanese)", value="\n".join(utility_cmds_ja), inline=False)
        embed.add_field(name="Useful Commands (English)", value="\n".join(utility_cmds_en), inline=False)
        footer_ja = "<> は必須引数、[] は任意引数を表します。"
        footer_en = "<> denotes a required argument, [] denotes an optional argument."
        embed.set_footer(text=f"{footer_ja}\n{footer_en}")
        self._add_support_footer(embed)
        view_items = []
        if self.bot_invite_url and self.bot_invite_url not in ["YOUR_BOT_INVITE_LINK_HERE", "HOGE_FUGA_PIYO"]:
            view_items.append(discord.ui.Button(label="Botを招待 / Invite Bot", style=discord.ButtonStyle.link,
                                                url=self.bot_invite_url))
        if view_items:
            view = discord.ui.View()
            for item in view_items:
                view.add_item(item)
            support_view = self._create_support_view()
            for item in support_view.children:
                view.add_item(item)
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/help が実行されました。 (User: {interaction.user.id})")


async def setup(bot: commands.Bot):
    if not hasattr(bot, 'config') or not bot.config:
        logger.error("SlashCommandsCog: Botインスタンスに 'config' 属性が見つからないか空です。Cogをロードできません。")
        raise commands.ExtensionFailed("SlashCommandsCog", "Botのconfigがロードされていません。")

    cog = SlashCommandsCog(bot)
    await bot.add_cog(cog)
    logger.info("SlashCommandsCogが正常にロードされました。")