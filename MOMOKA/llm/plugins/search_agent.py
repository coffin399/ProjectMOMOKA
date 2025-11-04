# MOMOKA/llm/plugins/search_agent.py
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from google import genai
from google.genai import errors, types

# カスタム例外をインポート
from PLANA.llm.error.errors import (
    SearchAPIRateLimitError,
    SearchAPIServerError,
    SearchAPIError,
    SearchExecutionError,
    SearchAgentError
)

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)


class SearchAgent:
    name = "search"
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Run a Google web search and return a report.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        gcfg = self.bot.cfg.get("search_agent")
        if not gcfg:
            logger.error("SearchAgent config is missing. Search will be disabled.")
            self.clients = []
            self.current_key_index = 0
            return

        # 複数のAPIキーを収集
        self.api_keys = []
        for key in sorted(gcfg.keys()):
            if key.startswith("api_key"):
                api_key = gcfg[key]
                if api_key:
                    self.api_keys.append(api_key)

        if not self.api_keys:
            logger.error("No valid API keys found in search_agent config. Search will be disabled.")
            self.clients = []
            self.current_key_index = 0
            return

        # 各APIキーに対してクライアントを初期化
        self.clients = []
        for i, api_key in enumerate(self.api_keys):
            try:
                client = genai.Client(api_key=api_key)
                self.clients.append(client)
                logger.info(f"SearchAgent: API key {i + 1}/{len(self.api_keys)} initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize client for API key {i + 1}: {e}", exc_info=True)

        if not self.clients:
            logger.error("Failed to initialize any Google Gen AI clients. Search will be disabled.")
            self.current_key_index = 0
            return

        self.current_key_index = 0
        self.model_name = gcfg.get("model", "gemini-2.5-flash")
        self.format_control = gcfg.get("format_control", "")
        logger.info(f"SearchAgent initialized with {len(self.clients)} API key(s) (model: {self.model_name}).")

    def _get_next_client(self) -> genai.Client | None:
        """次のクライアントを取得(ローテーション)"""
        if not self.clients:
            return None

        self.current_key_index = (self.current_key_index + 1) % len(self.clients)
        logger.info(f"Rotating to API key {self.current_key_index + 1}/{len(self.clients)}")
        return self.clients[self.current_key_index]

    async def _google_search(self, query: str):
        """Google Searchを使用して検索を実行"""
        if not self.clients:
            raise SearchExecutionError("SearchAgent is not properly initialized.")

        retries = 2
        delay = 1.5
        keys_tried = 0
        max_keys_to_try = len(self.clients)

        while keys_tried < max_keys_to_try:
            current_client = self.clients[self.current_key_index]

            for attempt in range(retries + 1):
                try:
                    # Google Search groundingツールを使用
                    prompt = f"**[DeepResearch Request]:** {query}\n{self.format_control}"

                    # 非同期実行
                    response = await asyncio.to_thread(
                        current_client.models.generate_content,
                        model=self.model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())]
                        )
                    )

                    # レスポンス全体を返す
                    return response

                except errors.APIError as e:
                    # 新しいSDKのエラーハンドリング
                    if e.code == 429:
                        # Rate Limit Error
                        raise SearchAPIRateLimitError(
                            "Google Search API rate limit was reached.",
                            original_exception=e
                        )
                    elif e.code == 503:
                        # 503 Service Unavailable - 次のキーに切り替え
                        logger.warning(
                            f"503 error on API key {self.current_key_index + 1}/{len(self.clients)}. Rotating to next key.")
                        keys_tried += 1
                        if keys_tried < max_keys_to_try:
                            self._get_next_client()
                            break  # 内側のループを抜けて次のキーで試行
                        else:
                            raise SearchAPIServerError(
                                "All API keys returned 503 errors.",
                                original_exception=e
                            )
                    elif 500 <= e.code < 600:
                        # その他の5xx Server-side Errors
                        logger.warning(f"SearchAgent server error (attempt {attempt + 1}/{retries + 1}): {e}")
                        if attempt < retries:
                            await asyncio.sleep(delay * (attempt + 1))
                            continue
                        raise SearchAPIServerError(
                            "Google Search API server-side error after retries.",
                            original_exception=e
                        )
                    else:
                        # その他のAPIエラー
                        logger.error(f"Search Agent unexpected API error: {e}")
                        raise SearchAPIError(
                            f"An unexpected API error occurred: {str(e)}",
                            original_exception=e
                        )

                except Exception as e:
                    # API以外の予期しない実行時エラー
                    logger.error(f"Search Agent unexpected execution error: {e}", exc_info=True)
                    raise SearchExecutionError(
                        f"An unexpected error occurred during search: {str(e)}",
                        original_exception=e
                    )

        # すべてのキーで失敗した場合
        raise SearchExecutionError("Search failed on all available API keys.")

    async def run(self, *, arguments: dict, bot, channel_id: int):
        """検索を実行するメインメソッド"""
        query = arguments.get("query", "")
        if not query:
            raise SearchExecutionError("Query cannot be empty.")

        # _google_searchは例外を発生させる可能性があるため、呼び出し側(llm_cog.py)で処理する
        return await self._google_search(query)