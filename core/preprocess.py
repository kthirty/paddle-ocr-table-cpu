from __future__ import annotations

import logging
import re
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

logger = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def clean_text(text: str, remove_whitespace: bool = False) -> str:
    text = "" if text is None else str(text)
    if remove_whitespace:
        # Includes line breaks, tabs, ASCII spaces and full-width spaces.
        text = re.sub(r"[\s\u3000]+", "", text)
    return text


def preprocess_image(source: str | Path, destination: str | Path) -> Path:
    """Conservative document enhancement before model inference.

    The original aspect ratio and color information are preserved. EXIF rotation,
    mild contrast normalization, subtle sharpening, and small-image upscaling help
    table borders and low-resolution text without forcing a harsh binary threshold.
    """
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")

    original_size = image.size
    width, height = image.size
    short_side = min(width, height)
    long_side = max(width, height)

    # Upscale low-resolution screenshots/photos only. Avoid making already large scans slower.
    if short_side < 1000 and long_side < 2600:
        scale = min(1.55, 2600 / max(long_side, 1))
        if scale > 1.02:
            image = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )

    # Limit extremely large images to bound CPU time and memory use.
    width, height = image.size
    if max(width, height) > 4200:
        scale = 4200 / max(width, height)
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )

    # Conservative contrast + edge sharpening. No aggressive binarization, which can
    # erase faint table borders, red stamps, decimal points, or small Chinese strokes.
    image = ImageOps.autocontrast(image, cutoff=0.4)
    image = ImageEnhance.Contrast(image).enhance(1.12)
    image = image.filter(ImageFilter.UnsharpMask(radius=1.1, percent=115, threshold=3))
    image.save(destination, format="PNG", optimize=True)
    logger.info(
        "图片预处理完成 | 文件=%s | 原始尺寸=%sx%s | 处理后尺寸=%sx%s | 输出=%s",
        source.name, original_size[0], original_size[1], image.width, image.height, destination,
    )
    return destination
