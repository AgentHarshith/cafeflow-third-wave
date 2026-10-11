"""List Gemini models available to the configured API key (prints names only).

Usage:  python scripts/list_models.py
Reads GEMINI_API_KEY from the environment or the project's .env file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
key = os.getenv("GEMINI_API_KEY", "").strip()
if not key:
    sys.exit("GEMINI_API_KEY is not set (add it to .env).")

client = genai.Client(api_key=key, vertexai=False)
for model in client.models.list():
    actions = getattr(model, "supported_actions", None) or []
    if "generateContent" in actions:
        print(model.name.removeprefix("models/"))
