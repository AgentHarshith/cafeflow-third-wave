"""Integration tests against a real PostgreSQL (the same engine as Cloud SQL).

Skipped unless TEST_DB_HOST is set — CI runs them with a postgres service container:
    TEST_DB_HOST=localhost TEST_DB_PASSWORD=... pytest tests/test_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.config import CAFES_DIR, Settings
from app.db import DatabaseConfig, PostgresOrderRepository
from app.menu_service import MenuService
from app.order_service import InvalidTransitionError, OrderNotFoundError, OrderStore
from app.tools import ToolExecutor

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST not set")


@pytest.fixture(scope="module")
def db_config() -> DatabaseConfig:
    return DatabaseConfig(
        name=os.getenv("TEST_DB_NAME", "cafeflow_test"),
        user=os.getenv("TEST_DB_USER", "postgres"),
        password=os.getenv("TEST_DB_PASSWORD", ""),
        host=os.getenv("TEST_DB_HOST"),
        port=int(os.getenv("TEST_DB_PORT", "5432")),
    )


@pytest.fixture
def pg_store(db_config) -> OrderStore:
    repo = PostgresOrderRepository(db_config)
    with repo._pool.connection() as conn:
        conn.execute("TRUNCATE orders")
    return OrderStore(MenuService.from_file(CAFES_DIR / "daily_grind.json"), repository=repo)


def test_orders_round_trip_through_postgres(pg_store):
    order = pg_store.create_order(["veg_puff_01", "veg_puff_01", "masala_chai_01"])
    assert pg_store.backend == "cloudsql"
    fetched = pg_store.get(order.order_id)
    assert fetched == order
    assert fetched.total_inr == 240 and fetched.item_count == 3
    assert [o.order_id for o in pg_store.list_orders()] == [order.order_id]
    assert pg_store.count_by_status() == {"confirmed": 1}


def test_status_transitions_are_enforced_in_postgres(pg_store):
    order = pg_store.create_order(["cappuccino_01"])
    with pytest.raises(InvalidTransitionError):
        pg_store.update_status(order.order_id, "ready")
    assert pg_store.get(order.order_id).status == "confirmed"  # rolled back
    for status in ("preparing", "ready", "completed"):
        assert pg_store.update_status(order.order_id, status).status == status
    with pytest.raises(OrderNotFoundError):
        pg_store.update_status("CF-000000", "preparing")


def test_queue_backlog_is_computed_from_postgres(pg_store):
    sandwich = [pg_store._menu.get("paneer_tikka_sandwich_01")]
    base = pg_store.estimate(sandwich)["estimated_wait_minutes"]
    order = pg_store.create_order(["veg_club_sandwich_01"])  # 7 min kitchen
    assert pg_store.estimate(sandwich)["estimated_wait_minutes"] == base + 7
    pg_store.update_status(order.order_id, "preparing")
    assert pg_store.estimate(sandwich)["estimated_wait_minutes"] == base + 4  # ceil(3.5)


def test_adk_sessions_persist_in_postgres_across_instances(db_config, pg_store):
    from google.adk.sessions import DatabaseSessionService

    from app.agent import CafeAgent
    from tests.test_agent import FakeLlm, call, text, transfer

    settings = Settings(gemini_api_key="test-key-not-real", gemini_model="gemini-test-model", max_tool_rounds=4)
    executor = ToolExecutor(pg_store._menu, pg_store)
    session = "pg-" + uuid.uuid4().hex[:12]
    script = {"steps": [transfer("discovery_agent"),
                        call("recommend_options", {"budget_inr": 250, "deadline_minutes": 10}),
                        text("Here you go."),
                        call("recommend_options", {"budget_inr": 250, "deadline_minutes": 5}),
                        text("Still quick.")], "requests": []}

    def instance() -> CafeAgent:  # a separate "Cloud Run instance" sharing only the database
        return CafeAgent(settings, executor, llm=FakeLlm(script=script),
                         session_service=DatabaseSessionService(db_url=db_config.sqlalchemy_url()))

    first = asyncio.run(instance().chat("₹250, 10 minutes", [], None, session))
    second = asyncio.run(instance().chat("actually five minutes", [], None, session))
    assert first.agent_route == ["Barista AI", "Discovery"]
    assert second.agent_route == ["Discovery"]  # the new instance resumed the stored session
