from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import CAFES_DIR, Settings
from app.main import create_app
from app.menu_service import MenuService
from app.order_service import OrderStore
from app.tools import ToolExecutor


@pytest.fixture
def menu() -> MenuService:
    return MenuService.from_file(CAFES_DIR / "daily_grind.json")


@pytest.fixture
def store(menu) -> OrderStore:
    return OrderStore(menu)


@pytest.fixture
def executor(menu, store) -> ToolExecutor:
    return ToolExecutor(menu, store)


@pytest.fixture
def client() -> TestClient:
    # Explicit settings with no API key: tests never depend on a local .env or spend quota.
    return TestClient(create_app(Settings(gemini_api_key=None, cafe_profile="daily_grind")))
