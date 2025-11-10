# MOMOKA/llm/plugins/reporter_plugin.py
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import discord
from discord.ext import tasks

try:
    import aiofiles  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    aiofiles = None

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from discord.ext import commands
    from MOMOKA.llm.plugins.deep_research import DeepResearchAgent


class ScheduledReporter:
    """Schedules periodic deep research reports per guild and channel."""

    DATA_PATH = "data/llm_report.json"

    def __init__(self, bot: commands.Bot, deep_research: Optional[DeepResearchAgent] = None) -> None:
        self.bot = bot
        self.deep_research = deep_research
        self._lock = asyncio.Lock()
        self.data_path = self.DATA_PATH
        self.schedules: Dict[str, List[Dict[str, Any]]] = self._load_data()
        self._next_ids: Dict[str, int] = {}
        self._normalise_entries()

        if self.deep_research is None:
            try:
                from MOMOKA.llm.plugins.deep_research import DeepResearchAgent as DeepResearchAgentImpl

                self.deep_research = DeepResearchAgentImpl(bot)
                logger.info("ScheduledReporter instantiated its own DeepResearchAgent.")
            except Exception as exc:  # pragma: no cover - defensive import
                logger.error("ScheduledReporter failed to initialise DeepResearchAgent: %s", exc, exc_info=True)
                self.deep_research = None

        if not self.scheduler_loop.is_running():
            self.scheduler_loop.start()

    # ------------------------------------------------------------------
    # Public API used by the Cog
    # ------------------------------------------------------------------
    async def add_schedule(
        self,
        guild_id: int,
        channel_id: int,
        interval_hours: float,
        query: str,
    ) -> Dict[str, Any]:
        """Register a new report schedule for the given guild."""
        guild_key = str(guild_id)
        async with self._lock:
            next_id = self._next_ids.get(guild_key, 1)
            now = self._now()
            entry = {
                "id": next_id,
                "channel_id": int(channel_id),
                "query": query,
                "interval_hours": float(interval_hours),
                "created_at": now.isoformat(),
                "next_run_at": (now + timedelta(hours=float(interval_hours))).isoformat(),
            }
            schedules = self.schedules.setdefault(guild_key, [])
            schedules.append(entry)
            self._next_ids[guild_key] = next_id + 1
            await self._save_locked()
            logger.info(
                "[ScheduledReporter] Added schedule id=%s guild=%s channel=%s interval=%.2fh",
                next_id,
                guild_id,
                channel_id,
                interval_hours,
            )
            return entry.copy()

    async def list_schedules(self, guild_id: int) -> List[Dict[str, Any]]:
        guild_key = str(guild_id)
        async with self._lock:
            entries = [entry.copy() for entry in self.schedules.get(guild_key, [])]
        entries.sort(key=lambda e: e.get("next_run_at", ""))
        return entries

    async def delete_schedule(self, guild_id: int, schedule_id: int) -> bool:
        guild_key = str(guild_id)
        async with self._lock:
            entries = self.schedules.get(guild_key, [])
            for index, entry in enumerate(entries):
                if int(entry.get("id", 0)) == schedule_id:
                    entries.pop(index)
                    logger.info("[ScheduledReporter] Removed schedule id=%s guild=%s", schedule_id, guild_id)
                    await self._save_locked()
                    return True
        return False

    async def shutdown(self) -> None:
        if self.scheduler_loop.is_running():
            self.scheduler_loop.cancel()
            try:
                await self.scheduler_loop
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------
    @tasks.loop(minutes=1)
    async def scheduler_loop(self) -> None:
        await self._dispatch_pending_reports()

    @scheduler_loop.before_loop
    async def _before_scheduler_loop(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("ScheduledReporter scheduler loop started.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _dispatch_pending_reports(self) -> None:
        if not self.deep_research:
            logger.debug("ScheduledReporter skipped dispatch; deep_research agent unavailable.")
            return

        now = self._now()
        to_execute: List[tuple[int, Dict[str, Any]]] = []

        async with self._lock:
            modified = False
            for guild_key, entries in self.schedules.items():
                for entry in entries:
                    try:
                        next_run_at = datetime.fromisoformat(entry["next_run_at"])
                    except (KeyError, ValueError):
                        next_run_at = now
                        entry["next_run_at"] = now.isoformat()
                        modified = True

                    if next_run_at <= now:
                        interval_hours = float(entry.get("interval_hours", 1.0))
                        entry["next_run_at"] = (now + timedelta(hours=interval_hours)).isoformat()
                        modified = True
                        to_execute.append((int(guild_key), entry.copy()))

            if modified:
                await self._save_locked()

        for guild_id, entry in to_execute:
            await self._execute_schedule(guild_id, entry)

    async def _execute_schedule(self, guild_id: int, entry: Dict[str, Any]) -> None:
        channel_id = entry.get("channel_id")
        query = entry.get("query", "")
        interval_hours = float(entry.get("interval_hours", 1.0))
        channel: Optional[discord.TextChannel | discord.Thread | discord.DMChannel] = self.bot.get_channel(channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound) as exc:
                logger.warning(
                    "[ScheduledReporter] Unable to fetch channel %s for guild %s: %s. Removing schedule.",
                    channel_id,
                    guild_id,
                    exc,
                )
                await self.delete_schedule(guild_id, int(entry.get("id", 0)))
                return

        header = (
            f"🕵️ Deep Research Report\n"
            f"• Query: {query}\n"
            f"• Interval: every {interval_hours:g} hour(s)\n"
            f"• Generated: {self._now().astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M JST')}"
        )
        try:
            await channel.send(header)
        except discord.Forbidden:
            logger.warning(
                "[ScheduledReporter] Missing permission to send messages to channel %s (guild %s). Removing schedule.",
                channel_id,
                guild_id,
            )
            await self.delete_schedule(guild_id, int(entry.get("id", 0)))
            return
        except discord.HTTPException as exc:
            logger.error("[ScheduledReporter] Failed to send report header: %s", exc)

        content = await self.deep_research.run(query=query, channel_id=channel.id) if self.deep_research else None

        if not content:
            fallback = "⚠️ Deep research did not return any content."
            try:
                await channel.send(fallback)
            except discord.HTTPException as exc:
                logger.error("[ScheduledReporter] Failed to send fallback message: %s", exc)
            return

        for chunk in self._chunk_text(content):
            try:
                await channel.send(chunk)
            except discord.HTTPException as exc:
                logger.error("[ScheduledReporter] Failed to send report chunk: %s", exc)
                break

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _load_data(self) -> Dict[str, List[Dict[str, Any]]]:
        if not os.path.exists(self.data_path):
            return {}

        try:
            with open(self.data_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (IOError, json.JSONDecodeError) as exc:
            logger.error("[ScheduledReporter] Failed to load JSON data: %s", exc)
            return {}

        if isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
            return data

        logger.warning("[ScheduledReporter] Unexpected JSON structure in %s; resetting.", self.data_path)
        return {}

    async def _save_locked(self) -> None:
        data = self.schedules
        os.makedirs(os.path.dirname(self.data_path), exist_ok=True)

        if aiofiles:
            async with aiofiles.open(self.data_path, "w", encoding="utf-8") as fp:
                await fp.write(json.dumps(data, indent=4, ensure_ascii=False))
        else:
            with open(self.data_path, "w", encoding="utf-8") as fp:
                json.dump(data, fp, indent=4, ensure_ascii=False)

    def _normalise_entries(self) -> None:
        now = self._now()
        for guild_key, entries in list(self.schedules.items()):
            valid_entries: List[Dict[str, Any]] = []
            max_id = 0
            for entry in entries:
                try:
                    entry_id = int(entry.get("id", 0))
                except (TypeError, ValueError):
                    entry_id = 0
                if entry_id <= 0:
                    entry_id = max_id + 1
                max_id = max(max_id, entry_id)

                entry["id"] = entry_id
                entry["channel_id"] = int(entry.get("channel_id", 0))
                entry["interval_hours"] = float(entry.get("interval_hours", 1.0))
                if not entry.get("query"):
                    entry["query"] = ""
                try:
                    datetime.fromisoformat(entry.get("next_run_at", ""))
                except (TypeError, ValueError):
                    entry["next_run_at"] = (now + timedelta(hours=entry["interval_hours"])).isoformat()
                valid_entries.append(entry)

            self.schedules[guild_key] = valid_entries
            self._next_ids[guild_key] = max_id + 1 if max_id else 1

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _chunk_text(text: str, limit: int = 1800) -> List[str]:
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=timezone.utc)
