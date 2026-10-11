"""Application configuration, read from environment variables.

A local ``.env`` file is loaded for development convenience only; on Cloud Run the
same variables are injected by the platform (or Secret Manager), so the app never
depends on the file existing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
CAFES_DIR = APP_DIR / "data" / "cafes"
DEFAULT_CAFE_PROFILE = "third_wave"
STATIC_DIR = APP_DIR / "static"

# Alias maintained by Google that always points at the current Flash model, so we
# never ship a hardcoded model name that has been shut down. Pin a specific stable
# model with GEMINI_MODEL when you need reproducible behaviour.
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None = None
    gemini_model: str = DEFAULT_GEMINI_MODEL
    # Gemini 3+ reasoning effort. "low" keeps chat latency demo-friendly; "default" leaves it to the model.
    gemini_thinking_level: str | None = "low"
    app_env: str = "development"
    cafe_profile: str = DEFAULT_CAFE_PROFILE
    staff_pin: str | None = None  # when set, staff endpoints require the X-Staff-Pin header
    max_tool_rounds: int = 6
    chat_rate_limit_per_minute: int = 20

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def menu_path(self) -> Path:
        path = CAFES_DIR / f"{self.cafe_profile}.json"
        if not path.is_file():
            available = ", ".join(sorted(p.stem for p in CAFES_DIR.glob("*.json")))
            raise RuntimeError(f"Unknown CAFE_PROFILE '{self.cafe_profile}'. Available: {available}")
        return path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def load_settings() -> Settings:
    load_dotenv(PROJECT_DIR / ".env", override=False)
    thinking = os.getenv("GEMINI_THINKING_LEVEL", "").strip().lower() or "low"
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
        gemini_model=os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL,
        gemini_thinking_level=None if thinking == "default" else thinking,
        app_env=os.getenv("APP_ENV", "").strip() or "development",
        cafe_profile=os.getenv("CAFE_PROFILE", "").strip() or DEFAULT_CAFE_PROFILE,
        staff_pin=os.getenv("STAFF_PIN", "").strip() or None,
        max_tool_rounds=_int_env("MAX_TOOL_ROUNDS", 6),
        chat_rate_limit_per_minute=_int_env("CHAT_RATE_LIMIT_PER_MINUTE", 20),
    )
