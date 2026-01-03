# MOMOKA/notifications/star_resonance_notification_cog.py

import asyncio
import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from MOMOKA.notifications.error.star_resonance_errors import (
    StarResonanceExceptionHandler,
    SpreadsheetError,
    DataParsingError,
    ConfigError,
    NotificationError
)

# ロガーの設定
logger = logging.getLogger('StarResonanceCog')

# --- 定数 ---
DATA_DIR = 'data'
CONFIG_FILE = os.path.join(DATA_DIR, 'starresonance.json')
JST = timezone(timedelta(hours=+9), 'JST')


class StarResonanceNotificationCog(commands.Cog, name="StarResonanceNotifications"):
    """スターレゾナンスのデイリー通知Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("🔄 StarResonanceNotificationCog 初期化開始...")

        self.ensure_data_dir()
        self.config = self.load_config()
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.jst = JST
        self.exception_handler = StarResonanceExceptionHandler(self)

        logger.info("✅ StarResonanceNotificationCog 初期化完了")

    async def cog_load(self):
        """Cogのセットアップ"""
        logger.info("🔄 StarResonanceNotificationCog セットアップ開始...")
        try:
            self.http_session = aiohttp.ClientSession()
            # 毎朝5時に通知を送信するタスクを開始
            self.daily_notification_task.start()
            logger.info("✅ StarResonanceNotificationCog セットアップ完了")
        except Exception as e:
            logger.error(f"❌ セットアップに失敗しました: {e}", exc_info=True)

    async def cog_unload(self):
        """Cogのアンロード"""
        logger.info("🔄 StarResonanceNotificationCog アンロード中...")

        if hasattr(self, 'daily_notification_task'):
            self.daily_notification_task.cancel()

        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

        logger.info("✅ StarResonanceNotificationCog アンロード完了")

    def ensure_data_dir(self):
        """データディレクトリの存在を確認"""
        try:
            if not os.path.exists(DATA_DIR):
                os.makedirs(DATA_DIR)
        except OSError as e:
            logger.error(f"データディレクトリの作成に失敗: {e}")

    def load_config(self) -> Dict[str, Any]:
        """
        設定ファイルの読み込み
        
        設定ファイル構造 (data/starresonance.json):
        {
            "guild_id_1": {
                "channel_id": 123456789,
                "spreadsheet_url": "https://docs.google.com/spreadsheets/d/...",
                "last_notified_date": "2026-01-04"
            },
            "guild_id_2": {
                "channel_id": 987654321,
                "spreadsheet_url": "https://docs.google.com/spreadsheets/d/...",
                "last_notified_date": "2026-01-04"
            }
        }
        
        Returns:
            ギルドIDをキーとした設定辞書
        """
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    logger.info(f"✅ 設定ファイルを読み込みました: {len(config)} ギルド")
                    return config
        except (json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"設定ファイル読み込みエラー: {e}")
        return {}

    def save_config(self):
        """設定ファイルの保存"""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logger.info(f"💾 設定ファイルを保存しました: {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"設定ファイルの保存に失敗: {e}")

    async def fetch_spreadsheet_data(self, spreadsheet_url: str) -> Dict[str, List[List[str]]]:
        """
        Google Sheetsから公開CSVとしてデータを取得
        
        Args:
            spreadsheet_url: スプレッドシートのURL
            
        Returns:
            シート名をキーとした辞書
        """
        try:
            # スプレッドシートIDを抽出
            if '/d/' in spreadsheet_url:
                sheet_id = spreadsheet_url.split('/d/')[1].split('/')[0]
            else:
                raise ValueError("無効なスプレッドシートURLです")

            data = {}
            
            # すべてのシートをスキャンして、ヘッダーから判定する
            # gid 0から順番に100個まで試す
            logger.info("📊 スプレッドシートの全シートをスキャン中...")
            
            candidate_gids = []
            
            # 一般的なgidパターンを優先的に試す
            priority_gids = ['0', '1', '2', '3', '4', '5', '10', '1975346704']
            
            # URLからgidを抽出
            if 'gid=' in spreadsheet_url:
                import re
                gids_in_url = re.findall(r'gid=(\d+)', spreadsheet_url)
                priority_gids.extend(gids_in_url)
            
            # さらに広範囲のgidを追加
            for i in range(0, 20):
                candidate_gids.append(str(i))
            
            # 大きな数値も試す
            for i in [100, 1000, 10000, 100000, 1000000]:
                candidate_gids.append(str(i))
            
            # 予告通知のgid付近も試す
            base = 1975346704
            for offset in range(-10, 10):
                candidate_gids.append(str(base + offset))
            
            # 優先gidを先頭に
            all_gids = priority_gids + [g for g in candidate_gids if g not in priority_gids]
            
            # 重複削除
            all_gids = list(dict.fromkeys(all_gids))
            
            logger.info(f"スキャン対象: {len(all_gids)}個のgid")
            
            found_sheets = {}
            
            for idx, gid in enumerate(all_gids):
                # 進捗表示（10個ごと）
                if idx > 0 and idx % 10 == 0:
                    logger.info(f"進捗: {idx}/{len(all_gids)} (発見: {len(found_sheets)}個)")
                
                sheet_data = await self._fetch_and_identify_sheet(sheet_id, gid)
                
                if sheet_data:
                    sheet_name = sheet_data['name']
                    rows = sheet_data['rows']
                    found_sheets[sheet_name] = rows
                    logger.info(f"✅ 発見: '{sheet_name}' (gid={gid}, {len(rows)}行)")
                    
                    # 両方のシートが見つかったら終了
                    if '定義_デイリー通知' in found_sheets and '定義_予告通知' in found_sheets:
                        logger.info("✅ 必要なシートをすべて発見しました")
                        break
            
            if not found_sheets:
                logger.error("❌ どのシートもデータを取得できませんでした")
            else:
                logger.info(f"✅ {len(found_sheets)}個のシートからデータを取得しました: {list(found_sheets.keys())}")
            
            return found_sheets

        except Exception as e:
            logger.error(f"スプレッドシートの取得中にエラーが発生: {e}", exc_info=True)
            return {}

    async def _fetch_and_identify_sheet(
        self,
        sheet_id: str,
        gid: str
    ) -> Optional[Dict[str, Any]]:
        """
        シートを取得して、ヘッダーから種類を判定
        
        Returns:
            {'name': シート名, 'rows': データ} または None
        """
        csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
        
        if not self.http_session:
            self.http_session = aiohttp.ClientSession()

        try:
            async with self.http_session.get(csv_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return None
                
                content = await response.text(encoding='utf-8')
                csv_reader = csv.reader(io.StringIO(content))
                rows = list(csv_reader)
                
                if not rows or len(rows) < 2:
                    return None
                
                # ヘッダー行から判定
                header = rows[0] if rows else []
                header_text = ''.join(str(cell) for cell in header).lower()
                
                # デイリー通知シートの判定
                # ヘッダーに「notify頻度」「イベント名」「日時」などがある
                if any(keyword in header_text for keyword in ['notify', 'イベント名', '日時']):
                    # さらに詳細にチェック
                    if len(header) >= 3:
                        # 2行目以降にデータがあるか確認
                        has_data = False
                        for row in rows[1:5]:  # 最初の数行をチェック
                            if len(row) >= 2 and any(str(cell).strip() for cell in row[:2]):
                                has_data = True
                                break
                        
                        if has_data:
                            # ヘッダーの内容で判定
                            if '開放日時' in header_text or '予告' in header_text:
                                return {'name': '定義_予告通知', 'rows': rows}
                            elif '日時' in header_text or 'デイリー' in header_text:
                                return {'name': '定義_デイリー通知', 'rows': rows}
                
                return None
                
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    def parse_event_data(self, rows: List[List[str]], event_type: str) -> List[Dict[str, str]]:
        """
        CSVデータをイベント情報にパース
        
        Args:
            rows: CSV行データ
            event_type: 'daily' または 'upcoming'
            
        Returns:
            イベント情報のリスト
        """
        events = []
        
        if not rows or len(rows) < 2:
            logger.warning(f"パース失敗: 行数が不足 (rows={len(rows) if rows else 0})")
            return events

        logger.info(f"パース開始 ({event_type}): 総行数={len(rows)}")
        
        # ヘッダー行を確認
        if rows:
            header = rows[0] if len(rows) > 0 else []
            logger.info(f"ヘッダー行 ({len(header)}列): {header[:6]}")

        # ヘッダー行をスキップ（1行目）
        data_rows = rows[1:]
        logger.info(f"データ行数: {len(data_rows)}")
        
        for idx, row in enumerate(data_rows, start=2):  # 2行目から開始
            # 完全に空の行をスキップ
            if not row:
                logger.debug(f"行{idx}: 完全に空の行をスキップ")
                continue
            
            # すべてのセルが空の行をスキップ
            if not any(str(cell).strip() for cell in row):
                logger.debug(f"行{idx}: すべてのセルが空")
                continue

            # 行の内容をデバッグ出力（最初の10行のみ）
            if idx <= 11:
                logger.info(f"行{idx} ({len(row)}列): {[str(cell)[:30] for cell in row[:6]]}")

            try:
                if event_type == 'daily':
                    # デイリー通知: "notify頻度、イベント名、日時、テキスト"
                    if len(row) < 2:
                        logger.debug(f"行{idx}: 列数不足 (len={len(row)})")
                        continue
                        
                    frequency = str(row[0]).strip() if len(row) > 0 and row[0] else ''
                    event_name = str(row[1]).strip() if len(row) > 1 and row[1] else ''
                    event_time = str(row[2]).strip() if len(row) > 2 and row[2] else ''
                    description = str(row[3]).strip() if len(row) > 3 and row[3] else ''

                    if not event_name:
                        logger.debug(f"行{idx}: イベント名が空")
                        continue

                    events.append({
                        'frequency': frequency,
                        'name': event_name,
                        'time': event_time,
                        'description': description
                    })
                    if idx <= 11:
                        logger.info(f"  ✅ 行{idx}: デイリーイベント追加 - [{frequency}] {event_name}")

                elif event_type == 'upcoming':
                    # 予告通知: "notify頻度、イベント名、開放日時、テキスト"
                    if len(row) < 2:
                        logger.debug(f"行{idx}: 列数不足 (len={len(row)})")
                        continue
                        
                    frequency = str(row[0]).strip() if len(row) > 0 and row[0] else ''
                    event_name = str(row[1]).strip() if len(row) > 1 and row[1] else ''
                    open_date = str(row[2]).strip() if len(row) > 2 and row[2] else ''
                    description = str(row[3]).strip() if len(row) > 3 and row[3] else ''

                    if not event_name:
                        logger.debug(f"行{idx}: イベント名が空")
                        continue
                    
                    if not open_date:
                        logger.debug(f"行{idx}: 開放日時が空 - {event_name}")
                        continue

                    events.append({
                        'frequency': frequency,
                        'name': event_name,
                        'open_date': open_date,
                        'description': description
                    })
                    if idx <= 11:
                        logger.info(f"  ✅ 行{idx}: 予告イベント追加 - [{frequency}] {event_name} ({open_date})")

            except Exception as e:
                logger.warning(f"行{idx}のパースに失敗: エラー: {e}, row={row[:4]}")
                continue

        logger.info(f"パース完了 ({event_type}): {len(events)}件のイベントを抽出")
        return events

    def filter_daily_events(self, events: List[Dict[str, str]], weekday: str) -> List[Dict[str, str]]:
        """
        デイリーイベントを曜日でフィルタリング
        
        Args:
            events: イベントリスト
            weekday: 曜日（日曜日、月曜日、...）
            
        Returns:
            フィルタリングされたイベントリスト
        """
        filtered = []
        
        for event in events:
            frequency = event.get('frequency', '')
            
            # 毎日のイベント
            if '毎日' in frequency or 'daily' in frequency.lower():
                filtered.append(event)
            # 特定曜日のイベント
            elif weekday in frequency:
                filtered.append(event)
        
        return filtered

    def calculate_days_until(self, open_date_str: str) -> Optional[int]:
        """
        開放日時までの残り日数を計算
        
        Args:
            open_date_str: 開放日時の文字列（例: "2025/01/10"）
            
        Returns:
            残り日数（負の値は過去、None はパースエラー）
        """
        try:
            # 日付形式のパース（様々な形式に対応）
            for fmt in ['%Y/%m/%d', '%Y-%m-%d', '%Y年%m月%d日']:
                try:
                    open_date = datetime.strptime(open_date_str, fmt).replace(tzinfo=self.jst)
                    now = datetime.now(self.jst)
                    
                    # 日付のみで比較
                    delta = (open_date.date() - now.date()).days
                    return delta
                except ValueError:
                    continue
            
            logger.warning(f"日付のパースに失敗: {open_date_str}")
            return None

        except Exception as e:
            logger.warning(f"日付計算エラー: {e}")
            return None

    def create_notification_embed(
        self,
        upcoming_events: List[Dict[str, str]],
        daily_events: List[Dict[str, str]],
        current_date: datetime
    ) -> discord.Embed:
        """
        通知用Embedを作成
        
        Args:
            upcoming_events: 予告イベントリスト
            daily_events: デイリーイベントリスト
            current_date: 現在日時
            
        Returns:
            Discord Embed
        """
        embed = discord.Embed(
            title="🌟 スターレゾナンス デイリー通知",
            color=discord.Color.blue(),
            timestamp=current_date
        )

        # 予告通知セクション
        if upcoming_events:
            upcoming_text = ""
            for event in upcoming_events[:10]:  # 最大10件
                name = event.get('name', '不明なイベント')
                open_date = event.get('open_date', '')
                days_until = self.calculate_days_until(open_date)
                
                if days_until is not None:
                    if days_until > 0:
                        upcoming_text += f"**{name}** まであと**{days_until}日** ({open_date})\n"
                    elif days_until == 0:
                        upcoming_text += f"**{name}** は**本日開放**🎉 ({open_date})\n"
                    # 過去のイベントは表示しない
            
            if upcoming_text:
                embed.add_field(
                    name="📅 開放予告",
                    value=upcoming_text[:1024],  # Discord制限
                    inline=False
                )

        # デイリー通知セクション
        weekday_jp = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']
        weekday = weekday_jp[current_date.weekday()]
        
        embed.add_field(
            name=f"📆 デイリー通知 - {current_date.strftime('%Y/%m/%d')}（{weekday}）",
            value="\u200b",  # 空白
            inline=False
        )

        if daily_events:
            daily_text = ""
            for event in daily_events[:15]:  # 最大15件
                name = event.get('name', '不明なイベント')
                time = event.get('time', '')
                description = event.get('description', '')
                
                if time:
                    daily_text += f"・**{name}** ({time})\n"
                else:
                    daily_text += f"・**{name}**\n"
                
                if description:
                    daily_text += f"  {description}\n"
            
            embed.add_field(
                name="本日のイベント",
                value=daily_text[:1024] if daily_text else "本日のイベントはありません",
                inline=False
            )
        else:
            embed.add_field(
                name="本日のイベント",
                value="本日のイベントはありません",
                inline=False
            )

        embed.set_footer(text="スターレゾナンス通知 | PLANA by coffin299")
        
        return embed

    @tasks.loop(minutes=30)
    async def daily_notification_task(self):
        """毎朝5時に通知を送信するタスク"""
        try:
            now = datetime.now(self.jst)
            
            # 5時0分〜5時30分の間に通知をチェック
            if now.hour != 5 or now.minute >= 30:
                return
            
            today_str = now.strftime('%Y-%m-%d')
            logger.info(f"🌅 デイリー通知チェック開始: {today_str}")
            
            # 各ギルドの設定をチェックして通知
            for guild_id_str, guild_config in list(self.config.items()):
                try:
                    # 今日既に通知済みかチェック
                    last_notified = guild_config.get('last_notified_date')
                    if last_notified == today_str:
                        logger.debug(f"ギルド {guild_id_str}: 本日は既に通知済み")
                        continue
                    
                    channel_id = guild_config.get('channel_id')
                    spreadsheet_url = guild_config.get('spreadsheet_url')
                    
                    if not channel_id or not spreadsheet_url:
                        logger.warning(f"ギルド {guild_id_str}: 設定が不完全です")
                        continue
                    
                    channel = self.bot.get_channel(channel_id)
                    if not channel:
                        logger.warning(f"ギルド {guild_id_str}: チャンネル {channel_id} が見つかりません")
                        continue
                    
                    # スプレッドシートからデータを取得
                    data = await self.fetch_spreadsheet_data(spreadsheet_url)
                    
                    if not data:
                        logger.warning(f"ギルド {guild_id_str}: データ取得に失敗")
                        continue
                    
                    # 予告通知のパース
                    upcoming_events = []
                    if '定義_予告通知' in data:
                        upcoming_events = self.parse_event_data(data['定義_予告通知'], 'upcoming')
                    
                    # デイリー通知のパース
                    daily_events = []
                    if '定義_デイリー通知' in data:
                        all_daily = self.parse_event_data(data['定義_デイリー通知'], 'daily')
                        weekday_jp = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']
                        today_weekday = weekday_jp[now.weekday()]
                        daily_events = self.filter_daily_events(all_daily, today_weekday)
                    
                    # Embedを作成して送信
                    embed = self.create_notification_embed(upcoming_events, daily_events, now)
                    await channel.send(embed=embed)
                    
                    # 通知済みフラグを更新
                    self.config[guild_id_str]['last_notified_date'] = today_str
                    self.save_config()
                    
                    logger.info(f"✅ ギルド {guild_id_str} に通知を送信しました")
                
                except Exception as e:
                    logger.error(f"ギルド {guild_id_str} への通知送信に失敗: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"デイリー通知タスクでエラーが発生: {e}", exc_info=True)

    @daily_notification_task.before_loop
    async def before_daily_notification(self):
        """タスク開始前にBotの準備を待つ"""
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="notify-starresonance",
        description="スターレゾナンスのデイリー通知を設定します"
    )
    @app_commands.describe(
        channel="通知を送信するチャンネル",
        spreadsheet_url="スプレッドシートのURL"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_notification(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        spreadsheet_url: str
    ):
        """スターレゾナンス通知を設定"""
        await interaction.response.defer()

        try:
            guild_id = str(interaction.guild.id)

            # スプレッドシートURLの検証
            if 'docs.google.com/spreadsheets' not in spreadsheet_url:
                await interaction.followup.send("❌ 無効なスプレッドシートURLです。")
                return

            # 設定を保存
            self.config[guild_id] = {
                'channel_id': channel.id,
                'spreadsheet_url': spreadsheet_url
            }
            self.save_config()

            embed = discord.Embed(
                title="✅ スターレゾナンス通知設定完了",
                description=f"{channel.mention} に毎朝5時に通知を送信します。",
                color=discord.Color.green()
            )
            embed.add_field(name="スプレッドシートURL", value=spreadsheet_url, inline=False)

            await interaction.followup.send(embed=embed)
            logger.info(f"ギルド {guild_id} の通知設定を保存しました")

        except Exception as e:
            logger.error(f"設定コマンドでエラーが発生: {e}", exc_info=True)
            await interaction.followup.send(f"❌ エラーが発生しました: {e}")

    @app_commands.command(
        name="starresonance-test",
        description="スターレゾナンス通知のテストを送信します"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test_notification(self, interaction: discord.Interaction):
        """テスト通知を送信"""
        await interaction.response.defer()

        try:
            guild_id = str(interaction.guild.id)

            if guild_id not in self.config:
                await interaction.followup.send("❌ 通知設定がありません。先に `/notify-starresonance` で設定してください。")
                return

            guild_config = self.config[guild_id]
            channel_id = guild_config.get('channel_id')
            spreadsheet_url = guild_config.get('spreadsheet_url')

            channel = self.bot.get_channel(channel_id)
            if not channel:
                await interaction.followup.send(f"❌ チャンネルが見つかりません: ID {channel_id}")
                return

            # スプレッドシートからデータを取得
            data = await self.fetch_spreadsheet_data(spreadsheet_url)

            if not data:
                await interaction.followup.send("❌ スプレッドシートからデータを取得できませんでした。")
                return

            # 予告通知のパース
            upcoming_events = []
            if '定義_予告通知' in data:
                upcoming_events = self.parse_event_data(data['定義_予告通知'], 'upcoming')

            # デイリー通知のパース
            daily_events = []
            now = datetime.now(self.jst)
            if '定義_デイリー通知' in data:
                all_daily = self.parse_event_data(data['定義_デイリー通知'], 'daily')
                weekday_jp = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']
                today_weekday = weekday_jp[now.weekday()]
                daily_events = self.filter_daily_events(all_daily, today_weekday)

            # Embedを作成して送信
            embed = self.create_notification_embed(upcoming_events, daily_events, now)
            await channel.send(embed=embed)

            await interaction.followup.send(f"✅ {channel.mention} にテスト通知を送信しました。")

        except Exception as e:
            logger.error(f"テストコマンドでエラーが発生: {e}", exc_info=True)
            await interaction.followup.send(f"❌ エラーが発生しました: {e}")

    @app_commands.command(
        name="starresonance-remove",
        description="スターレゾナンス通知設定を削除します"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_notification(self, interaction: discord.Interaction):
        """通知設定を削除"""
        guild_id = str(interaction.guild.id)

        if guild_id in self.config:
            del self.config[guild_id]
            self.save_config()
            await interaction.response.send_message("✅ スターレゾナンス通知設定を削除しました。")
            logger.info(f"ギルド {guild_id} の通知設定を削除しました")
        else:
            await interaction.response.send_message("ℹ️ 通知設定が見つかりませんでした。")

    @app_commands.command(
        name="starresonance-status",
        description="現在の通知設定を確認します"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def show_status(self, interaction: discord.Interaction):
        """通知設定の状態を表示"""
        guild_id = str(interaction.guild.id)

        if guild_id not in self.config:
            await interaction.response.send_message("ℹ️ このサーバーには通知設定がありません。")
            return

        guild_config = self.config[guild_id]
        channel_id = guild_config.get('channel_id')
        spreadsheet_url = guild_config.get('spreadsheet_url')
        last_notified = guild_config.get('last_notified_date', '未送信')

        channel = self.bot.get_channel(channel_id)
        channel_mention = channel.mention if channel else f"ID: {channel_id} (削除済み)"

        embed = discord.Embed(
            title="🌟 スターレゾナンス通知設定",
            color=discord.Color.blue(),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(name="📢 通知チャンネル", value=channel_mention, inline=False)
        embed.add_field(name="📊 スプレッドシートURL", value=f"[リンク]({spreadsheet_url})", inline=False)
        embed.add_field(name="📅 最終通知日", value=last_notified, inline=False)
        embed.add_field(name="⏰ 通知時刻", value="毎朝 5:00 (JST)", inline=False)
        embed.set_footer(text=f"ギルドID: {guild_id}")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="starresonance-list",
        description="全サーバーの通知設定一覧を表示します（Bot管理者専用）"
    )
    async def list_all_configs(self, interaction: discord.Interaction):
        """全サーバーの設定を表示（管理者専用）"""
        # Bot管理者チェック
        if not hasattr(self.bot, 'is_admin') or not self.bot.is_admin(interaction.user.id):
            await interaction.response.send_message("❌ このコマンドはBot管理者のみ実行できます。", ephemeral=True)
            return

        if not self.config:
            await interaction.response.send_message("ℹ️ 設定されているサーバーはありません。")
            return

        embed = discord.Embed(
            title="🌟 スターレゾナンス通知設定一覧",
            description=f"設定済みサーバー数: {len(self.config)}",
            color=discord.Color.blue(),
            timestamp=datetime.now(self.jst)
        )

        for guild_id_str, guild_config in list(self.config.items())[:25]:  # Discord制限: 最大25フィールド
            guild = self.bot.get_guild(int(guild_id_str))
            guild_name = guild.name if guild else f"不明 (ID: {guild_id_str})"
            
            channel_id = guild_config.get('channel_id')
            last_notified = guild_config.get('last_notified_date', '未送信')
            
            channel = self.bot.get_channel(channel_id) if guild else None
            channel_info = f"#{channel.name}" if channel else f"ID: {channel_id}"
            
            embed.add_field(
                name=f"🏠 {guild_name}",
                value=f"チャンネル: {channel_info}\n最終通知: {last_notified}",
                inline=True
            )

        embed.set_footer(text=f"設定ファイル: {CONFIG_FILE}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="starresonance-debug",
        description="スプレッドシートのデータ構造を確認します（デバッグ用）"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def debug_spreadsheet(self, interaction: discord.Interaction):
        """スプレッドシートのデバッグ情報を表示"""
        await interaction.response.defer()

        try:
            guild_id = str(interaction.guild.id)

            if guild_id not in self.config:
                await interaction.followup.send("❌ 通知設定がありません。先に `/notify-starresonance` で設定してください。")
                return

            guild_config = self.config[guild_id]
            spreadsheet_url = guild_config.get('spreadsheet_url')

            # スプレッドシートからデータを取得
            data = await self.fetch_spreadsheet_data(spreadsheet_url)

            embed = discord.Embed(
                title="🔍 スプレッドシート デバッグ情報",
                color=discord.Color.blue()
            )

            if not data:
                embed.description = "❌ データを取得できませんでした"
                await interaction.followup.send(embed=embed)
                return

            # 各シートの情報を表示
            for sheet_name, rows in data.items():
                if rows:
                    header = rows[0][:5] if len(rows) > 0 else []
                    sample = rows[1][:5] if len(rows) > 1 else []
                    
                    info = f"**行数**: {len(rows)}\n"
                    info += f"**ヘッダー**: `{', '.join(str(h) for h in header)}`\n"
                    if sample:
                        info += f"**サンプル**: `{', '.join(str(s) for s in sample)}`\n"
                    
                    # パース結果も表示
                    event_type = 'upcoming' if '予告' in sheet_name else 'daily'
                    events = self.parse_event_data(rows, event_type)
                    info += f"**パース結果**: {len(events)}件のイベント\n"
                    
                    if events:
                        # 最初の3件のイベントを表示
                        for i, event in enumerate(events[:3], 1):
                            event_name = event.get('name', '不明')
                            info += f"  {i}. {event_name}\n"
                    
                    embed.add_field(
                        name=f"📊 {sheet_name}",
                        value=info[:1024],
                        inline=False
                    )

            # 現在の曜日でフィルタリングした結果も表示
            if '定義_デイリー通知' in data:
                now = datetime.now(self.jst)
                weekday_jp = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']
                today_weekday = weekday_jp[now.weekday()]
                
                all_daily = self.parse_event_data(data['定義_デイリー通知'], 'daily')
                filtered = self.filter_daily_events(all_daily, today_weekday)
                
                filter_info = f"**本日の曜日**: {today_weekday}\n"
                filter_info += f"**全イベント数**: {len(all_daily)}件\n"
                filter_info += f"**本日該当**: {len(filtered)}件\n"
                
                if filtered:
                    for i, event in enumerate(filtered[:5], 1):
                        event_name = event.get('name', '不明')
                        frequency = event.get('frequency', '')
                        filter_info += f"  {i}. [{frequency}] {event_name}\n"
                
                embed.add_field(
                    name="🔍 フィルタリング結果",
                    value=filter_info[:1024],
                    inline=False
                )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"デバッグコマンドでエラーが発生: {e}", exc_info=True)
            await interaction.followup.send(f"❌ エラーが発生しました: {e}")


async def setup(bot: commands.Bot):
    """Cogのセットアップ"""
    try:
        await bot.add_cog(StarResonanceNotificationCog(bot))
        logger.info("✅ StarResonanceNotificationCog のセットアップが完了しました")
    except Exception as e:
        logger.critical(f"❌ StarResonanceNotificationCog のセットアップに失敗: {e}", exc_info=True)
        raise

