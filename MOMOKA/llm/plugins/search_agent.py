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

# Mistral API ベースURL
MISTRAL_API_BASE = "https://api.mistral.ai/v1"
# エージェント作成エンドポイント
MISTRAL_AGENTS_URL = f"{MISTRAL_API_BASE}/agents"
# エージェント完了エンドポイント
MISTRAL_AGENTS_COMPLETIONS_URL = f"{MISTRAL_API_BASE}/agents/completions"


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
        # 検索に使用するMistralモデル名
        self.model_name = gcfg.get("model", "mistral-medium-latest")
        # APIタイムアウト（秒）
        self.timeout = gcfg.get("timeout", 60.0)
        # レスポンスのフォーマット制御用プロンプト（エージェントのinstructionsに使用）
        self.format_control = gcfg.get("format_control", "")

        # 各APIキーに対応するエージェントID（遅延作成・キャッシュ用）
        # キーインデックス → agent_id のマッピング
        self._agent_ids: dict[int, str] = {}

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

        contentは文字列の場合もあれば、ContentChunk(dict)のリストの場合もある。
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

    async def _create_agent(self, session: aiohttp.ClientSession, api_key: str) -> str:
        """Mistral Agents APIでweb_searchツール付きエージェントを作成し、agent_idを返す。

        作成したエージェントは永続的なので、一度作成すればAPIキーごとに再利用可能。
        """
        # エージェント作成リクエストボディ
        payload = {
            "model": self.model_name,
            "name": "MOMOKA_SearchAgent",
            "description": "Web search agent for MOMOKA Discord bot.",
            "instructions": self.format_control or "検索結果に基づき、ユーザーの質問に直接回答する形で、構造化された詳細なレポートを作成してください。",
            # web_search ビルトインコネクタを有効化
            "tools": [{"type": "web_search"}],
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        async with session.post(MISTRAL_AGENTS_URL, json=payload, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                agent_id = data.get("id")
                if agent_id:
                    logger.info(f"SearchAgent: Created Mistral agent with ID: {agent_id}")
                    return agent_id
                else:
                    raise SearchExecutionError("Mistral agent creation succeeded but no ID returned.")
            else:
                error_body = await resp.text()
                raise SearchExecutionError(
                    f"Failed to create Mistral agent (HTTP {resp.status}): {error_body[:300]}"
                )

    async def _get_or_create_agent_id(
        self, session: aiohttp.ClientSession, key_index: int
    ) -> str:
        """指定キーインデックスのエージェントIDを取得（未作成なら作成）"""
        # キャッシュにあればそのまま返す
        if key_index in self._agent_ids:
            return self._agent_ids[key_index]

        # 新規作成してキャッシュ
        api_key = self.api_keys[key_index]
        agent_id = await self._create_agent(session, api_key)
        self._agent_ids[key_index] = agent_id
        return agent_id

    async def _mistral_search(self, query: str) -> str:
        """Mistral Agents APIを使用して検索を実行し、テキスト結果を返す。

        1. エージェント作成（初回のみ、以降はキャッシュ済みIDを使用）
        2. /v1/agents/completions でweb_search付きの応答を取得
        """
        if not self.api_keys:
            raise SearchExecutionError("SearchAgent is not properly initialized.")

        # リトライ設定
        retries = 2
        delay = 1.5
        keys_tried = 0
        max_keys_to_try = len(self.api_keys)

        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            while keys_tried < max_keys_to_try:
                # 現在のキーとインデックスを取得
                key_index = self.current_key_index
                current_key = self.api_keys[key_index]

                for attempt in range(retries + 1):
                    try:
                        # エージェントIDを取得（初回は自動作成）
                        agent_id = await self._get_or_create_agent_id(session, key_index)

                        # Agents Completions APIリクエストボディ
                        payload = {
                            "agent_id": agent_id,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": f"**[DeepResearch Request]:** {query}",
                                }
                            ],
                        }

                        headers = {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {current_key}",
                        }

                        # /v1/agents/completions に POST リクエスト
                        async with session.post(
                            MISTRAL_AGENTS_COMPLETIONS_URL,
                            json=payload,
                            headers=headers,
                        ) as resp:
                            status = resp.status

                            # 成功レスポンス (200)
                            if status == 200:
                                data = await resp.json()
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
                                    f"{key_index + 1}/{len(self.api_keys)}. "
                                    f"Rotating to next key."
                                )
                                keys_tried += 1
                                if keys_tried < max_keys_to_try:
                                    self._rotate_key()
                                    break
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
                                    f"Mistral API server error ({status}) after retries."
                                )

                            # その他のHTTPエラー (4xx等)
                            else:
                                error_body = await resp.text()
                                logger.error(
                                    f"Search Agent unexpected HTTP {status}: {error_body[:300]}"
                                )
                                # エージェントIDが無効な場合はキャッシュを破棄して再作成
                                if status in (400, 404):
                                    self._agent_ids.pop(key_index, None)
                                    logger.info("Cleared cached agent ID, will recreate on next attempt.")
                                raise SearchAPIError(
                                    f"Mistral API returned HTTP {status}: {error_body[:200]}"
                                )

                    except SearchAgentError:
                        # SearchAgentErrorのサブクラスはそのまま再raise
                        raise
                    except asyncio.TimeoutError:
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
                        logger.error(f"Search Agent unexpected error: {e}", exc_info=True)
                        raise SearchExecutionError(
                            f"An unexpected error occurred during search: {str(e)}",
                            original_exception=e,
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