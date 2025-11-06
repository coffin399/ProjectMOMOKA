from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Optional, Callable

import torch
from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline

try:  # diffusers>=0.27 removed SCHEDULER_MAP
    from diffusers.schedulers import SCHEDULER_MAP
except ImportError:  # pragma: no cover - fallback when map is unavailable
    SCHEDULER_MAP = {}
from PIL import Image

from .model_registry import ImageModelInfo

logger = logging.getLogger(__name__)


@dataclass
class GenerationParams:
    prompt: str
    negative_prompt: str
    width: int
    height: int
    steps: int
    cfg_scale: float
    seed: int
    sampler_name: Optional[str] = None


class LocalTxt2ImgPipeline:
    """Wrapper around diffusers StableDiffusionPipeline with async execution."""

    def __init__(self, device: Optional[str] = None) -> None:
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._pipeline_cache: dict[str, StableDiffusionPipeline | StableDiffusionXLPipeline] = {}
        self._lock = asyncio.Lock()
    
    def _is_sdxl_model(self, model: ImageModelInfo) -> bool:
        """Check if the model is SDXL based on name, metadata, or file size."""
        # Check metadata first
        if model.metadata.get("is_sdxl") is True:
            return True
        if model.metadata.get("is_sdxl") is False:
            return False
        
        # Check model name for "xl" indicator
        model_name_lower = model.name.lower()
        weights_name_lower = model.weights.name.lower()
        if "xl" in model_name_lower or "xl" in weights_name_lower:
            return True
        
        # Check file size (SDXL models are typically > 6GB)
        try:
            file_size_gb = model.weights.stat().st_size / (1024 ** 3)
            if file_size_gb > 5.5:  # Threshold around 6GB
                return True
        except (OSError, AttributeError):
            pass
        
        return False

    def _load_pipeline(self, model: ImageModelInfo) -> StableDiffusionPipeline | StableDiffusionXLPipeline:
        if model.name in self._pipeline_cache:
            return self._pipeline_cache[model.name]

        is_sdxl = self._is_sdxl_model(model)
        pipeline_class = StableDiffusionXLPipeline if is_sdxl else StableDiffusionPipeline
        
        logger.info("Loading diffusers pipeline for model '%s' (SDXL: %s)", model.name, is_sdxl)
        
        # SDXLモデルの場合とSD 1.x/2.xモデルの場合で読み込み方法を分ける
        try:
            pipeline = pipeline_class.from_single_file(
                str(model.weights),
                torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
                safety_checker=None,
            )
        except TypeError as exc:
            # text_encoder_2 エラーが発生した場合、SDXLパイプラインを試す
            if "text_encoder_2" in str(exc) and not is_sdxl:
                logger.warning(
                    "Failed to load model '%s' as SD 1.x/2.x. Retrying as SDXL...",
                    model.name
                )
                pipeline = StableDiffusionXLPipeline.from_single_file(
                    str(model.weights),
                    torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
                    safety_checker=None,
                )
                is_sdxl = True
            else:
                raise

        if model.vae:
            try:
                logger.info("Loading VAE weights for model '%s'", model.name)
                pipeline.vae.from_pretrained(model.vae.parent, subfolder=model.vae.stem)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load VAE for %s: %s", model.name, exc)

        if model.loras:
            for lora in model.loras:
                try:
                    logger.info("Loading LoRA '%s'", lora.name)
                    pipeline.load_lora_weights(str(lora))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to load LoRA %s: %s", lora, exc)

        pipeline.to(self.device)
        pipeline.enable_attention_slicing()
        try:
            pipeline.enable_xformers_memory_efficient_attention()
            logger.info("Enabled xFormers memory efficient attention for model '%s'", model.name)
        except (ImportError, AttributeError, RuntimeError, ValueError) as exc:
            logger.info("xFormers memory efficient attention unavailable: %s", exc)
        
        # キャッシュに保存
        self._pipeline_cache[model.name] = pipeline
        return pipeline

    def _apply_sampler(
        self, 
        pipeline: StableDiffusionPipeline | StableDiffusionXLPipeline, 
        sampler_name: Optional[str]
    ) -> None:
        if not sampler_name:
            return

        normalized = sampler_name.replace(" ", "").lower()
        for key, scheduler_cls in SCHEDULER_MAP.items():
            if normalized == key.replace(" ", "").lower():
                logger.info("Switching scheduler to '%s'", key)
                pipeline.scheduler = scheduler_cls.from_config(pipeline.scheduler.config)
                return
        logger.warning("Requested sampler '%s' not found. Using default scheduler.", sampler_name)

    async def generate(
        self,
        model: ImageModelInfo,
        params: GenerationParams,
        progress_callback: Optional[Callable[[int, int, object], None]] = None,
    ) -> bytes:
        async with self._lock:
            pipeline = self._load_pipeline(model)
            self._apply_sampler(pipeline, params.sampler_name)

        generator = torch.Generator(device=self.device)
        if params.seed >= 0:
            generator.manual_seed(params.seed)
        else:
            generator.seed()

        logger.info(
            "Running local txt2img | model=%s size=%dx%d steps=%d cfg=%.2f seed=%d sampler=%s",
            model.name,
            params.width,
            params.height,
            params.steps,
            params.cfg_scale,
            params.seed,
            params.sampler_name or "default",
        )

        loop = asyncio.get_event_loop()

        def _run_pipeline() -> Image.Image:
            invocation_kwargs = dict(
                prompt=params.prompt,
                negative_prompt=params.negative_prompt or None,
                width=params.width,
                height=params.height,
                num_inference_steps=params.steps,
                guidance_scale=params.cfg_scale,
                generator=generator,
            )
            if progress_callback:
                invocation_kwargs["callback"] = progress_callback
                invocation_kwargs["callback_steps"] = 1
            return pipeline(**invocation_kwargs).images[0]

        image: Image.Image = await loop.run_in_executor(None, _run_pipeline)

        buffer = BytesIO()
        await loop.run_in_executor(None, lambda: image.save(buffer, format="PNG"))
        return buffer.getvalue()


