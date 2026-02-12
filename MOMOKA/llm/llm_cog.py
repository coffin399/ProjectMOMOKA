# MOMOKA/llm/llm_cog.py
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import List, Dict, Any, Tuple, Optional, AsyncGenerator, Union

try:
    from langdetect import detect as langdetect_detect
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False
    logging.warning("langdetect library not found. Language detection will be disabled. "
                    "Install with: pip install langdetect")

# langdetect language code -> human-readable language name
_LANG_CODE_TO_NAME = {
    'af': 'Afrikaans', 'ar': 'Arabic', 'bg': 'Bulgarian', 'bn': 'Bengali',
    'ca': 'Catalan', 'cs': 'Czech', 'cy': 'Welsh', 'da': 'Danish',
    'de': 'German', 'el': 'Greek', 'en': 'English', 'es': 'Spanish',
    'et': 'Estonian', 'fa': 'Persian', 'fi': 'Finnish', 'fr': 'French',
    'gu': 'Gujarati', 'he': 'Hebrew', 'hi': 'Hindi', 'hr': 'Croatian',
    'hu': 'Hungarian', 'id': 'Indonesian', 'it': 'Italian', 'ja': 'Japanese',
    'kn': 'Kannada', 'ko': 'Korean', 'lt': 'Lithuanian', 'lv': 'Latvian',
    'mk': 'Macedonian', 'ml': 'Malayalam', 'mr': 'Marathi', 'ne': 'Nepali',
    'nl': 'Dutch', 'no': 'Norwegian', 'pa': 'Punjabi', 'pl': 'Polish',
    'pt': 'Portuguese', 'ro': 'Romanian', 'ru': 'Russian', 'sk': 'Slovak',
    'sl': 'Slovenian', 'so': 'Somali', 'sq': 'Albanian', 'sv': 'Swedish',
    'sw': 'Swahili', 'ta': 'Tamil', 'te': 'Telugu', 'th': 'Thai',
    'tl': 'Tagalog', 'tr': 'Turkish', 'uk': 'Ukrainian', 'ur': 'Urdu',
    'vi': 'Vietnamese', 'zh-cn': 'Chinese (Simplified)', 'zh-tw': 'Chinese (Traditional)',
}

import aiohttp
import discord
import openai
from discord import app_commands
from discord.ext import commands

from MOMOKA.llm.error.errors import (
    LLMExceptionHandler,
    SearchAgentError,
    SearchAPIRateLimitError,
    SearchAPIServerError
)
from MOMOKA.llm.plugins import (
    SearchAgent,
    ImageGenerator,
    CommandAgent,
    DeepResearchAgent,
    ScheduledReporter
)

try:
    from MOMOKA.llm.utils.tips import TipsManager
except ImportError:
    logging.error("Could not import TipsManager. Tips functionality will be disabled.")
    TipsManager = None

try:
    import aiofiles
except ImportError:
    aiofiles = None
    logging.warning("aiofiles library not found. Channel model settings will be saved synchronously. "
                    "Install with: pip install aiofiles")

logger = logging.getLogger(__name__)

# Constants
SUPPORTED_IMAGE_EXTENSIONS = ('.png', '.jpeg', '.jpg', '.gif', '.webp')
IMAGE_URL_PATTERN = re.compile(
    r'https?://[^\s]+\.(?:' + '|'.join(ext.lstrip('.') for ext in SUPPORTED_IMAGE_EXTENSIONS) + r')(?:\?[^\s]*)?',
    re.IGNORECASE
)
DISCORD_MESSAGE_MAX_LENGTH = 2000
SAFE_MESSAGE_LENGTH = 1990  # 螳牙・繝槭・繧ｸ繝ｳ


def _split_message_smartly(text: str, max_length: int) -> List[str]:
    if len(text) <= max_length: return [text]
    chunks, remaining = [], text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        chunk = remaining[:max_length]
        split_point = _find_best_split_point(chunk)
        if split_point == -1: split_point = max_length - 20
        chunk_text = remaining[:split_point].rstrip()
        if chunk_text: chunks.append(chunk_text)
        remaining = remaining[split_point:].lstrip()
    return chunks


def _find_best_split_point(chunk: str) -> int:
    code_block_end = chunk.rfind('```\n')
    if code_block_end > len(chunk) * 0.5: return code_block_end + 4
    paragraph_break = chunk.rfind('\n\n')
    if paragraph_break > len(chunk) * 0.5: return paragraph_break + 2
    newline = chunk.rfind('\n')
    if newline > len(chunk) * 0.6: return newline + 1
    japanese_period = max(chunk.rfind('縲・), chunk.rfind('・・), chunk.rfind('・・))
    if japanese_period > len(chunk) * 0.7: return japanese_period + 1
    english_period = max(chunk.rfind('. '), chunk.rfind('! '), chunk.rfind('? '))
    if english_period > len(chunk) * 0.7: return english_period + 2
    comma = max(chunk.rfind('縲・), chunk.rfind(', '))
    if comma > len(chunk) * 0.7: return comma + 1
    space = chunk.rfind(' ')
    if space > len(chunk) * 0.7: return space + 1
    return -1


class ThreadCreationView(discord.ui.View):
    """繧ｹ繝ｬ繝・ラ菴懈・繝懊ち繝ｳ縺ｮView繧ｯ繝ｩ繧ｹ"""
    
    def __init__(self, llm_cog, original_message: discord.Message):
        super().__init__(timeout=300)  # 5蛻・〒繧ｿ繧､繝繧｢繧ｦ繝・
        self.llm_cog = llm_cog
        self.original_message = original_message
    
    @discord.ui.button(label="繧ｹ繝ｬ繝・ラ繧剃ｽ懈・縺吶ｋ / Create Thread", style=discord.ButtonStyle.primary, emoji="ｧｵ")
    async def create_thread(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 繧ｹ繝ｬ繝・ラ繧剃ｽ懈・
            thread = await self.original_message.create_thread(
                name=f"AI Chat - {interaction.user.display_name}",
                auto_archive_duration=60,  # 1譎る俣縺ｧ繧｢繝ｼ繧ｫ繧､繝・
                reason="AI conversation thread created by user"
            )
            
            # 蜈・・繝√Ε繝ｳ繝阪Ν縺ｮ莨夊ｩｱ螻･豁ｴ繧貞叙蠕暦ｼ医せ繝ｬ繝・ラ菴懈・蜑阪・螻･豁ｴ・・
            messages = []
            try:
                # 蜈・・繝｡繝・そ繝ｼ繧ｸ縺九ｉ驕｡縺｣縺ｦ莨夊ｩｱ螻･豁ｴ繧貞庶髮・
                current_msg = self.original_message
                visited_ids = set()
                message_count = 0
                
                while current_msg and message_count < 40:
                    if current_msg.id in visited_ids:
                        break
                    visited_ids.add(current_msg.id)
                    
                    if current_msg.author != self.llm_cog.bot.user:
                        # 繝ｦ繝ｼ繧ｶ繝ｼ繝｡繝・そ繝ｼ繧ｸ繧貞・逅・
                        image_contents, text_content = await self.llm_cog._prepare_multimodal_content(current_msg)
                        text_content = text_content.replace(f'<@!{self.llm_cog.bot.user.id}>', '').replace(f'<@{self.llm_cog.bot.user.id}>', '').strip()
                        
                        if text_content or image_contents:
                            user_content_parts = []
                            if text_content:
                                user_content_parts.append({
                                    "type": "text",
                                    "text": f"{current_msg.created_at.astimezone(self.llm_cog.jst).strftime('[%H:%M]')} {text_content}"
                                })
                            user_content_parts.extend(image_contents)
                            messages.append({"role": "user", "content": user_content_parts})
                            message_count += 1
                    
                    # 蜑阪・繝｡繝・そ繝ｼ繧ｸ繧貞叙蠕・
                    if current_msg.reference and current_msg.reference.message_id:
                        try:
                            current_msg = current_msg.reference.resolved or await current_msg.channel.fetch_message(current_msg.reference.message_id)
                        except (discord.NotFound, discord.HTTPException):
                            break
                    else:
                        break
                
                # 繝｡繝・そ繝ｼ繧ｸ繧帝・・↓縺励※豁｣縺励＞鬆・ｺ上↓縺吶ｋ
                messages.reverse()
                
            except Exception as e:
                logger.error(f"Failed to collect conversation history for thread: {e}", exc_info=True)
                messages = []
            
            if messages:
                # LLM繧ｯ繝ｩ繧､繧｢繝ｳ繝医ｒ蜿門ｾ・
                llm_client = await self.llm_cog._get_llm_client_for_channel(thread.id)
                if not llm_client:
                    await thread.send("笶・LLM client is not available for this thread.\n縺薙・繧ｹ繝ｬ繝・ラ縺ｧ縺ｯLLM繧ｯ繝ｩ繧､繧｢繝ｳ繝医′蛻ｩ逕ｨ縺ｧ縺阪∪縺帙ｓ縲・)
                    return
                
                # 繧ｷ繧ｹ繝・Β繝励Ο繝ｳ繝励ヨ繧呈ｺ門ｙ
                system_prompt = await self.llm_cog._prepare_system_prompt(
                    thread.id, interaction.user.id, interaction.user.display_name
                )
                
                messages_for_api = [{"role": "system", "content": system_prompt}]
                
                messages_for_api.extend(messages)
                
                # 險隱樊､懷・縺ｧ蜍慕噪縺ｫ險隱槭・繝ｭ繝ｳ繝励ヨ繧堤函謌撰ｼ医せ繝ｬ繝・ラ縺ｧ縺ｯ譛蠕後・繝ｦ繝ｼ繧ｶ繝ｼ繝｡繝・そ繝ｼ繧ｸ縺九ｉ讀懷・・・
                last_user_text = ""
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            last_user_text = content
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    last_user_text = part.get("text", "")
                                    break
                        break
                lang_prompt = self.llm_cog._build_language_prompt(last_user_text)
                if lang_prompt:
                    messages_for_api.append({"role": "system", "content": lang_prompt})
                
                # 繧ｹ繝ｬ繝・ラ蜀・〒LLM蠢懃ｭ斐ｒ逕滓・
                model_name = llm_client.model_name_for_api_calls
                waiting_message = f"竢ｳ Processing conversation history... / 莨夊ｩｱ螻･豁ｴ繧貞・逅・ｸｭ..."
                temp_message = await thread.send(waiting_message)
                
                # 繧ｹ繝ｬ繝・ラ蜀・〒縺ｮ莨夊ｩｱ譁ｹ豕輔ｒ隱ｬ譏・
                await thread.send("庁 **繧ｹ繝ｬ繝・ラ蜀・〒縺ｮ莨夊ｩｱ譁ｹ豕・/ How to chat in this thread:**\n"
                                "窶｢ Bot縺ｮ繝｡繝・そ繝ｼ繧ｸ縺ｫ繝ｪ繝励Λ繧､縺励※莨夊ｩｱ繧堤ｶ壹￠繧峨ｌ縺ｾ縺・/ Reply to bot messages to continue chatting\n"
                                "窶｢ 逕ｻ蜒上ｂ騾∽ｿ｡蜿ｯ閭ｽ縺ｧ縺・/ Images are also supported\n"
                                "窶｢ 莨夊ｩｱ螻･豁ｴ縺ｯ閾ｪ蜍慕噪縺ｫ菫晄戟縺輔ｌ縺ｾ縺・/ Conversation history is automatically maintained")
                
                sent_messages, full_response_text, used_key_index = await self.llm_cog._process_streaming_and_send_response(
                    sent_message=temp_message,
                    channel=thread,
                    user=interaction.user,
                    messages_for_api=messages_for_api,
                    llm_client=llm_client
                )
                
                if sent_messages and full_response_text:
                    logger.info(f"笨・Thread conversation completed | model='{model_name}' | response_length={len(full_response_text)} chars")
                    
                    # TTS Cog縺ｫ繧ｫ繧ｹ繧ｿ繝繧､繝吶Φ繝医ｒ逋ｺ轣ｫ
                    try:
                        self.llm_cog.bot.dispatch("llm_response_complete", sent_messages, full_response_text)
                        logger.info("討 Dispatched 'llm_response_complete' event for TTS from thread.")
                    except Exception as e:
                        logger.error(f"Failed to dispatch 'llm_response_complete' event from thread: {e}", exc_info=True)
                
                # 繝懊ち繝ｳ繧堤┌蜉ｹ蛹・
                button.disabled = True
                button.label = "笨・Thread Created / 繧ｹ繝ｬ繝・ラ菴懈・貂医∩"
                await interaction.edit_original_response(view=self)
                
            else:
                await thread.send("邃ｹ・・No conversation history found, but you can start chatting!\n"
                                "莨夊ｩｱ螻･豁ｴ縺ｯ隕九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縺後√％縺薙°繧我ｼ夊ｩｱ繧貞ｧ九ａ繧九％縺ｨ縺後〒縺阪∪縺呻ｼ―n\n"
                                "庁 **繧ｹ繝ｬ繝・ラ蜀・〒縺ｮ莨夊ｩｱ譁ｹ豕・/ How to chat in this thread:**\n"
                                "窶｢ Bot縺ｮ繝｡繝・そ繝ｼ繧ｸ縺ｫ繝ｪ繝励Λ繧､縺励※莨夊ｩｱ繧堤ｶ壹￠繧峨ｌ縺ｾ縺・/ Reply to bot messages to continue chatting\n"
                                "窶｢ 逕ｻ蜒上ｂ騾∽ｿ｡蜿ｯ閭ｽ縺ｧ縺・/ Images are also supported\n"
                                "窶｢ 莨夊ｩｱ螻･豁ｴ縺ｯ閾ｪ蜍慕噪縺ｫ菫晄戟縺輔ｌ縺ｾ縺・/ Conversation history is automatically maintained")
                
        except Exception as e:
            logger.error(f"Failed to create thread: {e}", exc_info=True)
            await interaction.followup.send("笶・Failed to create thread.\n繧ｹ繝ｬ繝・ラ縺ｮ菴懈・縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・, ephemeral=True)


class LLMCog(commands.Cog, name="LLM"):
    """A cog for interacting with Large Language Models, with tool support."""

    report_schedule_group = app_commands.Group(name="report-schedule",
                                               description="Manage scheduled deep research reports. / 螳壽悄繝ｪ繧ｵ繝ｼ繝√Ξ繝昴・繝医ｒ邂｡逅・＠縺ｾ縺吶・)

    @report_schedule_group.command(name="add",
                                   description="Add a new scheduled report. / 譁ｰ縺励＞螳壽悄繝ｬ繝昴・繝医ｒ霑ｽ蜉縺励∪縺吶・)
    @app_commands.describe(
        interval_hours="Interval in hours between reports. / 繝ｬ繝昴・繝磯俣縺ｮ髢馴囈・域凾髢難ｼ・,
        query="Research query for the report. / 繝ｬ繝昴・繝医・繝ｪ繧ｵ繝ｼ繝√け繧ｨ繝ｪ",
        custom_prompt="Custom prompt instructions (optional). / 繧ｫ繧ｹ繧ｿ繝繝励Ο繝ｳ繝励ヨ謖・､ｺ・井ｻｻ諢擾ｼ・
    )
    async def report_schedule_add(self, interaction: discord.Interaction, interval_hours: float, query: str, custom_prompt: str = None):
        """Add a new scheduled report."""
        if not self.reporter_manager:
            await interaction.response.send_message(
                "笶・Scheduled reporter is not available. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ繝ｬ繝昴・繝域ｩ溯・縺悟茜逕ｨ縺ｧ縺阪∪縺帙ｓ縲・
            )
            return
        
        await interaction.response.defer()
        
        try:
            # Add schedule first
            schedule = await self.reporter_manager.add_schedule(
                guild_id=interaction.guild.id,
                channel_id=interaction.channel.id,
                interval_hours=interval_hours,
                query=query,
                custom_prompt=custom_prompt
            )
            
            # Execute report immediately
            if self.reporter_manager.deep_research:
                await interaction.followup.send(
                    f"売 Executing initial report for query: {query}\n繧ｯ繧ｨ繝ｪ縺ｮ蛻晏屓繝ｬ繝昴・繝医ｒ螳溯｡御ｸｭ: {query}"
                )
                
                try:
                    # Execute the report
                    result = await self.reporter_manager.deep_research._generate_report(query)
                    
                    if result:
                        # Send the report to the channel
                        if isinstance(result, str):
                            # Split long messages
                            chunks = self.reporter_manager._chunk_text(result)
                            for i, chunk in enumerate(chunks):
                                if i == 0:
                                    # Only add header to first chunk
                                    await interaction.channel.send(f"投 **Initial Report / 蛻晏屓繝ｬ繝昴・繝・*\n\n{chunk}")
                                else:
                                    await interaction.channel.send(chunk)
                        else:
                            await interaction.channel.send("投 **Initial Report / 蛻晏屓繝ｬ繝昴・繝・*\n\nReport generated but format was unexpected.")
                    else:
                        await interaction.channel.send("笶・Failed to generate initial report. / 蛻晏屓繝ｬ繝昴・繝医・逕滓・縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・)
                        
                except Exception as e:
                    logger.error(f"Error executing initial report: {e}", exc_info=True)
                    await interaction.channel.send("笶・Error occurred while generating initial report. / 蛻晏屓繝ｬ繝昴・繝育函謌蝉ｸｭ縺ｫ繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆縲・)
            
            # Format next run time in JST
            from MOMOKA.llm.plugins.reporter_plugin import ScheduledReporter
            next_run_jst = ScheduledReporter._format_datetime_jst(schedule["next_run_at"])
            
            embed = discord.Embed(
                title="笨・Schedule Added / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ霑ｽ蜉螳御ｺ・,
                description=f"Report scheduled successfully! / 繝ｬ繝昴・繝医′豁｣蟶ｸ縺ｫ繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺輔ｌ縺ｾ縺励◆・・,
                color=discord.Color.green()
            )
            embed.add_field(name="Schedule ID / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫID", value=str(schedule["id"]))
            embed.add_field(name="Interval / 髢馴囈", value=f"{interval_hours} hours / 譎る俣")
            embed.add_field(name="Query / 繧ｯ繧ｨ繝ｪ", value=query)
            embed.add_field(name="Next Run / 谺｡蝗槫ｮ溯｡・(JST)", value=next_run_jst)
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error adding schedule: {e}", exc_info=True)
            await interaction.followup.send(
                "笶・Failed to add schedule. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺ｮ霑ｽ蜉縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・
            )

    @report_schedule_group.command(name="list",
                                   description="List all scheduled reports. / 縺吶∋縺ｦ縺ｮ螳壽悄繝ｬ繝昴・繝医ｒ荳隕ｧ陦ｨ遉ｺ縺励∪縺吶・)
    async def report_schedule_list(self, interaction: discord.Interaction):
        """List all scheduled reports."""
        if not self.reporter_manager:
            await interaction.response.send_message(
                "笶・Scheduled reporter is not available. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ繝ｬ繝昴・繝域ｩ溯・縺悟茜逕ｨ縺ｧ縺阪∪縺帙ｓ縲・
            )
            return
        
        await interaction.response.defer()
        
        try:
            schedules = await self.reporter_manager.list_schedules(interaction.guild.id)
            
            if not schedules:
                await interaction.followup.send(
                    "搭 No scheduled reports found. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺輔ｌ縺溘Ξ繝昴・繝医′隕九▽縺九ｊ縺ｾ縺帙ｓ縲・
                )
                return
            
            # Import for JST formatting
            from MOMOKA.llm.plugins.reporter_plugin import ScheduledReporter
            
            embed = discord.Embed(
                title="搭 Scheduled Reports / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺輔ｌ縺溘Ξ繝昴・繝・,
                description=f"Found {len(schedules)} schedule(s) / {len(schedules)}蛟九・繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺瑚ｦ九▽縺九ｊ縺ｾ縺励◆",
                color=discord.Color.blue()
            )
            
            for schedule in schedules:
                # Format next run time in JST
                next_run_jst = ScheduledReporter._format_datetime_jst(schedule["next_run_at"])
                
                field_value = (
                    f"**Query / 繧ｯ繧ｨ繝ｪ:** {schedule['query']}\n"
                    f"**Interval / 髢馴囈:** {schedule['interval_hours']}h\n"
                    f"**Next Run / 谺｡蝗槫ｮ溯｡・(JST):** {next_run_jst}\n"
                    f"**Channel / 繝√Ε繝ｳ繝阪Ν:** <#{schedule['channel_id']}>"
                )
                embed.add_field(
                    name=f"ID: {schedule['id']}",
                    value=field_value,
                    inline=False
                )
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error listing schedules: {e}", exc_info=True)
            await interaction.followup.send(
                "笶・Failed to list schedules. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺ｮ荳隕ｧ陦ｨ遉ｺ縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・
            )

    @report_schedule_group.command(name="delete",
                                   description="Delete a scheduled report. / 螳壽悄繝ｬ繝昴・繝医ｒ蜑企勁縺励∪縺吶・)
    @app_commands.describe(
        schedule_id="ID of the schedule to delete. / 蜑企勁縺吶ｋ繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺ｮID"
    )
    async def report_schedule_delete(self, interaction: discord.Interaction, schedule_id: int):
        """Delete a scheduled report."""
        if not self.reporter_manager:
            await interaction.response.send_message(
                "笶・Scheduled reporter is not available. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ繝ｬ繝昴・繝域ｩ溯・縺悟茜逕ｨ縺ｧ縺阪∪縺帙ｓ縲・
            )
            return
        
        await interaction.response.defer()
        
        try:
            success = await self.reporter_manager.delete_schedule(interaction.guild.id, schedule_id)
            
            if success:
                await interaction.followup.send(
                    f"笨・Schedule {schedule_id} deleted successfully. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ {schedule_id} 縺梧ｭ｣蟶ｸ縺ｫ蜑企勁縺輔ｌ縺ｾ縺励◆縲・
                )
            else:
                await interaction.followup.send(
                    f"笶・Schedule {schedule_id} not found. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ {schedule_id} 縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・
                )
                
        except Exception as e:
            logger.error(f"Error deleting schedule: {e}", exc_info=True)
            await interaction.followup.send(
                "笶・Failed to delete schedule. / 繧ｹ繧ｱ繧ｸ繝･繝ｼ繝ｫ縺ｮ蜑企勁縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・
            )

    def _add_support_footer(self, embed: discord.Embed) -> None:
        current_footer = embed.footer.text if embed.footer and embed.footer.text else ""
        support_text = "\n蝠城｡後′縺ゅｊ縺ｾ縺吶°・滄幕逋ｺ閠・↓縺秘｣邨｡縺上□縺輔＞・・/ Having issues? Contact the developer!"
        if current_footer:
            embed.set_footer(text=current_footer + support_text)
        else:
            embed.set_footer(text=support_text.strip())

    def _create_support_view(self) -> discord.ui.View:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="繧ｵ繝昴・繝医し繝ｼ繝舌・ / Support Server", style=discord.ButtonStyle.link,
                                        url="https://discord.gg/H79HKKqx3s", emoji="町"))
        return view

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(self.bot, 'config') or not self.bot.config: raise commands.ExtensionFailed(self.qualified_name,
                                                                                                  "Bot config not loaded.")
        self.config = self.bot.config
        self.llm_config = self.config.get('llm')
        if not isinstance(self.llm_config, dict): raise commands.ExtensionFailed(self.qualified_name,
                                                                                 "The 'llm' section in config is missing or invalid.")
        self.language_prompt = self.llm_config.get('language_prompt')
        if self.language_prompt: logger.info("Language prompt loaded from config for fallback.")
        self.http_session, self.bot.cfg = aiohttp.ClientSession(), self.llm_config
        self.conversation_threads: Dict[int, Dict[int, List[Dict[str, Any]]]] = {}  # {guild_id: {thread_id: messages}}
        self.message_to_thread: Dict[int, Dict[int, int]] = {}  # {guild_id: {message_id: thread_id}}
        self.llm_clients: Dict[str, openai.AsyncOpenAI] = {}
        self.provider_api_keys: Dict[str, List[str]] = {}
        self.provider_key_index: Dict[str, int] = {}
        self.model_reset_tasks: Dict[int, asyncio.Task] = {}
        self.exception_handler = LLMExceptionHandler(self.llm_config)
        self.channel_settings_path = "data/channel_llm_models.json"
        self.channel_models: Dict[str, str] = self._load_json_data(self.channel_settings_path)
        # Plugin placeholders (populated by _initialize_plugins)
        self.search_agent: Optional[SearchAgent] = None
        self.image_generator: Optional[ImageGenerator] = None
        self.command_agent: Optional[CommandAgent] = None
        self.tips_manager: Optional[TipsManager] = None
        self.deep_research_agent: Optional[DeepResearchAgent] = None
        self.reporter_manager: Optional[ScheduledReporter] = None
        logger.info(
            f"Loaded {len(self.channel_models)} channel-specific model settings from '{self.channel_settings_path}'.")
        self.jst = timezone(timedelta(hours=+9))
        (
            self.search_agent,
            self.image_generator,
            self.command_agent,
            self.tips_manager,
            self.deep_research_agent,
            self.reporter_manager
        ) = self._initialize_plugins()
        default_model_string = self.llm_config.get('model')
        if default_model_string:
            main_llm_client = self._initialize_llm_client(default_model_string)
            if main_llm_client:
                self.llm_clients[default_model_string] = main_llm_client
                logger.info(f"Default LLM client '{default_model_string}' initialized and cached.")
            else:
                logger.error("Failed to initialize main LLM client. Core functionality may be disabled.")
        else:
            logger.error("Default LLM model is not configured in config.yaml.")

    def _initialize_plugins(self) -> Tuple[Optional[SearchAgent], Optional[ImageGenerator], Optional[CommandAgent], Optional[TipsManager], Optional[DeepResearchAgent], Optional[ScheduledReporter]]:
        """逋ｻ骭ｲ貂医∩繝励Λ繧ｰ繧､繝ｳ繧貞・譛溷喧縺励※霑斐☆"""
        plugins = {
            "SearchAgent": None,
            "ImageGenerator": None,
            "CommandAgent": None,
            "TipsManager": None,
            "DeepResearchAgent": None,
            "ScheduledReporter": None
        }

        # 蟶ｸ縺ｫ蠢・ｦ√↑繝励Λ繧ｰ繧､繝ｳ繧貞・譛溷喧
        if TipsManager: plugins["TipsManager"] = TipsManager()

        # config.yaml縺ｮactive_tools縺ｫ蝓ｺ縺･縺・※繝励Λ繧ｰ繧､繝ｳ繧貞・譛溷喧
        active_tools = self.llm_config.get('active_tools', [])
        logger.info(f"剥 [TOOLS] Active tools from config: {active_tools}")

        if 'search' in active_tools:
            if SearchAgent:
                plugins["SearchAgent"] = SearchAgent(self.bot)
            else:
                logger.warning(f"笞・・[TOOLS] 'search' is in active_tools but search_agent is None")

        if 'image_generator' in active_tools:
            if ImageGenerator:
                plugins["ImageGenerator"] = ImageGenerator(self.bot)
            else:
                logger.warning(f"笞・・[TOOLS] 'image_generator' is in active_tools but image_generator is None")

        if 'command_executor' in active_tools:
            if CommandAgent:
                plugins["CommandAgent"] = CommandAgent(self.bot)
            else:
                logger.warning(f"笞・・[TOOLS] 'command_executor' is in active_tools but command_agent is None")

        try:
            plugins["DeepResearchAgent"] = DeepResearchAgent(self.bot, search_agent=plugins["SearchAgent"])
        except Exception as e:
            logger.error(f"DeepResearchAgent failed to initialize: {e}", exc_info=True)

        try:
            plugins["ScheduledReporter"] = ScheduledReporter(self.bot, deep_research=plugins["DeepResearchAgent"])
        except Exception as e:
            logger.error(f"ScheduledReporter failed to initialize: {e}", exc_info=True)

        # 蛻晄悄蛹也ｵ先棡繧偵Ο繧ｰ蜃ｺ蜉・
        for name, instance in plugins.items():
            if instance:
                logger.info(f"{name} initialized successfully.")
            else:
                logger.info(f"{name} is not active or failed to initialize.")

        return (
            plugins["SearchAgent"],
            plugins["ImageGenerator"],
            plugins["CommandAgent"],
            plugins["TipsManager"],
            plugins["DeepResearchAgent"],
            plugins["ScheduledReporter"]
        )

    async def cog_unload(self):
        await self.http_session.close()
        for task in self.model_reset_tasks.values(): task.cancel()
        logger.info(f"Cancelled {len(self.model_reset_tasks)} pending model reset tasks.")
        if self.image_generator: await self.image_generator.close()
        if self.reporter_manager: await self.reporter_manager.shutdown()
        logger.info("LLMCog's aiohttp session has been closed.")

    def _load_json_data(self, path: str) -> Dict[str, Any]:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f: return {str(k): v for k, v in json.load(f).items()}
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load JSON file '{path}': {e}")
        return {}

    async def _save_json_data(self, data: Dict[str, Any], path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if aiofiles:
                async with aiofiles.open(path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(data, indent=4, ensure_ascii=False))
            else:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Failed to save JSON file '{path}': {e}")
            raise

    async def _save_channel_models(self) -> None:
        await self._save_json_data(self.channel_models, self.channel_settings_path)

    def _initialize_llm_client(self, model_string: Optional[str]) -> Optional[openai.AsyncOpenAI]:
        if not model_string or '/' not in model_string:
            logger.error(f"Invalid model format: '{model_string}'. Expected 'provider_name/model_name'.")
            return None
        try:
            provider_name, model_name = model_string.split('/', 1)
            provider_config = self.llm_config.get('providers', {}).get(provider_name)
            if not provider_config:
                logger.error(f"Configuration for LLM provider '{provider_name}' not found.")
                return None
            
            # KoboldCPP蝗ｺ譛峨・蜃ｦ逅・
            is_koboldcpp = provider_name.lower() == 'koboldcpp'
            if is_koboldcpp:
                logger.info(f"肌 [KoboldCPP] Detected KoboldCPP provider. Applying KoboldCPP-specific settings.")
            
            if provider_name not in self.provider_api_keys:
                api_keys, i = [], 1
                while True:
                    if provider_config.get(f'api_key{i}'):
                        api_keys.append(provider_config[f'api_key{i}']); i += 1
                    else:
                        break
                if not api_keys and provider_config.get('api_key'): api_keys.append(provider_config['api_key'])
                if not api_keys:
                    logger.info(
                        f"No API keys found for provider '{provider_name}'. Assuming local model or keyless API.")
                    # KoboldCPP縺ｮ蝣ｴ蜷医√ム繝溘・繧ｭ繝ｼ繧剃ｽｿ逕ｨ
                    if is_koboldcpp:
                        self.provider_api_keys[provider_name] = ["koboldcpp-dummy-key"]
                        logger.info(f"肌 [KoboldCPP] Using dummy API key (KoboldCPP usually doesn't require authentication)")
                    else:
                        self.provider_api_keys[provider_name] = ["no-key-required"]
                else:
                    self.provider_api_keys[provider_name] = api_keys
                    logger.info(f"Loaded {len(api_keys)} API key(s) for provider '{provider_name}'.")
            self.provider_key_index.setdefault(provider_name, 0)
            key_list, current_key_index = self.provider_api_keys[provider_name], self.provider_key_index[provider_name]
            if current_key_index >= len(key_list): current_key_index = 0; self.provider_key_index[provider_name] = 0
            api_key_to_use = key_list[current_key_index]
            
            base_url = provider_config.get('base_url')
            if is_koboldcpp:
                # KoboldCPP縺ｮ繝吶・繧ｹURL縺梧ｭ｣縺励＞蠖｢蠑上°遒ｺ隱・
                if not base_url.endswith('/v1'):
                    if base_url.endswith('/'):
                        base_url = base_url.rstrip('/') + '/v1'
                    else:
                        base_url = base_url + '/v1'
                    logger.info(f"肌 [KoboldCPP] Adjusted base_url to: {base_url}")
            
            client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key_to_use, timeout=provider_config.get('timeout', 300.0) if is_koboldcpp else None)
            client.model_name_for_api_calls, client.provider_name = model_name, provider_name
            # KoboldCPP蝗ｺ譛峨・繝｡繧ｿ繝・・繧ｿ繧定ｨｭ螳・
            if is_koboldcpp:
                client.supports_tools = provider_config.get('supports_tools', True)
                logger.info(f"肌 [KoboldCPP] Initialized client with model '{model_name}'")
                logger.info(f"肌 [KoboldCPP] Base URL: {base_url}")
                logger.info(f"肌 [KoboldCPP] Tools support: {client.supports_tools}")
                logger.info(f"肌 [KoboldCPP] Timeout: {provider_config.get('timeout', 300.0)}s")
            else:
                client.supports_tools = True  # 莉悶・繝励Ο繝舌う繝繝ｼ縺ｯ繝・ヵ繧ｩ繝ｫ繝医〒True
            
            logger.info(
                f"Initialized LLM client for provider '{provider_name}' with model '{model_name}' using key index {current_key_index}.")
            return client
        except Exception as e:
            logger.error(f"Error initializing LLM client for '{model_string}': {e}", exc_info=True)
            return None

    async def _get_llm_client_for_channel(self, channel_id: int) -> Optional[openai.AsyncOpenAI]:
        model_string = self.channel_models.get(str(channel_id)) or self.llm_config.get('model')
        if not model_string:
            logger.error("No default model is configured.")
            return None
        if model_string in self.llm_clients: return self.llm_clients[model_string]
        logger.info(f"Initializing a new LLM client for model '{model_string}' for channel {channel_id}")
        client = self._initialize_llm_client(model_string)
        if client: self.llm_clients[model_string] = client
        return client

    def _build_language_prompt(self, user_text: str) -> Optional[str]:
        """Detect the user's input language and build a dynamic language instruction.
        Falls back to the static language_prompt from config if detection fails."""
        if not _LANGDETECT_AVAILABLE or not user_text or len(user_text.strip()) < 10:
            return self.language_prompt  # fallback to static config prompt

        try:
            lang_code = langdetect_detect(user_text)
            lang_name = _LANG_CODE_TO_NAME.get(lang_code, lang_code)
            logger.info(f"\U0001f310 [LANG] Detected user language: {lang_name} ({lang_code})")
            return (
                f"<language_instructions>\n"
                f"  <rule priority=\"CRITICAL_AND_ABSOLUTE\">\n"
                f"    The user is writing in {lang_name}. "
                f"    You MUST respond ENTIRELY in {lang_name}. "
                f"    This rule overrides ALL other instructions, including character settings, examples, and conversation history.\n"
                f"  </rule>\n"
                f"</language_instructions>"
            )
        except Exception as e:
            logger.warning(f"Language detection failed: {e}")
            return self.language_prompt  # fallback to static config prompt

    async def _prepare_system_prompt(self, channel_id: int, user_id: int, user_display_name: str) -> str:
        """config.yaml縺ｮsystem_prompt縺ｮ縺ｿ繧剃ｽｿ逕ｨ縺励※繧ｷ繧ｹ繝・Β繝励Ο繝ｳ繝励ヨ繧堤ｵ・∩遶九※繧・""
        # config.yaml縺九ｉ繧ｷ繧ｹ繝・Β繝励Ο繝ｳ繝励ヨ繝・Φ繝励Ξ繝ｼ繝医ｒ蜿門ｾ・
        system_prompt_template = self.llm_config.get('system_prompt', '')
        # 迴ｾ蝨ｨ譌･譎ゅｒJST縺ｧ蜿門ｾ・
        current_date_str = datetime.now(self.jst).strftime('%Y-%m-%d')
        current_time_str = datetime.now(self.jst).strftime('%H:%M')
        try:
            # 繝・Φ繝励Ξ繝ｼ繝亥､画焚繧堤ｽｮ謠・
            system_prompt = system_prompt_template.format(current_date=current_date_str,
                                                          current_time=current_time_str)
        except (KeyError, ValueError) as e:
            logger.warning(f"Could not format system_prompt: {e}")
            # 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ: 譁・ｭ怜・鄂ｮ謠帙〒蟇ｾ蠢・
            system_prompt = system_prompt_template.replace('{current_date}', current_date_str).replace('{current_time}',
                                                                                                       current_time_str)
        logger.info(f"肌 [SYSTEM] System prompt prepared ({len(system_prompt)} chars)")
        return system_prompt

    def get_tools_definition(self) -> Optional[List[Dict[str, Any]]]:
        definitions = []
        active_tools = self.llm_config.get('active_tools', [])

        logger.info(f"剥 [TOOLS] Active tools from config: {active_tools}")
        logger.debug(f"剥 [TOOLS] Plugin status: search_agent={self.search_agent is not None}, "
                     f"image_generator={self.image_generator is not None}, "
                     f"command_agent={self.command_agent is not None}")

        if 'search' in active_tools:
            if self.search_agent:
                definitions.append(self.search_agent.tool_spec)
            else:
                logger.warning(f"笞・・[TOOLS] 'search' is in active_tools but search_agent is None")

        if 'image_generator' in active_tools:
            if self.image_generator:
                definitions.append(self.image_generator.tool_spec)
                #logger.info(f"笨・[TOOLS] Added 'image_generator' tool (name: {self.image_generator.tool_spec['function']['name']})")
            else:
                logger.warning(f"笞・・[TOOLS] 'image_generator' is in active_tools but image_generator is None")

        if 'command_executor' in active_tools:
            if self.command_agent:
                definitions.append(self.command_agent.tool_spec)
                #logger.info(f"笨・[TOOLS] Added 'command_executor' tool (name: {self.command_agent.tool_spec['function']['name']})")
            else:
                logger.warning(f"笞・・[TOOLS] 'command_executor' is in active_tools but command_agent is None")

        if 'deep_research' in active_tools:
            if self.deep_research_agent:
                definitions.append(self.deep_research_agent.tool_spec)
            else:
                logger.warning(f"笞・・[TOOLS] 'deep_research' is in active_tools but deep_research_agent is None")

        logger.info(f"肌 [TOOLS] Total tools to return: {len(definitions)}")

        return definitions or None

    def _format_report_datetime(self, iso_str: Optional[str]) -> str:
        try:
            if not iso_str:
                raise ValueError("missing")
            dt = datetime.fromisoformat(iso_str)
        except (ValueError, TypeError):
            return "Unknown"
        return dt.astimezone(self.jst).strftime('%Y-%m-%d %H:%M JST')

    def _resolve_channel_display(self, channel_id: int) -> str:
        channel = self.bot.get_channel(channel_id)
        if channel:
            return channel.mention
        return f"<#{channel_id}>"

    def _reporter_unavailable_embed(self) -> discord.Embed:
        embed = discord.Embed(title="笶・Reporter Not Available / 繝ｬ繝昴・繧ｿ繝ｼ讖溯・縺悟茜逕ｨ縺ｧ縺阪∪縺帙ｓ",
                              description="ScheduledReporter plugin is not initialized. / ScheduledReporter繝励Λ繧ｰ繧､繝ｳ縺悟・譛溷喧縺輔ｌ縺ｦ縺・∪縺帙ｓ縲・,
                              color=discord.Color.red())
        self._add_support_footer(embed)
        return embed

    async def _get_conversation_thread_id(self, message: discord.Message) -> int:
        guild_id = message.guild.id if message.guild else 0  # DM縺ｮ蝣ｴ蜷医・0
        
        # 繧ｮ繝ｫ繝牙崋譛峨・霎樊嶌繧貞・譛溷喧
        if guild_id not in self.message_to_thread:
            self.message_to_thread[guild_id] = {}
        
        if message.id in self.message_to_thread[guild_id]: 
            return self.message_to_thread[guild_id][message.id]
        
        current_msg, visited_ids = message, set()
        while current_msg.reference and current_msg.reference.message_id:
            if current_msg.id in visited_ids: break
            visited_ids.add(current_msg.id)
            try:
                parent_msg = current_msg.reference.resolved or await message.channel.fetch_message(
                    current_msg.reference.message_id)
                if parent_msg.author != self.bot.user: break
                current_msg = parent_msg
            except (discord.NotFound, discord.HTTPException):
                break
        thread_id = current_msg.id
        self.message_to_thread[guild_id][message.id] = thread_id
        return thread_id

    async def _collect_conversation_history(self, message: discord.Message) -> List[Dict[str, Any]]:
        guild_id = message.guild.id if message.guild else 0  # DM縺ｮ蝣ｴ蜷医・0
        
        # 繧ｮ繝ｫ繝牙崋譛峨・莨夊ｩｱ螻･豁ｴ繧貞・譛溷喧
        if guild_id not in self.conversation_threads:
            self.conversation_threads[guild_id] = {}
        
        history = []
        current_msg = message
        visited_ids = set()
        max_depth = 50  # 辟｡髯舌Ν繝ｼ繝鈴亟豁｢
        
        # 繝ｪ繝励Λ繧､繝√ぉ繝ｼ繝ｳ繧帝■縺｣縺ｦ莨夊ｩｱ螻･豁ｴ繧貞庶髮・
        depth = 0
        while current_msg.reference and current_msg.reference.message_id and depth < max_depth:
            if current_msg.reference.message_id in visited_ids:
                break
            visited_ids.add(current_msg.reference.message_id)
            depth += 1
            
            try:
                # 蜿ら・繝｡繝・そ繝ｼ繧ｸ繧貞叙蠕・
                parent_msg = current_msg.reference.resolved
                if not parent_msg:
                    parent_msg = await message.channel.fetch_message(current_msg.reference.message_id)
                
                if isinstance(parent_msg, discord.DeletedReferencedMessage):
                    logger.debug(f"Encountered deleted referenced message in history collection.")
                    break
                
                # Bot縺ｮ繝｡繝・そ繝ｼ繧ｸ縺ｮ蝣ｴ蜷医∽ｿ晏ｭ倥＆繧後◆莨夊ｩｱ螻･豁ｴ縺九ｉ蜿門ｾ・
                if parent_msg.author == self.bot.user:
                    thread_id = await self._get_conversation_thread_id(parent_msg)
                    if thread_id in self.conversation_threads[guild_id]:
                        # 縺薙・繝｡繝・そ繝ｼ繧ｸID縺ｫ蟇ｾ蠢懊☆繧蟻ssistant繝｡繝・そ繝ｼ繧ｸ繧呈､懃ｴ｢
                        found_assistant = False
                        for msg in reversed(self.conversation_threads[guild_id][thread_id]):
                            if msg.get("role") == "assistant" and msg.get("message_id") == parent_msg.id:
                                history.append({"role": "assistant", "content": msg["content"]})
                                found_assistant = True
                                # 縺薙・assistant繝｡繝・そ繝ｼ繧ｸ繧医ｊ蜑阪・莨夊ｩｱ螻･豁ｴ繧ょ性繧√ｋ
                                thread_history = self.conversation_threads[guild_id][thread_id]
                                assistant_index = thread_history.index(msg)
                                # assistant繧医ｊ蜑阪・繝｡繝・そ繝ｼ繧ｸ繧定ｿｽ蜉・域凾邉ｻ蛻鈴・↓・・
                                for prev_msg in thread_history[:assistant_index]:
                                    history.append(prev_msg)
                                break
                        
                        if not found_assistant:
                            # 螻･豁ｴ縺ｫ縺ｪ縺・ｴ蜷医・縲√◎縺ｮ繝｡繝・そ繝ｼ繧ｸ縺ｮ蜀・ｮｹ繧堤峩謗･蜿門ｾ・
                            if parent_msg.content:
                                history.append({"role": "assistant", "content": parent_msg.content})
                
                # 繝ｦ繝ｼ繧ｶ繝ｼ縺ｮ繝｡繝・そ繝ｼ繧ｸ縺ｮ蝣ｴ蜷・
                elif parent_msg.author != self.bot.user:
                    image_contents, text_content = await self._prepare_multimodal_content(parent_msg)
                    text_content = text_content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
                    if text_content or image_contents:
                        user_content_parts = []
                        if text_content:
                            user_content_parts.append({
                                "type": "text",
                                "text": f"{parent_msg.created_at.astimezone(self.jst).strftime('[%H:%M]')} {text_content}"
                            })
                        user_content_parts.extend(image_contents)
                        history.append({"role": "user", "content": user_content_parts})
                
                # 隕ｪ繝｡繝・そ繝ｼ繧ｸ縺ｫ遘ｻ蜍・
                current_msg = parent_msg
                
            except (discord.NotFound, discord.HTTPException) as e:
                logger.debug(f"Could not fetch parent message: {e}")
                break
            except Exception as e:
                logger.error(f"Error collecting conversation history: {e}", exc_info=True)
                break
        
        # 莨夊ｩｱ螻･豁ｴ繧呈凾邉ｻ蛻鈴・↓荳ｦ縺ｳ譖ｿ縺茨ｼ亥商縺・ｂ縺ｮ縺九ｉ譁ｰ縺励＞繧ゅ・縺ｸ・・
        history.reverse()
        
        # 譛螟ｧ螻･豁ｴ謨ｰ縺ｧ蛻ｶ髯・
        max_history_entries = self.llm_config.get('max_messages', 10) * 2
        if len(history) > max_history_entries:
            history = history[-max_history_entries:]
        
        return history

    async def _process_image_url(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    image_bytes = await response.read()
                    if len(image_bytes) > 20 * 1024 * 1024:
                        logger.warning(f"Image too large ({len(image_bytes)} bytes): {url}")
                        return None
                    mime_type = response.content_type
                    if not mime_type or not mime_type.startswith('image/'):
                        ext = url.split('.')[-1].lower().split('?')
                        mime_type = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif',
                                     'webp': 'image/webp'}.get(ext, 'image/jpeg')
                    if mime_type == 'image/gif':
                        try:
                            from PIL import Image
                            gif_image = Image.open(io.BytesIO(image_bytes))
                            if getattr(gif_image, 'is_animated', False):
                                logger.info(
                                    f"汐 [IMAGE] Detected animated GIF. Converting to static image: {url[:100]}...")
                                gif_image.seek(0)
                                if gif_image.mode != 'RGBA': gif_image = gif_image.convert('RGBA')
                                output_buffer = io.BytesIO()
                                gif_image.save(output_buffer, format='PNG', optimize=True)
                                image_bytes, mime_type = output_buffer.getvalue(), 'image/png'
                                logger.debug(
                                    f"名・・[IMAGE] Converted animated GIF to PNG (Size: {len(image_bytes)} bytes)")
                            else:
                                logger.debug(f"名・・[IMAGE] Static GIF detected, processing normally")
                        except ImportError:
                            logger.warning(
                                "笞・・Pillow (PIL) library not found. Cannot process animated GIFs. Skipping image.")
                            return None
                        except Exception as gif_error:
                            logger.error(f"笶・Error processing GIF image: {gif_error}", exc_info=True)
                            return None
                    encoded_image = base64.b64encode(image_bytes).decode('utf-8')
                    logger.debug(
                        f"名・・[IMAGE] Successfully processed image: {url[:100]}... (MIME: {mime_type}, Size: {len(image_bytes)} bytes)")
                    return {"type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded_image}", "detail": "auto"}}
                else:
                    logger.warning(f"Failed to download image from {url} (Status: {response.status})")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"Timeout while downloading image: {url}")
            return None
        except Exception as e:
            logger.error(f"Error processing image URL {url}: {e}", exc_info=True)
            return None

    async def _prepare_multimodal_content(self, message: discord.Message) -> Tuple[List[Dict[str, Any]], str]:
        image_inputs, processed_urls, messages_to_scan, visited_ids, current_msg = [], set(), [], set(), message
        for i in range(5):
            if not current_msg or current_msg.id in visited_ids: break
            if isinstance(current_msg, discord.DeletedReferencedMessage): break
            messages_to_scan.append(current_msg)
            visited_ids.add(current_msg.id)
            if current_msg.reference and current_msg.reference.message_id:
                try:
                    current_msg = current_msg.reference.resolved or await message.channel.fetch_message(
                        current_msg.reference.message_id)
                except (discord.NotFound, discord.HTTPException):
                    break
            else:
                break
        source_urls, text_parts = [], []
        for msg in reversed(messages_to_scan):
            if msg.author != self.bot.user:
                if text_content_part := IMAGE_URL_PATTERN.sub('', msg.content).strip(): text_parts.append(
                    text_content_part)
            for url in IMAGE_URL_PATTERN.findall(msg.content):
                if url not in processed_urls: source_urls.append(url); processed_urls.add(url)
            for attachment in msg.attachments:
                if attachment.content_type and attachment.content_type.startswith(
                    'image/') and attachment.url not in processed_urls: source_urls.append(
                    attachment.url); processed_urls.add(attachment.url)
            for embed in msg.embeds:
                if embed.image and embed.image.url and embed.image.url not in processed_urls: source_urls.append(
                    embed.image.url); processed_urls.add(embed.image.url)
                if embed.thumbnail and embed.thumbnail.url and embed.thumbnail.url not in processed_urls: source_urls.append(
                    embed.thumbnail.url); processed_urls.add(embed.thumbnail.url)
        max_images = self.llm_config.get('max_images', 1)
        for url in source_urls[:max_images]:
            if image_data := await self._process_image_url(url): image_inputs.append(image_data)
        if len(source_urls) > max_images:
            try:
                await message.channel.send(self.llm_config.get('error_msg', {}).get('msg_max_image_size',
                                                                                    "笞・・Max images ({max_images}) reached.\n笞・・荳蠎ｦ縺ｫ蜃ｦ逅・〒縺阪ｋ逕ｻ蜒上・譛螟ｧ譫壽焚({max_images}譫・繧定ｶ・∴縺ｾ縺励◆縲・).format(
                    max_images=max_images), delete_after=10, silent=True)
            except discord.HTTPException:
                pass
        return image_inputs, "\n".join(text_parts)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        
        # 繧ｹ繝ｬ繝・ラ蜀・〒縺ｯBot縺ｮ繝｡繝・そ繝ｼ繧ｸ縺ｸ縺ｮ繝ｪ繝励Λ繧､縺ｮ縺ｿ縺ｫ蜿榊ｿ・
        is_thread = isinstance(message.channel, discord.Thread)
        is_mentioned = self.bot.user.mentioned_in(message) and not message.mention_everyone
        is_reply_to_bot = (message.reference and message.reference.resolved and 
                           isinstance(message.reference.resolved, discord.Message) and 
                           message.reference.resolved.author == self.bot.user)
        
        # 繝ｪ繝励Λ繧､縺ｮ蝣ｴ蜷医・繝｡繝ｳ繧ｷ繝ｧ繝ｳ蠢・医・壼ｸｸ繝√Ε繝ｳ繝阪Ν縺ｧ縺ｯ繝｡繝ｳ繧ｷ繝ｧ繝ｳ縺ｮ縺ｿ
        if is_reply_to_bot:
            # 繝ｪ繝励Λ繧､縺ｮ蝣ｴ蜷医・繝｡繝ｳ繧ｷ繝ｧ繝ｳ縺悟ｿ・ｦ・
            if not is_mentioned:
                return
        elif not is_mentioned:
            # 繝ｪ繝励Λ繧､縺ｧ縺ｪ縺・ｴ蜷医ｂ繝｡繝ｳ繧ｷ繝ｧ繝ｳ縺悟ｿ・ｦ・
            return
        try:
            llm_client = await self._get_llm_client_for_channel(message.channel.id)
            if not llm_client:
                # 菫ｮ豁｣轤ｹ・壹ョ繝輔か繝ｫ繝医・繧ｨ繝ｩ繝ｼ繝｡繝・そ繝ｼ繧ｸ繧剃ｸ蠎ｦ螟画焚縺ｫ譬ｼ邏阪☆繧・
                default_error_msg = 'LLM client is not available for this channel.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｧ縺ｯLLM繧ｯ繝ｩ繧､繧｢繝ｳ繝医′蛻ｩ逕ｨ縺ｧ縺阪∪縺帙ｓ縲・
                error_msg = self.llm_config.get('error_msg', {}).get('general_error', default_error_msg)

                await message.reply(
                    content=f"笶・**Error / 繧ｨ繝ｩ繝ｼ** 笶圭n\n{error_msg}",  # 菫ｮ豁｣轤ｹ・壼､画焚繧剃ｽｿ縺｣縺ｦf-string繧呈ｧ区・縺吶ｋ
                    view=self._create_support_view(), silent=True)
                return
        except Exception as e:
            logger.error(f"Failed to get LLM client for channel {message.channel.id}: {e}", exc_info=True)
            await message.reply(content=f"笶・**Error / 繧ｨ繝ｩ繝ｼ** 笶圭n\n{self.exception_handler.handle_exception(e)}",
                                view=self._create_support_view(), silent=True)
            return
        guild_log = f"guild='{message.guild.name}({message.guild.id})'" if message.guild else "guild='DM'"
        user_log = f"user='{message.author.name}({message.author.id})'"
        model_in_use = llm_client.model_name_for_api_calls
        image_contents, text_content = await self._prepare_multimodal_content(message)
        text_content = text_content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not text_content and not image_contents:
            error_key = 'empty_reply' if is_reply_to_bot and not is_mentioned else 'empty_mention_reply'
            await message.reply(content=self.llm_config.get('error_msg', {}).get(error_key,
                                                                                 "Please say something.\n菴輔°縺願ｩｱ縺励￥縺縺輔＞縲・ if error_key == 'empty_reply' else "Yes, how can I help you?\n縺ｯ縺・∽ｽ輔°蠕｡逕ｨ縺ｧ縺励ｇ縺・°?"),
                                view=self._create_support_view(), silent=True)
            return
        logger.info(
            f"鐙 Received LLM request | {guild_log} | {user_log} | model='{model_in_use}' | text_length={len(text_content)} chars | images={len(image_contents)}")
        if text_content: logger.info(
            f"[on_message] {message.guild.name if message.guild else 'DM'}({message.guild.id if message.guild else 0}),{message.author.name}({message.author.id})町 [USER_INPUT] {((text_content[:200] + '...') if len(text_content) > 203 else text_content).replace(chr(10), ' ')}")
        thread_id = await self._get_conversation_thread_id(message)
        system_prompt = await self._prepare_system_prompt(message.channel.id, message.author.id,
                                                          message.author.display_name)
        messages_for_api: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        conversation_history = await self._collect_conversation_history(message)
        messages_for_api.extend(conversation_history)
        # 險隱樊､懷・縺ｧ蜍慕噪縺ｫ險隱槭・繝ｭ繝ｳ繝励ヨ繧堤函謌舌＠縲√Θ繝ｼ繧ｶ繝ｼ繝｡繝・そ繝ｼ繧ｸ縺ｮ逶ｴ蜑阪↓驟咲ｽｮ
        lang_prompt = self._build_language_prompt(text_content)
        if lang_prompt:
            messages_for_api.append({"role": "system", "content": lang_prompt})
        user_content_parts = []
        if text_content: user_content_parts.append(
            {"type": "text", "text": f"{message.created_at.astimezone(self.jst).strftime('[%H:%M]')} {text_content}"})
        user_content_parts.extend(image_contents)
        if image_contents: logger.debug(f"Including {len(image_contents)} image(s) in request")
        user_message_for_api = {"role": "user", "content": user_content_parts}
        messages_for_api.append(user_message_for_api)
        logger.info(f"鳩 [API] Sending {len(messages_for_api)} messages to LLM")
        logger.debug(
            # FIX IS HERE
            f"Messages structure: system={len(messages_for_api[0]['content'])} chars, lang_override={'present' if len(messages_for_api) > 1 and 'CRITICAL' in str(messages_for_api) else 'absent'}")
        try:
            # 繧ｹ繝ｬ繝・ラ菴懈・繝懊ち繝ｳ縺ｯ蜑企勁・亥ｸｸ縺ｫFalse・・
            is_first_response = False
            sent_messages, llm_response, used_key_index = await self._handle_llm_streaming_response(message,
                                                                                                    messages_for_api,
                                                                                                    llm_client,
                                                                                                    is_first_response)
            if sent_messages and llm_response:
                logger.info(
                    f"笨・LLM response completed | model='{model_in_use}' | response_length={len(llm_response)} chars")
                log_response = (llm_response[:200] + '...') if len(llm_response) > 203 else llm_response
                key_log_str = f" [key{used_key_index + 1}]" if used_key_index is not None else ""
                logger.info(f"､・[LLM_RESPONSE]{key_log_str} {log_response.replace(chr(10), ' ')}")
                logger.debug(f"LLM full response (length: {len(llm_response)} chars):\n{llm_response}")
                guild_id = message.guild.id if message.guild else 0  # DM縺ｮ蝣ｴ蜷医・0
                
                # 繧ｮ繝ｫ繝牙崋譛峨・莨夊ｩｱ螻･豁ｴ繧貞・譛溷喧
                if guild_id not in self.conversation_threads:
                    self.conversation_threads[guild_id] = {}
                if thread_id not in self.conversation_threads[guild_id]: 
                    self.conversation_threads[guild_id][thread_id] = []
                
                self.conversation_threads[guild_id][thread_id].append(user_message_for_api)
                assistant_message = {"role": "assistant", "content": llm_response, "message_id": sent_messages[0].id}
                self.conversation_threads[guild_id][thread_id].append(assistant_message)
                for msg in sent_messages: 
                    guild_id_for_msg = msg.guild.id if msg.guild else 0
                    if guild_id_for_msg not in self.message_to_thread:
                        self.message_to_thread[guild_id_for_msg] = {}
                    self.message_to_thread[guild_id_for_msg][msg.id] = thread_id
                self._cleanup_old_threads()

                # TTS Cog縺ｫ繧ｫ繧ｹ繧ｿ繝繧､繝吶Φ繝医ｒ逋ｺ轣ｫ縺輔○繧・
                try:
                    self.bot.dispatch("llm_response_complete", sent_messages, llm_response)
                    logger.info("討 Dispatched 'llm_response_complete' event for TTS.")
                except Exception as e:
                    logger.error(f"Failed to dispatch 'llm_response_complete' event: {e}", exc_info=True)

        except Exception as e:
            await message.reply(content=f"笶・**Error / 繧ｨ繝ｩ繝ｼ** 笶圭n\n{self.exception_handler.handle_exception(e)}",
                                view=self._create_support_view(), silent=True)

    def _cleanup_old_threads(self):
        for guild_id in list(self.conversation_threads.keys()):
            guild_threads = self.conversation_threads[guild_id]
            if len(guild_threads) > 100:
                threads_to_remove = list(guild_threads.keys())[:len(guild_threads) - 100]
                for thread_id in threads_to_remove:
                    del guild_threads[thread_id]
                    if guild_id in self.message_to_thread:
                        self.message_to_thread[guild_id] = {
                            k: v for k, v in self.message_to_thread[guild_id].items() 
                            if v != thread_id
                        }

    async def _handle_llm_streaming_response(self, message: discord.Message, initial_messages: List[Dict[str, Any]],
                                             client: openai.AsyncOpenAI, is_first_response: bool = False) -> Tuple[
        Optional[List[discord.Message]], str, Optional[int]]:
        sent_message = None
        try:
            model_name = client.model_name_for_api_calls
            if self.tips_manager:
                waiting_embed = self.tips_manager.get_waiting_embed(model_name)
                try:
                    sent_message = await message.reply(embed=waiting_embed, silent=True)
                except discord.HTTPException:
                    sent_message = await message.channel.send(embed=waiting_embed, silent=True)
            else:
                waiting_message = f"-# :incoming_envelope: waiting response for '{model_name}' :incoming_envelope:"
                try:
                    sent_message = await message.reply(waiting_message, silent=True)
                except discord.HTTPException:
                    sent_message = await message.channel.send(waiting_message, silent=True)
            return await self._process_streaming_and_send_response(sent_message=sent_message, channel=message.channel,
                                                                   user=message.author,
                                                                   messages_for_api=initial_messages, llm_client=client,
                                                                   is_first_response=is_first_response)
        except Exception as e:
            logger.error(f"笶・Error during LLM streaming response: {e}", exc_info=True)
            error_msg = f"笶・**Error / 繧ｨ繝ｩ繝ｼ** 笶圭n\n{self.exception_handler.handle_exception(e)}"
            if sent_message:
                try:
                    await sent_message.edit(content=error_msg, embed=None, view=self._create_support_view())
                except discord.HTTPException:
                    pass
            else:
                await message.reply(content=error_msg, view=self._create_support_view(), silent=True)
            return None, "", None

    async def _process_streaming_and_send_response(self, sent_message: discord.Message,
                                                   channel: discord.abc.Messageable,
                                                   user: Union[discord.User, discord.Member],
                                                   messages_for_api: List[Dict[str, Any]],
                                                   llm_client: openai.AsyncOpenAI,
                                                   is_first_response: bool = False) -> Tuple[
        Optional[List[discord.Message]], str, Optional[int]]:
        full_response_text, last_update, last_displayed_length, chunk_count = "", 0.0, 0, 0
        update_interval, min_update_chars, retry_sleep_time = 0.5, 15, 2.0
        emoji_prefix, emoji_suffix = ":incoming_envelope: ", " :incoming_envelope:"
        max_final_retries, final_retry_delay = 3, 2.0
        is_first_update = True
        logger.debug(f"Starting LLM stream for message {sent_message.id}")
        stream_generator = self._llm_stream_and_tool_handler(messages_for_api, llm_client, channel.id, user.id)
        async for content_chunk in stream_generator:
            if not content_chunk:
                continue
            chunk_count += 1
            full_response_text += content_chunk
            if chunk_count % 100 == 0: logger.debug(
                f"Stream chunk #{chunk_count}, total length: {len(full_response_text)} chars")
            current_time, chars_accumulated = time.time(), len(full_response_text) - last_displayed_length

            should_update = is_first_update or (
                    current_time - last_update > update_interval and chars_accumulated >= min_update_chars)

            if should_update and full_response_text:
                is_first_update = False
                display_length = len(full_response_text)
                if display_length > SAFE_MESSAGE_LENGTH:
                    display_text = f"{emoji_prefix}{full_response_text[:SAFE_MESSAGE_LENGTH - len(emoji_prefix) - len(emoji_suffix) - 100]}\n\n笞・・(Output is long, will be split...)\n笞・・(蜃ｺ蜉帙′髟ｷ縺・◆繧∝・蜑ｲ縺励∪縺・..){emoji_suffix}"
                else:
                    display_text = f"{emoji_prefix}{full_response_text[:SAFE_MESSAGE_LENGTH - len(emoji_prefix) - len(emoji_suffix)]}{emoji_suffix}"
                if display_text != sent_message.content:
                    try:
                        await sent_message.edit(content=display_text)
                        last_update, last_displayed_length = current_time, len(full_response_text)
                        logger.debug(f"Updated Discord message (displayed: {len(display_text)} chars)")
                    except discord.NotFound:
                        logger.warning(f"笞・・Message deleted during stream (ID: {sent_message.id}). Aborting.")
                        return None, "", None
                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = (e.retry_after or 1.0) + 0.5
                            logger.warning(
                                f"笞・・Rate limited on message edit (ID: {sent_message.id}). Waiting {retry_after:.2f}s")
                            await asyncio.sleep(retry_after)
                            last_update = time.time()
                        else:
                            logger.warning(
                                f"笞・・Failed to edit message (ID: {sent_message.id}): {e.status} - {getattr(e, 'text', str(e))}")
                            await asyncio.sleep(retry_sleep_time)
        logger.debug(f"Stream completed | Total chunks: {chunk_count} | Final length: {len(full_response_text)} chars")
        if full_response_text:
            if len(full_response_text) <= SAFE_MESSAGE_LENGTH:
                # 繧ｹ繝ｬ繝・ラ菴懈・繝懊ち繝ｳ縺ｯ蜑企勁
                view = None
                
                for attempt in range(max_final_retries):
                    try:
                        if full_response_text != sent_message.content:
                            await sent_message.edit(content=full_response_text, embed=None, view=view)
                        logger.debug(f"Final message updated successfully (attempt {attempt + 1})")
                        break
                    except discord.NotFound:
                        logger.error(f"笶・Message was deleted before final update")
                        return None, "", None
                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = (e.retry_after or 1.0) + 0.5
                            logger.warning(
                                f"笞・・Rate limited on final update (attempt {attempt + 1}/{max_final_retries}). Waiting {retry_after:.2f}s")
                            await asyncio.sleep(retry_after)
                        else:
                            logger.warning(
                                f"笞・・Failed to update final message (attempt {attempt + 1}/{max_final_retries}): {e.status} - {getattr(e, 'text', str(e))}")
                            if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                return [sent_message], full_response_text, getattr(llm_client, 'last_used_key_index', None)
            else:
                logger.debug(f"Response is {len(full_response_text)} chars, splitting into multiple messages")
                # 菫ｮ豁｣: 繧ｿ繝励Ν菴懈・縺ｮ繝舌げ繧剃ｿｮ豁｣
                chunks = _split_message_smartly(full_response_text, SAFE_MESSAGE_LENGTH)
                all_messages = []
                first_chunk = chunks[0]  # 譛蛻昴・繝√Ε繝ｳ繧ｯ繧貞叙蠕・

                # 繧ｹ繝ｬ繝・ラ菴懈・繝懊ち繝ｳ縺ｯ蜑企勁
                view = None

                for attempt in range(max_final_retries):
                    try:
                        await sent_message.edit(content=first_chunk, embed=None, view=view)
                        all_messages.append(sent_message)
                        logger.debug(f"Updated first message (1/{len(chunks)})")
                        break
                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = (e.retry_after or 1.0) + 0.5
                            logger.warning(f"笞・・Rate limited on first chunk update, waiting {retry_after:.2f}s")
                            await asyncio.sleep(retry_after)
                        else:
                            logger.error(f"笶・Failed to update first message: {e}")
                            if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                for i, chunk in enumerate(chunks[1:], start=2):
                    for attempt in range(max_final_retries):
                        try:
                            continuation_msg = await channel.send(chunk)
                            all_messages.append(continuation_msg)
                            logger.debug(f"Sent continuation message {i}/{len(chunks)}")
                            break
                        except discord.HTTPException as e:
                            if e.status == 429:
                                retry_after = (e.retry_after or 1.0) + 0.5
                                logger.warning(f"笞・・Rate limited on continuation {i}, waiting {retry_after:.2f}s")
                                await asyncio.sleep(retry_after)
                            else:
                                logger.error(f"笶・Failed to send continuation message {i}: {e}")
                                if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                return all_messages, full_response_text, getattr(llm_client, 'last_used_key_index', None)
        else:
            finish_reason = getattr(llm_client, 'last_finish_reason', None)
            if finish_reason == 'content_filter':
                error_msg = self.llm_config.get('error_msg', {}).get('content_filter_error',
                                                                     "The response was blocked by the content filter.\nAI縺ｮ蠢懃ｭ斐′繧ｳ繝ｳ繝・Φ繝・ヵ繧｣繝ｫ繧ｿ繝ｼ縺ｫ繧医▲縺ｦ繝悶Ο繝・け縺輔ｌ縺ｾ縺励◆縲・);
                logger.warning(
                    f"笞・・Empty response from LLM due to content filter.")
            else:
                error_msg = self.llm_config.get('error_msg', {}).get('empty_response_error',
                                                                     "There was no response from the AI. Please try rephrasing your message.\nAI縺九ｉ蠢懃ｭ斐′縺ゅｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲り｡ｨ迴ｾ繧貞､峨∴縺ｦ繧ゅ≧荳蠎ｦ縺願ｩｦ縺励￥縺縺輔＞縲・);
                logger.warning(
                    f"笞・・Empty response from LLM (Finish reason: {finish_reason})")
            await sent_message.edit(content=f"笶・**Error / 繧ｨ繝ｩ繝ｼ** 笶圭n\n{error_msg}", embed=None,
                                    view=self._create_support_view())
            return None, "", None

    def _convert_messages_for_gemini(self, messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
        system_prompts_content, other_messages, has_system_message = [], [], False
        for message in messages:
            if message.get("role") == "system":
                if isinstance(message.get("content"), str) and message["content"].strip():
                    system_prompts_content.append(message["content"])
                    has_system_message = True
            else:
                other_messages.append(message)
        if not has_system_message: return messages, ""
        combined_system_prompt = "\n\n".join(system_prompts_content)
        converted_messages = [{"role": "user", "content": combined_system_prompt},
                              {"role": "assistant", "content": "謇ｿ遏･縺・◆縺励∪縺励◆縲よ欠遉ｺ縺ｫ蠕薙＞縺ｾ縺吶・}]
        converted_messages.extend(other_messages)
        return converted_messages, combined_system_prompt

    async def _llm_stream_and_tool_handler(self, messages: List[Dict[str, Any]], client: openai.AsyncOpenAI,
                                           channel_id: int, user_id: int) -> AsyncGenerator[str, None]:
        model_string = self.channel_models.get(str(channel_id)) or self.llm_config.get('model')
        is_gemini = model_string and 'gemini' in model_string.lower()

        if is_gemini:
            original_messages_for_log = messages
            messages, combined_system_prompt = self._convert_messages_for_gemini(messages)
            if combined_system_prompt:
                logger.info(f"売 [GEMINI ADAPTER] Converting system prompts for Gemini model '{model_string}'.")
                logger.debug(
                    f"  - Combined system prompt ({len(combined_system_prompt)} chars): {combined_system_prompt.replace(chr(10), ' ')[:300]}...")
                logger.debug(f"  - Message count changed: {len(original_messages_for_log)} -> {len(messages)}")

        current_messages = messages.copy()
        max_iterations = self.llm_config.get('max_tool_iterations', 5)
        extra_params = self.llm_config.get('extra_api_parameters', {})

        provider_name = getattr(client, 'provider_name', None)
        if not provider_name:
            if model_string and '/' in model_string:
                provider_name = model_string.split('/', 1)[0]
                logger.debug(
                    "Provider name missing on client; inferring from model string as '%s'", provider_name
                )
            else:
                provider_name = "unknown"
                logger.warning(
                    "Provider name missing on client and could not be inferred from model string."
                )
            client.provider_name = provider_name

        for iteration in range(max_iterations):
            logger.debug(f"Starting LLM API call (iteration {iteration + 1}/{max_iterations})")
            tools_def = self.get_tools_definition()

            api_kwargs = {
                "model": client.model_name_for_api_calls,
                "messages": current_messages,
                "stream": True,
                "temperature": extra_params.get('temperature', 0.7),
                "max_tokens": extra_params.get('max_tokens', 4096)
            }

            # 笨・Gemini 縺ｧ繧・tools 繧呈ｭ｣縺励￥貂｡縺・
            # KoboldCPP縺ｮ蝣ｴ蜷医・繝・・繝ｫ繧ｵ繝昴・繝医ｒ繝√ぉ繝・け
            is_koboldcpp = provider_name.lower() == 'koboldcpp'
            supports_tools = getattr(client, 'supports_tools', True)
            
            if tools_def and supports_tools:
                api_kwargs["tools"] = tools_def
                # Gemini繝｢繝・Ν縺ｧ縺ｯ tool_choice 繝代Λ繝｡繝ｼ繧ｿ繧定ｨｭ螳壹＠縺ｪ縺・
                # Gemini縺ｯ tool_choice 繧偵し繝昴・繝医＠縺ｦ縺・↑縺・°縲∫焚縺ｪ繧句ｽ｢蠑上ｒ隕∵ｱゅ☆繧句庄閭ｽ諤ｧ縺後≠繧・
                if not is_gemini:
                    api_kwargs["tool_choice"] = "auto"
                else:
                    logger.debug(f"肌 [GEMINI] Skipping tool_choice parameter for Gemini model")
                # Safely get tool names, handling cases where the structure might be different
                tool_names = []
                for t in tools_def:
                    try:
                        if isinstance(t, dict):
                            if 'function' in t and isinstance(t['function'], dict):
                                tool_names.append(t['function'].get('name', 'unnamed_function'))
                            elif 'name' in t:
                                tool_names.append(t['name'])
                            else:
                                tool_names.append('unnamed_tool')
                        else:
                            tool_names.append(str(t))
                    except Exception as e:
                        logger.warning(f"笞・・[TOOLS] Error processing tool: {e}")
                        tool_names.append('error_processing_tool')
                
                logger.info(f"肌 [TOOLS] Passing {len(tools_def)} tools to API: {tool_names}")
                if is_koboldcpp:
                    logger.info(f"肌 [KoboldCPP] Tools are enabled for this model")
                if is_gemini:
                    logger.info(f"肌 [GEMINI] Tools are enabled for Gemini model (without tool_choice)")
            elif tools_def and not supports_tools:
                logger.warning(
                    f"笞・・[TOOLS] Tools are disabled for provider '{provider_name}' (supports_tools=false). Skipping tools.")
                if is_koboldcpp:
                    logger.warning(
                        f"笞・・[KoboldCPP] This KoboldCPP model may not support tools. Consider enabling 'supports_tools: true' in config if the model supports it.")
            else:
                logger.warning(f"笞・・[TOOLS] No tools available to pass to API")

            stream = None
            api_keys = self.provider_api_keys.get(client.provider_name, [])
            num_keys = len(api_keys)

            if num_keys == 0:
                raise Exception(f"No API keys available for provider {provider_name}")

            for attempt in range(num_keys):
                try:
                    current_key_index = self.provider_key_index.get(provider_name, 0)
                    client.last_used_key_index = current_key_index
                    logger.debug(
                        f"Attempting API call to '{provider_name}' with key index {current_key_index} (Attempt {attempt + 1}/{num_keys}).")
                    stream = await client.chat.completions.create(**api_kwargs)
                    logger.debug(f"Stream connection established successfully.")
                    break
                except (openai.RateLimitError, openai.InternalServerError) as e:
                    error_type = "Rate limit" if isinstance(e, openai.RateLimitError) else "Server"
                    status_code = getattr(e, 'status_code', 'N/A')
                    logger.warning(
                        f"笞・・{error_type} error ({status_code}) for provider '{provider_name}' with key index {current_key_index}. Details: {e}")
                    if attempt + 1 >= num_keys:
                        error_msg = f"Tried {num_keys} API key(s), but no response was received."
                        logger.error(f"笶・{error_msg} Provider: '{provider_name}'")
                        raise Exception(error_msg)
                    next_key_index = (current_key_index + 1) % num_keys
                    self.provider_key_index[provider_name] = next_key_index
                    next_key = api_keys[next_key_index]
                    logger.info(
                        f"売 Switching to next API key for provider '{provider_name}' (index: {next_key_index}) and retrying.")
                    provider_config = self.llm_config.get('providers', {}).get(provider_name, {})
                    is_koboldcpp = provider_name.lower() == 'koboldcpp'
                    timeout = provider_config.get('timeout', 300.0) if is_koboldcpp else None
                    new_client = openai.AsyncOpenAI(base_url=client.base_url, api_key=next_key, timeout=timeout)
                    new_client.model_name_for_api_calls = client.model_name_for_api_calls
                    new_client.provider_name = client.provider_name
                    # KoboldCPP繝｡繧ｿ繝・・繧ｿ繧剃ｿ晄戟
                    if is_koboldcpp:
                        new_client.supports_tools = getattr(client, 'supports_tools', provider_config.get('supports_tools', True))
                    else:
                        new_client.supports_tools = getattr(client, 'supports_tools', True)
                    client = new_client
                    self.llm_clients[f"{provider_name}/{client.model_name_for_api_calls}"] = new_client
                    await asyncio.sleep(1)
                except (openai.BadRequestError, openai.APIStatusError) as e:
                    status_code = getattr(e, 'status_code', None)
                    # 繝・ヰ繝・げ逕ｨ縺ｫapi_kwargs縺ｮ蜀・ｮｹ繧偵Ο繧ｰ縺ｫ險倬鹸・域ｩ溷ｯ・ュ蝣ｱ繧帝勁螟厄ｼ・
                    debug_kwargs = {k: v for k, v in api_kwargs.items() if k != 'messages'}
                    debug_kwargs['messages_count'] = len(api_kwargs.get('messages', []))
                    if 'messages' in api_kwargs and api_kwargs['messages']:
                        # 譛蛻昴→譛蠕後・繝｡繝・そ繝ｼ繧ｸ縺ｮ讎りｦ√・縺ｿ險倬鹸
                        first_msg = api_kwargs['messages'][0]
                        last_msg = api_kwargs['messages'][-1]
                        debug_kwargs['first_message'] = {
                            'role': first_msg.get('role'),
                            'content_preview': str(first_msg.get('content', ''))[:100] if isinstance(first_msg.get('content'), str) else type(first_msg.get('content')).__name__
                        }
                        debug_kwargs['last_message'] = {
                            'role': last_msg.get('role'),
                            'content_preview': str(last_msg.get('content', ''))[:100] if isinstance(last_msg.get('content'), str) else type(last_msg.get('content')).__name__
                        }
                    logger.error(f"笶・[API ERROR] Provider: '{provider_name}', Status: {status_code}")
                    logger.error(f"笶・[API ERROR] Request parameters: {debug_kwargs}")
                    logger.error(f"笶・[API ERROR] Full error: {e}")
                    
                    if isinstance(status_code, int) and status_code >= 500:
                        logger.warning(
                            f"笞・・Server-like status error ({status_code}) for provider '{provider_name}' with key index {current_key_index}. Details: {e}")
                    elif isinstance(status_code, int) and status_code >= 400:
                        logger.warning(
                            f"笞・・Client error ({status_code}) for provider '{provider_name}' with key index {current_key_index}. Details: {e}")
                    else:
                        logger.warning(
                            f"笞・・Bad request/API status error for provider '{provider_name}' with key index {current_key_index}. Details: {e}")

                    if attempt + 1 >= num_keys:
                        error_msg = f"Tried {num_keys} API key(s), but no response was received."
                        logger.error(f"笶・{error_msg} Provider: '{provider_name}'")
                        raise Exception(error_msg)

                    next_key_index = (current_key_index + 1) % num_keys
                    self.provider_key_index[provider_name] = next_key_index
                    next_key = api_keys[next_key_index]
                    logger.info(
                        f"売 Switching to next API key for provider '{provider_name}' (index: {next_key_index}) after error and retrying.")
                    provider_config = self.llm_config.get('providers', {}).get(provider_name, {})
                    is_koboldcpp = provider_name.lower() == 'koboldcpp'
                    timeout = provider_config.get('timeout', 300.0) if is_koboldcpp else None
                    new_client = openai.AsyncOpenAI(base_url=client.base_url, api_key=next_key, timeout=timeout)
                    new_client.model_name_for_api_calls = client.model_name_for_api_calls
                    new_client.provider_name = client.provider_name
                    if is_koboldcpp:
                        new_client.supports_tools = getattr(client, 'supports_tools', provider_config.get('supports_tools', True))
                    else:
                        new_client.supports_tools = getattr(client, 'supports_tools', True)
                    client = new_client
                    self.llm_clients[f"{provider_name}/{client.model_name_for_api_calls}"] = new_client
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"笶・Unhandled error calling LLM API: {e}", exc_info=True)
                    raise

            if stream is None:
                error_msg = f"Tried {num_keys} API key(s), but no response was received."
                logger.error(f"笶・{error_msg} Provider: '{provider_name}'")
                raise Exception(error_msg)

            tool_calls_buffer = []
            assistant_response_content = ""
            finish_reason = None

            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta and delta.content:
                    assistant_response_content += delta.content
                    yield delta.content
                if delta and delta.tool_calls:
                    for tool_call_chunk in delta.tool_calls:
                        chunk_index = tool_call_chunk.index if tool_call_chunk.index is not None else 0
                        if len(tool_calls_buffer) <= chunk_index:
                            tool_calls_buffer.append(
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        buffer = tool_calls_buffer[chunk_index]
                        if tool_call_chunk.id:
                            buffer["id"] = tool_call_chunk.id
                        if tool_call_chunk.function:
                            if tool_call_chunk.function.name:
                                buffer["function"]["name"] = tool_call_chunk.function.name
                            if tool_call_chunk.function.arguments:
                                buffer["function"]["arguments"] += tool_call_chunk.function.arguments

            client.last_finish_reason = finish_reason
            assistant_message = {"role": "assistant", "content": assistant_response_content or None}
            if tool_calls_buffer:
                assistant_message["tool_calls"] = tool_calls_buffer
            current_messages.append(assistant_message)

            if not tool_calls_buffer:
                logger.debug(f"No tool calls, returning final response (Finish reason: {finish_reason})")
                return

            logger.info(f"肌 [TOOL] LLM requested {len(tool_calls_buffer)} tool call(s)")
            for tc in tool_calls_buffer:
                logger.debug(
                    f"Tool call details: {tc['function']['name']} with args: {tc['function']['arguments'][:200]}")

            tool_calls_obj = [
                SimpleNamespace(
                    id=tc['id'],
                    function=SimpleNamespace(
                        name=tc['function']['name'],
                        arguments=tc['function']['arguments']
                    )
                ) for tc in tool_calls_buffer
            ]
            await self._process_tool_calls(tool_calls_obj, current_messages, channel_id, user_id)

        logger.warning(f"笞・・Tool processing exceeded max iterations ({max_iterations})")
        yield self.llm_config.get('error_msg', {}).get('tool_loop_timeout',
                                                       "Tool processing exceeded max iterations.\n繝・・繝ｫ縺ｮ蜃ｦ逅・′譛螟ｧ蜿榊ｾｩ蝗樊焚繧定ｶ・∴縺ｾ縺励◆.")

    async def _process_tool_calls(self, tool_calls: List[Any], messages: List[Dict[str, Any]], channel_id: int,
                                  user_id: int) -> None:
        for tool_call in tool_calls:
            raw_function_name = tool_call.function.name
            error_content = None
            tool_response_content = ""
            search_result = None
            function_args = {}

            # 笨・Gemini 縺ｮ "default_api.search" 竊・"search" 縺ｫ豁｣隕丞喧
            function_name = raw_function_name.split('.')[-1] if '.' in raw_function_name else raw_function_name

            try:
                function_args = json.loads(tool_call.function.arguments)
                logger.info(f"肌 [TOOL] Executing {raw_function_name} (normalized: {function_name})")
                logger.debug(f"肌 [TOOL] Arguments: {json.dumps(function_args, ensure_ascii=False, indent=2)}")

                if self.search_agent and function_name == self.search_agent.name:
                    search_result = await self.search_agent.run(arguments=function_args, bot=self.bot,
                                                                channel_id=channel_id)
                    # search_result縺ｯresponse繧ｪ繝悶ず繧ｧ繧ｯ繝医∪縺溘・譁・ｭ怜・
                    if hasattr(search_result, 'text'):
                        # 繝ｬ繧ｹ繝昴Φ繧ｹ繧ｪ繝悶ず繧ｧ繧ｯ繝医°繧峨ユ繧ｭ繧ｹ繝医ｒ蜿門ｾ・
                        tool_response_content = search_result.text
                    else:
                        # 譁・ｭ怜・縺ｮ蝣ｴ蜷茨ｼ医ヵ繧ｩ繝ｼ繝ｫ繝舌ャ繧ｯ・・
                        tool_response_content = str(search_result)
                    logger.debug(
                        f"肌 [TOOL] Result (length: {len(str(tool_response_content))} chars):\n{str(tool_response_content)[:1000]}")
                elif self.image_generator and function_name == self.image_generator.name:
                    tool_response_content = await self.image_generator.run(arguments=function_args,
                                                                           channel_id=channel_id)
                    logger.debug(f"肌 [TOOL] Result:\n{tool_response_content}")
                elif self.command_agent and function_name == self.command_agent.name:
                    logger.info(f"肌 [TOOL] CommandAgent called with arguments: {function_args}")
                    tool_response_content = await self.command_agent.run(arguments=function_args,
                                                                          bot=self.bot,
                                                                          channel_id=channel_id,
                                                                          user_id=user_id)
                    logger.info(f"肌 [TOOL] CommandAgent result: {tool_response_content[:200] if tool_response_content else 'None'}...")
                    logger.debug(f"肌 [TOOL] Full result:\n{tool_response_content}")
                elif self.deep_research_agent and function_name == self.deep_research_agent.name:
                    tool_response_content = await self.deep_research_agent.run_tool(arguments=function_args,
                                                                                   channel_id=channel_id)
                    logger.debug(f"肌 [TOOL] DeepResearch result (len={len(tool_response_content)}):\n{tool_response_content[:500]}")
                else:
                    logger.warning(f"笞・・Unsupported tool called: {raw_function_name} (normalized: {function_name})")
                    error_content = f"Error: Tool '{function_name}' is not available."
            except json.JSONDecodeError as e:
                logger.error(f"笶・Error decoding tool arguments for {function_name}: {e}", exc_info=True)
                error_content = f"Error: Invalid JSON arguments - {str(e)}"
            except SearchAPIRateLimitError as e:
                logger.warning(f"笞・・SearchAgent rate limit hit: {e}")
                error_content = "[Google Search Error]\nThe Google Search API rate limit has been reached. Please tell the user to try again later."
            except SearchAPIServerError as e:
                logger.error(f"笶・SearchAgent server error: {e}")
                error_content = "[Google Search Error]\nA temporary server error occurred with the search service. Please tell the user to try again later."
            except SearchAgentError as e:
                logger.error(f"笶・Error during SearchAgent execution for {function_name}: {e}", exc_info=True)
                error_content = f"[Google Search Error]\nAn error occurred during the search execution: {str(e)}"
            except Exception as e:
                logger.error(f"笶・Unexpected error during tool call for {function_name}: {e}", exc_info=True)
                error_content = f"[Tool Error]\nAn unexpected error occurred: {str(e)}"

            final_content = error_content if error_content else tool_response_content
            logger.debug(f"肌 [TOOL] Sending tool response back to LLM (length: {len(final_content)} chars)")
            messages.append(
                {"tool_call_id": tool_call.id, "role": "tool", "name": function_name, "content": final_content})
            
            # 讀懃ｴ｢縺梧・蜉溘＠縲√Ξ繧ｹ繝昴Φ繧ｹ繧ｪ繝悶ず繧ｧ繧ｯ繝医′蟄伜惠縺吶ｋ蝣ｴ蜷医√た繝ｼ繧ｹ繧弾mbed縺ｧ陦ｨ遉ｺ
            if search_result and hasattr(search_result, 'candidates'):
                await self._send_search_sources_embed(search_result, channel_id, function_args.get('query', ''))

    async def _send_search_sources_embed(self, response, channel_id: int, query: str) -> None:
        """讀懃ｴ｢邨先棡縺ｮ繧ｽ繝ｼ繧ｹ繧弾mbed縺ｧ陦ｨ遉ｺ"""
        try:
            # 繝√Ε繝ｳ繝阪Ν繧貞叙蠕・
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"Channel {channel_id} not found")
                return

            # 繝ｬ繧ｹ繝昴Φ繧ｹ縺九ｉ蠑慕畑諠・ｱ繧呈歓蜃ｺ
            sources = []
            try:
                # candidates縺九ｉgrounding metadata繧貞叙蠕・
                for candidate in response.candidates:
                    if hasattr(candidate, 'grounding_metadata'):
                        grounding = candidate.grounding_metadata
                        if hasattr(grounding, 'grounding_chunks') and grounding.grounding_chunks:
                            for chunk in grounding.grounding_chunks:
                                if hasattr(chunk, 'web'):
                                    web_info = chunk.web
                                    if hasattr(web_info, 'uri'):
                                        sources.append({
                                            'uri': web_info.uri,
                                            'title': getattr(web_info, 'title', ''),
                                        })
            except Exception as e:
                logger.error(f"Error extracting search sources: {e}", exc_info=True)
                return

            # 繧ｽ繝ｼ繧ｹ縺瑚ｦ九▽縺九ｉ縺ｪ縺・ｴ蜷医・菴輔ｂ縺励↑縺・
            if not sources:
                logger.debug("No sources found in search response")
                return

            # Embed繧剃ｽ懈・縺励※騾∽ｿ｡
            embed = discord.Embed(
                title="答 Search Sources / 讀懃ｴ｢繧ｽ繝ｼ繧ｹ",
                description=f"**Query / 繧ｯ繧ｨ繝ｪ:** {query}",
                color=discord.Color.blue()
            )

            # 繧ｽ繝ｼ繧ｹ繧呈怙螟ｧ10蛟玖｡ｨ遉ｺ
            sources_text = ""
            for i, source in enumerate(sources[:10], 1):
                title = source.get('title', 'No Title') or 'No Title'
                uri = source.get('uri', '')
                if len(title) > 50:
                    title = title[:47] + "..."
                sources_text += f"{i}. [{title}]({uri})\n"

            if sources_text:
                embed.description += f"\n\n**Sources / 繧ｽ繝ｼ繧ｹ荳隕ｧ:**\n{sources_text}"

            # 繧ｵ繝昴・繝医ヵ繝・ち繝ｼ繧定ｿｽ蜉
            self._add_support_footer(embed)

            # 繝｡繝・そ繝ｼ繧ｸ繧帝∽ｿ｡
            await channel.send(embed=embed, silent=True)
            logger.info(f"笨・Search sources embed sent to channel {channel_id}")

        except Exception as e:
            logger.error(f"Error sending search sources embed: {e}", exc_info=True)

    async def _schedule_model_reset(self, channel_id: int):
        try:
            await asyncio.sleep(3 * 60 * 60)
            logger.info(f"Executing scheduled model reset for channel {channel_id}.")
            channel_id_str = str(channel_id)
            if channel_id_str in self.channel_models:
                default_model, current_model = self.llm_config.get('model'), self.channel_models.get(channel_id_str)
                if current_model and current_model != default_model:
                    del self.channel_models[channel_id_str]
                    await self._save_channel_models()
                    logger.info(f"Model for channel {channel_id} automatically reset to default '{default_model}'.")
                    channel = self.bot.get_channel(channel_id)
                    if channel and isinstance(channel, discord.TextChannel):
                        try:
                            embed = discord.Embed(title="邃ｹ・・AI Model Reset / AI繝｢繝・Ν繧偵Μ繧ｻ繝・ヨ縺励∪縺励◆",
                                                  description=f"The AI model for this channel has been reset to the default (`{default_model}`) after 3 hours.\n3譎る俣縺檎ｵ碁℃縺励◆縺溘ａ縲√％縺ｮ繝√Ε繝ｳ繝阪Ν縺ｮAI繝｢繝・Ν繧偵ョ繝輔か繝ｫ繝・(`{default_model}`) 縺ｫ謌ｻ縺励∪縺励◆縲・,
                                                  color=discord.Color.blue())
                            self._add_support_footer(embed)
                            await channel.send(embed=embed, view=self._create_support_view())
                        except discord.HTTPException as e:
                            logger.warning(f"Failed to send model reset notification to channel {channel_id}: {e}")
        except asyncio.CancelledError:
            logger.info(f"Model reset task for channel {channel_id} was cancelled.")
        except Exception as e:
            logger.error(f"An error occurred in the model reset task for channel {channel_id}: {e}", exc_info=True)
        finally:
            self.model_reset_tasks.pop(channel_id, None)

    @app_commands.command(name="chat",
                          description="Chat with the AI without needing to mention.\nAI縺ｨ蟇ｾ隧ｱ縺励∪縺吶ゅΓ繝ｳ繧ｷ繝ｧ繝ｳ荳崎ｦ√〒莨夊ｩｱ縺ｧ縺阪∪縺吶・)
    @app_commands.describe(message="The message you want to send to the AI.\nAI縺ｫ騾∽ｿ｡縺励◆縺・Γ繝・そ繝ｼ繧ｸ",
                           image_url="URL of an image (optional).\n逕ｻ蜒上・URL・医が繝励す繝ｧ繝ｳ・・)
    async def chat_slash(self, interaction: discord.Interaction, message: str, image_url: str = None):
        await interaction.response.defer(ephemeral=False)
        temp_message = None
        try:
            llm_client = await self._get_llm_client_for_channel(interaction.channel_id)
            if not llm_client:
                # 菫ｮ豁｣轤ｹ・壹ョ繝輔か繝ｫ繝医・繧ｨ繝ｩ繝ｼ繝｡繝・そ繝ｼ繧ｸ繧剃ｸ蠎ｦ螟画焚縺ｫ譬ｼ邏阪☆繧・
                default_error_msg = 'LLM client is not available for this channel.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｧ縺ｯLLM繧ｯ繝ｩ繧､繧｢繝ｳ繝医′蛻ｩ逕ｨ縺ｧ縺阪∪縺帙ｓ縲・
                error_msg = self.llm_config.get('error_msg', {}).get('general_error', default_error_msg)

                await interaction.followup.send(
                    content=f"笶・**Error / 繧ｨ繝ｩ繝ｼ** 笶圭n\n{error_msg}",  # 菫ｮ豁｣轤ｹ・壼､画焚繧剃ｽｿ縺｣縺ｦf-string繧呈ｧ区・縺吶ｋ
                    view=self._create_support_view())
                return
            if not message.strip():
                await interaction.followup.send(
                    content="笞・・**Input Required / 蜈･蜉帙′蠢・ｦ√〒縺・* 笞・十n\nPlease enter a message.\n繝｡繝・そ繝ｼ繧ｸ繧貞・蜉帙＠縺ｦ縺上□縺輔＞縲・,
                    view=self._create_support_view())
                return
            model_in_use, image_contents = llm_client.model_name_for_api_calls, []
            if image_url:
                if image_data := await self._process_image_url(image_url):
                    image_contents.append(image_data)
                else:
                    await interaction.followup.send(
                        content="笞・・**Image Error / 逕ｻ蜒上お繝ｩ繝ｼ** 笞・十n\nFailed to process the specified image URL.\n謖・ｮ壹＆繧後◆逕ｻ蜒酋RL縺ｮ蜃ｦ逅・↓螟ｱ謨励＠縺ｾ縺励◆縲・,
                        view=self._create_support_view())
                    return
            guild_log, user_log = f"guild='{interaction.guild.name}({interaction.guild.id})'" if interaction.guild else "guild='DM'", f"user='{interaction.user.name}({interaction.user.id})'"
            logger.info(
                f"鐙 Received /chat request | {guild_log} | {user_log} | model='{model_in_use}' | text_length={len(message)} chars | images={len(image_contents)}")
            logger.info(
                f"[/chat] {interaction.guild.name if interaction.guild else 'DM'}({interaction.guild.id if interaction.guild else 0}),{interaction.user.name}({interaction.user.id})町 [USER_INPUT] {((message[:200] + '...') if len(message) > 203 else message).replace(chr(10), ' ')}")
            system_prompt = await self._prepare_system_prompt(interaction.channel_id, interaction.user.id,
                                                              interaction.user.display_name)
            messages_for_api: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
            user_content_parts = [{"type": "text",
                                   "text": f"{interaction.created_at.astimezone(self.jst).strftime('[%H:%M]')} {message}"}]
            user_content_parts.extend(image_contents)
            # 險隱樊､懷・縺ｧ蜍慕噪縺ｫ險隱槭・繝ｭ繝ｳ繝励ヨ繧堤函謌・
            lang_prompt = self._build_language_prompt(message)
            if lang_prompt:
                messages_for_api.append({"role": "system", "content": lang_prompt})
            messages_for_api.append({"role": "user", "content": user_content_parts})
            logger.info(f"鳩 [API] Sending {len(messages_for_api)} messages to LLM")
            model_name = llm_client.model_name_for_api_calls
            if self.tips_manager:
                waiting_embed = self.tips_manager.get_waiting_embed(model_name)
                temp_message = await interaction.followup.send(embed=waiting_embed, ephemeral=False, wait=True)
            else:
                waiting_message = f"-# :incoming_envelope: waiting response for '{model_name}' :incoming_envelope:"
                temp_message = await interaction.followup.send(waiting_message, ephemeral=False, wait=True)
            # 繧ｹ繝ｬ繝・ラ菴懈・繝懊ち繝ｳ縺ｯ蜑企勁・亥ｸｸ縺ｫFalse・・
            sent_messages, full_response_text, used_key_index = await self._process_streaming_and_send_response(
                sent_message=temp_message, channel=interaction.channel, user=interaction.user,
                messages_for_api=messages_for_api, llm_client=llm_client, is_first_response=False)
            if sent_messages and full_response_text:
                logger.info(
                    f"笨・LLM response completed | model='{model_in_use}' | response_length={len(full_response_text)} chars")
                log_response, key_log_str = (full_response_text[:200] + '...') if len(
                    full_response_text) > 203 else full_response_text, f" [key{used_key_index + 1}]" if used_key_index is not None else ""
                logger.info(f"､・[LLM_RESPONSE]{key_log_str} {log_response.replace(chr(10), ' ')}")
                logger.debug(
                    f"LLM full response for /chat (length: {len(full_response_text)} chars):\n{full_response_text}")

                # TTS Cog縺ｫ繧ｫ繧ｹ繧ｿ繝繧､繝吶Φ繝医ｒ逋ｺ轣ｫ縺輔○繧・
                try:
                    self.bot.dispatch("llm_response_complete", sent_messages, full_response_text)
                    logger.info("討 Dispatched 'llm_response_complete' event for TTS from /chat command.")
                except Exception as e:
                    logger.error(f"Failed to dispatch 'llm_response_complete' event from /chat: {e}", exc_info=True)

            elif not sent_messages:
                logger.warning("LLM response for /chat was empty or an error occurred.")
        except Exception as e:
            logger.error(f"笶・Error during /chat command execution: {e}", exc_info=True)
            error_msg = f"笶・**Error / 繧ｨ繝ｩ繝ｼ** 笶圭n\n{self.exception_handler.handle_exception(e)}"
            try:
                if temp_message:
                    await temp_message.edit(content=error_msg, embed=None, view=self._create_support_view())
                else:
                    await interaction.followup.send(content=error_msg, view=self._create_support_view())
            except discord.HTTPException:
                pass





    async def model_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        available_models = self.llm_config.get('available_models', [])
        return [app_commands.Choice(name=model, value=model) for model in available_models if
                current.lower() in model.lower()][:25]

    @app_commands.command(name="switch-models",
                          description="Switches the AI model used for this channel.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｧ菴ｿ逕ｨ縺吶ｋAI繝｢繝・Ν繧貞・繧頑崛縺医∪縺吶・)
    @app_commands.describe(model="Select the model you want to use.\n菴ｿ逕ｨ縺励◆縺・Δ繝・Ν繧帝∈謚槭＠縺ｦ縺上□縺輔＞縲・)
    @app_commands.autocomplete(model=model_autocomplete)
    async def switch_model_slash(self, interaction: discord.Interaction, model: str):
        await interaction.response.defer(ephemeral=False)
        available_models = self.llm_config.get('available_models', [])
        if model not in available_models:
            embed = discord.Embed(title="笞・・Invalid Model / 辟｡蜉ｹ縺ｪ繝｢繝・Ν",
                                  description=f"The specified model '{model}' is not available.\n謖・ｮ壹＆繧後◆繝｢繝・Ν '{model}' 縺ｯ蛻ｩ逕ｨ縺ｧ縺阪∪縺帙ｓ縲・,
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        channel_id, channel_id_str, default_model = interaction.channel_id, str(
            interaction.channel_id), self.llm_config.get('model')
        if channel_id in self.model_reset_tasks:
            self.model_reset_tasks[channel_id].cancel()
            self.model_reset_tasks.pop(channel_id, None)
            logger.info(f"Cancelled previous model reset task for channel {channel_id}.")
        self.channel_models[channel_id_str] = model
        try:
            await self._save_channel_models()
            await self._get_llm_client_for_channel(interaction.channel_id)
            if model != default_model:
                task = asyncio.create_task(self._schedule_model_reset(channel_id))
                self.model_reset_tasks[channel_id] = task
                embed = discord.Embed(title="笨・Model Switched / 繝｢繝・Ν繧貞・繧頑崛縺医∪縺励◆",
                                      description=f"The AI model for this channel has been switched to `{model}`.\nIt will automatically revert to the default model (`{default_model}`) **after 3 hours**.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｮAI繝｢繝・Ν縺・`{model}` 縺ｫ蛻・ｊ譖ｿ縺医ｉ繧後∪縺励◆縲・n**3譎る俣蠕・*縺ｫ繝・ヵ繧ｩ繝ｫ繝医Δ繝・Ν (`{default_model}`) 縺ｫ閾ｪ蜍慕噪縺ｫ謌ｻ繧翫∪縺吶・,
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(
                    f"Model for channel {channel_id} switched to '{model}' by {interaction.user.name}. Reset scheduled in 3 hours.")
            else:
                embed = discord.Embed(title="笨・Model Reset to Default / 繝｢繝・Ν繧偵ョ繝輔か繝ｫ繝医↓謌ｻ縺励∪縺励◆",
                                      description=f"The AI model for this channel has been reset to the default `{model}`.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｮAI繝｢繝・Ν縺後ョ繝輔か繝ｫ繝医・ `{model}` 縺ｫ謌ｻ縺輔ｌ縺ｾ縺励◆縲・,
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(f"Model for channel {channel_id} switched to default '{model}' by {interaction.user.name}.")
        except Exception as e:
            logger.error(f"Failed to save channel model settings: {e}", exc_info=True)
            embed = discord.Embed(title="笶・Save Error / 菫晏ｭ倥お繝ｩ繝ｼ",
                                  description="Failed to save settings.\n險ｭ螳壹・菫晏ｭ倥↓螟ｱ謨励＠縺ｾ縺励◆縲・,
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())

    @app_commands.command(name="switch-models-default-server",
                          description="Resets the AI model for this channel to the server default.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｮAI繝｢繝・Ν繧偵し繝ｼ繝舌・縺ｮ繝・ヵ繧ｩ繝ｫ繝郁ｨｭ螳壹↓謌ｻ縺励∪縺吶・)
    async def reset_model_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        channel_id, channel_id_str = interaction.channel_id, str(interaction.channel_id)
        if channel_id in self.model_reset_tasks:
            self.model_reset_tasks[channel_id].cancel()
            self.model_reset_tasks.pop(channel_id, None)
            logger.info(f"Cancelled scheduled model reset for channel {channel_id} due to manual reset.")
        if channel_id_str in self.channel_models:
            del self.channel_models[channel_id_str]
            try:
                await self._save_channel_models()
                default_model = self.llm_config.get('model', 'Not set / 譛ｪ險ｭ螳・)
                embed = discord.Embed(title="笨・Model Reset to Default / 繝｢繝・Ν繧偵ョ繝輔か繝ｫ繝医↓謌ｻ縺励∪縺励◆",
                                      description=f"The AI model for this channel has been reset to the default (`{default_model}`).\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｮAI繝｢繝・Ν繧偵ョ繝輔か繝ｫ繝・(`{default_model}`) 縺ｫ謌ｻ縺励∪縺励◆縲・,
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(f"Model for channel {interaction.channel_id} reset to default by {interaction.user.name}")
            except Exception as e:
                logger.error(f"Failed to save channel model settings after reset: {e}", exc_info=True)
                embed = discord.Embed(title="笶・Save Error / 菫晏ｭ倥お繝ｩ繝ｼ",
                                      description="Failed to save settings.\n險ｭ螳壹・菫晏ｭ倥↓螟ｱ謨励＠縺ｾ縺励◆縲・,
                                      color=discord.Color.red())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view())
        else:
            embed = discord.Embed(title="邃ｹ・・No Custom Model Set / 蟆ら畑繝｢繝・Ν縺ｯ縺ゅｊ縺ｾ縺帙ｓ",
                                  description="No custom model is set for this channel.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｫ縺ｯ蟆ら畑縺ｮ繝｢繝・Ν縺瑚ｨｭ螳壹＆繧後※縺・∪縺帙ｓ縲・,
                                  color=discord.Color.blue())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @switch_model_slash.error
    async def switch_model_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Error in /switch-model command: {error}", exc_info=True)
        error_message = f"An unexpected error occurred: {error}\n莠域悄縺帙〓繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {error}"
        embed = discord.Embed(title="笶・Unexpected Error / 莠域悄縺帙〓繧ｨ繝ｩ繝ｼ", description=error_message,
                              color=discord.Color.red())
        self._add_support_footer(embed)
        view = self._create_support_view()
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    async def image_model_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        if not self.image_generator: return []
        available_models, current_lower = self.image_generator.get_available_models(), current.lower()
        filtered = [model for model in available_models if current_lower in model.lower()]
        if len(filtered) > 25:
            models_by_provider, choices = self.image_generator.get_models_by_provider(), []
            for provider, models in sorted(models_by_provider.items()):
                if current_lower in provider.lower():
                    for model in models[:5]:
                        if len(choices) >= 25: break
                        choices.append(app_commands.Choice(name=model, value=model))
                    if len(choices) >= 25: break
            return choices[:25]
        return [app_commands.Choice(name=model, value=model) for model in filtered][:25]

    @app_commands.command(name="switch-image-model",
                          description="Switch the image generation model for this channel. / 縺薙・繝√Ε繝ｳ繝阪Ν縺ｮ逕ｻ蜒冗函謌舌Δ繝・Ν繧貞・繧頑崛縺医∪縺吶・)
    @app_commands.describe(
        model="Select the image generation model you want to use. / 菴ｿ逕ｨ縺励◆縺・判蜒冗函謌舌Δ繝・Ν繧帝∈謚槭＠縺ｦ縺上□縺輔＞縲・)
    @app_commands.autocomplete(model=image_model_autocomplete)
    async def switch_image_model_slash(self, interaction: discord.Interaction, model: str):
        await interaction.response.defer(ephemeral=False)
        if not self.image_generator:
            embed = discord.Embed(title="笶・Plugin Error / 繝励Λ繧ｰ繧､繝ｳ繧ｨ繝ｩ繝ｼ",
                                  description="ImageGenerator is not available.\nImageGenerator縺悟茜逕ｨ縺ｧ縺阪∪縺帙ｓ縲・,
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        # 繝励Ο繝舌う繝繝ｼ莉倥″蠖｢蠑擾ｼ・rovider/model_name・峨・蝣ｴ蜷医・螳滄圀縺ｮ繝｢繝・Ν蜷阪ｒ謚ｽ蜃ｺ
        actual_model = model.split('/', 1)[1] if '/' in model else model
        available_models = self.image_generator.get_available_models()
        if actual_model not in available_models:
            embed = discord.Embed(title="笞・・Invalid Model / 辟｡蜉ｹ縺ｪ繝｢繝・Ν",
                                  description=f"The specified model `{model}` is not available.\n謖・ｮ壹＆繧後◆繝｢繝・Ν `{model}` 縺ｯ蛻ｩ逕ｨ縺ｧ縺阪∪縺帙ｓ縲・,
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        try:
            await self.image_generator.set_model_for_channel(interaction.channel_id, actual_model)
            default_model = self.image_generator.default_model
            try:
                provider, model_name = model.split('/', 1)
            except ValueError:
                provider, model_name = "local", model

            if model != default_model:
                embed = discord.Embed(title="笨・Image Model Switched / 逕ｻ蜒冗函謌舌Δ繝・Ν繧貞・繧頑崛縺医∪縺励◆",
                                      description="The image generation model for this channel has been switched.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｮ逕ｻ蜒冗函謌舌Δ繝・Ν繧貞・繧頑崛縺医∪縺励◆縲・,
                                      color=discord.Color.green())
                embed.add_field(name="New Model / 譁ｰ縺励＞繝｢繝・Ν", value=f"```\n{model}\n```", inline=False)
                embed.add_field(name="Provider / 繝励Ο繝舌う繝繝ｼ", value=f"`{provider}`", inline=True)
                embed.add_field(name="Model Name / 繝｢繝・Ν蜷・, value=f"`{model_name}`", inline=True)
                embed.add_field(name="庁 Tip / 繝偵Φ繝・,
                                value=f"To reset to default (`{default_model}`), use `/reset-image-model`\n繝・ヵ繧ｩ繝ｫ繝・(`{default_model}`) 縺ｫ謌ｻ縺吶↓縺ｯ `/reset-image-model`",
                                inline=False)
            else:
                embed = discord.Embed(title="笨・Image Model Set to Default / 逕ｻ蜒冗函謌舌Δ繝・Ν繧偵ョ繝輔か繝ｫ繝医↓險ｭ螳壹＠縺ｾ縺励◆",
                                      description="The image generation model for this channel is now the default.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｮ逕ｻ蜒冗函謌舌Δ繝・Ν縺後ョ繝輔か繝ｫ繝医↓縺ｪ繧翫∪縺励◆縲・,
                                      color=discord.Color.green())
                embed.add_field(name="Model / 繝｢繝・Ν", value=f"```\n{model}\n```", inline=False)
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            logger.info(
                f"Image model for channel {interaction.channel_id} switched to '{model}' by {interaction.user.name}")
        except Exception as e:
            logger.error(f"Failed to save channel image model settings: {e}", exc_info=True)
            embed = discord.Embed(title="笶・Save Error / 菫晏ｭ倥お繝ｩ繝ｼ",
                                  description="Failed to save settings.\n險ｭ螳壹・菫晏ｭ倥↓螟ｱ謨励＠縺ｾ縺励◆縲・,
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())

    @app_commands.command(name="show-image-model",
                          description="Show the current image generation model for this channel. / 縺薙・繝√Ε繝ｳ繝阪Ν縺ｮ迴ｾ蝨ｨ縺ｮ逕ｻ蜒冗函謌舌Δ繝・Ν繧定｡ｨ遉ｺ縺励∪縺吶・)
    async def show_image_model_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        if not self.image_generator:
            embed = discord.Embed(title="笶・Plugin Error / 繝励Λ繧ｰ繧､繝ｳ繧ｨ繝ｩ繝ｼ",
                                  description="ImageGenerator is not available.\nImageGenerator縺悟茜逕ｨ縺ｧ縺阪∪縺帙ｓ縲・,
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        current_model, default_model, is_default = self.image_generator.get_model_for_channel(
            interaction.channel_id), self.image_generator.default_model, self.image_generator.get_model_for_channel(
            interaction.channel_id) == self.image_generator.default_model
        try:
            provider, model_name = current_model.split('/', 1)
        except ValueError:
            provider, model_name = "local", current_model

        embed = discord.Embed(title="耳 Current Image Generation Model / 迴ｾ蝨ｨ縺ｮ逕ｻ蜒冗函謌舌Δ繝・Ν",
                              color=discord.Color.blue() if is_default else discord.Color.purple())
        embed.add_field(name="Current Model / 迴ｾ蝨ｨ縺ｮ繝｢繝・Ν", value=f"```\n{current_model}\n```", inline=False)
        embed.add_field(name="Provider / 繝励Ο繝舌う繝繝ｼ", value=f"`{provider}`", inline=True)
        embed.add_field(name="Status / 迥ｶ諷・, value='`Default / 繝・ヵ繧ｩ繝ｫ繝・' if is_default else '`Custom / 繧ｫ繧ｹ繧ｿ繝`',
                        inline=True)
        models_by_provider = self.image_generator.get_models_by_provider()
        for provider_name, models in sorted(models_by_provider.items()):
            model_list = "\n".join([f"窶｢ `{m.split('/', 1)[1]}`" for m in models[:5]])
            if len(models) > 5: model_list += f"\n窶｢ ... and {len(models) - 5} more"
            embed.add_field(name=f"逃 {provider_name.title()} Models", value=model_list or "None", inline=True)
        embed.add_field(name="庁 Commands / 繧ｳ繝槭Φ繝・,
                        value="窶｢ `/switch-image-model` - Change model / 繝｢繝・Ν螟画峩\n窶｢ `/reset-image-model` - Reset to default / 繝・ヵ繧ｩ繝ｫ繝医↓謌ｻ縺・,
                        inline=False)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="list-image-models",
                          description="List all available image generation models. / 蛻ｩ逕ｨ蜿ｯ閭ｽ縺ｪ逕ｻ蜒冗函謌舌Δ繝・Ν縺ｮ荳隕ｧ繧定｡ｨ遉ｺ縺励∪縺吶・)
    @app_commands.describe(provider="Filter by provider (optional). / 繝励Ο繝舌う繝繝ｼ縺ｧ邨槭ｊ霎ｼ縺ｿ・医が繝励す繝ｧ繝ｳ・・)
    async def list_image_models_slash(self, interaction: discord.Interaction, provider: str = None):
        await interaction.response.defer(ephemeral=False)
        if not self.image_generator:
            embed = discord.Embed(title="笶・Plugin Error / 繝励Λ繧ｰ繧､繝ｳ繧ｨ繝ｩ繝ｼ",
                                  description="ImageGenerator is not available.\nImageGenerator縺悟茜逕ｨ縺ｧ縺阪∪縺帙ｓ縲・,
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        models_by_provider = self.image_generator.get_models_by_provider()
        if provider:
            provider_lower = provider.lower()
            models_by_provider = {k: v for k, v in models_by_provider.items() if provider_lower in k.lower()}
            if not models_by_provider:
                embed = discord.Embed(title="笞・・No Models Found / 繝｢繝・Ν縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ",
                                      description=f"No models found for provider: `{provider}`\n繝励Ο繝舌う繝繝ｼ `{provider}` 縺ｮ繝｢繝・Ν縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・,
                                      color=discord.Color.gold())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view())
                return
        total_models = sum(len(models) for models in models_by_provider.values())
        embed = discord.Embed(title="耳 Available Image Generation Models / 蛻ｩ逕ｨ蜿ｯ閭ｽ縺ｪ逕ｻ蜒冗函謌舌Δ繝・Ν",
                              description=f"Total: {total_models} models across {len(models_by_provider)} provider(s)\n蜷郁ｨ・ {len(models_by_provider)}繝励Ο繝舌う繝繝ｼ縲＋total_models}繝｢繝・Ν",
                              color=discord.Color.blue())
        for provider_name, models in sorted(models_by_provider.items()):
            # 繝｢繝・Ν蜷阪°繧峨・繝ｭ繝舌う繝繝ｼ驛ｨ蛻・ｒ髯､蜴ｻ・郁｡ｨ遉ｺ逕ｨ・・
            model_names = [m.split('/', 1)[1] if '/' in m else m for m in models]
            if len(model_names) > 10:
                model_text = "\n".join([f"{i + 1}. `{m}`" for i, m in enumerate(model_names[:10])])
                model_text += f"\n... and {len(model_names) - 10} more"
            else:
                model_text = "\n".join([f"{i + 1}. `{m}`" for i, m in enumerate(model_names)])
            embed.add_field(name=f"逃 {provider_name.title()} ({len(models)} models)", value=model_text or "None",
                            inline=False)
        embed.add_field(name="庁 How to Use / 菴ｿ縺・婿",
                        value="Use `/switch-image-model` to change the model for this channel.\n`/switch-image-model` 縺ｧ縺薙・繝√Ε繝ｳ繝阪Ν縺ｮ繝｢繝・Ν繧貞､画峩縺ｧ縺阪∪縺吶・,
                        inline=False)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @switch_image_model_slash.error
    async def switch_image_model_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Error in /switch-image-model command: {error}", exc_info=True)
        error_message = f"An unexpected error occurred: {error}\n莠域悄縺帙〓繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {error}"
        embed = discord.Embed(title="笶・Unexpected Error / 莠域悄縺帙〓繧ｨ繝ｩ繝ｼ", description=error_message,
                              color=discord.Color.red())
        self._add_support_footer(embed)
        view = self._create_support_view()
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    @app_commands.command(name="llm_help",
                          description="Displays help and usage guidelines for LLM (AI Chat) features.\nLLM (AI蟇ｾ隧ｱ) 讖溯・縺ｮ繝倥Ν繝励→蛻ｩ逕ｨ繧ｬ繧､繝峨Λ繧､繝ｳ繧定｡ｨ遉ｺ縺励∪縺吶・)
    async def llm_help_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        bot_user = self.bot.user or interaction.client.user
        bot_name = bot_user.name if bot_user else "This Bot / 蠖釘ot"
        embed = discord.Embed(title=f"庁 {bot_name} AI Chat Help & Guidelines / AI蟇ｾ隧ｱ讖溯・繝倥Ν繝暦ｼ・ぎ繧､繝峨Λ繧､繝ｳ",
                              description=f"Explanation and terms of use for the AI chat features.\n{bot_name}縺ｮAI蟇ｾ隧ｱ讖溯・縺ｫ縺､縺・※縺ｮ隱ｬ譏弱→蛻ｩ逕ｨ隕冗ｴ・〒縺吶・,
                              color=discord.Color.purple())
        if bot_user and bot_user.avatar: embed.set_thumbnail(url=bot_user.avatar.url)
        embed.add_field(name="Basic Usage / 蝓ｺ譛ｬ逧・↑菴ｿ縺・婿",
                        value=f"窶｢ Mention the bot (`@{bot_name}`) to get a response from the AI.\n  Bot縺ｫ繝｡繝ｳ繧ｷ繝ｧ繝ｳ (`@{bot_name}`) 縺励※隧ｱ縺励°縺代ｋ縺ｨ縲、I縺悟ｿ懃ｭ斐＠縺ｾ縺吶・n窶｢ **You can also continue the conversation by replying to the bot's messages (no mention needed).**\n  **Bot縺ｮ繝｡繝・そ繝ｼ繧ｸ縺ｫ霑比ｿ｡縺吶ｋ縺薙→縺ｧ繧ゆｼ夊ｩｱ繧堤ｶ壹￠繧峨ｌ縺ｾ縺呻ｼ医Γ繝ｳ繧ｷ繝ｧ繝ｳ荳崎ｦ・ｼ峨・*\n窶｢ If you ask the AI to remember something, it will try to store that information.\n  縲檎ｧ√・蜷榊燕縺ｯ縲・・〒縺吶りｦ壹∴縺ｦ縺翫＞縺ｦ縲阪・繧医≧縺ｫ隧ｱ縺励°縺代ｋ縺ｨ縲、I縺後≠縺ｪ縺溘・諠・ｱ繧定ｨ俶・縺励ｈ縺・→縺励∪縺吶・n窶｢ Attach images or paste image URLs with your message, and the AI will try to understand them.\n  逕ｻ蜒上→荳邱偵↓隧ｱ縺励°縺代ｋ縺ｨ縲、I縺檎判蜒上・蜀・ｮｹ繧ら炊隗｣縺励ｈ縺・→縺励∪縺吶・,
                        inline=False)

        # Split "Useful Commands" into multiple fields to avoid character limits
        embed.add_field(name="Commands - AI/Channel Settings / 繧ｳ繝槭Φ繝・- AI/繝√Ε繝ｳ繝阪Ν險ｭ螳・,
                        value="窶｢ `/switch-models`: Change the AI model used in this channel. / 縺薙・繝√Ε繝ｳ繝阪Ν縺ｧ菴ｿ縺・I繝｢繝・Ν繧貞､画峩縺励∪縺吶・n"
                              "窶｢ `/set-ai-bio`: Set a custom personality/role for the AI in this channel. / 縺薙・繝√Ε繝ｳ繝阪Ν蟆ら畑縺ｮAI縺ｮ諤ｧ譬ｼ繧・ｽｹ蜑ｲ繧定ｨｭ螳壹＠縺ｾ縺吶・n"
                              "窶｢ `/show-ai-bio`: Check the current AI bio setting. / 迴ｾ蝨ｨ縺ｮAI縺ｮbio險ｭ螳壹ｒ遒ｺ隱阪＠縺ｾ縺吶・n"
                              "窶｢ `/reset-ai-bio`: Reset the AI bio to the default. / AI縺ｮbio險ｭ螳壹ｒ繝・ヵ繧ｩ繝ｫ繝医↓謌ｻ縺励∪縺吶・,
                        inline=False)

        embed.add_field(name="Commands - Image Generation / 繧ｳ繝槭Φ繝・- 逕ｻ蜒冗函謌・,
                        value="窶｢ `/switch-image-model`: Switch the image generation model for this channel. / 縺薙・繝√Ε繝ｳ繝阪Ν縺ｮ逕ｻ蜒冗函謌舌Δ繝・Ν繧貞・繧頑崛縺医∪縺吶・n"
                              "窶｢ `/reset-image-model`: Reset the image generation model to default. / 逕ｻ蜒冗函謌舌Δ繝・Ν繧偵ョ繝輔か繝ｫ繝医↓謌ｻ縺励∪縺吶・n"
                              "窶｢ `/show-image-model`: Show the current image generation model. / 迴ｾ蝨ｨ縺ｮ逕ｻ蜒冗函謌舌Δ繝・Ν繧定｡ｨ遉ｺ縺励∪縺吶・n"
                              "窶｢ `/list-image-models`: List all available image generation models. / 蛻ｩ逕ｨ蜿ｯ閭ｽ縺ｪ蜈ｨ逕ｻ蜒冗函謌舌Δ繝・Ν繧剃ｸ隕ｧ陦ｨ遉ｺ縺励∪縺吶・,
                        inline=False)


        embed.add_field(name="Commands - Other / 繧ｳ繝槭Φ繝・- 縺昴・莉・,
                        value="窶｢ `/chat`: Chat with the AI without needing to mention. / AI縺ｨ繝｡繝ｳ繧ｷ繝ｧ繝ｳ縺ｪ縺励〒蟇ｾ隧ｱ縺励∪縺吶・n"
                              "窶｢ `/clear_history`: Reset the conversation history. / 莨夊ｩｱ螻･豁ｴ繧偵Μ繧ｻ繝・ヨ縺励∪縺吶・,
                        inline=False)
                        
        channel_model_str = self.channel_models.get(str(interaction.channel_id))
        model_display = f"`{channel_model_str}` (Channel-specific / 縺薙・繝√Ε繝ｳ繝阪Ν蟆ら畑)" if channel_model_str else f"`{self.llm_config.get('model', 'Not set / 譛ｪ險ｭ螳・)}` (Default / 繝・ヵ繧ｩ繝ｫ繝・"
        active_tools = self.llm_config.get('active_tools', [])
        tools_info = "窶｢ None / 縺ｪ縺・ if not active_tools else "窶｢ " + ", ".join(active_tools)
        embed.add_field(name="Current AI Settings / 迴ｾ蝨ｨ縺ｮAI險ｭ螳・,
                        value=f"窶｢ **Model in Use / 菴ｿ逕ｨ繝｢繝・Ν:** {model_display}\n窶｢ **AI Role (Channel) / AI縺ｮ蠖ｹ蜑ｲ(繝√Ε繝ｳ繝阪Ν):** {ai_bio_display} (see `/show-ai-bio`)\n窶｢ **Your Info / 縺ゅ↑縺溘・諠・ｱ:** {user_bio_display} (see `/show-user-bio`)\n窶｢ **Max Conversation History / 莨夊ｩｱ螻･豁ｴ縺ｮ譛螟ｧ菫晄戟謨ｰ:** {self.llm_config.get('max_messages', 'Not set / 譛ｪ險ｭ螳・)} pairs\n窶｢ **Max Images at Once / 荳蠎ｦ縺ｫ蜃ｦ逅・〒縺阪ｋ譛螟ｧ逕ｻ蜒乗椢謨ｰ:** {self.llm_config.get('max_images', 'Not set / 譛ｪ險ｭ螳・)} image(s)\n窶｢ **Available Tools / 蛻ｩ逕ｨ蜿ｯ閭ｽ縺ｪ繝・・繝ｫ:** {tools_info}",
                        inline=False)
        embed.add_field(name="--- 糖 AI Usage Guidelines / AI蛻ｩ逕ｨ繧ｬ繧､繝峨Λ繧､繝ｳ ---",
                        value="Please review the following to ensure safe use of the AI features.\nAI讖溯・繧貞ｮ牙・縺ｫ縺泌茜逕ｨ縺・◆縺縺上◆繧√∽ｻ･荳九・蜀・ｮｹ繧貞ｿ・★縺皮｢ｺ隱阪￥縺縺輔＞縲・,
                        inline=False)
        embed.add_field(name="笞・・1. Data Input Precautions / 繝・・繧ｿ蜈･蜉帶凾縺ｮ豕ｨ諢・,
                        value="**NEVER include personal or confidential information** such as your name, contact details, or passwords.\nAI縺ｫ險俶・縺輔○繧区ュ蝣ｱ縺ｫ縺ｯ縲∵ｰ丞錐縲・｣邨｡蜈医√ヱ繧ｹ繝ｯ繝ｼ繝峨↑縺ｩ縺ｮ**蛟倶ｺｺ諠・ｱ繧・ｧ伜ｯ・ュ蝣ｱ繧堤ｵｶ蟇ｾ縺ｫ蜷ｫ繧√↑縺・〒縺上□縺輔＞縲・*",
                        inline=False)
        embed.add_field(name="笨・2. Precautions for Using Generated Output / 逕滓・迚ｩ蛻ｩ逕ｨ譎ゅ・豕ｨ諢・,
                        value="The AI's responses may contain inaccuracies or biases. **Always fact-check and use them at your own risk.**\nAI縺ｮ蠢懃ｭ斐↓縺ｯ陌壼⊃繧・￥隕九′蜷ｫ縺ｾ繧後ｋ蜿ｯ閭ｽ諤ｧ縺後≠繧翫∪縺吶・*蠢・★繝輔ぃ繧ｯ繝医メ繧ｧ繝・け繧定｡後＞縲∬・蟾ｱ縺ｮ雋ｬ莉ｻ縺ｧ蛻ｩ逕ｨ縺励※縺上□縺輔＞縲・*",
                        inline=False)
        embed.set_footer(
            text="These guidelines are subject to change without notice.\n繧ｬ繧､繝峨Λ繧､繝ｳ縺ｯ莠亥相縺ｪ縺丞､画峩縺輔ｌ繧句ｴ蜷医′縺ゅｊ縺ｾ縺吶・)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="clear_history",
                          description="Clears the history of the current conversation thread.\n迴ｾ蝨ｨ縺ｮ莨夊ｩｱ繧ｹ繝ｬ繝・ラ縺ｮ螻･豁ｴ繧偵け繝ｪ繧｢縺励∪縺吶・)
    async def clear_history_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        guild_id = interaction.guild.id if interaction.guild else 0  # DM縺ｮ蝣ｴ蜷医・0
        cleared_count, threads_to_clear = 0, set()
        
        try:
            async for msg in interaction.channel.history(limit=200):
                if guild_id in self.message_to_thread and msg.id in self.message_to_thread[guild_id]: 
                    threads_to_clear.add(self.message_to_thread[guild_id][msg.id])
        except (discord.Forbidden, discord.HTTPException):
            embed = discord.Embed(title="笞・・Permission Error / 讓ｩ髯舌お繝ｩ繝ｼ",
                                  description="Could not read the channel's message history.\n繝√Ε繝ｳ繝阪Ν縺ｮ繝｡繝・そ繝ｼ繧ｸ螻･豁ｴ繧定ｪｭ縺ｿ蜿悶ｌ縺ｾ縺帙ｓ縺ｧ縺励◆縲・,
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        
        for thread_id in threads_to_clear:
            if guild_id in self.conversation_threads and thread_id in self.conversation_threads[guild_id]:
                del self.conversation_threads[guild_id][thread_id]
                if guild_id in self.message_to_thread:
                    self.message_to_thread[guild_id] = {
                        k: v for k, v in self.message_to_thread[guild_id].items() 
                        if v != thread_id
                    }
                cleared_count += 1
        
        if cleared_count > 0:
            embed = discord.Embed(title="笨・History Cleared / 螻･豁ｴ繧偵け繝ｪ繧｢縺励∪縺励◆",
                                  description=f"Cleared the history of {cleared_count} conversation thread(s) related to this channel.\n縺薙・繝√Ε繝ｳ繝阪Ν縺ｫ髢｢騾｣縺吶ｋ {cleared_count} 蛟九・莨夊ｩｱ繧ｹ繝ｬ繝・ラ縺ｮ螻･豁ｴ繧偵け繝ｪ繧｢縺励∪縺励◆縲・,
                                  color=discord.Color.green())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
        else:
            embed = discord.Embed(title="邃ｹ・・No History Found / 螻･豁ｴ縺後≠繧翫∪縺帙ｓ",
                                  description="No conversation history to clear was found.\n繧ｯ繝ｪ繧｢蟇ｾ雎｡縺ｮ莨夊ｩｱ螻･豁ｴ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・,
                                  color=discord.Color.blue())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())


async def setup(bot: commands.Bot):
    """Sets up the LLMCog."""
    try:
        await bot.add_cog(LLMCog(bot))
        logger.info("LLMCog loaded successfully.")
    except Exception as e:
        logger.critical(f"Failed to set up LLMCog: {e}", exc_info=True)
        raise
