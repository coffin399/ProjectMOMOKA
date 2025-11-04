# MOMOKA/llm/plugins/image_generator.py
from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Dict, Any, Optional, List
from collections import deque
from dataclasses import dataclass

import aiohttp
import discord

logger = logging.getLogger(__name__)


@dataclass
class GenerationTask:
    """画像生成タスク情報"""
    user_id: int
    user_name: str
    prompt: str
    channel_id: int
    position: int
    queue_message: Optional[discord.Message] = None


class ImageGenerator:
    """
    画像生成プラグイン - Stable Diffusion WebUI Forge / KoboldCPP対応
    
    Stable Diffusion WebUI ForgeとKoboldCPPの両方の画像生成APIをサポートします。
    両プロバイダーは同じAPI仕様を使用するため、URLを設定するだけで切り替え可能です。
    """

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config.get('llm', {})
        self.image_gen_config = self.config.get('image_generator', {})

        # プロバイダー設定 (Forge WebUI / KoboldCPP)
        # koboldcpp_urlが設定されている場合はそれを優先、なければforge_urlを使用
        self.koboldcpp_url = self.image_gen_config.get('koboldcpp_url')
        self.forge_url = self.image_gen_config.get('forge_url', 'http://127.0.0.1:7860')
        
        # 実際に使用するURLとプロバイダー名を決定
        if self.koboldcpp_url:
            self.api_url = self.koboldcpp_url.rstrip('/')
            self.provider_name = "KoboldCPP"
            logger.info(f"Using KoboldCPP for image generation at: {self.api_url}")
        else:
            self.api_url = self.forge_url.rstrip('/')
            self.provider_name = "Stable Diffusion WebUI Forge"
            logger.info(f"Using Stable Diffusion WebUI Forge for image generation at: {self.api_url}")

        self.default_model = self.image_gen_config.get('model', 'sd_xl_base_1.0.safetensors')
        self.default_size = self.image_gen_config.get('default_size', '1024x1024')
        self.timeout = self.image_gen_config.get('timeout', 180.0)

        # プログレスバー設定
        self.show_progress = self.image_gen_config.get('show_progress', True)
        self.progress_update_interval = self.image_gen_config.get('progress_update_interval', 2.0)

        # 画像保存設定
        self.save_images = self.image_gen_config.get('save_images', True)
        self.save_directory = self.image_gen_config.get('save_directory', 'data/image')

        # 生成パラメータ
        self.default_params = self.image_gen_config.get('default_params', {})

        # 解像度の制限設定（config.yamlで設定可能）
        self.max_width = self.image_gen_config.get('max_width', 2048)
        self.max_height = self.image_gen_config.get('max_height', 2048)
        self.min_width = self.image_gen_config.get('min_width', 256)
        self.min_height = self.image_gen_config.get('min_height', 256)

        # 利用可能なモデルリスト
        self.available_models = self.image_gen_config.get('available_models', [self.default_model])
        if self.default_model not in self.available_models:
            self.available_models.insert(0, self.default_model)
            logger.warning(f"Default model '{self.default_model}' not in available_models, adding it")

        # チャンネルごとのモデル設定
        self.channel_models_path = "data/channel_image_models.json"
        self.channel_models: Dict[str, str] = self._load_channel_models()

        # キュー管理
        self.generation_queue: deque[GenerationTask] = deque()
        self.is_generating = False
        self.queue_lock = asyncio.Lock()
        self.current_task: Optional[GenerationTask] = None

        self.http_session = aiohttp.ClientSession()

        logger.info(f"ImageGenerator initialized with {self.provider_name} at: {self.api_url}")
        logger.info(f"Default model: {self.default_model}")
        logger.info(f"Available models: {len(self.available_models)} models")
        logger.info(f"Save images: {self.save_images} (directory: {self.save_directory})")
        logger.info(f"Resolution limits: {self.min_width}x{self.min_height} to {self.max_width}x{self.max_height}")

    def _load_channel_models(self) -> Dict[str, str]:
        """チャンネルごとのモデル設定を読み込む"""
        import os
        import json

        if os.path.exists(self.channel_models_path):
            try:
                with open(self.channel_models_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"Loaded {len(data)} channel-specific image model settings")
                    return {str(k): v for k, v in data.items()}
            except Exception as e:
                logger.error(f"Failed to load channel image models: {e}")
        return {}

    async def _save_channel_models(self) -> None:
        """チャンネルごとのモデル設定を保存"""
        import os
        import json

        try:
            os.makedirs(os.path.dirname(self.channel_models_path), exist_ok=True)

            try:
                import aiofiles
                async with aiofiles.open(self.channel_models_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(self.channel_models, indent=4, ensure_ascii=False))
            except ImportError:
                with open(self.channel_models_path, 'w', encoding='utf-8') as f:
                    json.dump(self.channel_models, f, indent=4, ensure_ascii=False)

            logger.info(f"Saved channel image model settings")
        except Exception as e:
            logger.error(f"Failed to save channel image models: {e}")
            raise

    def get_model_for_channel(self, channel_id: int) -> str:
        """指定されたチャンネルで使用するモデルを取得"""
        channel_id_str = str(channel_id)
        model = self.channel_models.get(channel_id_str, self.default_model)

        if model not in self.available_models:
            logger.warning(f"Model '{model}' for channel {channel_id} not in available models, using default")
            return self.default_model

        return model

    async def set_model_for_channel(self, channel_id: int, model: str) -> None:
        """指定されたチャンネルのモデルを設定"""
        if model not in self.available_models:
            raise ValueError(f"Model '{model}' is not in available models list")

        channel_id_str = str(channel_id)
        self.channel_models[channel_id_str] = model
        await self._save_channel_models()
        logger.info(f"Set image model for channel {channel_id} to {model}")

    async def reset_model_for_channel(self, channel_id: int) -> bool:
        """指定されたチャンネルのモデルをデフォルトに戻す"""
        channel_id_str = str(channel_id)
        if channel_id_str in self.channel_models:
            del self.channel_models[channel_id_str]
            await self._save_channel_models()
            logger.info(f"Reset image model for channel {channel_id} to default")
            return True
        return False

    def get_available_models(self) -> List[str]:
        """利用可能なモデルのリストを取得"""
        return self.available_models.copy()

    def get_models_by_provider(self) -> Dict[str, List[str]]:
        """
        モデルをプロバイダーごとにグループ化して返す
        
        Stable Diffusion WebUI Forge / KoboldCPPの場合、モデル名はプロバイダーなしの形式（例: sd_xl_base_1.0.safetensors）
        だが、表示用に "provider/model_name" 形式で返す。
        
        Returns:
            プロバイダー名をキー、モデルリストを値とする辞書
            値のモデル名は "provider/model_name" 形式（表示用）
        """
        models_by_provider: Dict[str, List[str]] = {}
        
        # 使用中のプロバイダーに応じてデフォルトプロバイダー名を決定
        if self.provider_name == "KoboldCPP":
            default_provider = "koboldcpp"
        else:
            default_provider = "forge"
        
        for model in self.available_models:
            # モデル名が "provider/model" 形式かチェック
            if '/' in model:
                provider, model_name = model.split('/', 1)
                if provider not in models_by_provider:
                    models_by_provider[provider] = []
                models_by_provider[provider].append(model)
            else:
                # プロバイダー情報がないので、現在使用中のプロバイダーをデフォルトとして使用
                # 表示用に "provider/model_name" 形式に変換
                if default_provider not in models_by_provider:
                    models_by_provider[default_provider] = []
                # プロバイダー付きの形式に変換して追加（表示用）
                full_model_name = f"{default_provider}/{model}"
                models_by_provider[default_provider].append(full_model_name)
        
        return models_by_provider

    def _validate_and_adjust_size(self, size: str) -> tuple[int, int, str]:
        """
        サイズ文字列を検証し、必要に応じて調整する

        Returns:
            (width, height, adjusted_size_string)
        """
        try:
            parts = size.lower().replace(' ', '').split('x')
            if len(parts) != 2:
                raise ValueError(f"Invalid size format: {size}")

            width = int(parts[0])
            height = int(parts[1])

            # 範囲チェックと調整
            original_width, original_height = width, height
            width = max(self.min_width, min(width, self.max_width))
            height = max(self.min_height, min(height, self.max_height))

            # 8の倍数に調整（SD WebUIの要件）
            width = (width // 8) * 8
            height = (height // 8) * 8

            adjusted_size = f"{width}x{height}"

            if width != original_width or height != original_height:
                logger.info(f"Adjusted size from {original_width}x{original_height} to {adjusted_size}")

            return width, height, adjusted_size

        except (ValueError, IndexError) as e:
            logger.warning(f"Invalid size '{size}', using default: {e}")
            return self._validate_and_adjust_size(self.default_size)

    def _create_progress_bar(self, current: int, total: int, it_per_sec: float = 0.0, width: int = 20) -> str:
        """プログレスバーの文字列を生成"""
        if total == 0:
            percentage = 0
        else:
            percentage = int((current / total) * 100)

        filled = int((current / total) * width) if total > 0 else 0
        bar = '█' * filled + '░' * (width - filled)

        if it_per_sec > 0:
            return f"{bar} {percentage}% ({current}/{total}) - {it_per_sec:.2f}it/s"
        else:
            return f"{bar} {percentage}% ({current}/{total})"

    async def _update_progress_message(
            self,
            message: discord.Message,
            current: int,
            total: int,
            prompt: str,
            model: str,
            elapsed_time: float = 0.0,
            it_per_sec: float = 0.0
    ):
        """プログレスメッセージを更新"""
        progress_bar = self._create_progress_bar(current, total, it_per_sec)

        # キュー情報を取得
        queue_info = ""
        current_position = 0
        async with self.queue_lock:
            queue_length = len(self.generation_queue)
            if self.current_task:
                current_position = self.current_task.position
            if queue_length > 0:
                queue_info = f"\n📋 **Queue:** {queue_length} task(s) waiting / {queue_length}件待機中"

        # キュー位置情報を追加
        position_info = f"\n🔢 **Queue Position / キュー位置:** #{current_position}" if current_position > 0 else ""

        embed = discord.Embed(
            title="🎨 Generating Image... / 画像生成中...",
            description=f"**Prompt:** {prompt[:150]}{'...' if len(prompt) > 150 else ''}{position_info}{queue_info}",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Progress / 進捗",
            value=f"```\n{progress_bar}\n```",
            inline=False
        )
        embed.add_field(name="Model", value=model, inline=True)

        if elapsed_time > 0:
            embed.add_field(
                name="Elapsed Time / 経過時間",
                value=f"{elapsed_time:.1f}s",
                inline=True
            )

        if current < total:
            embed.set_footer(text="⏳ Please wait... / お待ちください...")
        else:
            embed.set_footer(text="✅ Finalizing... / 最終処理中...")

        try:
            await message.edit(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"Failed to update progress message: {e}")

    async def _show_queue_message(self, channel_id: int, position: int, prompt: str) -> Optional[discord.Message]:
        """キュー待機メッセージを表示"""
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return None

        try:
            embed = discord.Embed(
                title="⏳ Added to Queue / キューに追加されました",
                description=f"**Prompt:** {prompt[:100]}{'...' if len(prompt) > 100 else ''}",
                color=discord.Color.gold()
            )
            embed.add_field(
                name="Position in Queue / キュー位置",
                value=f"#{position}",
                inline=True
            )
            embed.add_field(
                name="Status / ステータス",
                value="Waiting... / 待機中...",
                inline=True
            )
            embed.set_footer(text="Your generation will start soon / まもなく生成が開始されます")

            return await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Failed to send queue message: {e}")
            return None

    async def _update_queue_message(self, message: discord.Message, status: str, position: int, prompt: str):
        """キューメッセージを更新"""
        try:
            embed = discord.Embed(
                title="🎨 Generation Starting... / 生成開始中...",
                description=f"**Prompt:** {prompt[:100]}{'...' if len(prompt) > 100 else ''}",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Position in Queue / キュー位置",
                value=f"#{position}",
                inline=True
            )
            embed.add_field(
                name="Status / ステータス",
                value=status,
                inline=True
            )
            embed.set_footer(text="🎨 Now generating... / 生成中...")
            await message.edit(embed=embed)
        except Exception as e:
            logger.warning(f"Failed to update queue message: {e}")

    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def tool_spec(self) -> Dict[str, Any]:
        """LLMに渡すツール定義"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (
                    "Generate an image based on a text prompt using AI image generation. "
                    "Use this when the user asks you to create, generate, or draw an image. "
                    "ユーザーが画像の生成、作成、描画を依頼した時にこのツールを使用してください。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "A detailed description of the image to generate. "
                                "Be specific and descriptive. Include style, mood, colors, etc. "
                                "生成する画像の詳細な説明。具体的で詳細に。"
                            )
                        },
                        "negative_prompt": {
                            "type": "string",
                            "description": (
                                "Things to avoid in the image (optional). "
                                "画像に含めたくない要素(オプション)。"
                            )
                        },
                        "size": {
                            "type": "string",
                            "description": (
                                f"Image size in format WIDTHxHEIGHT (e.g., '1024x1024', '512x768', '1920x1080'). "
                                f"Allowed range: {self.min_width}x{self.min_height} to {self.max_width}x{self.max_height}. "
                                f"Dimensions will be automatically adjusted to multiples of 8. "
                                f"Default is {self.default_size}. "
                                f"Common sizes: 1024x1024 (square), 1024x768 (landscape), 768x1024 (portrait), "
                                f"1920x1080 (16:9 landscape), 1080x1920 (9:16 portrait)."
                            ),
                            "pattern": "^[0-9]+x[0-9]+$"
                        },
                        "steps": {
                            "type": "integer",
                            "description": (
                                "Number of sampling steps (optional). "
                                "Higher values = better quality but slower. "
                                "Recommended: 20-30. Default from config if not specified."
                            ),
                            "minimum": 1,
                            "maximum": 150
                        },
                        "cfg_scale": {
                            "type": "number",
                            "description": (
                                "CFG Scale - how closely to follow the prompt (optional). "
                                "Higher values = more adherence to prompt. "
                                "Recommended: 7-11. Default from config if not specified."
                            ),
                            "minimum": 1.0,
                            "maximum": 30.0
                        },
                        "sampler_name": {
                            "type": "string",
                            "description": (
                                "Sampling method (optional). "
                                "Common options: 'DPM++ 2M Karras', 'Euler a', 'DPM++ SDE Karras'. "
                                "Default from config if not specified."
                            )
                        },
                        "seed": {
                            "type": "integer",
                            "description": (
                                "Seed for reproducibility (optional). "
                                "Use -1 for random seed. Default is -1."
                            ),
                            "minimum": -1
                        },
                        "restore_faces": {
                            "type": "boolean",
                            "description": (
                                "Enable face restoration (optional). "
                                "Improves face quality. Default from config if not specified."
                            )
                        }
                    },
                    "required": ["prompt"]
                }
            }
        }

    async def run(self, arguments: Dict[str, Any], channel_id: int, user_id: int = 0,
                  user_name: str = "Unknown") -> str:
        """
        画像生成を実行し、結果を返す

        Args:
            arguments: ツール呼び出しの引数
            channel_id: Discordチャンネルid
            user_id: ユーザーID
            user_name: ユーザー名

        Returns:
            LLMに返すレスポンスメッセージ
        """
        prompt = arguments.get('prompt', '').strip()
        if not prompt:
            return "❌ Error: Empty prompt provided. / エラー: プロンプトが空です。"

        # キューに追加
        async with self.queue_lock:
            position = len(self.generation_queue) + 1
            task = GenerationTask(
                user_id=user_id,
                user_name=user_name,
                prompt=prompt,
                channel_id=channel_id,
                position=position,
                queue_message=None
            )

            # 既に生成中の場合はキュー待機メッセージを表示
            if self.is_generating:
                queue_message = await self._show_queue_message(channel_id, position, prompt)
                task.queue_message = queue_message
                self.generation_queue.append(task)
                logger.info(f"📋 [IMAGE_GEN] User {user_name} added to queue at position {position}")
                return f"⏳ Your request has been added to the queue (Position #{position}). Please wait... / リクエストをキューに追加しました（位置: #{position}）。お待ちください..."

            self.generation_queue.append(task)

        # 生成を開始
        try:
            result = await self._process_queue(arguments, channel_id)
            return result
        except Exception as e:
            logger.error(f"❌ [IMAGE_GEN] Error in run: {e}", exc_info=True)
            return f"❌ Error during image generation: {str(e)[:200]}"

    async def _process_queue(self, arguments: Dict[str, Any], channel_id: int) -> str:
        """キューを処理して画像を生成"""
        async with self.queue_lock:
            if not self.generation_queue:
                return "❌ Error: No tasks in queue."

            self.is_generating = True
            self.current_task = self.generation_queue.popleft()

        # キューメッセージを更新（生成開始）
        if self.current_task.queue_message:
            await self._update_queue_message(
                self.current_task.queue_message,
                "Generating... / 生成中...",
                self.current_task.position,
                self.current_task.prompt
            )

        try:
            # 引数から生成パラメータを取得（指定されていない場合はデフォルト値を使用）
            prompt = arguments.get('prompt', '').strip()
            negative_prompt = arguments.get('negative_prompt', '').strip()
            size_input = arguments.get('size', self.default_size)

            # サイズを検証・調整
            width, height, adjusted_size = self._validate_and_adjust_size(size_input)

            # 動的パラメータの取得（LLMからの指定があればそれを使用、なければconfig.yamlのデフォルト）
            steps = arguments.get('steps', self.default_params.get('steps', 20))
            cfg_scale = arguments.get('cfg_scale', self.default_params.get('cfg_scale', 7.0))
            sampler_name = arguments.get('sampler_name', self.default_params.get('sampler_name', 'DPM++ 2M Karras'))
            seed = arguments.get('seed', self.default_params.get('seed', -1))
            restore_faces = arguments.get('restore_faces', self.default_params.get('restore_faces', False))

            model = self.get_model_for_channel(channel_id)

            logger.info(f"🎨 [IMAGE_GEN] Starting image generation for {self.current_task.user_name}")
            logger.info(f"🎨 [IMAGE_GEN] Model: {model}, Size: {adjusted_size} (requested: {size_input})")
            logger.info(f"🎨 [IMAGE_GEN] Steps: {steps}, CFG: {cfg_scale}, Sampler: {sampler_name}")
            logger.info(f"🎨 [IMAGE_GEN] Seed: {seed}, Restore Faces: {restore_faces}")
            logger.info(f"🎨 [IMAGE_GEN] Prompt: {prompt[:100]}...")

            # パラメータ辞書を作成
            gen_params = {
                'steps': steps,
                'cfg_scale': cfg_scale,
                'sampler_name': sampler_name,
                'seed': seed,
                'restore_faces': restore_faces
            }

            image_data = await self._generate_image_forge(
                prompt, negative_prompt, adjusted_size, model, channel_id, gen_params
            )

            if not image_data:
                return "❌ Failed to generate image. / 画像の生成に失敗しました。"

            # 画像を保存
            saved_path = None
            if self.save_images:
                saved_path = await self._save_image(image_data, prompt, model, adjusted_size)

            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.error(f"Channel {channel_id} not found!")
                return "❌ Error: Could not find channel to send image."

            image_file = discord.File(fp=io.BytesIO(image_data), filename="generated_image.png")

            # Embedに詳細なパラメータ情報を追加
            embed = discord.Embed(
                title="🎨 Generated Image / 生成された画像",
                description=f"**Prompt:** {prompt[:200]}{'...' if len(prompt) > 200 else ''}",
                color=discord.Color.blue()
            )
            if negative_prompt:
                embed.add_field(
                    name="Negative Prompt",
                    value=negative_prompt[:100] + ('...' if len(negative_prompt) > 100 else ''),
                    inline=False
                )
            embed.add_field(name="Size", value=adjusted_size, inline=True)
            embed.add_field(name="Model", value=model, inline=True)
            embed.add_field(name="Steps", value=str(steps), inline=True)
            embed.add_field(name="CFG Scale", value=str(cfg_scale), inline=True)
            embed.add_field(name="Sampler", value=sampler_name, inline=True)
            if seed != -1:
                embed.add_field(name="Seed", value=str(seed), inline=True)
            if restore_faces:
                embed.add_field(name="Face Restoration", value="✅ Enabled", inline=True)

            # サイズが調整された場合は注記
            if size_input != adjusted_size:
                embed.add_field(
                    name="ℹ️ Size Adjusted",
                    value=f"Requested: {size_input} → Used: {adjusted_size}",
                    inline=False
                )

            # フッターメッセージをプロバイダーに応じて動的に変更
            if self.provider_name == "KoboldCPP":
                embed.set_footer(text=f"Powered by KoboldCPP and PLANA")
            else:
                embed.set_footer(text="Powered by SDWebUI reForge and PLANA")

            await channel.send(embed=embed, file=image_file)

            # キューメッセージを削除
            if self.current_task.queue_message:
                try:
                    await self.current_task.queue_message.delete()
                except Exception as e:
                    logger.warning(f"Failed to delete queue message: {e}")

            logger.info(f"✅ [IMAGE_GEN] Successfully generated and sent image")
            if saved_path:
                logger.info(f"💾 [IMAGE_GEN] Image saved to: {saved_path}")

            # 現在のキュー位置を取得
            queue_position_info = ""
            async with self.queue_lock:
                if self.current_task:
                    queue_position_info = f" Queue position / キュー位置: #{self.current_task.position}"

            # パラメータ情報を含めたレスポンス
            param_info = f"\nParameters: size={adjusted_size}, steps={steps}, cfg={cfg_scale}, sampler={sampler_name}"
            if seed != -1:
                param_info += f", seed={seed}"
            if restore_faces:
                param_info += f", restore_faces=true"
            if size_input != adjusted_size:
                param_info += f"\n(Size adjusted from {size_input} to {adjusted_size})"

            return (
                f"✅ Successfully generated image with prompt: '{prompt[:100]}{'...' if len(prompt) > 100 else ''}'\n"
                f"The image has been sent to the channel. / 画像をチャンネルに送信しました。"
                f"{queue_position_info}"
                f"{param_info}"
                f"{f' (Saved locally)' if saved_path else ''}"
            )

        finally:
            async with self.queue_lock:
                self.is_generating = False
                self.current_task = None

                # 次のタスクがあれば処理
                if self.generation_queue:
                    next_task = self.generation_queue[0]
                    logger.info(f"📋 [IMAGE_GEN] Processing next task for {next_task.user_name}")
                    # 次のタスクを非同期で処理
                    asyncio.create_task(self._process_next_task())

    async def _process_next_task(self):
        """次のキュータスクを処理"""
        async with self.queue_lock:
            if not self.generation_queue:
                return
            task = self.generation_queue[0]

        # タスク情報から引数を再構築
        arguments = {
            'prompt': task.prompt,
            'size': self.default_size
        }

        await self._process_queue(arguments, task.channel_id)

    async def _generate_image_forge(
            self,
            prompt: str,
            negative_prompt: str,
            size: str,
            model: str,
            channel_id: int,
            gen_params: Dict[str, Any]
    ) -> Optional[bytes]:
        """
        Stable Diffusion WebUI Forge APIで画像を生成

        Args:
            prompt: 生成する画像の説明
            negative_prompt: 除外する要素
            size: 画像サイズ
            model: 使用するモデル名
            channel_id: Discordチャンネルid (プログレス表示用)
            gen_params: 生成パラメータ (steps, cfg_scale, sampler_name, seed, restore_faces)

        Returns:
            生成された画像データ(PNG形式)
        """
        width, height = map(int, size.split('x'))

        # Forge WebUI API エンドポイント
        url = f"{self.api_url}/sdapi/v1/txt2img"

        # 渡されたパラメータを使用（デフォルトは既に適用済み）
        steps = gen_params.get('steps', 20)
        cfg_scale = gen_params.get('cfg_scale', 7.0)
        sampler_name = gen_params.get('sampler_name', 'DPM++ 2M Karras')
        seed = gen_params.get('seed', -1)
        restore_faces = gen_params.get('restore_faces', False)

        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt or self.default_params.get('negative_prompt', ''),
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "sampler_name": sampler_name,
            "batch_size": 1,
            "n_iter": 1,
            "seed": seed,
            "restore_faces": restore_faces,
            "tiling": self.default_params.get('tiling', False),
            "override_settings": {
                "sd_model_checkpoint": model
            },
            "override_settings_restore_afterwards": True
        }

        # 追加パラメータがあればマージ（ただしユーザー指定を優先）
        extra_params = self.default_params.get('extra_params')
        if extra_params and isinstance(extra_params, dict):
            # ユーザー指定のパラメータで上書きされないように注意
            for key, value in extra_params.items():
                if key not in payload:
                    payload[key] = value

        logger.info(f"🟢 [IMAGE_GEN] Calling {self.provider_name} API")
        logger.info(f"🟢 [IMAGE_GEN] URL: {url}")
        logger.info(f"🟢 [IMAGE_GEN] Model: {model}")
        logger.info(f"🟢 [IMAGE_GEN] Size: {width}x{height}")
        logger.info(f"🟢 [IMAGE_GEN] Steps: {payload['steps']}, CFG: {payload['cfg_scale']}")
        logger.info(f"🟢 [IMAGE_GEN] Sampler: {payload['sampler_name']}, Seed: {payload['seed']}")
        logger.info(f"🟢 [IMAGE_GEN] Restore Faces: {payload['restore_faces']}")

        # プログレスメッセージを投稿
        progress_message = None
        if self.show_progress:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    # キュー情報を追加
                    queue_info = ""
                    current_position = 0
                    async with self.queue_lock:
                        queue_length = len(self.generation_queue)
                        if self.current_task:
                            current_position = self.current_task.position
                        if queue_length > 0:
                            queue_info = f"\n📋 **Queue:** {queue_length} task(s) waiting / {queue_length}件待機中"

                    position_info = f"\n🔢 **Queue Position / キュー位置:** #{current_position}" if current_position > 0 else ""

                    embed = discord.Embed(
                        title="🎨 Starting Image Generation... / 画像生成を開始...",
                        description=f"**Prompt:** {prompt[:150]}{'...' if len(prompt) > 150 else ''}{position_info}{queue_info}",
                        color=discord.Color.orange()
                    )
                    embed.add_field(name="Model", value=model, inline=True)
                    embed.add_field(name="Size", value=size, inline=True)
                    embed.set_footer(text="⏳ Initializing... / 初期化中...")
                    progress_message = await channel.send(embed=embed)
                except Exception as e:
                    logger.warning(f"Failed to send progress message: {e}")

        # プログレス監視タスクを起動
        progress_task = None
        if self.show_progress and progress_message:
            import time
            start_time = time.time()
            progress_task = asyncio.create_task(
                self._monitor_progress(progress_message, steps, prompt, model, start_time)
            )
            logger.info(f"🟢 [IMAGE_GEN] Progress monitoring task started")

        try:
            # 画像生成リクエストを送信
            import time
            start_time = time.time()

            async with self.http_session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as response:

                logger.info(f"🟢 [IMAGE_GEN] Response status: {response.status}")

                if response.status == 200:
                    result = await response.json()

                    # プログレス監視を停止
                    if progress_task:
                        progress_task.cancel()
                        try:
                            await progress_task
                        except asyncio.CancelledError:
                            pass

                    if result.get('images') and len(result['images']) > 0:
                        # Base64エンコードされた画像をデコード
                        b64_image = result['images'][0]
                        image_bytes = base64.b64decode(b64_image)

                        elapsed_time = time.time() - start_time
                        logger.info(f"✅ [IMAGE_GEN] Successfully received image ({len(image_bytes)} bytes)")
                        logger.info(f"✅ [IMAGE_GEN] Total generation time: {elapsed_time:.1f}s")

                        # 生成情報をログ出力
                        if 'info' in result:
                            logger.info(f"🟢 [IMAGE_GEN] Generation info: {result['info'][:200]}...")

                        # 完了メッセージを表示
                        if progress_message:
                            try:
                                final_embed = discord.Embed(
                                    title="✅ Image Generation Complete! / 画像生成完了!",
                                    description=f"**Prompt:** {prompt[:150]}{'...' if len(prompt) > 150 else ''}",
                                    color=discord.Color.green()
                                )
                                final_embed.add_field(
                                    name="Generation Time / 生成時間",
                                    value=f"{elapsed_time:.1f}s",
                                    inline=True
                                )
                                final_embed.set_footer(text="🎉 Sending image... / 画像を送信中...")
                                await progress_message.edit(embed=final_embed)

                                # 少し待ってから削除
                                await asyncio.sleep(2)
                                await progress_message.delete()
                            except Exception as e:
                                logger.warning(f"Failed to update final progress: {e}")

                        return image_bytes

                    logger.error(f"❌ [IMAGE_GEN] No image data in response")
                    if progress_message:
                        try:
                            await progress_message.delete()
                        except:
                            pass
                    return None
                else:
                    error_text = await response.text()
                    logger.error(f"❌ [IMAGE_GEN] API error {response.status}: {error_text[:500]}")
                    if progress_message:
                        try:
                            await progress_message.delete()
                        except:
                            pass
                    return None

        except asyncio.TimeoutError:
            logger.error(f"❌ [IMAGE_GEN] Request timed out after {self.timeout}s")
            if progress_task:
                progress_task.cancel()
            if progress_message:
                try:
                    await progress_message.delete()
                except:
                    pass
            return None
        except aiohttp.ClientConnectorError as e:
            logger.error(f"❌ [IMAGE_GEN] Connection error: {e}")
            logger.error(f"❌ [IMAGE_GEN] Make sure {self.provider_name} is running at {self.api_url}")
            if progress_task:
                progress_task.cancel()
            if progress_message:
                try:
                    await progress_message.delete()
                except:
                    pass
            return None
        except Exception as e:
            logger.error(f"❌ [IMAGE_GEN] Exception during API call: {e}", exc_info=True)
            if progress_task:
                progress_task.cancel()
            if progress_message:
                try:
                    await progress_message.delete()
                except:
                    pass
            return None

    async def _monitor_progress(
            self,
            message: discord.Message,
            total_steps: int,
            prompt: str,
            model: str,
            start_time: float
    ):
        """プログレスを監視してメッセージを更新"""
        progress_url = f"{self.api_url}/sdapi/v1/progress"

        last_step = 0
        last_update_time = start_time
        consecutive_errors = 0
        max_consecutive_errors = 5

        try:
            # 少し待ってから監視開始（API起動待ち）
            await asyncio.sleep(1.0)

            while True:
                try:
                    async with self.http_session.get(
                            progress_url,
                            timeout=aiohttp.ClientTimeout(total=5.0)
                    ) as response:
                        if response.status == 200:
                            import time
                            data = await response.json()

                            # progressが0.0の場合はまだ開始していない
                            progress = data.get('progress', 0.0)
                            state = data.get('state', {})
                            job_count = state.get('job_count', 0)

                            # 生成が開始されているか確認
                            if progress > 0.0 or job_count > 0:
                                consecutive_errors = 0  # エラーカウントをリセット

                                current_step = int(progress * total_steps)
                                if current_step > total_steps:
                                    current_step = total_steps

                                current_time = time.time()
                                elapsed_time = current_time - start_time

                                # it/s を計算
                                it_per_sec = 0.0
                                time_diff = current_time - last_update_time
                                if time_diff > 0 and current_step > last_step:
                                    steps_diff = current_step - last_step
                                    it_per_sec = steps_diff / time_diff

                                await self._update_progress_message(
                                    message,
                                    current_step,
                                    total_steps,
                                    prompt,
                                    model,
                                    elapsed_time,
                                    it_per_sec
                                )

                                last_step = current_step
                                last_update_time = current_time

                                logger.debug(
                                    f"📊 [IMAGE_GEN] Progress: {current_step}/{total_steps} ({progress * 100:.1f}%)")
                            else:
                                # まだ開始していない場合は初期化中と表示
                                logger.debug(f"⏳ [IMAGE_GEN] Waiting for generation to start...")
                        else:
                            consecutive_errors += 1
                            logger.warning(f"⚠️ [IMAGE_GEN] Progress API returned status {response.status}")

                except asyncio.TimeoutError:
                    consecutive_errors += 1
                    logger.debug(
                        f"⚠️ [IMAGE_GEN] Progress check timeout (attempt {consecutive_errors}/{max_consecutive_errors})")
                except Exception as e:
                    consecutive_errors += 1
                    logger.debug(
                        f"⚠️ [IMAGE_GEN] Progress check error: {e} (attempt {consecutive_errors}/{max_consecutive_errors})")

                # 連続エラーが多すぎる場合は監視を停止
                if consecutive_errors >= max_consecutive_errors:
                    logger.warning(f"❌ [IMAGE_GEN] Too many consecutive errors, stopping progress monitoring")
                    break

                await asyncio.sleep(self.progress_update_interval)

        except asyncio.CancelledError:
            # タスクがキャンセルされた場合は正常終了
            logger.info(f"🛑 [IMAGE_GEN] Progress monitoring cancelled")
            pass
        except Exception as e:
            logger.error(f"❌ [IMAGE_GEN] Unexpected error in progress monitoring: {e}", exc_info=True)

    async def _save_image(self, image_data: bytes, prompt: str, model: str, size: str) -> Optional[str]:
        """
        生成された画像をファイルに保存

        Args:
            image_data: 画像データ（バイト列）
            prompt: 生成プロンプト
            model: 使用したモデル名
            size: 画像サイズ

        Returns:
            保存されたファイルパス（相対パス）、失敗時はNone
        """
        import os
        import datetime
        import re

        try:
            # 保存ディレクトリを作成
            os.makedirs(self.save_directory, exist_ok=True)

            # ファイル名を生成（タイムスタンプ + プロンプトの一部）
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            # プロンプトから安全なファイル名を生成（最初の50文字まで）
            safe_prompt = re.sub(r'[^\w\s-]', '', prompt[:50])
            safe_prompt = re.sub(r'[-\s]+', '_', safe_prompt).strip('_')

            # モデル名から簡単な識別子を抽出
            model_short = model.split('.')[0][:20] if '.' in model else model[:20]
            model_short = re.sub(r'[^\w-]', '', model_short)

            # ファイル名を構築
            filename = f"{timestamp}_{model_short}_{size}_{safe_prompt}.png"
            filepath = os.path.join(self.save_directory, filename)

            # 画像を保存
            try:
                import aiofiles
                async with aiofiles.open(filepath, 'wb') as f:
                    await f.write(image_data)
            except ImportError:
                # aiofilesがない場合は通常の書き込み
                with open(filepath, 'wb') as f:
                    f.write(image_data)

            logger.info(f"💾 [IMAGE_GEN] Image saved to: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"❌ [IMAGE_GEN] Failed to save image: {e}", exc_info=True)
            return None

    async def get_available_models_from_api(self) -> Optional[List[str]]:
        """Forge WebUI / KoboldCPPから利用可能なモデルリストを取得"""
        url = f"{self.api_url}/sdapi/v1/sd-models"

        try:
            async with self.http_session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10.0)
            ) as response:
                if response.status == 200:
                    models = await response.json()
                    model_names = [model['title'] for model in models]
                    logger.info(f"📋 [IMAGE_GEN] Found {len(model_names)} models in {self.provider_name}")
                    return model_names
                else:
                    logger.error(f"❌ [IMAGE_GEN] Failed to fetch models: {response.status}")
                    return None
        except Exception as e:
            logger.error(f"❌ [IMAGE_GEN] Error fetching models: {e}")
            return None

    async def close(self):
        """HTTPセッションをクローズ"""
        await self.http_session.close()
        logger.info(f"ImageGenerator HTTP session closed for {self.provider_name}")