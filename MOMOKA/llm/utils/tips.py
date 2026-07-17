# MOMOKA/llm/utils/tips.py
import json
import logging
import os
import random
import time
from collections import defaultdict, deque
from typing import List, Dict, Any, Optional

import discord

# ロガー設定
logger = logging.getLogger(__name__)

# 応答時間記録の保存先パス
RESPONSE_TIMES_PATH = "data/response_times.json"
# ローリング平均に使用する直近のサンプル数
MAX_SAMPLES = 20


class ResponseTimeTracker:
    """モデルごとの応答時間をローリング平均で追跡するクラス"""

    def __init__(self, save_path: str = RESPONSE_TIMES_PATH,
                 max_samples: int = MAX_SAMPLES):
        # 保存先ファイルパス
        self.save_path = save_path
        # ローリング平均に使うサンプル数上限
        self.max_samples = max_samples
        # モデル名 → 応答時間(秒)のdeque
        self._times: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.max_samples)
        )
        # 永続化ファイルからデータを復元
        self._load()

    # ------------------------------------------------------------------
    # 永続化: ロード / セーブ
    # ------------------------------------------------------------------
    def _load(self) -> None:
        """data/response_times.json から過去の記録を復元する"""
        if not os.path.exists(self.save_path):
            logger.info("応答時間データファイルが未作成: %s", self.save_path)
            return
        try:
            with open(self.save_path, "r", encoding="utf-8") as f:
                raw: Dict[str, List[float]] = json.load(f)
            # JSON → deque へ変換
            for model, times in raw.items():
                self._times[model] = deque(
                    times[-self.max_samples:], maxlen=self.max_samples
                )
            logger.info(
                "応答時間データを復元: %d モデル分", len(self._times)
            )
        except Exception as e:
            logger.warning("応答時間データの読込に失敗: %s", e)

    def _save(self) -> None:
        """現在の記録を data/response_times.json に保存する"""
        try:
            # data/ ディレクトリが無ければ作成
            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            # deque → list へ変換して JSON 化
            payload = {
                model: list(times)
                for model, times in self._times.items()
            }
            with open(self.save_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("応答時間データの保存に失敗: %s", e)

    # ------------------------------------------------------------------
    # 記録 / 取得
    # ------------------------------------------------------------------
    def record(self, model_name: str, elapsed_seconds: float) -> None:
        """応答完了後に呼び出し、応答にかかった秒数を記録する"""
        # 極端に短い/長い値はフィルタ（0.5秒未満 or 10分超は除外）
        if elapsed_seconds < 0.5 or elapsed_seconds > 600:
            return
        self._times[model_name].append(elapsed_seconds)
        # 記録のたびにファイルへ永続化
        self._save()
        logger.debug(
            "応答時間を記録: %s = %.1f秒 (サンプル数: %d)",
            model_name, elapsed_seconds, len(self._times[model_name])
        )

    def get_estimate(self, model_name: str) -> Optional[float]:
        """モデルの予想応答時間(秒)を返す。データ不足時は None"""
        times = self._times.get(model_name)
        # 最低3サンプル無いと予想を出さない
        if not times or len(times) < 3:
            return None
        # ローリング平均を算出
        return sum(times) / len(times)

    def format_estimate(self, model_name: str) -> str:
        """予想時間を人間向け文字列にフォーマットする"""
        estimate = self.get_estimate(model_name)
        if estimate is None:
            # データ不足時は「計測中」と表示
            return "⏱️ 予想応答時間: *計測中...* / Estimated time: *Measuring...*"
        if estimate < 60:
            # 60秒未満は秒表示
            return f"⏱️ 予想応答時間: ~**{estimate:.0f}秒** / Estimated: ~**{estimate:.0f}s**"
        # 60秒以上は分+秒表示
        minutes = int(estimate // 60)
        seconds = int(estimate % 60)
        return (
            f"⏱️ 予想応答時間: ~**{minutes}分{seconds}秒** "
            f"/ Estimated: ~**{minutes}m{seconds}s**"
        )


class TipsManager:
    """LLM待機中に表示するランダムなtipsを管理するクラス"""

    def __init__(self):
        self.tips = self._create_tips_list()
        # 応答時間トラッカーを内蔵
        self.response_tracker = ResponseTimeTracker()

    def _create_tips_list(self) -> List[Dict[str, Any]]:
        """tipsのリストを作成する"""
        return [
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**画像を送信できます！**\n画像URLを貼り付けるか、画像ファイルを添付してAIに説明を求めることができます。\n\n**You can send images!**\nPaste image URLs or attach image files to ask the AI for descriptions.",
                "color": discord.Color.blue()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**会話を続けるには返信機能を！**\nBotのメッセージに返信することで、メンションなしで会話を続けられます。\n\n**Use reply to continue conversations!**\nReply to bot messages to continue chatting without mentioning.",
                "color": discord.Color.green()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**モデルを切り替えられます！**\n`/switch-models`コマンドでこのチャンネルのAIモデルを変更できます。\n\n**You can switch models!**\nUse `/switch-models` command to change the AI model for this channel.",
                "color": discord.Color.orange()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**画像生成も可能！**\nAIに画像生成を依頼すると、StableDiffusionが画像生成AIが画像を作成します。\n\n**Image generation available!**\nAsk the AI to generate images and it will use StableDiffusion image generation AI.",
                "color": discord.Color.gold()
            },
            {
                "title": "💡 AI Tips / AIのヒント",
                "description": "**検索機能を利用！**\nAIに最新情報を調べてもらうことができます。リアルタイムの情報取得が可能です。\n\n**Use search functionality!**\nAsk the AI to search for the latest information. Real-time information retrieval is available.",
                "color": discord.Color.red()
            }
        ]

    def get_random_tip(self) -> discord.Embed:
        """ランダムなtipのembedを取得する"""
        tip_data = random.choice(self.tips)
        embed = discord.Embed(
            title=tip_data["title"],
            description=tip_data["description"],
            color=tip_data["color"]
        )
        embed.set_footer(text="we are experiencing technical difficulties with our main server. \n full documentation : https://coffin299.net")
        return embed

    # 応答時間がこの秒数以上ならモデル切替の提案を表示する閾値
    SLOW_MODEL_THRESHOLD = 30

    def get_waiting_embed(self, model_name: str) -> discord.Embed:
        """待機中の embed（後方互換。新規は get_waiting_layout を使う）。"""
        tip_embed = self.get_random_tip()
        # タイトル: モデル名の応答待ち表示
        tip_embed.title = f"⏳ Waiting for '{model_name}' response..."
        # 予想応答時間をdescriptionの先頭に挿入
        time_estimate = self.response_tracker.format_estimate(model_name)
        # 予想時間が閾値を超える場合、モデル切替の提案を追加
        estimate = self.response_tracker.get_estimate(model_name)
        switch_hint = ""
        if estimate is not None and estimate >= self.SLOW_MODEL_THRESHOLD:
            switch_hint = (
                "\n💡 応答が遅い場合は `/switch-models` で他のモデルへの切り替えもご検討ください。"
                "\n💡 If response is slow, consider switching to another model with `/switch-models`."
            )
        original_desc = tip_embed.description or ""
        # 「予想時間 → 切替提案 → 空行 → tips本文」の構成
        tip_embed.description = f"{time_estimate}{switch_hint}\n\n{original_desc}"
        return tip_embed

    def get_waiting_layout_parts(self, model_name: str) -> tuple[str, discord.Color]:
        """待機 LayoutView 用の本文とアクセント色を返す。"""
        # ランダム tip を1つ選ぶ
        tip_data = random.choice(self.tips)
        # 予想時間文字列
        time_estimate = self.response_tracker.format_estimate(model_name)
        # 遅いモデルなら切替提案
        estimate = self.response_tracker.get_estimate(model_name)
        switch_hint = ""
        if estimate is not None and estimate >= self.SLOW_MODEL_THRESHOLD:
            switch_hint = (
                "\n💡 応答が遅い場合は `/switch-models` で他のモデルへの切り替えもご検討ください。"
                "\n💡 If response is slow, consider switching to another model with `/switch-models`."
            )
        # V2 TextDisplay 用本文（タイトル相当を先頭に）
        body = (
            f"### ⏳ Waiting for '{model_name}' response...\n"
            f"{time_estimate}{switch_hint}\n\n"
            f"**{tip_data['title']}**\n"
            f"{tip_data['description']}\n\n"
            f"-# we are experiencing technical difficulties with our main server.\n"
            f"-# full documentation : https://coffin299.net"
        )
        # tip の色をアクセントに使う
        accent = tip_data.get("color") or discord.Color.orange()
        return body, accent
