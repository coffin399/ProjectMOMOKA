# MOMOKA/llm/plugins/search_agent.py
from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from typing import TYPE_CHECKING, List, Dict, Any
from urllib.parse import unquote

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

# DuckDuckGo HTML検索エンドポイント（APIキー不要）
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"


class SearchAgent:
    name = "search"
    # LLMが呼び出すツール定義（OpenAI互換Function Calling形式）
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Run a DuckDuckGo web search and return results.",
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
        elif hasattr(self.bot, 'cfg') and self.bot.cfg:
            logger.info("SearchAgent init with bot.cfg.")
            gcfg = self.bot.cfg.get("llm", {}).get("agent")

        # デフォルト設定（DuckDuckGoはAPIキー不要）
        self.max_results = 10
        self.timeout = 30.0

        # configがあれば設定を上書き
        if gcfg:
            self.max_results = gcfg.get("max_results", 10)
            self.timeout = gcfg.get("timeout", 30.0)

        logger.info(
            f"SearchAgent initialized (DuckDuckGo, max_results={self.max_results})."
        )

    @staticmethod
    def _strip_html_tags(text: str) -> str:
        """HTMLタグを除去してプレーンテキストに変換する"""
        # HTMLタグを正規表現で除去
        cleaned = re.sub(r"<[^>]+>", "", text)
        # HTMLエンティティをデコード
        cleaned = unescape(cleaned)
        # 連続する空白を正規化
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @staticmethod
    def _extract_real_url(ddg_redirect_url: str) -> str:
        """DuckDuckGoのリダイレクトURLから実際のURLを抽出する。

        DDGの検索結果リンクは //duckduckgo.com/l/?uddg=ENCODED_URL&... 形式のため、
        実際のURLをデコードして返す。
        """
        # uddgパラメータから実際のURLを抽出
        match = re.search(r"uddg=([^&]+)", ddg_redirect_url)
        if match:
            return unquote(match.group(1))
        # リダイレクトURLでない場合はそのまま返す
        return ddg_redirect_url

    async def _search_duckduckgo(self, query: str) -> List[Dict[str, str]]:
        """DuckDuckGo HTML検索を実行し、結果リストを返す。

        DDGのHTML検索ページをPOSTでリクエストし、レスポンスHTMLから
        タイトル・URL・スニペットを正規表現で抽出する。
        APIキー不要・追加パッケージ不要。
        """
        # ブラウザを模したリクエストヘッダー
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        # 検索クエリをフォームデータとしてPOST
        form_data = {"q": query}

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                DUCKDUCKGO_URL, data=form_data, headers=headers
            ) as resp:
                if resp.status != 200:
                    raise SearchExecutionError(
                        f"DuckDuckGo returned HTTP {resp.status}"
                    )
                html = await resp.text()

        # 検索結果をHTMLから正規表現で抽出
        results: List[Dict[str, str]] = []

        # DuckDuckGo HTMLの結果構造:
        # <a rel="nofollow" class="result__a" href="REDIRECT_URL">TITLE</a>
        # <a class="result__snippet" href="...">SNIPPET</a>
        result_pattern = re.compile(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.+?)</a>'
            r'.*?'
            r'class="result__snippet"[^>]*>(.+?)</a>',
            re.DOTALL,
        )

        for match in result_pattern.finditer(html):
            raw_url, raw_title, raw_snippet = match.groups()

            # HTMLタグを除去してプレーンテキスト化
            title = self._strip_html_tags(raw_title)
            snippet = self._strip_html_tags(raw_snippet)
            # DDGリダイレクトURLから実際のURLを取得
            url = self._extract_real_url(raw_url)

            # 有効な結果のみ追加
            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                })

            # 最大件数に達したら終了
            if len(results) >= self.max_results:
                break

        logger.info(f"DuckDuckGo search for '{query}' returned {len(results)} results.")
        return results

    @staticmethod
    def _format_results_as_text(query: str, results: List[Dict[str, str]]) -> str:
        """検索結果をLLMが理解しやすいテキスト形式にフォーマットする"""
        if not results:
            return f"Web search for '{query}' returned no results."

        # ヘッダー
        lines = [f"## Web Search Results for: {query}", ""]

        # 各結果をフォーマット
        for i, result in enumerate(results, 1):
            title = result.get("title", "No Title")
            url = result.get("url", "")
            snippet = result.get("snippet", "")

            lines.append(f"### {i}. {title}")
            lines.append(f"**URL:** {url}")
            if snippet:
                lines.append(f"**Summary:** {snippet}")
            lines.append("")  # 空行で区切り

        # LLMへの指示
        lines.append("---")
        lines.append(
            "Use the above search results to provide a comprehensive, "
            "accurate answer to the user's question. "
            "Cite sources where appropriate."
        )

        return "\n".join(lines)

    async def run(self, *, arguments: dict, bot: "commands.Bot", channel_id: int) -> str:
        """検索を実行するメインメソッド。フォーマット済みテキスト結果を返す。"""
        query = arguments.get("query", "")
        if not query:
            raise SearchExecutionError("Query cannot be empty.")

        try:
            # DuckDuckGoで検索を実行
            results = await self._search_duckduckgo(query)
            # 結果をLLM向けテキストにフォーマットして返す
            return self._format_results_as_text(query, results)

        except SearchAgentError:
            # SearchAgentError系はそのまま再raise
            raise
        except asyncio.TimeoutError:
            raise SearchExecutionError(
                f"DuckDuckGo search timed out after {self.timeout}s."
            )
        except Exception as e:
            logger.error(f"Search Agent unexpected error: {e}", exc_info=True)
            raise SearchExecutionError(
                f"An unexpected error occurred during search: {str(e)}",
                original_exception=e,
            )