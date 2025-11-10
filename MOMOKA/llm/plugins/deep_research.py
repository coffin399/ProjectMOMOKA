# MOMOKA/llm/plugins/deep_research.py
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from google import genai
from google.genai import errors, types

if TYPE_CHECKING:
    from discord.ext import commands
    from MOMOKA.llm.plugins.search_agent import SearchAgent

logger = logging.getLogger(__name__)


class DeepResearchAgent:
    """Provides a higher-level interface for executing deep research queries."""

    name = "deep_research"
    tool_spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Performs an in-depth research using Gemini and returns a textual report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The research topic or question to investigate."
                    }
                },
                "required": ["query"]
            }
        }
    }

    def __init__(self, bot: commands.Bot, search_agent: Optional["SearchAgent"] = None) -> None:
        self.bot = bot
        self._legacy_search_agent = search_agent  # kept for backward compatibility logging
        if search_agent:
            logger.info("DeepResearchAgent: ignoring provided SearchAgent; using dedicated Gemini clients.")

        self.api_keys: List[str] = []
        self.clients: List[genai.Client] = []
        self.current_key_index: int = 0
        self.model_name: str = "gemini-2.5-flash"
        self.format_control: str = ""

        self._initialize_clients()

    def _initialize_clients(self) -> None:
        cfg = getattr(self.bot, "cfg", {})
        agent_cfg = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
        if not agent_cfg:
            logger.error("DeepResearchAgent config is missing under bot.cfg['agent']. Deep research disabled.")
            return

        for key in sorted(agent_cfg.keys()):
            if key.startswith("api_key"):
                value = agent_cfg[key]
                if value and value.strip() and value.strip() != "YOUR_GOOGLE_GEMINI_API_KEY_HERE":
                    self.api_keys.append(value.strip())

        if not self.api_keys:
            logger.error("DeepResearchAgent: no valid API keys configured. Deep research disabled.")
            return

        self.model_name = agent_cfg.get("deep_research_model") or agent_cfg.get("model", "gemini-2.5-flash")
        self.format_control = agent_cfg.get("format_control_deep_research") or agent_cfg.get("format_control", "")

        total = len(self.api_keys)
        for idx, api_key in enumerate(self.api_keys, start=1):
            try:
                client = genai.Client(api_key=api_key)
                self.clients.append(client)
                logger.info("DeepResearchAgent: API key %s/%s initialized successfully.", idx, total)
            except Exception as exc:  # pragma: no cover - depends on runtime credentials
                logger.error("DeepResearchAgent: failed to initialize client for API key %s: %s", idx, exc, exc_info=True)

        if not self.clients:
            logger.error("DeepResearchAgent: failed to initialize any Gemini clients. Deep research disabled.")

    def _advance_to_next_client(self) -> Optional[genai.Client]:
        if not self.clients:
            return None
        self.current_key_index = (self.current_key_index + 1) % len(self.clients)
        return self.clients[self.current_key_index]

    def _current_client(self) -> Optional[genai.Client]:
        if not self.clients:
            return None
        return self.clients[self.current_key_index]

    def _build_prompt(self, query: str) -> str:
        detailed_report_instructions = """
**[Detailed Report Instructions]**

Please generate a comprehensive and detailed report on the requested topic. Follow these guidelines:

1. **Depth and Detail**: Provide extensive information with in-depth analysis. Go beyond surface-level facts and explore nuanced aspects of the topic.

2. **Structure and Organization**:
   - Start with a clear executive summary
   - Use well-organized sections with descriptive headings
   - Include bullet points, numbered lists, and tables where appropriate
   - End with conclusions and key takeaways

3. **Comprehensive Coverage**:
   - Include historical context and background information
   - Cover current developments and recent trends
   - Discuss various perspectives and viewpoints
   - Include relevant data, statistics, and examples
   - Address potential implications and future outlook

4. **Clarity and Accessibility**:
   - Explain complex concepts in clear, understandable language
   - Define technical terms when necessary
   - Use analogies and real-world examples to illustrate points
   - Maintain a professional yet engaging tone

5. **Research Quality**:
   - Provide specific facts, figures, and evidence
   - Include sources and references when applicable
   - Distinguish between established facts and speculative analysis
   - Acknowledge limitations or uncertainties in the information

6. **Length and Thoroughness**: Aim for a substantial report (typically 1500-3000 words) that thoroughly explores the topic from multiple angles.

Please ensure the report is valuable to readers seeking deep understanding of the subject matter.
"""
        
        parts = [
            "**[DeepResearch Request]**",
            query.strip(),
            detailed_report_instructions.strip()
        ]
        if self.format_control:
            parts.append(self.format_control.strip())
        return "\n\n".join(part for part in parts if part)

    async def _invoke_gemini(self, client: genai.Client, query: str) -> Any:
        prompt = self._build_prompt(query)
        config = types.GenerateContentConfig(response_mime_type="text/plain")
        return await asyncio.to_thread(
            client.models.generate_content,
            model=self.model_name,
            contents=prompt,
            config=config
        )

    def _extract_response_text(self, response: Any) -> Optional[str]:
        if response is None:
            return None
        text = getattr(response, "text", None)
        if text:
            return text
        if hasattr(response, "candidates"):
            for candidate in getattr(response, "candidates", []):
                content = getattr(candidate, "content", None)
                if hasattr(content, "parts"):
                    for part in content.parts:
                        value = getattr(part, "text", None)
                        if value:
                            return value
        return str(response) if response else None

    async def _generate_report(self, query: str) -> Optional[str]:
        if not self.clients:
            logger.error("DeepResearchAgent: no available clients to execute deep research.")
            return None

        attempts = 0
        last_error: Optional[Exception] = None
        total_clients = len(self.clients)

        while attempts < total_clients:
            client = self._current_client()
            if client is None:
                break
            try:
                response = await self._invoke_gemini(client, query)
                return self._extract_response_text(response)
            except errors.APIError as exc:
                last_error = exc
                logger.warning(
                    "DeepResearchAgent: API error with key index %s/%s (%s). Trying next key.",
                    self.current_key_index + 1,
                    total_clients,
                    exc,
                    exc_info=True,
                )
            except Exception as exc:  # pragma: no cover - runtime safeguard
                last_error = exc
                logger.error(
                    "DeepResearchAgent: unexpected error during Gemini call with key index %s/%s: %s",
                    self.current_key_index + 1,
                    total_clients,
                    exc,
                    exc_info=True,
                )
            attempts += 1
            if attempts < total_clients:
                self._advance_to_next_client()

        if last_error:
            logger.error("DeepResearchAgent: all API keys failed. Last error: %s", last_error)
        return None

    async def run(self, query: str, channel_id: int) -> Optional[str]:
        """Execute deep research using Gemini and return the textual result."""
        if not query:
            raise ValueError("Query cannot be empty.")

        result = await self._generate_report(query)
        if result:
            logger.info(
                "DeepResearchAgent: completed research for channel %s (len=%s).",
                channel_id,
                len(result),
            )
        else:
            logger.warning("DeepResearchAgent: no result produced for channel %s.", channel_id)
        return result

    async def run_tool(self, arguments: Dict[str, Any], channel_id: int) -> str:
        """Entry point used by the LLM cog when invoked as a tool."""
        query = arguments.get("query", "")
        if not query:
            logger.warning("DeepResearchAgent.run_tool called without a query.")
            return "Error: query parameter is required."

        result = await self.run(query=query, channel_id=channel_id)
        if not result:
            return "Error: Deep research did not return any content."
        return result
