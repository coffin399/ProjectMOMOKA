# MOMOKA/llm/plugins/search_agent.py
from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from typing import TYPE_CHECKING, List, Dict, Optional, Tuple
from urllib.parse import unquote

# aiohttp は既にプロジェクトの依存関係に含まれている
import aiohttp

# カスタム例外をインポート
from MOMOKA.llm.error.errors import (
    SearchExecutionError,
    SearchAgentError,
)

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)

# DuckDuckGo HTML検索エンドポイント（APIキー不要・no-JS）
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
# DuckDuckGo Lite検索エンドポイント（HTMLが弾かれた場合のフォールバック）
DUCKDUCKGO_LITE_URL = "https://lite.duckduckgo.com/lite/"
# Instant Answer API（最終フォールバック・結果は限定的）
DUCKDUCKGO_INSTANT_URL = "https://api.duckduckgo.com/"

# DDGのボット検知チャレンジを示すレスポンス指紋
_DDG_CHALLENGE_MARKERS = ("anomaly.js", "anomaly-modal", "challenge-form", "bots use DuckDuckGo")

# Chrome完全版UAはJS未実行だとHTTP 202チャレンジを誘発しやすいため、
# no-JSクライアントとして素直なUAを使う
_DDG_USER_AGENT = (
    "Mozilla/5.0 (compatible; MOMOKABot/1.0; +https://github.com/) "
    "AppleWebKit/537.36 (KHTML, like Gecko)"
)


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

    @staticmethod
    def _build_headers(referer: str) -> Dict[str, str]:
        """DDGのボット検知を避けやすいリクエストヘッダーを構築する。"""
        # no-JSフォーム送信を模したSec-Fetch系ヘッダーを含める
        return {
            "User-Agent": _DDG_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": referer.rstrip("/"),
            "Referer": referer,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }

    @staticmethod
    def _is_challenge_response(status: int, html: str) -> bool:
        """HTTP 202またはchallenge HTMLならボット検知と判定する。"""
        # 202 Accepted はDDGのソフトブロック（CAPTCHA相当）
        if status == 202:
            # チャレンジ確定
            return True
        # 200でもanomaly.js等が含まれていればチャレンジページ
        lowered = html.lower()
        # 指紋マーカーのいずれかに一致するか確認する
        return any(marker.lower() in lowered for marker in _DDG_CHALLENGE_MARKERS)

    def _parse_html_results(self, html: str) -> List[Dict[str, str]]:
        """html.duckduckgo.com の結果HTMLをパースする。"""
        # 結果リストを初期化する
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
            # マッチしたURL・タイトル・スニペットを取り出す
            raw_url, raw_title, raw_snippet = match.groups()
            # HTMLタグを除去してプレーンテキスト化
            title = self._strip_html_tags(raw_title)
            # スニペットも同様に正規化する
            snippet = self._strip_html_tags(raw_snippet)
            # DDGリダイレクトURLから実際のURLを取得
            url = self._extract_real_url(raw_url)
            # 有効な結果のみ追加
            if title and url:
                # 結果エントリを追加する
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                })
            # 最大件数に達したら終了
            if len(results) >= self.max_results:
                # ループを抜ける
                break
        # パース結果を返す
        return results

    def _parse_lite_results(self, html: str) -> List[Dict[str, str]]:
        """lite.duckduckgo.com の結果HTMLをパースする。"""
        # 結果リストを初期化する
        results: List[Dict[str, str]] = []
        # Lite版は result-link / result-snippet クラスを使う
        result_pattern = re.compile(
            r'class="result-link"[^>]*href="([^"]+)"[^>]*>(.+?)</a>'
            r'.*?'
            r'class="result-snippet"[^>]*>(.+?)</(?:td|a|span)>',
            re.DOTALL | re.IGNORECASE,
        )

        for match in result_pattern.finditer(html):
            # マッチしたURL・タイトル・スニペットを取り出す
            raw_url, raw_title, raw_snippet = match.groups()
            # タイトルを正規化する
            title = self._strip_html_tags(raw_title)
            # スニペットを正規化する
            snippet = self._strip_html_tags(raw_snippet)
            # リダイレクトを解決する
            url = self._extract_real_url(raw_url)
            # 有効な結果のみ追加
            if title and url:
                # 結果エントリを追加する
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                })
            # 最大件数に達したら終了
            if len(results) >= self.max_results:
                # ループを抜ける
                break

        # 専用クラスが無い場合はリンク表の簡易パースへフォールバック
        if not results:
            # Liteの簡易リンク行を拾う
            link_pattern = re.compile(
                r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]*>(.+?)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            for match in link_pattern.finditer(html):
                # URLとタイトルを取り出す
                raw_url, raw_title = match.groups()
                # タイトルを正規化する
                title = self._strip_html_tags(raw_title)
                # リダイレクトを解決する
                url = self._extract_real_url(raw_url)
                # duckduckgo自身や空タイトルはスキップ
                if not title or not url or "duckduckgo.com" in url:
                    # 次の候補へ
                    continue
                # 結果エントリを追加する
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": "",
                })
                # 最大件数に達したら終了
                if len(results) >= self.max_results:
                    # ループを抜ける
                    break
        # パース結果を返す
        return results

    async def _post_search_html(
        self,
        session: aiohttp.ClientSession,
        url: str,
        query: str,
        referer: str,
    ) -> Tuple[int, str]:
        """DDGのHTML/LiteエンドポイントへPOSTし、(status, body)を返す。"""
        # エンドポイント向けヘッダーを構築する
        headers = self._build_headers(referer)
        # 初回ページ用フォームデータ（b="" はSearXNGと同様）
        form_data = {"q": query, "b": "", "kl": "wt-wt"}
        # POSTで検索を実行する
        async with session.post(url, data=form_data, headers=headers) as resp:
            # ステータスと本文を返す
            return resp.status, await resp.text()

    async def _search_instant_answer(
        self,
        session: aiohttp.ClientSession,
        query: str,
    ) -> List[Dict[str, str]]:
        """Instant Answer APIを最終フォールバックとして使う。"""
        # Instant Answer用クエリパラメータ
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        # JSON API向けの軽いヘッダー
        headers = {
            "User-Agent": _DDG_USER_AGENT,
            "Accept": "application/json",
        }
        # GETでInstant Answerを取得する
        async with session.get(
            DUCKDUCKGO_INSTANT_URL, params=params, headers=headers
        ) as resp:
            # 失敗時は空結果
            if resp.status != 200:
                # 空リストを返す
                return []
            # JSONをパースする
            data = await resp.json(content_type=None)

        # 結果リストを初期化する
        results: List[Dict[str, str]] = []
        # Abstractがあれば先頭結果にする
        abstract = (data.get("AbstractText") or "").strip()
        # AbstractURLがあればURLにする
        abstract_url = (data.get("AbstractURL") or "").strip()
        # Headingがあればタイトルにする
        heading = (data.get("Heading") or query).strip()
        # Abstractがある場合のみ追加
        if abstract and abstract_url:
            # Abstractエントリを追加する
            results.append({
                "title": heading,
                "url": abstract_url,
                "snippet": abstract,
            })

        # RelatedTopicsから追加結果を拾う
        for topic in data.get("RelatedTopics") or []:
            # ネストTopicsは再帰的にフラット化する
            candidates = topic.get("Topics") if isinstance(topic, dict) and "Topics" in topic else [topic]
            # 候補を走査する
            for item in candidates or []:
                # dict以外はスキップ
                if not isinstance(item, dict):
                    # 次へ
                    continue
                # テキストとURLを取り出す
                text = (item.get("Text") or "").strip()
                # FirstURLを取り出す
                url = (item.get("FirstURL") or "").strip()
                # 不完全なエントリはスキップ
                if not text or not url:
                    # 次へ
                    continue
                # 結果エントリを追加する
                results.append({
                    "title": text[:80],
                    "url": url,
                    "snippet": text,
                })
                # 最大件数に達したら終了
                if len(results) >= self.max_results:
                    # 外側も含めて終了
                    return results
        # 収集結果を返す
        return results

    async def _search_duckduckgo(self, query: str) -> List[Dict[str, str]]:
        """DuckDuckGo検索を実行し、結果リストを返す。

        1) html.duckduckgo.com
        2) lite.duckduckgo.com（202/チャレンジ時）
        3) Instant Answer API（最終フォールバック）
        """
        # タイムアウト設定
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        # 最後に観測したエラー内容
        last_error: Optional[str] = None

        async with aiohttp.ClientSession(timeout=timeout) as session:
            # HTMLエンドポイントを最大2回試す（短いバックオフ付き）
            for attempt in range(2):
                try:
                    # html.duckduckgo.com へPOSTする
                    status, html = await self._post_search_html(
                        session,
                        DUCKDUCKGO_HTML_URL,
                        query,
                        "https://html.duckduckgo.com/",
                    )
                except Exception as e:
                    # ネットワーク例外を記録して次の手段へ
                    last_error = f"HTML endpoint error: {e}"
                    # リトライ前に少し待つ
                    await asyncio.sleep(0.5 * (attempt + 1))
                    # 次の試行へ
                    continue

                # チャレンジ応答ならログしてフォールバックへ
                if self._is_challenge_response(status, html):
                    # チャレンジを記録する
                    last_error = f"DuckDuckGo HTML challenge (HTTP {status})"
                    # 警告ログを出す
                    logger.warning(
                        "DuckDuckGo HTML endpoint returned challenge "
                        f"(status={status}, attempt={attempt + 1}); trying fallbacks."
                    )
                    # バックオフしてから次手段
                    await asyncio.sleep(0.5 * (attempt + 1))
                    # HTMLリトライまたはフォールバックへ
                    if attempt == 0:
                        # 1回目は再試行
                        continue
                    # 2回目以降はフォールバックへ進む
                    break

                # 200以外はエラーとして記録
                if status != 200:
                    # ステータス異常を記録する
                    last_error = f"DuckDuckGo HTML returned HTTP {status}"
                    # 次の試行へ
                    continue

                # HTML結果をパースする
                results = self._parse_html_results(html)
                # 結果があれば返す
                if results:
                    # 成功ログ
                    logger.info(
                        f"DuckDuckGo HTML search for '{query}' "
                        f"returned {len(results)} results."
                    )
                    # 結果を返す
                    return results
                # パース空はチャレンジやレイアウト変更の可能性
                last_error = "DuckDuckGo HTML returned no parseable results"
                # 次の試行へ
                break

            # Liteエンドポイントへフォールバック
            try:
                # lite.duckduckgo.com へPOSTする
                status, html = await self._post_search_html(
                    session,
                    DUCKDUCKGO_LITE_URL,
                    query,
                    "https://lite.duckduckgo.com/",
                )
                # チャレンジでなければパースを試みる
                if not self._is_challenge_response(status, html) and status == 200:
                    # Lite結果をパースする
                    results = self._parse_lite_results(html)
                    # 結果があれば返す
                    if results:
                        # 成功ログ
                        logger.info(
                            f"DuckDuckGo Lite search for '{query}' "
                            f"returned {len(results)} results."
                        )
                        # 結果を返す
                        return results
                    # パース空を記録する
                    last_error = "DuckDuckGo Lite returned no parseable results"
                else:
                    # Liteもチャレンジ/異常なら記録する
                    last_error = f"DuckDuckGo Lite challenge/error (HTTP {status})"
                    # 警告ログ
                    logger.warning(last_error)
            except Exception as e:
                # Lite失敗を記録する
                last_error = f"Lite endpoint error: {e}"
                # 警告ログ
                logger.warning(last_error)

            # Instant Answer APIを最終フォールバックとして使う
            try:
                # Instant Answerを取得する
                results = await self._search_instant_answer(session, query)
                # 結果があれば返す
                if results:
                    # 成功ログ（限定結果である旨）
                    logger.info(
                        f"DuckDuckGo Instant Answer fallback for '{query}' "
                        f"returned {len(results)} results."
                    )
                    # 結果を返す
                    return results
            except Exception as e:
                # Instant Answer失敗を記録する
                last_error = f"Instant Answer error: {e}"
                # 警告ログ
                logger.warning(last_error)

        # すべての経路が失敗した場合は実行エラーにする
        raise SearchExecutionError(
            last_error or "DuckDuckGo search failed with no results"
        )

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
