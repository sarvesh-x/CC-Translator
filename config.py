"""Application configuration loaded from environment and optional .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(__file__).with_name(".env")) -> None:
    """Load simple KEY=VALUE lines without overriding existing environment values."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv()

_requested_deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
LEGACY_DEEPSEEK_MODEL = _requested_deepseek_model in {"deepseek-chat", "deepseek-reasoner"}
_deepseek_model = "deepseek-flash" if LEGACY_DEEPSEEK_MODEL else _requested_deepseek_model


@dataclass(frozen=True)
class Settings:
    """Runtime settings for translation providers and request behavior."""

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = _deepseek_model
    libretranslate_url: str = os.getenv("LIBRETRANSLATE_URL", "http://localhost:5000")
    libretranslate_api_key: str = os.getenv("LIBRETRANSLATE_API_KEY", "")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3:4b")
    ollama_timeout: int = int(os.getenv("OLLAMA_TIMEOUT", "300"))
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "45"))
    request_retries: int = int(os.getenv("REQUEST_RETRIES", "4"))
    batch_size: int = int(os.getenv("TRANSLATION_BATCH_SIZE", "12"))


SETTINGS = Settings()
