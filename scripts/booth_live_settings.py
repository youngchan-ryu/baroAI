"""Read only approved AI settings for a private booth, without persisting them."""
from __future__ import annotations

import io
import os
import stat
from pathlib import Path

from dotenv import dotenv_values

TEXT_KEYS = frozenset({
    "OPENAI_API_KEY", "OPENAI_MODEL_TEXT_SMALL", "LLM_PROTOCOL",
    "LLM_CHAT_REASONING_EFFORT", "LLM_CHAT_EXPLICIT_CACHE", "LLM_PRICE_TABLE",
    "LLM_PRICE_AS_OF", "LLM_PRICE_SOURCE", "LLM_MAX_OUTPUT_TOKENS",
})
VISION_KEYS = frozenset({"OPENAI_MODEL_VISION", "LLM_MAX_IMAGES"})


def live_overlay(path: Path, *, booth_live=False):
    """Return a memory-only overlay or None; validation of Settings follows separately."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 131072):
                return None
            allowed = TEXT_KEYS | VISION_KEYS if booth_live else TEXT_KEYS
            with os.fdopen(fd, encoding="utf-8", closefd=False) as f:
                selected = []
                seen = set()
                for line in f:
                    key, sep, _ = line.partition("=")
                    key = key.strip()
                    if sep and key in allowed:
                        if key in seen:
                            return None
                        seen.add(key)
                        selected.append(line)
            values = dotenv_values(stream=io.StringIO("".join(selected)), interpolate=False)
            if set(values) != seen or not values.get("OPENAI_API_KEY") or not values.get(
                "OPENAI_MODEL_TEXT_SMALL"
            ):
                return None
            if any(v is None for v in values.values()):
                return None
            return {**values, "OPENAI_MODEL_VISION": values.get("OPENAI_MODEL_VISION", ""),
                    "LLM_FAKE": "false",
                    "DAILY_LLM_BUDGET_USD": "2.00"}
        finally:
            os.close(fd)
    except (OSError, UnicodeError, ValueError):
        return None
