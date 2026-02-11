# MOMOKA/scheduler/match_time_cog.py
# 「調整さん」風の時間調整機能を提供するCog
# ユーザーが希望する時間帯を入力し、最もマッチする時間帯を動的に算出・表示する
import logging
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# カスタムエラーをインポート
from MOMOKA.scheduler.error.errors import InvalidTimeFormatError, TimeRangeError

# ロガーの初期化
logger = logging.getLogger(__name__)


# =============================================================================
# ユーティリティ関数
# =============================================================================

def parse_time(time_str: str) -> Optional[int]:
    """
    HH:MM形式の文字列を分単位の整数に変換する。
    無効な形式の場合はNoneを返す。

    Args:
        time_str: "21:00" のような24H表記の時刻文字列

    Returns:
        0時0分からの経過分数（例: "21:00" → 1260）、無効時はNone
    """
    # HH:MM形式にマッチするかチェック
    match = re.match(r'^(\d{1,2}):(\d{2})$', time_str.strip())
    if not match:
        return None
    # 時と分をそれぞれ取得
    hour = int(match.group(1))
    minute = int(match.group(2))
    # 時刻の範囲チェック（0:00〜23:59）
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    # 分単位に変換して返す
    return hour * 60 + minute


def minutes_to_time_str(minutes: int) -> str:
    """
    分単位の整数をHH:MM形式の文字列に変換する。

    Args:
        minutes: 0時0分からの経過分数

    Returns:
        "21:00" のような24H表記の時刻文字列
    """
    # 時と分に分解
    h = minutes // 60
    m = minutes % 60
    # ゼロ埋め2桁でフォーマット
    return f"{h:02d}:{m:02d}"


def calculate_best_match(
    entries: Dict[int, dict]
) -> Tuple[Optional[str], int, List[str]]:
    """
    全ユーザーのエントリから最もマッチする時間帯を算出する。
    各分に何人が参加可能かカウントし、最大人数の連続時間帯を返す。

    Args:
        entries: {user_id: {"user_name": str, "start_time": str, "end_time": str}}

    Returns:
        (最適時間帯文字列, 参加可能人数, 参加可能ユーザー名リスト)
        エントリが空の場合は (None, 0, [])
    """
    # エントリが空なら早期リターン
    if not entries:
        return None, 0, []

    # 各分ごとの参加可能ユーザーIDを集計する配列（0:00〜23:59 = 1440分）
    minute_slots = defaultdict(set)

    for user_id, entry in entries.items():
        # 開始・終了時刻を分に変換
        start = parse_time(entry["start_time"])
        end = parse_time(entry["end_time"])
        # パース失敗時はスキップ
        if start is None or end is None:
            continue

        if start < end:
            # 通常パターン（例: 21:00〜23:00）
            for m in range(start, end + 1):
                minute_slots[m].add(user_id)
        else:
            # 日跨ぎパターン（例: 23:00〜01:00）
            for m in range(start, 1440):
                minute_slots[m].add(user_id)
            for m in range(0, end + 1):
                minute_slots[m].add(user_id)

    # スロットが空なら早期リターン
    if not minute_slots:
        return None, 0, []

    # 最大参加人数を算出
    max_count = max(len(users) for users in minute_slots.values())

    # 最大人数が参加可能な連続時間帯を探索
    best_start = None
    best_end = None
    current_start = None
    longest_duration = 0
    best_users = set()

    # 全1440分をスキャンして最大人数の連続区間を検出
    sorted_minutes = sorted(minute_slots.keys())
    for i, m in enumerate(sorted_minutes):
        if len(minute_slots[m]) == max_count:
            # 最大人数に到達している分
            if current_start is None:
                current_start = m
            # 次の分が連続しているか、または最後の要素かチェック
            if i + 1 >= len(sorted_minutes) or sorted_minutes[i + 1] != m + 1 or len(minute_slots[sorted_minutes[i + 1]]) != max_count:
                # 連続区間の終端
                duration = m - current_start
                if duration > longest_duration:
                    longest_duration = duration
                    best_start = current_start
                    best_end = m
                    best_users = minute_slots[m].copy()
                current_start = None
        else:
            # 最大人数未満なのでリセット
            current_start = None

    # 結果がなければ早期リターン
    if best_start is None or best_end is None:
        return None, 0, []

    # 最適時間帯の文字列を生成
    time_range_str = f"{minutes_to_time_str(best_start)} 〜 {minutes_to_time_str(best_end)}"
    # 参加可能ユーザー名リストを取得
    matched_user_names = [
        entries[uid]["user_name"]
        for uid in best_users
        if uid in entries
    ]

    return time_range_str, max_count, matched_user_names


# =============================================================================
# Embed生成関数
# =============================================================================

def build_schedule_embed(
    title: str,
    entries: Dict[int, dict],
    is_closed: bool = False,
    closed_by: Optional[str] = None
) -> discord.Embed:
    """
    時間調整の状況を表すEmbedを生成する。

    Args:
        title: 募集タイトル名
        entries: ユーザーエントリの辞書
        is_closed: 調整が終了したかどうか
        closed_by: 終了させたユーザー名（終了時のみ）

    Returns:
        discord.Embed オブジェクト
    """
    # 終了・進行中で色を分岐
    if is_closed:
        color = discord.Color.dark_grey()
        status = "🔒 調整終了"
    else:
        color = discord.Color.blue()
        status = "📅 回答受付中"

    # Embedの基本構造を構築
    embed = discord.Embed(
        title=f"🗓️ {title}",
        description=f"**ステータス:** {status}",
        color=color
    )

    # --- 参加者一覧セクション ---
    if entries:
        # 各ユーザーの入力情報をフォーマット
        participants_lines = []
        for user_id, entry in entries.items():
            # ユーザー名と入力した時間帯を1行で表示
            line = f"👤 **{entry['user_name']}** ── `{entry['start_time']}` 〜 `{entry['end_time']}`"
            participants_lines.append(line)
        # フィールドに追加
        embed.add_field(
            name=f"📝 回答一覧（{len(entries)}名）",
            value="\n".join(participants_lines),
            inline=False
        )

        # --- ベストマッチ時間帯の算出・表示 ---
        best_range, best_count, matched_users = calculate_best_match(entries)
        if best_range:
            # マッチしたユーザー名を列挙
            matched_str = "、".join(matched_users)
            embed.add_field(
                name="✅ ベストマッチ時間帯",
                value=(
                    f"**{best_range}**\n"
                    f"参加可能: **{best_count}名** ({matched_str})"
                ),
                inline=False
            )
        else:
            embed.add_field(
                name="✅ ベストマッチ時間帯",
                value="まだ重複する時間帯がありません。",
                inline=False
            )
    else:
        # 参加者がいない場合
        embed.add_field(
            name="📝 回答一覧",
            value="まだ回答がありません。\n下の「時刻を入力する」ボタンから入力してください。",
            inline=False
        )

    # 終了時にフッター情報を追加
    if is_closed and closed_by:
        embed.set_footer(text=f"🔒 {closed_by} が調整を終了しました")

    return embed


# =============================================================================
# モーダル（時刻入力フォーム）
# =============================================================================

class TimeInputModal(discord.ui.Modal, title="時刻を入力"):
    """時刻入力用のモーダルダイアログ"""

    # 開始時刻の入力欄
    start_time = discord.ui.TextInput(
        label="開始時刻（24H表記）",
        placeholder="例: 21:00",
        required=True,
        max_length=5,
        min_length=4,
        style=discord.TextStyle.short
    )

    # 終了時刻の入力欄
    end_time = discord.ui.TextInput(
        label="終了予定時刻（24H表記）",
        placeholder="例: 22:30",
        required=True,
        max_length=5,
        min_length=4,
        style=discord.TextStyle.short
    )

    def __init__(self, schedule_title: str, cog: "MatchTimeCog", message_id: int):
        """
        モーダルの初期化。

        Args:
            schedule_title: 募集タイトル名（モーダルのタイトルに使用）
            cog: MatchTimeCogインスタンス（データ保存用）
            message_id: 対象の調整メッセージID
        """
        # モーダルのタイトルに募集名を含める（最大45文字制限対応）
        super().__init__(title=f"📅 {schedule_title[:40]}")
        # Cogインスタンスの参照を保持
        self.cog = cog
        # 対象メッセージIDを保持
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        """モーダル送信時の処理"""
        # 入力値を取得
        start_str = self.start_time.value.strip()
        end_str = self.end_time.value.strip()

        # 開始時刻のバリデーション
        start_minutes = parse_time(start_str)
        if start_minutes is None:
            await interaction.response.send_message(
                "❌ 開始時刻の形式が正しくありません。`HH:MM`（例: `21:00`）の形式で入力してください。",
                ephemeral=True
            )
            return

        # 終了時刻のバリデーション
        end_minutes = parse_time(end_str)
        if end_minutes is None:
            await interaction.response.send_message(
                "❌ 終了時刻の形式が正しくありません。`HH:MM`（例: `22:30`）の形式で入力してください。",
                ephemeral=True
            )
            return

        # セッションの存在チェック
        if self.message_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ この時間調整セッションは既に終了しています。",
                ephemeral=True
            )
            return

        # セッションデータを取得
        session = self.cog.sessions[self.message_id]

        # ユーザーのエントリを登録（上書き可能）
        session["entries"][interaction.user.id] = {
            "user_name": interaction.user.display_name,
            "start_time": start_str,
            "end_time": end_str
        }

        # Embedを再構築して更新
        embed = build_schedule_embed(
            title=session["title"],
            entries=session["entries"]
        )

        try:
            # 元のメッセージを編集して最新状態に更新
            message = session.get("message")
            if message:
                await message.edit(embed=embed)
        except discord.NotFound:
            logger.warning(f"時間調整メッセージが見つかりません (ID: {self.message_id})")
        except discord.Forbidden:
            logger.warning(f"時間調整メッセージの編集権限がありません (ID: {self.message_id})")
        except Exception as e:
            logger.error(f"時間調整メッセージの更新中にエラー: {e}")

        # 入力完了を通知（エフェメラル）
        await interaction.response.send_message(
            f"✅ 時刻を登録しました！\n"
            f"**開始:** `{start_str}` ── **終了:** `{end_str}`",
            ephemeral=True
        )
        logger.info(
            f"時間調整に回答: {interaction.user} ({start_str}〜{end_str}), "
            f"セッション: {self.message_id}"
        )


# =============================================================================
# ボタンUI（View）
# =============================================================================

class MatchTimeView(discord.ui.View):
    """時間調整用のボタンView（永続的）"""

    def __init__(self, cog: "MatchTimeCog", message_id: int, schedule_title: str):
        """
        Viewの初期化。タイムアウトなし（永続表示）。

        Args:
            cog: MatchTimeCogインスタンス
            message_id: 対象メッセージID
            schedule_title: 募集タイトル名
        """
        # タイムアウトなし（ボタンを永続的に表示）
        super().__init__(timeout=None)
        # Cogインスタンス参照を保持
        self.cog = cog
        # メッセージIDを保持
        self.message_id = message_id
        # 募集タイトルを保持
        self.schedule_title = schedule_title

    @discord.ui.button(
        label="時刻を入力する",
        style=discord.ButtonStyle.success,
        emoji="⏰",
        custom_id="match_time_input"
    )
    async def input_time_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """「時刻を入力する」ボタン押下時の処理（緑ボタン）"""
        # セッションの存在チェック
        if self.message_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ この時間調整セッションは既に終了しています。",
                ephemeral=True
            )
            return

        # モーダルを表示
        modal = TimeInputModal(
            schedule_title=self.schedule_title,
            cog=self.cog,
            message_id=self.message_id
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="時間調整を終了する",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="match_time_close"
    )
    async def close_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """「時間調整を終了する」ボタン押下時の処理（赤ボタン）"""
        # セッションの存在チェック
        if self.message_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ この時間調整セッションは既に終了しています。",
                ephemeral=True
            )
            return

        # セッションデータを取得
        session = self.cog.sessions[self.message_id]

        # 終了済みEmbedを生成
        embed = build_schedule_embed(
            title=session["title"],
            entries=session["entries"],
            is_closed=True,
            closed_by=interaction.user.display_name
        )

        # ボタンをすべて無効化した新しいViewを作成
        disabled_view = discord.ui.View(timeout=None)
        # 入力ボタン（無効化）
        disabled_input = discord.ui.Button(
            label="時刻を入力する",
            style=discord.ButtonStyle.success,
            emoji="⏰",
            disabled=True
        )
        # 終了ボタン（無効化）
        disabled_close = discord.ui.Button(
            label="時間調整を終了する",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            disabled=True
        )
        disabled_view.add_item(disabled_input)
        disabled_view.add_item(disabled_close)

        try:
            # メッセージを更新（ボタン無効化 + Embed更新）
            message = session.get("message")
            if message:
                await message.edit(embed=embed, view=disabled_view)
        except Exception as e:
            logger.error(f"時間調整の終了処理中にエラー: {e}")

        # セッションをメモリから削除
        self.cog.sessions.pop(self.message_id, None)

        # 終了通知（エフェメラル）
        await interaction.response.send_message(
            "🔒 時間調整を終了しました。",
            ephemeral=True
        )
        logger.info(
            f"時間調整セッション終了: {self.message_id} by {interaction.user}"
        )


# =============================================================================
# Cogメインクラス
# =============================================================================

class MatchTimeCog(commands.Cog, name="時間調整"):
    """調整さん風の時間調整機能を提供するCog"""

    def __init__(self, bot: commands.Bot):
        """
        Cogの初期化。

        Args:
            bot: Botインスタンス
        """
        self.bot = bot
        # アクティブなセッションを格納する辞書
        # { message_id: { "title": str, "entries": dict, "message": discord.Message } }
        self.sessions: Dict[int, dict] = {}

    @app_commands.command(
        name="match_time",
        description="時間調整を開始します。参加者が希望時間帯を入力し、ベストマッチを自動算出します。"
    )
    @app_commands.describe(
        title="募集タイトル名（例: スクリム練習、ランク周回 など）"
    )
    async def match_time(self, interaction: discord.Interaction, title: str):
        """
        /match_time <title> コマンドのエントリポイント。
        時間調整セッションを開始し、入力用のボタン付きEmbedを送信する。

        Args:
            interaction: Discordインタラクション
            title: 募集タイトル名
        """
        # 初期状態のEmbed（参加者なし）を生成
        embed = build_schedule_embed(title=title, entries={})

        # 仮のmessage_id（後で実際のIDに置換）
        # まずdeferせずに直接応答してメッセージを取得
        await interaction.response.defer()

        # フォローアップでメッセージを送信（Viewは後でセット）
        # 仮Viewを作成（message_id=0で初期化、後で差し替え）
        temp_view = discord.ui.View(timeout=None)
        # 仮ボタン（後で正式なViewに差し替え）
        message = await interaction.followup.send(embed=embed, wait=True)

        # 実際のメッセージIDでセッションを登録
        self.sessions[message.id] = {
            "title": title,
            "entries": {},
            "message": message
        }

        # 正式なViewを作成してメッセージを編集
        view = MatchTimeView(
            cog=self,
            message_id=message.id,
            schedule_title=title
        )
        await message.edit(view=view)

        logger.info(
            f"時間調整セッション開始: '{title}' (ID: {message.id}) "
            f"by {interaction.user} in {interaction.guild}"
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        """Cog内のアプリコマンドエラーハンドリング"""
        if isinstance(error, (InvalidTimeFormatError, TimeRangeError)):
            # カスタムエラーはユーザーに通知
            embed = discord.Embed(
                title="❌ 入力エラー",
                description=str(error),
                color=discord.Color.red()
            )
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # 予期しないエラーはログに記録
            logger.error(f"MatchTimeCogで予期しないエラー: {error}", exc_info=error)
            msg = "❌ 予期しないエラーが発生しました。"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


# =============================================================================
# Cogセットアップ
# =============================================================================

async def setup(bot: commands.Bot):
    """CogをBotに登録するセットアップ関数"""
    cog = MatchTimeCog(bot)
    await bot.add_cog(cog)
    logger.info("MatchTimeCogが正常にロードされました。")
