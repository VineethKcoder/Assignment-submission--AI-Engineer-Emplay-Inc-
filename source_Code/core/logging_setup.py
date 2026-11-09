"""Logging configuration shared by CLI, API and UI."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from core.settings import get_settings

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    log_dir = settings.path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(level)
    fileh = RotatingFileHandler(log_dir / "rfp.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(fileh)
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "sentence_transformers", "faiss", "PIL",
                  "transformers", "huggingface_hub", "easyocr"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
