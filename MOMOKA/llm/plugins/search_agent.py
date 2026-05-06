# MOMOKA/llm/plugins/search_agent.py
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional, List, Any

# Mistral AI SDKをインポート（Google genaiから移行）
from mistralai import Mistral

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
            self.clients: List[Mistral] = []
            self.current_key_index = 0
            return

        # 複数のAPIキーを収集（キーローテーション対応）
        self.api_keys: List[str] = []
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
            self.clients = []
            self.current_key_index = 0
            return

        # 各APIキーに対してMistralクライアントを初期化
        self.clients = []
        for i, api_key in enumerate(self.api_keys):
            try:
                # Mistral SDKクライアントを作成
                client = Mistral(api_key=api_key)
                self.clients.append(client)
                logger.info(f"SearchAgent: Mistral API key {i + 1}/{len(self.api_keys)} initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize Mistral client for API key {i + 1}: {e}", exc_info=True)

        # すべてのクライアント初期化に失敗した場合
        if not self.clients:
            logger.error("Failed to initialize any Mistral clients. Search will be disabled.")
            self.current_key_index = 0
            return

        # キーローテーション用のインデックス
        self.current_key_index = 0
        # 検索に使用するMistralモデル名（web_searchツール対応モデル）
        self.model_name = gcfg.get("model", "mistral-medium-latest")
        # レスポンスのフォーマット制御用プロンプト
        self.format_control = gcfg.get("format_control", "")
        logger.info(f"SearchAgent initialized with {len(self.clients)} Mistral API key(s) (model: {self.model_name}).")

    def _get_next_client(self) -> Optional[Mistral]:
        """次のクライアントを取得（ローテーション）"""
        if not self.clients:
            return None

        # ラウンドロビンで次のクライアントに切り替え
        self.current_key_index = (self.current_key_index + 1) % len(self.clients)
        logger.info(f"Rotating to Mistral API key {self.current_key_index + 1}/{len(self.clients)}")
        return self.clients[self.current_key_index]

    def _extract_text_from_content(self, content: Any) -> str:
        """Mistralレスポンスのcontentからテキストを抽出する。
        
        contentは文字列の場合もあれば、ContentChunkオブジェクトのリストの場合もある。
        """
        # contentが文字列の場合はそのまま返す
        if isinstance(content, str):
            return content
        
        # contentがリスト（ContentChunkのリスト）の場合
        if isinstance(content, list):
            text_parts = []
            for chunk in content:
                # text type のチャンクからテキストを抽出
                if hasattr(chunk, 'type') and chunk.type == 'text':
                    text_parts.append(getattr(chunk, 'text', ''))
                # dictの場合のフォールバック処理
                elif isinstance(chunk, dict) and chunk.get('type') == 'text':
                    text_parts.append(chunk.get('text', ''))
            return ''.join(text_parts)
        
        # その他の場合は文字列変換
        return str(content) if content else ""

    async def _mistral_search(self, query: str) -> str:
        """Mistral Web Searchを使用して検索を実行し、テキスト結果を返す"""
        if not self.clients:
            raise SearchExecutionError("SearchAgent is not properly initialized.")

        # リトライ設定
        retries = 2
        delay = 1.5
        keys_tried = 0
        max_keys_to_try = len(self.clients)

        while keys_tried < max_keys_to_try:
            # 現在のキーインデックスのクライアントを取得
            current_client = self.clients[self.current_key_index]

            for attempt in range(retries + 1):
                try:
                    # 検索用プロンプトの構築
                    prompt = f"**[DeepResearch Request]:** {query}\n{self.format_control}"

                    # Mistral chat.complete を web_search ツール付きで呼び出し
                    # asyncio.to_thread で同期APIを非同期化
                    response = await asyncio.to_thread(
                        current_client.chat.complete,
                        model=self.model_name,
                        messages=[{"role": "user", "content": prompt}],
                        # web_search ツールを有効化（Mistralビルトインコネクタ）
                        tools=[{"type": "web_search"}]
                    )

                    # レスポンスからテキストを抽出して返す
                    if response and response.choices:
                        message = response.choices[0].message
                        # テキスト部分のみ抽出
                        return self._extract_text_from_content(message.content)
                    else:
                        # レスポンスが空の場合
                        raise SearchExecutionError("Mistral returned an empty response.")

                except Exception as e:
                    # エラーの種類に応じたハンドリング
                    error_str = str(e).lower()
                    status_code = getattr(e, 'status_code', None) or getattr(e, 'code', None)

                    # Rate Limit (429) の判定
                    if status_code == 429 or '429' in error_str or 'rate limit' in error_str:
                        raise SearchAPIRateLimitError(
                            "Mistral Search API rate limit was reached.",
                            original_exception=e
                        )
                    # 503 Service Unavailable → 次のキーに切り替え
                    elif status_code == 503 or '503' in error_str:
                        logger.warning(
                            f"503 error on Mistral API key {self.current_key_index + 1}/{len(self.clients)}. "
                            f"Rotating to next key."
                        )
                        keys_tried += 1
                        if keys_tried < max_keys_to_try:
                            # 次のキーにローテーション
                            self._get_next_client()
                            break  # 内側のループを抜けて次のキーで再試行
                        else:
                            raise SearchAPIServerError(
                                "All Mistral API keys returned 503 errors.",
                                original_exception=e
                            )
                    # その他の5xx サーバーエラー → リトライ
                    elif (isinstance(status_code, int) and 500 <= status_code < 600) or 'server' in error_str:
                        logger.warning(
                            f"SearchAgent server error (attempt {attempt + 1}/{retries + 1}): {e}"
                        )
                        if attempt < retries:
                            # 指数バックオフ的にリトライ間隔を増加
                            await asyncio.sleep(delay * (attempt + 1))
                            continue
                        raise SearchAPIServerError(
                            "Mistral Search API server-side error after retries.",
                            original_exception=e
                        )
                    # SearchAgentErrorのサブクラスはそのまま再raise
                    elif isinstance(e, SearchAgentError):
                        raise
                    else:
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