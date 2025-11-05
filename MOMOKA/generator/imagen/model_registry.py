from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


SUPPORTED_WEIGHTS_EXTENSIONS = {
    ".safetensors",
    ".ckpt",
    ".pt",
    ".bin",
}
SUPPORTED_VAE_EXTENSIONS = {
    ".vae",
    ".safetensors",
}
SUPPORTED_LORA_EXTENSIONS = {
    ".safetensors",
    ".ckpt",
    ".pt",
}


@dataclass
class ImageModelInfo:
    """Metadata for a single image generation model directory."""

    name: str
    path: Path
    weights: Path
    vae: Optional[Path]
    loras: List[Path]
    metadata: Dict[str, str]

    @property
    def display_name(self) -> str:
        return self.metadata.get("display_name", self.name)


class ImageModelRegistry:
    """Discovers and caches models located under models/image-models."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: Dict[str, ImageModelInfo] = {}
        self.refresh()

    def refresh(self) -> None:
        self._cache.clear()
        if not self.root.exists():
            logger.warning("Image model root '%s' does not exist", self.root)
            return

        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue

            model_info = self._load_model_dir(entry)
            if model_info:
                self._cache[model_info.name] = model_info

        logger.info("Discovered %d local image models", len(self._cache))

    def _load_model_dir(self, directory: Path) -> Optional[ImageModelInfo]:
        weights_file: Optional[Path] = None
        vae_file: Optional[Path] = None
        lora_files: List[Path] = []
        metadata: Dict[str, str] = {}

        for file in sorted(directory.iterdir()):
            if file.is_dir():
                continue

            suffix = file.suffix.lower()
            if suffix in SUPPORTED_WEIGHTS_EXTENSIONS and weights_file is None:
                weights_file = file
            elif suffix in SUPPORTED_VAE_EXTENSIONS and vae_file is None:
                vae_file = file
            elif suffix in SUPPORTED_LORA_EXTENSIONS:
                lora_files.append(file)
            elif file.name == "model.json":
                try:
                    metadata = json.loads(file.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to parse metadata file %s: %s", file, exc)

        if not weights_file:
            logger.warning("Skipping model directory '%s': no weights file found", directory)
            return None

        name = directory.name
        return ImageModelInfo(
            name=name,
            path=directory,
            weights=weights_file,
            vae=vae_file,
            loras=lora_files,
            metadata=metadata,
        )

    def get(self, name: str) -> Optional[ImageModelInfo]:
        return self._cache.get(name)

    def all(self) -> Iterable[ImageModelInfo]:
        return self._cache.values()

    def names(self) -> List[str]:
        return list(self._cache.keys())

    def ensure_model(self, name: str) -> ImageModelInfo:
        info = self.get(name)
        if not info:
            raise ValueError(f"Model '{name}' is not registered")
        return info

    @classmethod
    def from_default_root(cls) -> "ImageModelRegistry":
        module_path = Path(__file__).resolve()
        candidates: List[Path] = []

        for ancestor in module_path.parents:
            name = ancestor.name.lower()
            if name in {"projectmomoka", "project-momoka"}:
                candidates.append(ancestor / "models" / "image-models")

            project_variant = ancestor / "ProjectMOMOKA"
            if project_variant.exists():
                candidates.append(project_variant / "models" / "image-models")

            candidates.append(ancestor / "models" / "image-models")

        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.exists():
                logger.debug("Using image model root %s", candidate)
                return cls(candidate)

        default_root = module_path.parents[3] / "models" / "image-models"
        logger.debug("Using default image model root %s", default_root)
        return cls(default_root)
