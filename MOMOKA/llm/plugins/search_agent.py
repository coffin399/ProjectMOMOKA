# MOMOKA/llm/plugins/search_agent.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Optional, List, Any

# aiohttp は既にプロジェクトの依存関係に含まれている
import aiohttp

# カスタム例外をインポート
from MOMOKA.llm.error.errors import (
    SearchAPIRateLimitError,
    SearchAPIServerError,
    SearchAPIError,
    SearchExecutionError,
    SearchAgentError
)

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)

# Mistral Chat Completions APIエンドポイント
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"


class SearchAgent:
    name = "search"
    # LLMが呼び出すツール定義（OpenAI互換Function Calling形式）
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Run a Mistral web search and return a report.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }

    def __init__(self, bot: "commands.Bot", config: dict = None) -> None:
        self.bot = bot
        # 設定セクションの取得（configはllmセクション全体）
        gcfg = None
        if config:
            logger.info(f"SearchAgent init with config. Keys: {list(config.keys())}")
            # configはllmセクション全体 → "agent"キーで検索設定を取得
            gcfg = config.get("agent")
            if gcfg:
                logger.info("SearchAgent config found in provided config.")
            else:
                logger.error("SearchAgent config NOT found in provided config.")
        elif hasattr(self.bot, 'cfg') and self.bot.cfg:
            logger.info("SearchAgent init with bot.cfg.")
            # bot.cfgからはllm.agentのパスで取得
            gcfg = self.bot.cfg.get("llm", {}).get("agent")

        # 設定が見つからない場合は検索を無効化
        if not gcfg:
            logger.error("SearchAgent config is missing (gcfg is None). Search will be disabled.")
            self.api_keys: List[str] = []
            self.current_key_index = 0
            return

        # 複数のAPIキーを収集（キーローテーション対応）
        self.api_keys = []
        for key in sorted(gcfg.keys()):
            # api_key1, api_key2, ... の形式でAPIキーを取得
            if key.startswith("api_key"):
                api_key = gcfg[key]
                # 有効なキーのみ追加（プレースホルダーを除外）
                if api_key and api_key.strip() and api_key.strip() != "YOUR_MISTRAL_API_KEY_HERE":
                    self.api_keys.append(api_key.strip())

        # 有効なAPIキーがない場合は検索を無効化
        if not self.api_keys:
            logger.error("No valid API keys found in search_agent config. Search will be disabled.")
            self.current_key_index = 0
            return

        # キーローテーション用のインデックス
        self.current_key_index = 0
        # 検索に使用するMistralモデル名（web_searchツール対応モデル）
        self.model_name = gcfg.get("model", "mistral-medium-latest")
        # APIタイムアウト（秒）
        self.timeout = gcfg.get("timeout", 60.0)
        # レスポンスのフォーマット制御用プロンプト
        self.format_control = gcfg.get("format_control", "")
        logger.info(
            f"SearchAgent initialized with {len(self.api_keys)} Mistral API key(s) "
            f"(model: {self.model_name})."
        )

    def _rotate_key(self) -> str:
        """次のAPIキーにローテーションし、そのキーを返す"""
        if not self.api_keys:
            raise SearchExecutionError("No API keys available.")
        # ラウンドロビンで次のキーに切り替え
        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        logger.info(f"Rotating to Mistral API key {self.current_key_index + 1}/{len(self.api_keys)}")
        return self.api_keys[self.current_key_index]

    def _extract_text_from_content(self, content: Any) -> str:
        """Mistralレスポンスのcontentからテキストを抽出する。

        contentは文字列の場合もあれば、ContentChunkオブジェクト(dict)のリストの場合もある。
        """
        # contentが文字列の場合はそのまま返す
        if isinstance(content, str):
            return content

        # contentがリスト（ContentChunkのリスト）の場合
        if isinstance(content, list):
            text_parts = []
            for chunk in content:
                # dict形式のチャンクからtext typeを抽出
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    text_parts.append(chunk.get("text", ""))
            return "".join(text_parts)

        # その他の場合は文字列変換
        return str(content) if content else ""

    async def _mistral_search(self, query: str) -> str:
        """Mistral Web Search APIを直接呼び出して検索を実行し、テキスト結果を返す。

        mistralai SDKを使わず、aiohttp で REST API を直接叩くため
        SDK バージョン互換性の問題を回避できる。
        """
        if not self.api_keys:
            raise SearchExecutionError("SearchAgent is not properly initialized.")

        # リトライ設定
        retries = 2
        delay = 1.5
        keys_tried = 0
        max_keys_to_try = len(self.api_keys)

        while keys_tried < max_keys_to_try:
            # 現在のキーを取得
            current_key = self.api_keys[self.current_key_index]

            for attempt in range(retries + 1):
                try:
                    # 検索用プロンプトの構築
                    prompt = f"**[DeepResearch Request]:** {query}\n{self.format_control}"

                    # Mistral Chat Completions APIリクエストボディ
                    payload = {
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        # web_search ツールを有効化（Mistralビルトインコネクタ）
                        "tools": [{"type": "web_search"}],
                    }

                    # リクエストヘッダー（Bearer認証）
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {current_key}",
                    }

                    # aiohttp で POST リクエストを送信
                    timeout = aiohttp.ClientTimeout(total=self.timeout)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            MISTRAL_API_URL, json=payload, headers=headers
                        ) as resp:
                            status = resp.status

                            # 成功レスポンス (200)
                            if status == 200:
                                data = await resp.json()
                                # choices[0].message.content からテキストを抽出
                                choices = data.get("choices", [])
                                if choices:
                                    content = choices[0].get("message", {}).get("content", "")
                                    return self._extract_text_from_content(content)
                                else:
                                    raise SearchExecutionError(
                                        "Mistral returned an empty response (no choices)."
                                    )

                            # Rate Limit (429)
                            elif status == 429:
                                raise SearchAPIRateLimitError(
                                    "Mistral Search API rate limit was reached."
                                )

                            # 503 Service Unavailable → 次のキーに切り替え
                            elif status == 503:
                                logger.warning(
                                    f"503 error on Mistral API key "
                                    f"{self.current_key_index + 1}/{len(self.api_keys)}. "
                                    f"Rotating to next key."
                                )
                                keys_tried += 1
                                if keys_tried < max_keys_to_try:
                                    self._rotate_key()
                                    break  # 内側のループを抜けて次のキーで再試行
                                else:
                                    raise SearchAPIServerError(
                                        "All Mistral API keys returned 503 errors."
                                    )

                            # その他の5xx サーバーエラー → リトライ
                            elif 500 <= status < 600:
                                error_body = await resp.text()
                                logger.warning(
                                    f"SearchAgent server error {status} "
                                    f"(attempt {attempt + 1}/{retries + 1}): {error_body[:200]}"
                                )
                                if attempt < retries:
                                    await asyncio.sleep(delay * (attempt + 1))
                                    continue
                                raise SearchAPIServerError(
                                    f"Mistral Search API server error ({status}) after retries."
                                )

                            # その他のHTTPエラー (4xx等)
                            else:
                                error_body = await resp.text()
                                logger.error(
                                    f"Search Agent unexpected HTTP {status}: {error_body[:300]}"
                                )
                                raise SearchAPIError(
                                    f"Mistral API returned HTTP {status}: {error_body[:200]}"
                                )

                except SearchAgentError:
                    # SearchAgentErrorのサブクラスはそのまま再raise
                    raise
                except asyncio.TimeoutError:
                    # タイムアウト → リトライ
                    logger.warning(
                        f"SearchAgent timeout (attempt {attempt + 1}/{retries + 1})"
                    )
                    if attempt < retries:
                        await asyncio.sleep(delay * (attempt + 1))
                        continue
                    raise SearchExecutionError(
                        f"Mistral Search API timed out after {self.timeout}s."
                    )
                except Exception as e:
                    # その他の予期しないエラー
                    logger.error(f"Search Agent unexpected error: {e}", exc_info=True)
                    raise SearchExecutionError(
                        f"An unexpected error occurred during search: {str(e)}",
                        original_exception=e
                    )

        # すべてのキーで失敗した場合
        raise SearchExecutionError("Search failed on all available Mistral API keys.")

    async def run(self, *, arguments: dict, bot: "commands.Bot", channel_id: int) -> str:
        """検索を実行するメインメソッド。テキスト結果を返す。"""
        query = arguments.get("query", "")
        if not query:
            raise SearchExecutionError("Query cannot be empty.")

        # _mistral_searchは例外を発生させる可能性があるため、呼び出し側(llm_cog.py)で処理する
        return await self._mistral_search(query)