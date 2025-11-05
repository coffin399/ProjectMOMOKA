# MOMOKA/llm/plugins/image_generator.py
from __future__ import annotations

import asyncio
import datetime
import io
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

import discord

from MOMOKA.generator.imagen import (
    GenerationParams,
    ImageModelRegistry,
    LocalTxt2ImgPipeline,
)

logger = logging.getLogger(__name__)


@dataclass
class GenerationTask:
    user_id: int
    user_name: str
    prompt: str
    channel_id: int
    arguments: Dict[str, Any]
    position: int = 0
    queue_message: Optional[discord.Message] = None


class ImageGenerator:
    """ローカル diffusers パイプラインを用いた画像生成プラグイン"""

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config.get("llm", {})
        self.image_gen_config = self.config.get("image_generator", {})

        self.model_registry = ImageModelRegistry.from_default_root()
        self.pipeline = LocalTxt2ImgPipeline(device=self.image_gen_config.get("device"))

        discovered_models = sorted(self.model_registry.names())
        if not discovered_models:
            raise RuntimeError("No local image models found under models/image-models")

        configured_models = self.image_gen_config.get("available_models")
        if configured_models:
            available = [model for model in configured_models if model in discovered_models]
            if not available:
                logger.warning("Configured available_models not found locally. Using discovered models instead.")
                available = discovered_models
        else:
            available = discovered_models

        self.available_models = available
        configured_default = self.image_gen_config.get("model")
        self.default_model = configured_default if configured_default in self.available_models else self.available_models[0]

        self.default_size = self.image_gen_config.get("default_size", "1024x1024")
        self.save_images = self.image_gen_config.get("save_images", True)
        self.save_directory = self.image_gen_config.get("save_directory", "data/image")
        self.default_params = self.image_gen_config.get("default_params", {})
        self.max_width = self.image_gen_config.get("max_width", 2048)
        self.max_height = self.image_gen_config.get("max_height", 2048)
        self.min_width = self.image_gen_config.get("min_width", 256)
        self.min_height = self.image_gen_config.get("min_height", 256)

        self.channel_models_path = "data/channel_image_models.json"
        self.channel_models: Dict[str, str] = self._load_channel_models()

        self.generation_queue: Deque[GenerationTask] = deque()
        self.queue_lock = asyncio.Lock()
        self.is_generating = False
        self.current_task: Optional[GenerationTask] = None

        logger.info("ImageGenerator initialised with %d local model(s)", len(self.available_models))
        logger.info("Default model: %s", self.default_model)

    # ------------------------------------------------------------------
    # Channel model helpers
    # ------------------------------------------------------------------
    def _load_channel_models(self) -> Dict[str, str]:
        if os.path.exists(self.channel_models_path):
            try:
                with open(self.channel_models_path, "r", encoding="utf-8") as fp:
                    data = fp.read()
                loaded = {str(k): v for k, v in json.loads(data).items()}
                logger.info("Loaded %d channel-specific image model settings", len(loaded))
                return loaded
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to load channel image models: %s", exc)
        return {}

    async def _save_channel_models(self) -> None:
        os.makedirs(os.path.dirname(self.channel_models_path), exist_ok=True)
        try:
            try:
                import aiofiles

                async with aiofiles.open(self.channel_models_path, "w", encoding="utf-8") as fp:
                    await fp.write(json.dumps(self.channel_models, indent=4, ensure_ascii=False))
            except ImportError:
                with open(self.channel_models_path, "w", encoding="utf-8") as fp:
                    json.dump(self.channel_models, fp, indent=4, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save channel image models: %s", exc)
            raise

    def get_model_for_channel(self, channel_id: int) -> str:
        selected = self.channel_models.get(str(channel_id), self.default_model)
        if selected not in self.available_models:
            logger.warning("Model '%s' not available. Falling back to default.", selected)
            return self.default_model
        return selected

    async def set_model_for_channel(self, channel_id: int, model: str) -> None:
        if model not in self.available_models:
            raise ValueError(f"Model '{model}' is not available")
        self.channel_models[str(channel_id)] = model
        await self._save_channel_models()

    async def reset_model_for_channel(self, channel_id: int) -> bool:
        if str(channel_id) in self.channel_models:
            del self.channel_models[str(channel_id)]
            await self._save_channel_models()
            return True
        return False

    def get_available_models(self) -> List[str]:
        return self.available_models.copy()

    def get_models_by_provider(self) -> Dict[str, List[str]]:
        provider = "local"
        return {provider: [f"{provider}/{name}" for name in self.available_models]}

    # ------------------------------------------------------------------
    # Public tool definition
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def tool_spec(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (
                    "Generate an image via local Stable Diffusion pipeline using the provided text prompt."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "A detailed description of the desired image. Include style, mood, colours, etc."
                            ),
                        },
                        "negative_prompt": {
                            "type": "string",
                            "description": "Elements to avoid (optional).",
                        },
                        "size": {
                            "type": "string",
                            "description": (
                                f"Image size in WIDTHxHEIGHT format. Allowed range: {self.min_width}x{self.min_height} to "
                                f"{self.max_width}x{self.max_height}. Automatically rounded to multiples of 8."
                            ),
                            "pattern": "^[0-9]+x[0-9]+$",
                        },
                        "steps": {
                            "type": "integer",
                            "description": "Number of sampling steps (optional).",
                            "minimum": 1,
                            "maximum": 150,
                        },
                        "cfg_scale": {
                            "type": "number",
                            "description": "CFG scale / prompt adherence (optional).",
                            "minimum": 1.0,
                            "maximum": 30.0,
                        },
                        "sampler_name": {
                            "type": "string",
                            "description": "Sampler name (optional).",
                        },
                        "seed": {
                            "type": "integer",
                            "description": "Seed for reproducibility (optional, -1 for random).",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        }

    # ------------------------------------------------------------------
    # Queue handling
    # ------------------------------------------------------------------
    async def run(self, arguments: Dict[str, Any], channel_id: int, user_id: int = 0,
                  user_name: str = "Unknown") -> str:
        prompt = arguments.get("prompt", "").strip()
        if not prompt:
            return "❌ Error: Empty prompt provided. / エラー: プロンプトが空です。"

        sanitized = dict(arguments)
        sanitized["prompt"] = prompt

        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.error("Channel %s not found when preparing modal", channel_id)
            return "❌ Error: Unable to open image generator modal because the channel was not found."

        requester = self.bot.get_user(user_id) if user_id else None
        if requester:
            user_name = requester.display_name or requester.name or user_name

        view = ImageGenerationSetupView(
            image_generator=self,
            channel_id=channel_id,
            base_arguments=sanitized,
            requester_id=user_id,
            requester_name=user_name,
        )

        description_lines = [f"**Prompt:** {prompt[:200]}{'...' if len(prompt) > 200 else ''}"]
        negative_prompt = sanitized.get("negative_prompt", "").strip()
        if negative_prompt:
            description_lines.append(
                f"**Negative Prompt:** {negative_prompt[:200]}{'...' if len(negative_prompt) > 200 else ''}"
            )
        description_lines.append(
            "Fill in the modal to customise generation parameters. If no action is taken, the modal will expire in 5 minutes."
        )

        embed = discord.Embed(
            title="🖼️ Image Generation Setup / 画像生成設定",
            description="\n".join(description_lines),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Model",
            value=view.selected_model,
            inline=True,
        )
        embed.add_field(
            name="Size",
            value=str(sanitized.get("size", self.default_size)),
            inline=True,
        )
        embed.set_footer(text="Modal auto-closes after 5 minutes of inactivity / 5分間操作が無いとモーダルは閉じます")

        message = await channel.send(embed=embed, view=view)
        view.message = message

        return (
            "📝 Prompted the user to configure image generation parameters via modal. "
            "User interaction is required to proceed."
        )

    async def _enqueue_task(self, arguments: Dict[str, Any], channel_id: int, user_id: int,
                             user_name: str) -> str:
        prompt = arguments.get("prompt", "").strip()
        if not prompt:
            return "❌ Error: Empty prompt provided. / エラー: プロンプトが空です。"

        task = GenerationTask(
            user_id=user_id,
            user_name=user_name,
            prompt=prompt,
            channel_id=channel_id,
            arguments=dict(arguments),
        )

        queue_message: Optional[discord.Message] = None
        async with self.queue_lock:
            if self.is_generating:
                task.position = len(self.generation_queue) + 1
                self.generation_queue.append(task)
                logger.info("📋 [IMAGE_GEN] User %s enqueued at position %d", user_name, task.position)
                queue_position = task.position
            else:
                self.is_generating = True
                self.current_task = task
                queue_position = 0

        if queue_position:
            queue_message = await self._show_queue_message(channel_id, queue_position, prompt)
            task.queue_message = queue_message
            return (
                "⏳ Your request has been added to the queue (Position #{pos}). Please wait... / "
                "リクエストをキューに追加しました（位置: #{pos}）。お待ちください..."
            ).format(pos=queue_position)

        result = await self._process_task(task, return_result=True)
        return result if result else "✅ Image generation completed."

    async def _process_task(self, task: GenerationTask, return_result: bool) -> Optional[str]:
        if task.queue_message:
            await self._update_queue_message(task.queue_message, "Generating... / 生成中...", task.position, task.prompt)

        prompt = task.arguments.get("prompt", "").strip()
        negative_prompt = task.arguments.get("negative_prompt", "").strip()
        size_input = task.arguments.get("size", self.default_size)
        width, height, adjusted_size = self._validate_and_adjust_size(size_input)

        steps = int(task.arguments.get("steps", self.default_params.get("steps", 20)))
        cfg_scale = float(task.arguments.get("cfg_scale", self.default_params.get("cfg_scale", 7.0)))
        sampler_name = task.arguments.get("sampler_name") or self.default_params.get("sampler_name")
        seed = int(task.arguments.get("seed", self.default_params.get("seed", -1)))

        requested_model = task.arguments.get("model")
        if requested_model and requested_model in self.available_models:
            model_name = requested_model
        else:
            if requested_model and requested_model not in self.available_models:
                logger.warning("Requested model '%s' not available. Falling back to channel/default model.", requested_model)
            model_name = self.get_model_for_channel(task.channel_id)
        model_info = self.model_registry.ensure_model(model_name)

        params = GenerationParams(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            steps=steps,
            cfg_scale=cfg_scale,
            seed=seed,
            sampler_name=sampler_name,
        )

        logger.info(
            "🎨 [IMAGE_GEN] Generating for user=%s | model=%s | size=%s | steps=%d | cfg=%.2f | seed=%d",
            task.user_name,
            model_name,
            adjusted_size,
            steps,
            cfg_scale,
            seed,
        )

        start_time = time.time()
        image_bytes = await self.pipeline.generate(model_info, params)
        elapsed_time = time.time() - start_time

        if self.save_images:
            saved_path = await self._save_image(image_bytes, prompt, model_name, adjusted_size)
        else:
            saved_path = None

        channel = self.bot.get_channel(task.channel_id)
        if not channel:
            logger.error("Channel %s not found; aborting send", task.channel_id)
            result_text = "❌ Error: Could not find channel to send image."
        else:
            embed = discord.Embed(
                title="🎨 Generated Image / 生成された画像",
                description=f"**Prompt:** {prompt[:200]}{'...' if len(prompt) > 200 else ''}",
                color=discord.Color.blue(),
            )
            if negative_prompt:
                embed.add_field(
                    name="Negative Prompt",
                    value=negative_prompt[:100] + ("..." if len(negative_prompt) > 100 else ""),
                    inline=False,
                )
            embed.add_field(name="Model", value=model_name, inline=True)
            embed.add_field(name="Size", value=adjusted_size, inline=True)
            embed.add_field(name="Steps", value=str(steps), inline=True)
            embed.add_field(name="CFG Scale", value=f"{cfg_scale:.2f}", inline=True)
            embed.add_field(name="Sampler", value=sampler_name or "default", inline=True)
            if seed != -1:
                embed.add_field(name="Seed", value=str(seed), inline=True)
            embed.add_field(name="Generation Time", value=f"{elapsed_time:.1f}s", inline=True)
            if size_input != adjusted_size:
                embed.add_field(
                    name="ℹ️ Size Adjusted",
                    value=f"Requested: {size_input} → Used: {adjusted_size}",
                    inline=False,
                )
            embed.set_footer(text="Powered by MOMOKA Local Diffusers Pipeline")

            file = discord.File(io.BytesIO(image_bytes), filename="generated_image.png")
            await channel.send(embed=embed, file=file)

            details = (
                f"✅ Successfully generated image with prompt: '{prompt[:100]}{'...' if len(prompt) > 100 else ''}'\n"
                f"Parameters: size={adjusted_size}, steps={steps}, cfg={cfg_scale:.2f}, sampler={sampler_name or 'default'}"
            )
            if seed != -1:
                details += f", seed={seed}"
            if size_input != adjusted_size:
                details += f"\n(Size adjusted from {size_input} to {adjusted_size})"
            if saved_path:
                details += " (Saved locally)"
            result_text = details

        if task.queue_message:
            try:
                await task.queue_message.delete()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to delete queue message: %s", exc)

        await self._schedule_next_task()
        return result_text if return_result else None

    async def _schedule_next_task(self) -> None:
        async with self.queue_lock:
            if not self.generation_queue:
                self.is_generating = False
                self.current_task = None
                return

            next_task = self.generation_queue.popleft()
            next_task.position = 1
            self.current_task = next_task

        asyncio.create_task(self._process_task(next_task, return_result=False))

    async def _handle_modal_submission(self, interaction: discord.Interaction, updated_arguments: Dict[str, Any],
                                       requester_name: str) -> None:
        result = await self._enqueue_task(
            updated_arguments,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            user_name=requester_name,
        )
        await interaction.response.send_message(result, ephemeral=True)

    # ------------------------------------------------------------------
    # Discord helpers
    # ------------------------------------------------------------------
    async def _show_queue_message(self, channel_id: int, position: int, prompt: str) -> Optional[discord.Message]:
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return None
        try:
            embed = discord.Embed(
                title="⏳ Added to Queue / キューに追加されました",
                description=f"**Prompt:** {prompt[:100]}{'...' if len(prompt) > 100 else ''}",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Position", value=f"#{position}", inline=True)
            embed.add_field(name="Status", value="Waiting... / 待機中...", inline=True)
            embed.set_footer(text="Generation will begin automatically")
            return await channel.send(embed=embed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send queue message: %s", exc)
            return None

    async def _update_queue_message(self, message: discord.Message, status: str, position: int, prompt: str) -> None:
        try:
            embed = discord.Embed(
                title="🎨 Generation Starting... / 生成開始中...",
                description=f"**Prompt:** {prompt[:100]}{'...' if len(prompt) > 100 else ''}",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Position", value=f"#{position}", inline=True)
            embed.add_field(name="Status", value=status, inline=True)
            embed.set_footer(text="Generating... / 生成中...")
            await message.edit(embed=embed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to update queue message: %s", exc)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _validate_and_adjust_size(self, size: str) -> tuple[int, int, str]:
        try:
            width_str, height_str = size.lower().replace(" ", "").split("x", maxsplit=1)
            width = int(width_str)
            height = int(height_str)
        except (ValueError, AttributeError):
            logger.warning("Invalid size '%s'. Falling back to default %s.", size, self.default_size)
            return self._validate_and_adjust_size(self.default_size) if size != self.default_size else (1024, 1024, "1024x1024")

        original = (width, height)
        width = max(self.min_width, min(width, self.max_width))
        height = max(self.min_height, min(height, self.max_height))
        width = (width // 8) * 8
        height = (height // 8) * 8
        adjusted = f"{width}x{height}"
        if original != (width, height):
            logger.info("Adjusted requested size %sx%s to %s", original[0], original[1], adjusted)
        return width, height, adjusted

    async def _save_image(self, image_data: bytes, prompt: str, model: str, size: str) -> Optional[str]:
        try:
            os.makedirs(self.save_directory, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_prompt = re.sub(r"[^\w\s-]", "", prompt[:50])
            safe_prompt = re.sub(r"[-\s]+", "_", safe_prompt).strip("_")
            model_token = re.sub(r"[^\w-]", "", model.split(".")[0])
            filename = f"{timestamp}_{model_token}_{size}_{safe_prompt}.png"
            path = os.path.join(self.save_directory, filename)
            try:
                import aiofiles

                async with aiofiles.open(path, "wb") as fp:
                    await fp.write(image_data)
            except ImportError:
                with open(path, "wb") as fp:
                    fp.write(image_data)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save image: %s", exc, exc_info=True)
            return None

    async def close(self) -> None:
        logger.info("ImageGenerator local pipeline does not require explicit cleanup.")


class ImageModelSelect(discord.ui.Select):
    def __init__(self, parent_view: "ImageGenerationSetupView", default_model: str):
        options = [
            discord.SelectOption(label=model, value=model, default=(model == default_model))
            for model in parent_view.image_generator.get_available_models()
        ]
        if not options:
            options = [discord.SelectOption(label="No Models Available", value=default_model, default=True)]
        super().__init__(
            placeholder="Select an image generation model... / モデルを選択してください",
            min_values=1,
            max_values=1,
            options=options[:25],
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.parent_view._is_authorized(interaction):
            await interaction.response.send_message(
                "❌ Only the original requester can change the model selection. / 元のリクエストしたユーザーのみ変更できます。",
                ephemeral=True,
            )
            return
        self.parent_view.selected_model = self.values[0]
        await interaction.response.defer()


class ImageGenerationModal(discord.ui.Modal, title="Configure Image Generation / 画像生成設定"):
    def __init__(self, parent_view: "ImageGenerationSetupView"):
        super().__init__(timeout=None)
        self.parent_view = parent_view
        generator = parent_view.image_generator
        base_args = parent_view.base_arguments
        default_steps = base_args.get("steps", generator.default_params.get("steps", 20))
        default_cfg = base_args.get("cfg_scale", generator.default_params.get("cfg_scale", 7.0))
        default_size = base_args.get("size", generator.default_size)
        default_seed = base_args.get("seed", generator.default_params.get("seed", -1))
        default_sampler = base_args.get("sampler_name", generator.default_params.get("sampler_name", ""))

        self.steps_input = discord.ui.TextInput(
            label="Steps",
            placeholder="20",
            default=str(default_steps),
            required=False,
            max_length=4,
        )
        self.cfg_input = discord.ui.TextInput(
            label="CFG Scale",
            placeholder="7.0",
            default=str(default_cfg),
            required=False,
            max_length=5,
        )
        self.size_input = discord.ui.TextInput(
            label="Size (WIDTHxHEIGHT)",
            placeholder=generator.default_size,
            default=str(default_size),
            required=False,
            max_length=15,
        )
        self.seed_input = discord.ui.TextInput(
            label="Seed (-1 for random)",
            placeholder="-1",
            default=str(default_seed),
            required=False,
            max_length=12,
        )
        self.sampler_input = discord.ui.TextInput(
            label="Sampler (optional)",
            placeholder=default_sampler or "e.g. DPM++ 2M Karras",
            default=default_sampler or "",
            required=False,
            max_length=40,
        )

        self.add_item(self.steps_input)
        self.add_item(self.cfg_input)
        self.add_item(self.size_input)
        self.add_item(self.seed_input)
        self.add_item(self.sampler_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.parent_view._is_authorized(interaction):
            await interaction.response.send_message(
                "❌ Only the original requester can configure image generation. / 元のリクエストしたユーザーのみ設定できます。",
                ephemeral=True,
            )
            return

        updated_args = dict(self.parent_view.base_arguments)
        errors: List[str] = []

        def parse_int(value: str, field: str) -> Optional[int]:
            if not value.strip():
                return None
            try:
                return int(value.strip())
            except ValueError:
                errors.append(f"{field}: invalid integer")
                return None

        def parse_float(value: str, field: str) -> Optional[float]:
            if not value.strip():
                return None
            try:
                return float(value.strip())
            except ValueError:
                errors.append(f"{field}: invalid number")
                return None

        if (steps := parse_int(self.steps_input.value, "Steps")) is not None:
            updated_args["steps"] = steps
        if (cfg := parse_float(self.cfg_input.value, "CFG Scale")) is not None:
            updated_args["cfg_scale"] = cfg
        if self.size_input.value.strip():
            updated_args["size"] = self.size_input.value.strip()
        if (seed := parse_int(self.seed_input.value, "Seed")) is not None:
            updated_args["seed"] = seed
        if self.sampler_input.value.strip():
            updated_args["sampler_name"] = self.sampler_input.value.strip()
        else:
            updated_args.pop("sampler_name", None)

        if errors:
            await interaction.response.send_message(
                "❌ Invalid input:\n" + "\n".join(errors),
                ephemeral=True,
            )
            return

        updated_args["model"] = self.parent_view.selected_model

        requester_name = interaction.user.display_name or interaction.user.name or self.parent_view.requester_name

        await self.parent_view.finalize_interaction(message_suffix="✅ Configuration received")
        await self.parent_view.image_generator._handle_modal_submission(interaction, updated_args, requester_name)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("Error in ImageGenerationModal: %s", error, exc_info=True)
        await interaction.response.send_message(
            "❌ An unexpected error occurred while processing the modal. / モーダル処理中に予期しないエラーが発生しました。",
            ephemeral=True,
        )


class ImageGenerationSetupView(discord.ui.View):
    def __init__(
        self,
        image_generator: ImageGenerator,
        channel_id: int,
        base_arguments: Dict[str, Any],
        requester_id: int,
        requester_name: str,
    ):
        super().__init__(timeout=300)
        self.image_generator = image_generator
        self.channel_id = channel_id
        self.base_arguments = base_arguments
        self.requester_id = requester_id
        self.requester_name = requester_name
        default_model = base_arguments.get("model") or image_generator.get_model_for_channel(channel_id)
        if default_model not in image_generator.get_available_models():
            default_model = image_generator.default_model
        self.selected_model = default_model
        self.message: Optional[discord.Message] = None

        self.model_select = ImageModelSelect(self, default_model)
        self.add_item(self.model_select)

    def _is_authorized(self, interaction: discord.Interaction) -> bool:
        return not self.requester_id or interaction.user.id == self.requester_id

    async def finalize_interaction(self, message_suffix: str = "") -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                embed = self.message.embeds[0] if self.message.embeds else None
                if embed and message_suffix:
                    embed = embed.copy()
                    embed.add_field(name="Status", value=message_suffix, inline=False)
                await self.message.edit(embed=embed, view=self)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to update modal setup message: %s", exc)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                embed = self.message.embeds[0] if self.message.embeds else None
                if embed:
                    embed = embed.copy()
                    embed.add_field(
                        name="Status",
                        value="⏱️ Modal expired due to inactivity. / 5分間操作が無かったためモーダルは無効化されました。",
                        inline=False,
                    )
                for item in self.children:
                    item.disabled = True
                await self.message.edit(embed=embed, view=self)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to disable modal view after timeout: %s", exc)

    @discord.ui.button(label="Configure & Generate / 設定して生成", style=discord.ButtonStyle.primary, emoji="🎨")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_authorized(interaction):
            await interaction.response.send_message(
                "❌ Only the original requester can configure image generation. / 元のリクエストしたユーザーのみ設定できます。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ImageGenerationModal(self))


# Local imports that require json
import json  # noqa: E402