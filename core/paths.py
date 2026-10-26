from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    """Directory beside the source file or executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_root() -> Path:
    """PyInstaller's temporary resource root for one-file bundles."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return project_root()


def resolve_models_root() -> Path:
    """Resolve an external model directory first, then packaged resources."""
    override = os.environ.get("OFFLINE_OCR_MODELS")
    if override:
        return Path(override).expanduser().resolve()

    candidates = [project_root() / "models", bundled_root() / "models"]
    for candidate in candidates:
        if (candidate / "READY.json").is_file():
            return candidate.resolve()
    # Keep a predictable path so the error message can tell the user where to put models.
    return candidates[0].resolve()


def configure_model_environment() -> Path:
    models_root = resolve_models_root()
    cache_home = models_root / "paddlex_cache"
    # Set these before importing paddleocr / paddlex. Otherwise they may initialize
    # a cache in the user's profile and attempt an online download on the target PC.
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache_home)
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")
    return models_root
