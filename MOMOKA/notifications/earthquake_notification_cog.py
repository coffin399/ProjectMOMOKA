# MOMOKA/notifications/earthquake_notification_cog.py

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Optional, Dict, Set, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# 最初にロガーを定義
logger = logging.getLogger('EarthquakeTsunamiCog')

# Matplotlibのインポート
MATPLOTLIB_AVAILABLE = False
CARTOPY_AVAILABLE = False
plt = None

try:
    import matplotlib

    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    MATPLOTLIB_AVAILABLE = True
    logger.info("✅ Matplotlibが正常にインポートされました。")

    # 日本語フォント設定（改善版）
    try:
        import japanize_matplotlib

        logger.info("✅ japanize_matplotlibが正常にインポートされました。")
    except ImportError:
        logger.info("ℹ️ japanize_matplotlibなし。代替フォントを設定します。")
        try:
            import matplotlib.font_manager as fm

            japanese_fonts = ['MS Gothic', 'Yu Gothic', 'Meiryo', 'MS UI Gothic', 'DejaVu Sans']
            available_fonts = [f.name for f in fm.fontManager.ttflist]

            for font in japanese_fonts:
                if font in available_fonts:
                    plt.rcParams['font.family'] = font
                    logger.info(f"✅ 日本語フォント設定: {font}")
                    break
            else:
                plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
                logger.warning("⚠️ 日本語フォントが見つかりません。")
        except Exception as e:
            logger.debug(f"フォント設定エラー（続行）: {e}")

    # Cartopyのインポート
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature

        CARTOPY_AVAILABLE = True
        logger.info("✅ Cartopyが正常にインポートされました。地図機能が有効です。")
    except ImportError as e:
        CARTOPY_AVAILABLE = False
        logger.warning(f"⚠️ Cartopyが見つかりません。地図機能は無効になります。")
        logger.error(f"   詳細エラー: {e}", exc_info=True)

except ImportError as e:
    MATPLOTLIB_AVAILABLE = False
    CARTOPY_AVAILABLE = False
    plt = None
    logger.error(f"❌ Matplotlibのインポートに失敗しました: {e}")
except Exception as e:
    MATPLOTLIB_AVAILABLE = False
    CARTOPY_AVAILABLE = False
    plt = None
    logger.error(f"❌ 予期しないエラーが発生しました: {e}", exc_info=True)

from MOMOKA.notifications.error.earthquake_errors import (
    EarthquakeTsunamiExceptionHandler,
    APIError,
    DataParsingError,
    ConfigError,
    NotificationError
)

DATA_DIR = 'data'
CONFIG_FILE = os.path.join(DATA_DIR, 'earthquake_tsunami_notification_config.json')


class InfoType(Enum):
    """情報タイプの定義"""
    EEW = "eew"
    QUAKE = "quake"
    TSUNAMI = "tsunami"
    UNKNOWN = "unknown"


class EarthquakeTsunamiCog(commands.Cog, name="EarthquakeNotifications"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("🔄 EarthquakeTsunamiCog 初期化開始...")

        self.ensure_data_dir()
        self.config = self.load_config()

        self.last_ids: Dict[str, Optional[str]] = {
            InfoType.EEW.value: None, InfoType.QUAKE.value: None, InfoType.TSUNAMI.value: None
        }
        self.processed_ids: Dict[str, Set[str]] = {
            InfoType.EEW.value: set(), InfoType.QUAKE.value: set(), InfoType.TSUNAMI.value: set()
        }
        self.max_processed_ids = 1000

        self.ws_session = None
        self.ws_connection = None
        self.ws_reconnect_delay = 5
        self.ws_max_reconnect_delay = 300
        self.ws_running = False

        self.http_session = None
        self.jst = timezone(timedelta(hours=+9), 'JST')
        self.api_base_url = "https://api.p2pquake.net/v2"
        self.ws_url = "wss://api.p2pquake.net/v2/ws"
        self.request_headers = {'User-Agent': 'Discord-Bot-EarthquakeTsunami/3.0', 'Accept': 'application/json'}

        self.error_stats = {'api_errors': 0, 'parsing_errors': 0, 'network_errors': 0, 'ws_disconnects': 0,
                            'last_error_time': None}
        self.processing_stats = {'eew_processed': 0, 'quake_processed': 0, 'tsunami_processed': 0, 'unknown_skipped': 0,
                                 'last_stats_output': datetime.now(self.jst)}
        self.stats_interval = 3600

        self.exception_handler = EarthquakeTsunamiExceptionHandler(self)
        logger.info("✅ EarthquakeTsunamiCog 初期化完了")

    async def cog_load(self):
        logger.info("🔄 EarthquakeTsunamiCog セットアップ開始...")
        try:
            await self.recreate_http_session()
            logger.info("🔄 最新情報のIDを初期化中...")
            await self.initialize_processed_ids()

            self.ws_running = True
            asyncio.create_task(self.websocket_listener())

            self.output_stats_task.start()

            logger.info("✅ EarthquakeTsunamiCog セットアップ完了")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "Cogのセットアップ")
            logger.critical(f"❌ セットアップに失敗しました: {e}")

    async def cog_unload(self):
        logger.info("🔄 EarthquakeTsunamiCog アンロード中...")

        self.ws_running = False
        if self.ws_connection and not self.ws_connection.closed:
            await self.ws_connection.close()
        if self.ws_session and not self.ws_session.closed:
            await self.ws_session.close()

        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

        if hasattr(self, 'output_stats_task'):
            self.output_stats_task.cancel()

        logger.info("✅ EarthquakeTsunamiCog アンロード完了")

    async def websocket_listener(self):
        """WebSocketで地震情報をリアルタイム受信"""
        reconnect_delay = self.ws_reconnect_delay

        while self.ws_running:
            try:
                logger.info(f"🔌 WebSocket接続開始: {self.ws_url}")

                if not self.ws_session or self.ws_session.closed:
                    self.ws_session = aiohttp.ClientSession(headers=self.request_headers)

                async with self.ws_session.ws_connect(self.ws_url) as ws:
                    self.ws_connection = ws
                    logger.info("✅ WebSocket接続成功")
                    reconnect_delay = self.ws_reconnect_delay

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                logger.debug(
                                    f"WebSocket受信: code={data.get('code')}, id={data.get('_id') or data.get('id')}")
                                await self.process_websocket_message(data)
                            except json.JSONDecodeError as e:
                                logger.error(f"WebSocketメッセージのJSON解析エラー: {e}")
                                self.error_stats['parsing_errors'] += 1
                            except Exception as e:
                                self.exception_handler.log_generic_error(e, "WebSocketメッセージ処理")

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WebSocketエラー: {ws.exception()}")
                            break

            except aiohttp.ClientError as e:
                logger.error(f"WebSocket接続エラー: {e}")
                self.error_stats['network_errors'] += 1
                self.error_stats['ws_disconnects'] += 1
            except Exception as e:
                self.exception_handler.log_generic_error(e, "WebSocket接続")

            if self.ws_running:
                logger.warning(f"⚠️ WebSocket切断。{reconnect_delay}秒後に再接続...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self.ws_max_reconnect_delay)

    async def process_websocket_message(self, data: Dict[str, Any]):
        """WebSocketから受信したメッセージを処理"""
        try:
            if not isinstance(data, dict):
                logger.debug("受信データが辞書型ではありません")
                return

            code = data.get('code', 0)

            if code not in [551, 552]:
                logger.debug(f"処理対象外のcode: {code}")
                return

            info_id = self.extract_id_safe(data)
            if not info_id:
                logger.warning(f"IDを抽出できませんでした: {data}")
                return

            info_type = self.classify_info_type(data)

            if info_type == InfoType.UNKNOWN:
                self.processing_stats['unknown_skipped'] += 1
                logger.debug(f"UNKNOWN情報をスキップ: ID {info_id}, code={code}")
                return

            if info_id in self.processed_ids[info_type.value]:
                logger.debug(f"既に処理済みのID: {info_id} ({info_type.value})")
                return

            logger.info(f"🆕 WebSocketで新しい{info_type.value}情報を受信: ID {info_id}, code={code}")

            if info_type == InfoType.EEW:
                await self.send_eew_notification(data)
                self.processing_stats['eew_processed'] += 1
            elif info_type == InfoType.QUAKE:
                await self.send_quake_notification(data)
                self.processing_stats['quake_processed'] += 1
            elif info_type == InfoType.TSUNAMI:
                tsunami_info = self.get_tsunami_info(data)
                if tsunami_info.get('has_tsunami', False):
                    await self.send_tsunami_notification(data, tsunami_info)
                    self.processing_stats['tsunami_processed'] += 1
                else:
                    logger.debug(f"津波データなし: ID {info_id}")
                    return

            self.processed_ids[info_type.value].add(info_id)
            self.last_ids[info_type.value] = info_id
            self.manage_processed_ids(info_type.value)

        except NotificationError as e:
            logger.error(f"通知エラー: {e}", exc_info=True)
        except Exception as e:
            self.exception_handler.log_generic_error(e, "WebSocketメッセージ処理")

    async def recreate_http_session(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers=self.request_headers,
            connector=aiohttp.TCPConnector(limit=10)
        )
        logger.info("HTTPセッションを再作成しました")

    async def safe_api_request(self, url: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
        try:
            if not self.http_session or self.http_session.closed:
                await self.recreate_http_session()
            async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except json.JSONDecodeError as e:
                        self.error_stats['last_error_time'] = datetime.now(self.jst)
                        raise self.exception_handler.handle_json_decode_error(e, url)
                else:
                    self.error_stats['last_error_time'] = datetime.now(self.jst)
                    raise self.exception_handler.handle_api_response_error(response.status, url)
        except Exception as e:
            if isinstance(e, (APIError, DataParsingError)):
                raise e
            self.error_stats['last_error_time'] = datetime.now(self.jst)
            raise self.exception_handler.handle_api_error(e, url)

    def manage_processed_ids(self, info_type: str):
        if len(self.processed_ids[info_type]) > self.max_processed_ids:
            self.processed_ids[info_type] = set(list(self.processed_ids[info_type])[-self.max_processed_ids:])
            logger.info(f"{info_type}: 処理済みID数を{self.max_processed_ids}に制限")

    async def initialize_processed_ids(self):
        logger.info("🔍 最新情報のIDを初期化中...")

        # code 551 (地震情報・緊急地震速報)
        try:
            url = f"{self.api_base_url}/history?codes=551&limit=100"
            logger.info(f"📡 地震情報取得: {url}")
            data = await self.safe_api_request(url)

            if data and isinstance(data, list):
                logger.info(f"✅ 地震情報を{len(data)}件取得")
                latest_eew_id = None
                latest_quake_id = None

                for item in data:
                    item_id = self.extract_id_safe(item)
                    if not item_id:
                        continue

                    info_type = self.classify_info_type(item)

                    if info_type == InfoType.EEW:
                        self.processed_ids[InfoType.EEW.value].add(item_id)
                        if latest_eew_id is None:
                            latest_eew_id = item_id
                            logger.info(f"  EEW最新ID: {item_id[:12]}...")
                    elif info_type == InfoType.QUAKE:
                        self.processed_ids[InfoType.QUAKE.value].add(item_id)
                        if latest_quake_id is None:
                            latest_quake_id = item_id
                            logger.info(f"  QUAKE最新ID: {item_id[:12]}...")

                if latest_eew_id:
                    self.last_ids[InfoType.EEW.value] = latest_eew_id
                if latest_quake_id:
                    self.last_ids[InfoType.QUAKE.value] = latest_quake_id
            else:
                logger.warning("⚠️ 地震情報の取得結果が空です")

        except (APIError, DataParsingError) as e:
            logger.error(f"❌ 地震情報(code 551)のID初期化に失敗: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "地震情報(code 551)のID初期化")

        # code 552 (津波情報)
        try:
            url = f"{self.api_base_url}/history?codes=552&limit=100"
            logger.info(f"📡 津波情報取得: {url}")
            data = await self.safe_api_request(url)

            if data and isinstance(data, list):
                logger.info(f"✅ 津波情報を{len(data)}件取得")

                latest_tsunami_id = None

                for idx, item in enumerate(data):
                    item_id = self.extract_id_safe(item)
                    if not item_id:
                        if idx < 3:
                            logger.warning(f"  津波情報[{idx}]のID抽出失敗: keys={list(item.keys())}")
                        continue

                    if item.get('code') == 552:
                        self.processed_ids[InfoType.TSUNAMI.value].add(item_id)
                        if latest_tsunami_id is None:
                            latest_tsunami_id = item_id
                            logger.info(f"  TSUNAMI最新ID: {item_id[:12]}...")

                if latest_tsunami_id:
                    self.last_ids[InfoType.TSUNAMI.value] = latest_tsunami_id
                else:
                    logger.warning("⚠️ 津波情報のIDが1件も取得できませんでした（過去に津波予報がない可能性があります）")
            else:
                logger.warning("⚠️ 津波情報の取得結果が空です（過去に津波予報がない可能性があります）")

        except (APIError, DataParsingError) as e:
            logger.error(f"❌ 津波情報(code 552)のID初期化に失敗: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "津波情報(code 552)のID初期化")

        logger.info("🔍 ID初期化結果:")
        for it, lid in self.last_ids.items():
            count = len(self.processed_ids.get(it, set()))
            logger.info(f"  {it.upper()}: {lid[:8] if lid else '未取得'} (処理済み: {count}件)")

    def extract_id_safe(self, item: Dict[str, Any]) -> Optional[str]:
        """IDを安全に抽出"""
        try:
            item_id = item.get('_id') or item.get('id')
            if item_id is None:
                return None
            return str(item_id)
        except Exception as e:
            logger.warning(f"ID抽出エラー: {e}")
            return None

    @tasks.loop(seconds=3600)
    async def output_stats_task(self):
        """統計情報を定期的に出力"""
        error_total = sum(v for k, v in self.error_stats.items() if k.endswith('_errors') or k == 'ws_disconnects')
        stats_msg = (
            f"[統計] EEW:{self.processing_stats['eew_processed']} "
            f"QUAKE:{self.processing_stats['quake_processed']} "
            f"TSUNAMI:{self.processing_stats['tsunami_processed']} "
            f"UNKNOWN:{self.processing_stats['unknown_skipped']} "
            f"エラー:{error_total} WS切断:{self.error_stats['ws_disconnects']}"
        )
        logger.info(stats_msg)

    def classify_info_type(self, item: Dict[str, Any]) -> InfoType:
        """情報タイプを判定"""
        try:
            code = item.get('code', 0)
            issue_type = item.get('issue', {}).get('type', '').lower()

            if code == 552:
                return InfoType.TSUNAMI

            if code == 551:
                earthquake_data = item.get('earthquake', {})

                if 'eew' in issue_type or issue_type == 'foreign':
                    return InfoType.EEW

                if issue_type == 'scaleprompt':
                    domestic_tsunami = earthquake_data.get('domesticTsunami', '')
                    if domestic_tsunami in ['Unknown', '', None]:
                        return InfoType.EEW

                if issue_type in ['detailscale', 'destination', 'scaleanddetail', 'scaleprompt']:
                    return InfoType.QUAKE

                if earthquake_data and issue_type:
                    return InfoType.QUAKE

            logger.debug(f"UNKNOWN情報: code={code}, issue.type={issue_type}")
            return InfoType.UNKNOWN

        except Exception as e:
            logger.warning(f"情報分類エラー: {e}", exc_info=True)
            return InfoType.UNKNOWN

    def ensure_data_dir(self):
        try:
            if not os.path.exists(DATA_DIR):
                os.makedirs(DATA_DIR)
        except OSError as e:
            raise ConfigError(f"データディレクトリの作成に失敗: {e}")

    def load_config(self) -> Dict[str, Any]:
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    for guild_id, value in list(config.items()):
                        if isinstance(value, int):
                            config[guild_id] = {it.value: value for it in InfoType if it != InfoType.UNKNOWN}
                    return config
        except (json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"設定ファイル読み込みエラー: {e}")
        return {}

    def save_config(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            raise ConfigError(f"設定ファイルの保存に失敗: {e}")

    def scale_to_japanese(self, scale_code):
        if scale_code is None or scale_code == -1:
            return "震度情報なし"
        scale_map = {
            10: "震度1", 20: "震度2", 30: "震度3", 40: "震度4",
            45: "震度5弱", 50: "震度5強", 55: "震度6弱", 60: "震度6強", 70: "震度7"
        }
        return scale_map.get(scale_code, f"不明({scale_code})")

    def get_embed_color(self, scale_code, info_type="quake"):
        if info_type == "tsunami":
            return discord.Color.purple()
        if scale_code is None or scale_code == -1:
            return discord.Color.light_grey()
        if scale_code >= 55:
            return discord.Color.dark_red()
        if scale_code >= 50:
            return discord.Color.red()
        if scale_code >= 40:
            return discord.Color.orange()
        if scale_code >= 30:
            return discord.Color.gold()
        return discord.Color.blue()

    def parse_earthquake_time(self, time_str, announced_time=None):
        try:
            if isinstance(time_str, str) and time_str.strip():
                try:
                    return datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S").replace(tzinfo=self.jst)
                except ValueError:
                    pass
            if announced_time and isinstance(announced_time, str):
                try:
                    return datetime.strptime(announced_time, "%Y/%m/%d %H:%M:%S").replace(tzinfo=self.jst)
                except ValueError:
                    pass
            return datetime.now(self.jst)
        except Exception:
            return datetime.now(self.jst)

    def format_magnitude(self, magnitude):
        try:
            if magnitude is None or magnitude == -1 or magnitude == "-1":
                return "不明"
            mag_value = float(magnitude)
            if mag_value == -1:
                return "不明"
            return f"M{mag_value:.1f}"
        except (ValueError, TypeError):
            return "不明"

    def format_depth(self, depth):
        try:
            if depth is None or depth == -1 or depth == "-1":
                return "不明"
            if isinstance(depth, str):
                if not depth.replace('km', '').replace('m', '').strip().isdigit():
                    return depth
                depth_value = int(depth.replace('km', '').strip())
            else:
                depth_value = int(depth)

            if depth_value == -1:
                return "不明"
            return "ごく浅い" if depth_value == 0 else f"{depth_value}km"
        except (ValueError, TypeError):
            return "不明"

    def get_tsunami_info(self, data):
        """津波情報を抽出"""
        info = {'has_tsunami': False, 'warning_level': None, 'areas': [], 'description': ""}
        try:
            if data.get('code') == 552:
                tsunami_data = data.get('tsunami')
                if not tsunami_data:
                    return info

                info['has_tsunami'] = True
                grades = {'MajorWarning': '大津波警報', 'Warning': '津波警報', 'Watch': '津波注意報'}
                highest_level = 0
                level_text = '津波予報'

                areas_data = tsunami_data.get('areas', [])
                for area in areas_data if isinstance(areas_data, list) else []:
                    if not isinstance(area, dict):
                        continue
                    grade = area.get('grade')
                    if grade == 'MajorWarning' and highest_level < 3:
                        highest_level, level_text = 3, grades[grade]
                    elif grade == 'Warning' and highest_level < 2:
                        highest_level, level_text = 2, grades[grade]
                    elif grade == 'Watch' and highest_level < 1:
                        highest_level, level_text = 1, grades[grade]
                    if area.get('name'):
                        info['areas'].append({'name': area['name'], 'grade': grades.get(grade, '情報')})

                info['warning_level'] = level_text
                return info

            earthquake_data = data.get('earthquake', {})
            domestic_tsunami = earthquake_data.get('domesticTsunami', 'None')

            if domestic_tsunami and domestic_tsunami not in ['None', '', None]:
                info['has_tsunami'] = True
                tsunami_map = {
                    'Checking': '津波の有無調査中',
                    'NonEffective': '津波の心配なし',
                    'Watch': '津波注意報',
                    'Warning': '津波警報',
                    'Unknown': '不明'
                }
                info['warning_level'] = tsunami_map.get(domestic_tsunami, domestic_tsunami)

        except Exception as e:
            logger.warning(f"津波情報取得エラー: {e}", exc_info=True)

        return info

    async def send_eew_notification(self, data):
        await self.send_notification(data, InfoType.EEW.value, "🚨 緊急地震速報")

    async def send_quake_notification(self, data):
        await self.send_notification(data, InfoType.QUAKE.value, "📊 地震情報")

    async def send_notification(self, data, info_type, title_prefix):
        try:
            earthquake = data.get('earthquake', {})
            if not earthquake:
                logger.warning(f"{info_type}: earthquake データが存在しません")
                return

            hypocenter = earthquake.get('hypocenter', {})
            issue_data = data.get('issue', {})
            report_type = issue_data.get('type', '情報')
            max_scale = earthquake.get('maxScale', -1)
            quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue_data.get('time', ''))

            magnitude = hypocenter.get('magnitude', -1)
            depth = hypocenter.get('depth', -1)

            if info_type == InfoType.EEW.value:
                description = f"強い揺れに警戒してください。" if max_scale == -1 else f"**最大震度 {self.scale_to_japanese(max_scale)}** 程度の揺れが予想されます。"
                description += "\n⚠️ **これは速報です。情報が更新される可能性があります。**"
            else:
                description = f"**最大震度 {self.scale_to_japanese(max_scale)}** の地震が発生しました。"

            embed = discord.Embed(
                title=f"{title_prefix} ({report_type})",
                description=description,
                color=self.get_embed_color(max_scale, info_type),
                timestamp=quake_time
            )
            hypocenter_name = hypocenter.get('name', '不明')
            embed.add_field(name="🌏 震源地", value=f"```{hypocenter_name or '調査中'}```", inline=True)
            mag_prefix = "推定 " if info_type == InfoType.EEW.value else ""
            embed.add_field(name="📊 マグニチュード", value=f"```{mag_prefix}{self.format_magnitude(magnitude)}```",
                            inline=True)
            embed.add_field(name="📏 深さ", value=f"```{self.format_depth(depth)}```", inline=True)

            points = data.get('points', [])
            if points and isinstance(points, list):
                areas_text = ""
                field_name = "📍 予測震度" if info_type == InfoType.EEW.value else "📍 各地の震度"
                for point in sorted(points, key=lambda p: p.get('scale', 0), reverse=True)[:8]:
                    scale, addr = point.get('scale', -1), point.get('addr', '不明')
                    emoji = "🔴" if scale >= 55 else "🟠" if scale >= 50 else "🟡" if scale >= 40 else "🟢" if scale >= 30 else "🔵"
                    scale_suffix = " 程度" if info_type == InfoType.EEW.value else ""
                    areas_text += f"{emoji} **{self.scale_to_japanese(scale)}{scale_suffix}** - {addr}\n"
                if areas_text:
                    embed.add_field(name=field_name, value=areas_text[:1024], inline=False)
            elif info_type == InfoType.EEW.value:
                embed.add_field(name="📍 震度情報", value="詳細な震度情報は確定情報をお待ちください", inline=False)

            tsunami_info = self.get_tsunami_info(data)
            if tsunami_info['has_tsunami'] and info_type == InfoType.QUAKE.value:
                embed.add_field(name="🌊 津波情報",
                                value=f"🌊 **{tsunami_info.get('warning_level', '津波予報')}** が発表されています",
                                inline=False)
            if info_type == InfoType.EEW.value:
                embed.add_field(name="⚠️ 注意",
                                value="この情報は速報です。揺れが予想される地域の方は、身の安全を確保してください。",
                                inline=False)

            embed.set_footer(text="Powered by P2P地震情報 WebSocket API | PLANA by coffin299")
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")

            map_file = None
            if CARTOPY_AVAILABLE:
                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    try:
                        quake_data = {
                            'lat': lat,
                            'lon': lon,
                            'magnitude': magnitude,
                            'depth': depth,
                            'max_scale': max_scale,
                            'name': hypocenter_name,
                            'time': quake_time
                        }

                        map_buffer = await self.generate_single_earthquake_map(quake_data, info_type)
                        map_file = discord.File(fp=map_buffer, filename="earthquake_location.png")
                        embed.set_image(url="attachment://earthquake_location.png")
                    except Exception as e:
                        logger.warning(f"地図生成に失敗: {e}")

            await self.send_embed_to_channels(embed, info_type, map_file)

        except Exception as e:
            raise NotificationError(f"{info_type}通知処理エラー: {e}")

    async def send_tsunami_notification(self, data, tsunami_info):
        try:
            warning_level = tsunami_info.get('warning_level', '津波予報')
            emoji_map = {"大津波警報": "🔴", "津波警報": "🟠", "津波注意報": "🟡"}
            embed = discord.Embed(
                title=f"{emoji_map.get(warning_level, '🌊')} {warning_level}",
                description=f"**{warning_level}** が発表されました。",
                color=discord.Color.purple(),
                timestamp=datetime.now(self.jst)
            )
            earthquake = data.get('earthquake', {})
            if earthquake and isinstance(earthquake, dict):
                hypocenter = earthquake.get('hypocenter', {})
                magnitude = hypocenter.get('magnitude', -1)
                depth = hypocenter.get('depth', -1)
                embed.add_field(name="🌏 震源地", value=f"```{hypocenter.get('name', '不明')}```", inline=True)
                embed.add_field(name="📊 マグニチュード", value=f"```{self.format_magnitude(magnitude)}```", inline=True)
                embed.add_field(name="📏 深さ", value=f"```{self.format_depth(depth)}```", inline=True)

            areas = tsunami_info.get('areas', [])
            if areas and isinstance(areas, list):
                area_text = "".join(
                    f"🌊 **{area.get('grade', warning_level)}** - {area.get('name', '不明')}\n"
                    for area in areas[:5] if isinstance(area, dict)
                )
                if area_text:
                    embed.add_field(name="🏖️ 予報区域", value=area_text, inline=False)

            warning_text = (
                "⚠️ **直ちに避難してください** ⚠️\n高台や避難ビルなど安全な場所へ" if warning_level == "大津波警報"
                else "⚠️ **直ちに避難してください**\n海岸や川から離れ、高いところへ" if warning_level == "津波警報"
                else "⚠️ 海の中や海岸付近は危険です\n海から上がって、海岸から離れてください"
            )
            embed.add_field(name="⚠️ 避難指示", value=warning_text, inline=False)
            if tsunami_info.get('description'):
                embed.add_field(name="ℹ️ 詳細情報", value=tsunami_info['description'][:500], inline=False)

            embed.set_footer(text="気象庁 | 津波から身を守るため直ちに避難を | PLANA by coffin299")
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
            await self.send_embed_to_channels(embed, InfoType.TSUNAMI.value)
        except Exception as e:
            raise NotificationError(f"津波通知処理エラー: {e}")

    async def generate_single_earthquake_map(self, quake: dict, info_type: str) -> io.BytesIO:
        """単一の地震の位置を地図に表示"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._generate_single_map_sync, quake, info_type)

    def _calculate_smart_map_extent(self, lat: float, lon: float, max_scale: int) -> tuple:
        """
        震源地の位置と震度に基づいて、最適な地図表示範囲を計算
        フィリピンなど遠方の地震にも対応
        """
        # 拡大した日本周辺の境界（フィリピンを含む）
        REGION_LON_MIN, REGION_LON_MAX = 118, 150
        REGION_LAT_MIN, REGION_LAT_MAX = 10, 46

        # 震源地が範囲外（フィリピンなど）の場合の判定
        is_far_south = lat < 24
        is_far_west = lon < 122

        # 震度に応じた基本ズーム範囲
        if max_scale >= 50:
            base_zoom = 5.0
        elif max_scale >= 40:
            base_zoom = 4.0
        else:
            base_zoom = 3.0

        # フィリピン付近の場合はズームを調整
        if is_far_south or is_far_west:
            base_zoom = max(base_zoom, 8.0)

        lon_span = base_zoom * 2
        lat_span = base_zoom * 1.6

        # 震源地からの距離を計算
        dist_to_west = lon - REGION_LON_MIN
        dist_to_east = REGION_LON_MAX - lon
        dist_to_south = lat - REGION_LAT_MIN
        dist_to_north = REGION_LAT_MAX - lat

        edge_threshold = base_zoom

        center_lon = lon
        center_lat = lat

        # 西端・東端の調整
        if dist_to_west < edge_threshold:
            center_lon = lon + (edge_threshold - dist_to_west) * 0.5
        elif dist_to_east < edge_threshold:
            center_lon = lon - (edge_threshold - dist_to_east) * 0.5

        # 南端・北端の調整（フィリピンなど南方向を特に考慮）
        if dist_to_south < edge_threshold:
            center_lat = lat + (edge_threshold - dist_to_south) * 0.5
        elif dist_to_north < edge_threshold:
            center_lat = lat - (edge_threshold - dist_to_north) * 0.5

        # 表示範囲を計算
        lon_min = center_lon - lon_span / 2
        lon_max = center_lon + lon_span / 2
        lat_min = center_lat - lat_span / 2
        lat_max = center_lat + lat_span / 2

        # 境界調整
        if lon_min < REGION_LON_MIN:
            shift = REGION_LON_MIN - lon_min
            lon_min = REGION_LON_MIN
            lon_max = min(lon_max + shift, REGION_LON_MAX)

        if lon_max > REGION_LON_MAX:
            shift = lon_max - REGION_LON_MAX
            lon_max = REGION_LON_MAX
            lon_min = max(lon_min - shift, REGION_LON_MIN)

        if lat_min < REGION_LAT_MIN:
            shift = REGION_LAT_MIN - lat_min
            lat_min = REGION_LAT_MIN
            lat_max = min(lat_max + shift, REGION_LAT_MAX)

        if lat_max > REGION_LAT_MAX:
            shift = lat_max - REGION_LAT_MAX
            lat_max = REGION_LAT_MAX
            lat_min = max(lat_min - shift, REGION_LAT_MIN)

        return (lon_min, lon_max, lat_min, lat_max)

    def _generate_single_map_sync(self, quake: dict, info_type: str) -> io.BytesIO:
        """単一の地震マップ画像を生成（台風風デザイン）"""
        lat, lon = quake['lat'], quake['lon']
        max_scale = quake['max_scale']

        fig = plt.figure(figsize=(16, 16), dpi=150, facecolor='#2c3e50')
        ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree(), facecolor='#2c3e50')

        # スマートな地図範囲計算
        lon_min, lon_max, lat_min, lat_max = self._calculate_smart_map_extent(lat, lon, max_scale)
        ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())

        # 台風風のデザイン：海と陸の色分け
        ax.add_feature(cfeature.OCEAN, facecolor='#2c3e50', zorder=0)
        ax.add_feature(cfeature.LAND, facecolor='#95a5a6', edgecolor='none', zorder=1)
        ax.add_feature(cfeature.COASTLINE, edgecolor='white', linewidth=1.5, zorder=3)

        # 都道府県境界
        try:
            states = cfeature.NaturalEarthFeature(
                category='cultural',
                name='admin_1_states_provinces_lines',
                scale='10m',
                facecolor='none'
            )
            ax.add_feature(states, edgecolor='white', linewidth=0.6, alpha=0.5, zorder=2)
        except:
            logger.debug("都道府県境界の追加をスキップ")

        # グリッド線（白色）
        ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=False,
                     linewidth=0.5, color='white', alpha=0.3, linestyle='--')

        # タイトル
        title_prefix = "緊急地震速報" if info_type == "eew" else "地震情報"
        title = f'{title_prefix} - 震源位置\n{quake["name"]}'
        ax.text(0.5, 0.98, title, transform=ax.transAxes,
                fontsize=18, fontweight='bold', ha='center', va='top', color='white',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='black',
                          edgecolor='white', alpha=0.8, linewidth=2))

        # 主要都市のマーカー
        cities = {
            '札幌': (141.35, 43.06), '仙台': (140.87, 38.27), '東京': (139.69, 35.69),
            '名古屋': (136.91, 35.18), '大阪': (135.50, 34.69), '福岡': (130.42, 33.59),
            '那覇': (127.68, 26.21), 'マニラ': (120.98, 14.60)
        }

        displayed_cities = 0
        for city, (city_lon, city_lat) in cities.items():
            if lon_min <= city_lon <= lon_max and lat_min <= city_lat <= lat_max:
                ax.plot(city_lon, city_lat, marker='^', color='yellow',
                        markersize=8, zorder=8, transform=ccrs.Geodetic(),
                        markeredgecolor='black', markeredgewidth=1.5)
                ax.text(city_lon, city_lat + 0.15, city, fontsize=9, ha='center', color='white',
                        bbox=dict(boxstyle='round,pad=0.3', facecolor='black',
                                  edgecolor='yellow', alpha=0.85, linewidth=1),
                        transform=ccrs.Geodetic(), zorder=9, fontweight='bold')
                displayed_cities += 1

        # 震源地の色とサイズ
        def get_color_and_size(scale):
            if scale >= 70:
                return '#8B0000', 550
            elif scale >= 60:
                return '#DC143C', 500
            elif scale >= 55:
                return '#FF0000', 450
            elif scale >= 50:
                return '#FF4500', 400
            elif scale >= 45:
                return '#FF8C00', 350
            elif scale >= 40:
                return '#FFA500', 300
            elif scale >= 30:
                return '#FFD700', 250
            else:
                return '#87CEEB', 200

        color, size = get_color_and_size(max_scale)

        # 震源地をマーク
        ax.scatter(lon, lat, marker='x', c='red', s=size * 2,
                   linewidths=6, zorder=11, transform=ccrs.Geodetic())
        ax.scatter(lon, lat, c='red', s=size, alpha=0.8,
                   edgecolors='white', linewidths=3, zorder=10,
                   transform=ccrs.Geodetic(), label='震源')

        # 震源地情報
        info_text = f'震度: {self.scale_to_japanese(max_scale)}\n'
        if quake['magnitude'] != -1:
            info_text += f'M{quake["magnitude"]:.1f}\n'
        if quake['depth'] != -1:
            info_text += f'深さ: {quake["depth"]}km'

        zoom_range = (lon_max - lon_min) / 2
        text_offset = zoom_range * 0.6
        text_y = lat - text_offset

        if text_y < lat_min + 0.5:
            text_y = lat + text_offset

        text_x = lon
        if lon < lon_min + 1:
            text_x = lon_min + 1.5
        elif lon > lon_max - 1:
            text_x = lon_max - 1.5

        ax.text(text_x, text_y, info_text,
                fontsize=13, ha='center', va='top', color='white',
                bbox=dict(boxstyle='round,pad=0.7', facecolor='black',
                          edgecolor='red', linewidth=2.5, alpha=0.9),
                transform=ccrs.Geodetic(), zorder=12, fontweight='bold')

        # 凡例
        ax.legend(loc='upper left', frameon=True, fontsize=12,
                  fancybox=True, shadow=True, framealpha=0.9,
                  bbox_to_anchor=(0.02, 0.92), facecolor='black',
                  edgecolor='white', labelcolor='white')

        # 画像として保存
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight',
                    pad_inches=0, facecolor='#2c3e50', edgecolor='none')
        buffer.seek(0)
        plt.close(fig)

        return buffer

    def _generate_map_sync(self, quakes: list, min_scale: Optional[str], hours: Optional[int]) -> io.BytesIO:
        """複数の地震マップ画像を生成（台風風デザイン）"""
        fig = plt.figure(figsize=(16, 16), dpi=150, facecolor='#2c3e50')
        ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree(), facecolor='#2c3e50')

        # 日本周辺に範囲を限定
        ax.set_extent([128, 146, 30, 46], crs=ccrs.PlateCarree())

        ax.add_feature(cfeature.OCEAN, facecolor='#2c3e50', zorder=0)
        ax.add_feature(cfeature.LAND, facecolor='#95a5a6', edgecolor='none', zorder=1)
        ax.add_feature(cfeature.COASTLINE, edgecolor='white', linewidth=1.5, zorder=3)

        # 都道府県境界
        try:
            states = cfeature.NaturalEarthFeature(
                category='cultural',
                name='admin_1_states_provinces_lines',
                scale='10m',
                facecolor='none'
            )
            ax.add_feature(states, edgecolor='white', linewidth=0.6, alpha=0.5, zorder=2)
        except:
            logger.debug("都道府県境界の追加をスキップ")

        # グリッド線
        ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=False,
                     linewidth=0.5, color='white', alpha=0.3, linestyle='--')

        # タイトル
        if hours is not None:
            title = f'地震発生地点マップ（過去{hours}時間、{len(quakes)}件）'
        else:
            title = f'地震発生地点マップ（{len(quakes)}件）'
        if min_scale:
            title += f'\n最小震度: {min_scale}'
        ax.text(0.5, 0.98, title, transform=ax.transAxes,
                fontsize=18, fontweight='bold', ha='center', va='top', color='white',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='black',
                          edgecolor='white', alpha=0.9, linewidth=2))

        # 震度に応じた色とサイズ
        def get_color_and_size(max_scale):
            if max_scale >= 70:
                return '#8B0000', 350, '震度7'
            elif max_scale >= 60:
                return '#DC143C', 300, '震度6強'
            elif max_scale >= 55:
                return '#FF0000', 250, '震度6弱'
            elif max_scale >= 50:
                return '#FF4500', 200, '震度5強'
            elif max_scale >= 45:
                return '#FF8C00', 150, '震度5弱'
            elif max_scale >= 40:
                return '#FFA500', 120, '震度4'
            elif max_scale >= 30:
                return '#FFD700', 100, '震度3'
            elif max_scale >= 20:
                return '#90EE90', 80, '震度2'
            else:
                return '#87CEEB', 60, '震度1'

        legend_elements = {}

        # 各地震をプロット
        for quake in quakes:
            color, size, label = get_color_and_size(quake['max_scale'])
            ax.scatter(quake['lon'], quake['lat'], c=color, s=size, alpha=0.7,
                       edgecolors='white', linewidths=1.5, zorder=5,
                       transform=ccrs.Geodetic())
            if label not in legend_elements:
                legend_elements[label] = plt.scatter([], [], c=color, s=120,
                                                     edgecolors='white', linewidths=1.5, alpha=0.7)

        # 凡例
        scale_order = ['震度7', '震度6強', '震度6弱', '震度5強', '震度5弱', '震度4', '震度3', '震度2', '震度1']
        legend_items = [legend_elements[s] for s in scale_order if s in legend_elements]
        legend_labels = [s for s in scale_order if s in legend_elements]

        if legend_items:
            legend = ax.legend(legend_items, legend_labels, loc='upper right', frameon=True,
                               fontsize=11, title='震度', title_fontsize=12,
                               fancybox=True, shadow=True, framealpha=0.9,
                               bbox_to_anchor=(0.98, 0.92), facecolor='black',
                               edgecolor='white')
            plt.setp(legend.get_texts(), color='white')
            plt.setp(legend.get_title(), color='white')

        # 主要都市
        cities = {
            '札幌': (141.35, 43.06), '東京': (139.69, 35.69),
            '名古屋': (136.91, 35.18), '大阪': (135.50, 34.69),
            '福岡': (130.42, 33.59),
        }

        for city, (lon, lat) in cities.items():
            ax.plot(lon, lat, marker='^', color='yellow', markersize=7,
                    zorder=4, transform=ccrs.Geodetic(),
                    markeredgecolor='black', markeredgewidth=1.2)
            ax.text(lon, lat + 0.35, city, fontsize=9, ha='center', color='white',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='black',
                              edgecolor='yellow', alpha=0.85, linewidth=0.8),
                    transform=ccrs.Geodetic(), zorder=4, fontweight='bold')

        # 画像として保存
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight',
                    pad_inches=0, facecolor='#2c3e50', edgecolor='none')
        buffer.seek(0)
        plt.close(fig)

        return buffer

    async def send_embed_to_channels(self, embed, info_type, map_file=None):
        if not self.config:
            logger.warning(f"通知送信スキップ ({info_type}): config が空です")
            return

        logger.info(f"📤 {info_type}通知送信開始 - 設定ギルド数: {len(self.config)}")
        sent_count, failed_count, skipped_count = 0, 0, 0
        config_modified = False

        for guild_id, guild_config in self.config.copy().items():
            try:
                if not isinstance(guild_config, dict):
                    logger.warning(f"送信スキップ ({info_type}): ギルド {guild_id} の設定が辞書型ではありません")
                    skipped_count += 1
                    continue

                channel_id = guild_config.get(info_type)
                if not channel_id:
                    skipped_count += 1
                    continue

                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    logger.warning(f"送信スキップ ({info_type}): ギルド {guild_id} が見つかりません (Bot退出済みの可能性)")
                    # ギルド自体が見つからない場合は設定全体を削除
                    del self.config[guild_id]
                    config_modified = True
                    logger.info(f"🗑️ ギルド {guild_id} の設定を削除しました")
                    failed_count += 1
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    logger.warning(f"送信スキップ ({info_type}): チャンネル {channel_id} が見つかりません (削除済み)")
                    # チャンネルが見つからない場合は該当の通知設定のみを削除
                    del self.config[guild_id][info_type]
                    config_modified = True
                    logger.info(f"🗑️ ギルド '{guild.name}' の {info_type} チャンネル設定を削除しました")
                    # 設定が空になった場合はギルド設定自体も削除
                    if not self.config[guild_id]:
                        del self.config[guild_id]
                        logger.info(f"🗑️ ギルド '{guild.name}' の設定が空になったため削除しました")
                    failed_count += 1
                    continue

                permissions = channel.permissions_for(guild.me)
                if not permissions.send_messages or not permissions.embed_links:
                    logger.error(f"送信失敗 ({info_type}): チャンネル '{channel.name}' への権限が不足")
                    failed_count += 1
                    continue

                if map_file:
                    map_file.fp.seek(0)
                    file_copy = discord.File(fp=io.BytesIO(map_file.fp.read()), filename=map_file.filename)
                    await channel.send(embed=embed, file=file_copy)
                else:
                    await channel.send(embed=embed)

                sent_count += 1
                logger.info(f"✅ 送信成功: '{guild.name}' の '{channel.name}'")

            except discord.Forbidden:
                logger.error(f"送信失敗 ({info_type}): 権限不足 - ギルド {guild_id}")
                failed_count += 1
            except discord.HTTPException as e:
                logger.error(f"送信失敗 ({info_type}): Discord APIエラー - {e.status}")
                failed_count += 1
            except Exception as e:
                logger.error(f"予期せぬ送信失敗 ({info_type}): ギルド {guild_id}", exc_info=True)
                failed_count += 1

        # 設定が変更された場合は保存
        if config_modified:
            try:
                self.save_config()
                logger.info("💾 無効なチャンネル設定を削除し、設定ファイルを更新しました")
            except Exception as e:
                logger.error(f"設定ファイルの保存に失敗: {e}")

        logger.info(
            f"📊 {info_type}通知送信完了: 成功 {sent_count}件, 失敗 {failed_count}件, スキップ {skipped_count}件")

        if sent_count == 0 and (failed_count > 0 or skipped_count > 0):
            logger.warning(f"⚠️ {info_type}の通知が1件も送信されませんでした")

    @app_commands.command(name="earthquake_channel", description="地震・津波情報の通知チャンネルを設定します")
    @app_commands.describe(channel="通知を送信するチャンネル", info_type="通知したい情報の種類")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel,
                          info_type: Literal["緊急地震速報", "地震情報", "津波予報", "すべて"]):
        try:
            guild_id = str(interaction.guild.id)
            if guild_id not in self.config:
                self.config[guild_id] = {}

            types_to_set = (
                [InfoType.EEW.value, InfoType.QUAKE.value, InfoType.TSUNAMI.value]
                if info_type == "すべて"
                else [{"緊急地震速報": InfoType.EEW.value, "地震情報": InfoType.QUAKE.value,
                       "津波予報": InfoType.TSUNAMI.value}[info_type]]
            )

            for t in types_to_set:
                self.config[guild_id][t] = channel.id

            self.save_config()
            await interaction.response.send_message(
                f"✅ **{info_type}** の通知チャンネルを {channel.mention} に設定しました。")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "チャンネル設定コマンド")
            await interaction.response.send_message(self.exception_handler.get_user_friendly_message(e),
                                                    ephemeral=False)

    @app_commands.command(name="earthquake_status", description="地震・津波情報システムの状態を確認します")
    async def status_system(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)
            embed = discord.Embed(
                title="🔧 地震・津波情報システム状態",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            ws_status = "✅ 接続中" if self.ws_connection and not self.ws_connection.closed else "❌ 切断中"
            embed.add_field(name="🔌 WebSocket状態", value=ws_status, inline=True)

            embed.add_field(
                name="🌐 HTTPセッション",
                value="✅ 正常" if self.http_session and not self.http_session.closed else "❌ 無効",
                inline=True
            )

            id_status = ""
            for it, lid in self.last_ids.items():
                count = len(self.processed_ids.get(it, set()))
                id_status += f"**{it.upper()}**: `{lid[:8] if lid else '未取得'}` ({count}件)\n"
            embed.add_field(name="🆔 最後のID", value=id_status, inline=False)

            guild_id = str(interaction.guild.id)
            if guild_id in self.config:
                channel_status = ""
                type_map = {
                    InfoType.EEW.value: '緊急地震速報',
                    InfoType.QUAKE.value: '地震情報',
                    InfoType.TSUNAMI.value: '津波予報'
                }
                for it, name in type_map.items():
                    if it in self.config[guild_id]:
                        channel = interaction.guild.get_channel(self.config[guild_id][it])
                        status = f"✅ {channel.mention}" if channel else "❌ 削除済み"
                    else:
                        status = "⚠️ 未設定"
                    channel_status += f"**{name}**: {status}\n"
            else:
                channel_status = "⚠️ すべて未設定"

            embed.add_field(name="📢 通知チャンネル", value=channel_status, inline=False)

            if self.error_stats['last_error_time']:
                embed.add_field(
                    name="🕐 最後のエラー",
                    value=self.error_stats['last_error_time'].strftime('%m/%d %H:%M:%S'),
                    inline=True
                )

            error_summary = (
                f"API: {self.error_stats['api_errors']} | "
                f"解析: {self.error_stats['parsing_errors']} | "
                f"WS切断: {self.error_stats['ws_disconnects']}"
            )
            embed.add_field(name="📊 エラー統計", value=error_summary, inline=False)

            embed.set_footer(text="システム診断完了 | P2P地震情報 WebSocket API | PLANA by coffin299")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            self.exception_handler.log_generic_error(e, "ステータスコマンド")
            msg = self.exception_handler.get_user_friendly_message(e)
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=False)
            else:
                await interaction.followup.send(msg)

    @app_commands.command(name="earthquake_test", description="地震・津波情報のテスト通知を送信します")
    @app_commands.describe(
        info_type="テストしたい情報の種類",
        max_scale="テストしたい最大震度",
        tsunami_level="テストしたい津波レベル"
    )
    async def test_notification(
            self,
            interaction: discord.Interaction,
            info_type: Literal["緊急地震速報", "地震情報", "津波予報"],
            max_scale: Optional[Literal["震度3", "震度5強", "震度7"]] = "震度5強",
            tsunami_level: Optional[Literal["津波注意報", "津波警報", "大津波警報"]] = "津波警報"
    ):
        try:
            await interaction.response.defer(ephemeral=False)
            target_channel, is_configured = interaction.channel, False
            guild_id = str(interaction.guild.id)

            if guild_id in self.config:
                type_map = {
                    "緊急地震速報": InfoType.EEW.value,
                    "地震情報": InfoType.QUAKE.value,
                    "津波予報": InfoType.TSUNAMI.value
                }
                config_key = type_map.get(info_type)
                if config_key and config_key in self.config[guild_id]:
                    channel = interaction.guild.get_channel(self.config[guild_id][config_key])
                    if channel:
                        target_channel, is_configured = channel, True

            map_file = None
            embed = None

            if info_type == "津波予報":
                embed = await self.create_tsunami_test_embed(tsunami_level)
            else:
                scale_code = {"震度3": 30, "震度5強": 50, "震度7": 70}.get(max_scale, 50)
                embed = await self.create_earthquake_test_embed(info_type, max_scale, scale_code)

                if CARTOPY_AVAILABLE:
                    try:
                        test_quake_data = {
                            'lat': 36.0, 'lon': 140.5, 'magnitude': 7.0, 'depth': 30,
                            'max_scale': scale_code, 'name': 'テスト震源地 (関東沖)',
                            'time': datetime.now(self.jst)
                        }
                        info_type_value = "eew" if info_type == "緊急地震速報" else "quake"
                        map_buffer = await self.generate_single_earthquake_map(test_quake_data, info_type_value)
                        map_file = discord.File(fp=map_buffer, filename="earthquake_test_map.png")
                        embed.set_image(url="attachment://earthquake_test_map.png")
                    except Exception as e:
                        logger.warning(f"テスト通知の地図生成に失敗: {e}")

            await target_channel.send(embed=embed, file=map_file)

            msg = (
                f"✅ 設定されたチャンネル {target_channel.mention} に **{info_type}** のテスト通知を送信しました。"
                if is_configured
                else f"✅ このチャンネルに **{info_type}** のテスト通知を送信しました。\nℹ️ 本番の通知は `/earthquake_channel` コマンドで設定したチャンネルに送信されます。"
            )
            await interaction.followup.send(msg)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {target_channel.mention} にメッセージを送信する権限がありません。")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "テスト通知コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    async def create_earthquake_test_embed(self, info_type, max_scale, scale_code):
        title = (
            f"🚨【テスト】緊急地震速報 (予報)"
            if info_type == "緊急地震速報"
            else f"📊【テスト】地震情報"
        )
        description = f"**最大震度 {max_scale}** の地震が{'検知されました' if info_type == '緊急地震速報' else '発生しました'}。"

        embed = discord.Embed(
            title=title,
            description=description,
            color=self.get_embed_color(scale_code),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(name="🌏 震源地", value="```テスト震源地```", inline=True)
        embed.add_field(name="📊 マグニチュード", value="```M7.0```", inline=True)
        embed.add_field(name="📏 深さ", value="```30km```", inline=True)
        embed.add_field(
            name="📍 各地の震度",
            value=f"🔴 **{max_scale}** - テスト県A市\n🟠 **震度4** - テスト県B市\n🟡 **震度3** - テスト県C市",
            inline=False
        )
        embed.set_footer(text="これはテスト通知です | Powered by P2P地震情報 WebSocket API | PLANA by coffin299")
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        return embed

    async def create_tsunami_test_embed(self, tsunami_level):
        emoji_map = {"津波注意報": "🟡", "津波警報": "🟠", "大津波警報": "🔴"}
        embed = discord.Embed(
            title=f"{emoji_map.get(tsunami_level, '🌊')}【テスト】{tsunami_level}",
            description=f"**{tsunami_level}** が発表されました。",
            color=discord.Color.purple(),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(name="🌏 震源地", value="```テスト海域```", inline=True)
        embed.add_field(name="📊 マグニチュード", value="```M7.5```", inline=True)
        embed.add_field(name="📏 深さ", value="```10km```", inline=True)
        embed.add_field(
            name="🏖️ 予報区域",
            value=f"🌊 **{tsunami_level}**\n・テスト県沿岸\n・テスト湾\n・テスト海岸",
            inline=False
        )
        warning_text = (
            "⚠️ **直ちに避難してください** ⚠️"
            if tsunami_level == "大津波警報"
            else "⚠️ 直ちに海岸や川から離れ、高いところに避難してください。"
            if tsunami_level == "津波警報"
            else "⚠️ 海の中や海岸付近は危険です。海から上がって、海岸から離れてください。"
        )
        embed.add_field(name="⚠️ 注意事項", value=warning_text, inline=False)
        embed.set_footer(text="これはテスト通知です | 気象庁 | PLANA by coffin299")
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        return embed

    @app_commands.command(name="earthquake_remove", description="地震・津波情報の通知設定を削除します")
    @app_commands.describe(info_type="削除したい通知設定")
    async def remove_channel(
            self,
            interaction: discord.Interaction,
            info_type: Literal["緊急地震速報", "地震情報", "津波予報", "すべて"]
    ):
        try:
            guild_id = str(interaction.guild.id)

            if guild_id not in self.config:
                await interaction.response.send_message("❌ このサーバーには通知設定がありません。", ephemeral=False)
                return

            type_map = {
                "緊急地震速報": InfoType.EEW.value,
                "地震情報": InfoType.QUAKE.value,
                "津波予報": InfoType.TSUNAMI.value
            }

            removed_types = []

            if info_type == "すべて":
                # すべての設定を削除
                if guild_id in self.config:
                    del self.config[guild_id]
                    removed_types = ["緊急地震速報", "地震情報", "津波予報"]
                    self.save_config()
                    await interaction.response.send_message(
                        "✅ **すべての通知設定** を削除しました。",
                        ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        "❌ このサーバーには通知設定がありません。",
                        ephemeral=False
                    )
                return
            else:
                # 個別の設定を削除
                config_key = type_map[info_type]
                if config_key in self.config[guild_id]:
                    del self.config[guild_id][config_key]
                    removed_types.append(info_type)

                    # 設定が空になった場合はギルド設定自体も削除
                    if not self.config[guild_id]:
                        del self.config[guild_id]
                        logger.info(f"ギルド '{interaction.guild.name}' の設定が空になったため削除しました")

                    self.save_config()
                    await interaction.response.send_message(
                        f"✅ **{info_type}** の通知設定を削除しました。",
                        ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        f"❌ **{info_type}** の通知設定は存在しません。",
                        ephemeral=False
                    )

        except Exception as e:
            self.exception_handler.log_generic_error(e, "通知削除コマンド")
            await interaction.response.send_message(
                self.exception_handler.get_user_friendly_message(e),
                ephemeral=False
            )

    @app_commands.command(name="earthquake_help", description="このシステムのヘルプを表示します")
    async def help_system(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 地震・津波情報システム ヘルプ",
            description="このボットは気象庁の地震・津波情報をリアルタイムで通知します（WebSocket接続）。",
            color=discord.Color.green(),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(
            name="🛠️ 利用可能なコマンド",
            value=(
                "**🔧 設定コマンド**\n"
                "`/earthquake_channel` - 通知チャンネルを設定\n"
                "`/earthquake_remove` - 通知設定を削除\n"
                "`/earthquake_test` - テスト通知を送信\n\n"
                "**📊 情報表示コマンド**\n"
                "`/earthquake_status` - システム状態を確認\n"
                "`/earthquake_history` - 最近の地震履歴を表示\n"
                "`/earthquake_map` - 地震を地図上に表示\n"
                "`/earthquake_debug` - 詳細診断情報を表示\n\n"
                "**❓ その他**\n"
                "`/earthquake_help` - このヘルプを表示"
            ),
            inline=False
        )

    @app_commands.command(name="earthquake_map", description="最近の地震を日本地図上に表示します")
    @app_commands.describe(
        limit="表示する地震の数（1-50）",
        min_scale="表示する最小震度",
        hours="過去何時間以内の地震を表示（1-168時間=7日）"
    )
    async def show_earthquake_map(
            self,
            interaction: discord.Interaction,
            limit: Optional[int] = 20,
            min_scale: Optional[Literal[
                "震度1", "震度2", "震度3", "震度4", "震度5弱", "震度5強", "震度6弱", "震度6強", "震度7"]] = None,
            hours: Optional[int] = 24
    ):
        try:
            await interaction.response.defer(ephemeral=False)

            if not CARTOPY_AVAILABLE:
                await interaction.followup.send("❌ 地図機能は現在利用できません。Bot管理者にお問い合わせください。")
                return

            limit = max(1, min(limit, 50))
            hours = max(1, min(hours, 168))

            scale_map = {
                "震度1": 10, "震度2": 20, "震度3": 30, "震度4": 40,
                "震度5弱": 45, "震度5強": 50, "震度6弱": 55, "震度6強": 60, "震度7": 70
            }
            min_scale_code = scale_map.get(min_scale, 0) if min_scale else 0

            cutoff_time = datetime.now(self.jst) - timedelta(hours=hours)

            url = f"{self.api_base_url}/history?codes=551&limit=100"
            data = await self.safe_api_request(url)

            if not data or not isinstance(data, list):
                await interaction.followup.send("❌ 地震情報の取得に失敗しました。")
                return

            filtered_quakes = []
            for item in data:
                info_type = self.classify_info_type(item)
                if info_type != InfoType.QUAKE:
                    continue

                earthquake = item.get('earthquake', {})
                max_scale = earthquake.get('maxScale', -1)

                if max_scale < min_scale_code:
                    continue

                issue = item.get('issue', {})
                quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                if quake_time < cutoff_time:
                    continue

                hypocenter = earthquake.get('hypocenter', {})
                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    filtered_quakes.append({
                        'lat': lat,
                        'lon': lon,
                        'magnitude': hypocenter.get('magnitude', -1),
                        'depth': hypocenter.get('depth', -1),
                        'max_scale': max_scale,
                        'name': hypocenter.get('name', '不明'),
                        'time': quake_time
                    })

                    if len(filtered_quakes) >= limit:
                        break

            if not filtered_quakes:
                filter_text = f"（{min_scale}以上、過去{hours}時間以内）" if min_scale else f"（過去{hours}時間以内）"
                await interaction.followup.send(f"ℹ️ 該当する地震情報{filter_text}が見つかりませんでした。")
                return

            image_buffer = await self.generate_earthquake_map(filtered_quakes, min_scale, hours)

            file = discord.File(fp=image_buffer, filename="earthquake_map.png")

            embed = discord.Embed(
                title=f"📍 地震発生地点マップ ({len(filtered_quakes)}件)",
                description=f"過去{hours}時間以内、最小震度: {min_scale or '指定なし'}",
                color=discord.Color.red(),
                timestamp=datetime.now(self.jst)
            )
            embed.set_image(url="attachment://earthquake_map.png")
            embed.set_footer(text="データ提供: P2P地震情報 API | PLANA by coffin299")

            await interaction.followup.send(embed=embed, file=file)

        except (APIError, DataParsingError) as e:
            logger.error(f"地図生成エラー: {e}")
            await interaction.followup.send(f"❌ 地震情報の取得中にエラーが発生しました: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "地図表示コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    async def generate_earthquake_map(self, quakes: list, min_scale: Optional[str], hours: int) -> io.BytesIO:
        """地震マップ画像を生成"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._generate_map_sync, quakes, min_scale, hours)

    @app_commands.command(name="earthquake_history", description="最近の地震情報を表示します")
    @app_commands.describe(
        limit="表示する地震の数（1-20）",
        min_scale="表示する最小震度"
    )
    async def show_history(
            self,
            interaction: discord.Interaction,
            limit: Optional[int] = 10,
            min_scale: Optional[
                Literal["震度1", "震度2", "震度3", "震度4", "震度5弱", "震度5強", "震度6弱", "震度6強", "震度7"]] = None
    ):
        try:
            await interaction.response.defer(ephemeral=False)

            limit = max(1, min(limit, 20))

            scale_map = {
                "震度1": 10, "震度2": 20, "震度3": 30, "震度4": 40,
                "震度5弱": 45, "震度5強": 50, "震度6弱": 55, "震度6強": 60, "震度7": 70
            }
            min_scale_code = scale_map.get(min_scale, 0) if min_scale else 0

            url = f"{self.api_base_url}/history?codes=551&limit=100"
            data = await self.safe_api_request(url)

            if not data or not isinstance(data, list):
                await interaction.followup.send("❌ 地震情報の取得に失敗しました。")
                return

            filtered_quakes = []
            for item in data:
                info_type = self.classify_info_type(item)
                if info_type == InfoType.QUAKE:
                    max_scale = item.get('earthquake', {}).get('maxScale', -1)
                    if max_scale >= min_scale_code:
                        filtered_quakes.append(item)
                        if len(filtered_quakes) >= limit:
                            break

            if not filtered_quakes:
                filter_text = f"（{min_scale}以上）" if min_scale else ""
                await interaction.followup.send(f"ℹ️ 該当する地震情報{filter_text}が見つかりませんでした。")
                return

            map_quakes = []
            for quake in filtered_quakes:
                earthquake = quake.get('earthquake', {})
                hypocenter = earthquake.get('hypocenter', {})
                issue = quake.get('issue', {})

                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    max_scale = earthquake.get('maxScale', -1)
                    quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                    magnitude = hypocenter.get('magnitude', -1)
                    depth = hypocenter.get('depth', -1)

                    map_quakes.append({
                        'lat': lat,
                        'lon': lon,
                        'magnitude': magnitude,
                        'depth': depth,
                        'max_scale': max_scale,
                        'name': hypocenter.get('name', '不明'),
                        'time': quake_time
                    })

            embed = discord.Embed(
                title=f"📊 最近の地震情報 ({len(filtered_quakes)}件)",
                description=f"最小震度: {min_scale or '指定なし'}",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            for idx, quake in enumerate(filtered_quakes, 1):
                earthquake = quake.get('earthquake', {})
                hypocenter = earthquake.get('hypocenter', {})
                issue = quake.get('issue', {})

                max_scale = earthquake.get('maxScale', -1)
                quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                magnitude = hypocenter.get('magnitude', -1)
                depth = hypocenter.get('depth', -1)
                location = hypocenter.get('name', '不明')

                emoji = "🔴" if max_scale >= 55 else "🟠" if max_scale >= 50 else "🟡" if max_scale >= 40 else "🟢" if max_scale >= 30 else "🔵"

                field_value = (
                    f"{emoji} **{self.scale_to_japanese(max_scale)}**\n"
                    f"🌏 {location}\n"
                    f"📊 {self.format_magnitude(magnitude)} / 📏 {self.format_depth(depth)}\n"
                    f"🕐 {quake_time.strftime('%m/%d %H:%M:%S')}"
                )

                embed.add_field(
                    name=f"{idx}. {quake_time.strftime('%m/%d %H:%M')}",
                    value=field_value,
                    inline=True if idx <= 3 else False
                )

                if idx % 3 == 0 and idx < len(filtered_quakes):
                    embed.add_field(name="\u200b", value="\u200b", inline=False)

            embed.set_footer(text="データ提供: P2P地震情報 API | PLANA by coffin299")
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")

            if map_quakes and CARTOPY_AVAILABLE:
                try:
                    map_buffer = await self.generate_earthquake_map(map_quakes, min_scale, None)
                    map_file = discord.File(fp=map_buffer, filename="earthquake_history_map.png")
                    embed.set_image(url="attachment://earthquake_history_map.png")
                    await interaction.followup.send(embed=embed, file=map_file)
                except Exception as e:
                    logger.warning(f"履歴地図生成に失敗: {e}")
                    await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(embed=embed)

        except (APIError, DataParsingError) as e:
            logger.error(f"履歴取得エラー: {e}")
            await interaction.followup.send(f"❌ 地震情報の取得中にエラーが発生しました: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "履歴表示コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    @app_commands.command(name="earthquake_debug", description="通知設定の詳細診断")
    async def debug_config(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)

            guild_id = str(interaction.guild.id)
            embed = discord.Embed(
                title="🔍 通知設定診断",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            embed.add_field(
                name="📁 設定ファイル",
                value=f"```json\n{json.dumps(self.config, indent=2, ensure_ascii=False)[:500]}```",
                inline=False
            )

            if guild_id in self.config:
                guild_config = self.config[guild_id]
                config_text = ""
                for info_type, channel_id in guild_config.items():
                    channel = interaction.guild.get_channel(channel_id)
                    if channel:
                        perms = channel.permissions_for(interaction.guild.me)
                        config_text += f"**{info_type}**:\n"
                        config_text += f"  チャンネル: {channel.mention} (ID: {channel_id})\n"
                        config_text += f"  メッセージ送信: {'✅' if perms.send_messages else '❌'}\n"
                        config_text += f"  埋め込みリンク: {'✅' if perms.embed_links else '❌'}\n"
                    else:
                        config_text += f"**{info_type}**: ❌ チャンネル {channel_id} が見つかりません\n"

                embed.add_field(name="⚙️ このサーバーの設定", value=config_text or "設定なし", inline=False)
            else:
                embed.add_field(name="⚙️ このサーバーの設定", value="❌ 未設定", inline=False)

            ws_info = "✅ 接続中" if self.ws_connection and not self.ws_connection.closed else "❌ 切断中"
            embed.add_field(
                name="🤖 Bot状態",
                value=(
                    f"ギルド数: {len(self.bot.guilds)}\n"
                    f"WebSocket: {ws_info}\n"
                    f"HTTPセッション: {'✅' if self.http_session and not self.http_session.closed else '❌'}\n"
                    f"WS切断回数: {self.error_stats['ws_disconnects']}"
                ),
                inline=False
            )

            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            logger.error(f"診断コマンドエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=False)


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(EarthquakeTsunamiCog(bot))
    except Exception as e:
        logger.critical(f"Cogセットアップエラー: {e}", exc_info=True)
        raise