"""Staff menu editing, feedback + Room insights, Connect People, and order delays."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import CAFES_DIR, Settings
from app.connect_service import REQUEST_KIND, ConnectService, score
from app.db import InMemoryDocStore
from app.feedback_service import DemandLog, FeedbackService
from app.main import create_app
from app.menu_admin import MenuAdmin
from app.menu_service import CafeError, MenuService, UnavailableItemError
from app.order_service import InvalidTransitionError, OrderNotFoundError, OrderStore
from app.tools import ToolExecutor

PIN = "2468"


def fresh_menu() -> MenuService:
    return MenuService.from_file(CAFES_DIR / "third_wave.json")


# -- menu editing -----------------------------------------------------------------


def test_price_edit_is_used_for_search_quotes_and_orders():
    menu, docs = fresh_menu(), InMemoryDocStore()
    admin, store = MenuAdmin(menu, docs), OrderStore(menu)
    admin.update("tw_latte", price_inr=199, price_large_inr=229)
    assert menu.get("tw_latte").price_inr == 199
    assert store.quote(["tw_latte"])["total_inr"] == 199
    assert store.create_order(["tw_latte"]).total_inr == 199
    assert {i.id for i in menu.search(budget_inr=200)} >= {"tw_latte"}


def test_sold_out_items_are_not_recommended_or_orderable():
    menu, docs = fresh_menu(), InMemoryDocStore()
    admin, store = MenuAdmin(menu, docs), OrderStore(menu)
    admin.update("tw_margherita_pizza", available=False)
    assert "tw_margherita_pizza" not in {i.id for i in menu.search()}
    with pytest.raises(UnavailableItemError):
        store.create_order(["tw_margherita_pizza"])
    admin.reset("tw_margherita_pizza")
    assert menu.get("tw_margherita_pizza").available


def test_invalid_price_edits_are_rejected():
    admin = MenuAdmin(fresh_menu(), InMemoryDocStore())
    with pytest.raises(CafeError):
        admin.update("tw_margherita_pizza", price_large_inr=200)  # pizza has no Large size
    with pytest.raises(CafeError):
        admin.update("tw_latte", price_large_inr=100)  # Large cheaper than Regular


def test_price_edits_reach_other_instances_through_the_shared_store():
    docs = InMemoryDocStore()
    menu_a, menu_b = fresh_menu(), fresh_menu()
    admin_a, admin_b = MenuAdmin(menu_a, docs), MenuAdmin(menu_b, docs)
    admin_a.update("tw_americano", price_inr=199)
    admin_b.sync(force=True)
    assert menu_b.get("tw_americano").price_inr == 199
    admin_a.reset("tw_americano")
    admin_b.sync(force=True)
    assert menu_b.get("tw_americano").price_inr == 209


# -- feedback & demand ----------------------------------------------------------


def test_feedback_is_recorded_merged_and_summarised():
    menu, docs = fresh_menu(), InMemoryDocStore()
    store = OrderStore(menu)
    order = store.create_order(["tw_latte"])
    service = FeedbackService(docs, store)
    saved = service.record(order_id=order.order_id, rating=2, themes=["wait_time"], comment="took ages", source="form")
    assert saved["sentiment"] == "negative" and saved["id"].startswith("FB-")
    merged = service.record(feedback_id=saved["id"], themes=["temperature"], comment="and it was lukewarm")
    assert merged["themes"] == ["temperature", "wait_time"] and len(merged["comments"]) == 2
    assert service.given_for_order(order.order_id)
    summary = service.summary()
    assert summary["count"] == 1 and summary["average_rating"] == 2.0 and summary["sentiment"] == {"negative": 1}
    with pytest.raises(OrderNotFoundError):
        service.record(order_id="CF-000000", rating=5)
    with pytest.raises(CafeError):
        service.record(themes=["vibes"])


def test_demand_log_surfaces_unmet_requests():
    docs = InMemoryDocStore()
    log = DemandLog(docs)
    log.record({"dietary_preference": "vegetarian", "deadline_minutes": 5, "want_filling": True}, 0)
    log.record({"dietary_preference": "vegetarian", "deadline_minutes": 5, "want_filling": True}, 0)
    log.record({"budget_inr": 300}, 4)
    summary = log.summary()
    assert summary["requests"] == 3 and summary["unmet"] == 2
    assert summary["top_unmet"][0] == {"request": "vegetarian · ≤5 min · filling", "count": 2}


# -- order delays ---------------------------------------------------------------


def test_delay_pushes_the_estimate_and_keeps_the_apology():
    store = OrderStore(fresh_menu())
    order = store.create_order(["tw_latte"])
    delayed = store.delay(order.order_id, 10, "Sorry, running 10 minutes behind.")
    assert delayed.delay_minutes == 10
    assert delayed.estimated_wait_minutes == order.estimated_wait_minutes + 10
    assert delayed.estimated_ready_at >= order.estimated_ready_at + timedelta(minutes=10)
    assert delayed.delay_message.startswith("Sorry")
    for status in ("preparing", "ready"):
        store.update_status(order.order_id, status)
    with pytest.raises(InvalidTransitionError):
        store.delay(order.order_id, 5, "too late")


# -- Connect People ---------------------------------------------------------------

ANSWERS_A = {"coffee": "iced", "milk": "plant", "vibe": "chat"}
ANSWERS_B = {"coffee": "iced", "milk": "plant", "vibe": "catch_up"}


def test_match_score_is_symmetric_and_bounded():
    assert score(ANSWERS_A, ANSWERS_A)[0] == 100
    assert score(ANSWERS_A, ANSWERS_B) == score(ANSWERS_B, ANSWERS_A)
    low, shared = score({"coffee": "black", "milk": "none", "vibe": "work"},
                        {"coffee": "no_coffee", "milk": "regular", "vibe": "catch_up"})
    assert 0 <= low < 30 and shared == []


def test_two_accounts_connect_answer_and_both_see_the_match():
    docs = InMemoryDocStore()
    service = ConnectService(docs, icebreaker=lambda a, b, shared: "Ask which iced drink they'd recommend.")
    assert service.start("session-alice")["needs_signup"] is True
    service.register("session-alice", "Oat Latte Fan", 4)
    service.register("session-bob", "Cold Brew Bob", 7)

    started = service.start("session-alice")
    assert started["people_notified"] == 1 and not started["answered"]
    invites = service.state("session-bob")["invites"]
    assert invites == [{"request_id": started["request_id"], "from": {"nickname": "Oat Latte Fan", "table": 4}}]

    service.answer("session-alice", started["request_id"], ANSWERS_A)
    service.answer("session-bob", started["request_id"], ANSWERS_B)

    alice, bob = service.state("session-alice"), service.state("session-bob")
    assert bob["invites"] == []  # answered, so no more pop-up
    assert len(alice["matches"]) == len(bob["matches"]) == 1
    assert alice["matches"][0]["percent"] == bob["matches"][0]["percent"] == score(ANSWERS_A, ANSWERS_B)[0]
    assert alice["matches"][0]["with_profile"] == {"nickname": "Cold Brew Bob", "table": 7}
    assert bob["matches"][0]["with_profile"] == {"nickname": "Oat Latte Fan", "table": 4}
    assert alice["matches"][0]["icebreaker_by"] == "gemini"


def test_connect_validation_decline_and_expiry():
    docs = InMemoryDocStore()
    service = ConnectService(docs)
    with pytest.raises(CafeError):
        service.register("session-x", "<script>", None)
    service.register("session-alice", "Alice", None)
    service.register("session-bob", "Bob", None)
    request_id = service.start("session-alice")["request_id"]
    with pytest.raises(CafeError):
        service.answer("session-bob", request_id, {"coffee": "iced"})  # all 3 questions required
    service.decline("session-bob", request_id)
    assert service.state("session-bob")["invites"] == []
    docs.update(REQUEST_KIND, request_id, lambda r: {**r, "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    assert service.state("session-alice")["my_request"] is None


# -- API: staff PIN, menu editing, delays, feedback form, connect -------------------


@pytest.fixture
def api() -> TestClient:
    return TestClient(create_app(Settings(gemini_api_key=None, cafe_profile="third_wave", staff_pin=PIN),
                                 use_database=False))


def test_staff_endpoints_require_the_pin(api):
    assert api.get("/api/orders").status_code == 401
    assert api.get("/api/orders", headers={"X-Staff-Pin": "0000"}).status_code == 401
    assert api.get("/api/orders", headers={"X-Staff-Pin": PIN}).status_code == 200
    assert api.patch("/api/staff/menu/tw_latte", json={"price_inr": 1}).status_code == 401


def test_staff_menu_edit_via_api_changes_customer_prices(api):
    pin = {"X-Staff-Pin": PIN}
    assert api.patch("/api/staff/menu/tw_latte", json={"price_inr": 205}, headers=pin).json()["price_inr"] == 205
    assert next(i for i in api.get("/api/menu").json()["items"] if i["id"] == "tw_latte")["price_inr"] == 205
    assert api.post("/api/orders/quote", json={"item_ids": ["tw_latte"]}).json()["total_inr"] == 205
    staff_row = next(i for i in api.get("/api/staff/menu", headers=pin).json()["items"] if i["id"] == "tw_latte")
    assert staff_row["edited"] and staff_row["default_price_inr"] == 219
    assert api.patch("/api/staff/menu/tw_latte", json={"price_inr": 0}, headers=pin).status_code == 422


def test_delay_and_customer_tracking_via_api(api):
    order = api.post("/api/orders", json={"item_ids": ["tw_latte"], "confirmed": True}).json()
    delayed = api.post(f"/api/orders/{order['order_id']}/delay", json={"minutes": 10}, headers={"X-Staff-Pin": PIN}).json()
    assert delayed["delay_minutes"] == 10 and "Sorry" in delayed["delay_message"]
    tracked = api.get(f"/api/orders/{order['order_id']}/track").json()
    assert tracked["order"]["delay_minutes"] == 10 and tracked["feedback_given"] is False
    assert api.post(f"/api/orders/{order['order_id']}/delay", json={"minutes": 7}, headers={"X-Staff-Pin": PIN}).status_code == 422


def test_feedback_form_is_saved_and_shows_in_the_room(api):
    order = api.post("/api/orders", json={"item_ids": ["tw_latte"], "confirmed": True}).json()
    reply = api.post("/api/chat", json={
        "message": f"Feedback for order {order['order_id']}: 2/5 — Took too long",
        "session_id": "session-feedback-1",
        "feedback_form": {"order_id": order["order_id"], "rating": 2, "chips": ["Took too long"], "comment": "slow today"},
    }).json()
    assert reply["feedback"]["saved"] and "feedback is saved" in reply["reply"] and reply["recommendations"] == []
    assert api.get(f"/api/orders/{order['order_id']}/track").json()["feedback_given"] is True
    room = api.get("/api/staff/room", headers={"X-Staff-Pin": PIN}).json()
    assert room["feedback"]["count"] == 1 and room["feedback"]["themes"] == {"wait_time": 1}
    brief = api.post("/api/staff/brief", headers={"X-Staff-Pin": PIN}).json()["brief"]
    assert brief["by"] == "template" and brief["text"].startswith("- ")


def test_connect_flow_via_api_with_two_browsers(api):
    a, b = "session-alice-01", "session-bob-0001"
    assert api.post("/api/connect/request", json={"session_id": a}).json()["needs_signup"] is True
    for sid, nick in ((a, "Alice"), (b, "Bob")):
        assert api.post("/api/connect/profile", json={"session_id": sid, "nickname": nick, "consent": True}).status_code == 200
    assert api.post("/api/connect/profile", json={"session_id": b, "nickname": "Bob", "consent": False}).status_code == 422
    request_id = api.post("/api/connect/request", json={"session_id": a}).json()["request_id"]
    assert api.post("/api/connect/state", json={"session_id": b}).json()["invites"][0]["request_id"] == request_id
    for sid, ans in ((a, ANSWERS_A), (b, ANSWERS_B)):
        assert api.post("/api/connect/answer", json={"session_id": sid, "request_id": request_id, "answers": ans}).status_code == 200
    match = api.post("/api/connect/state", json={"session_id": a}).json()["matches"][0]
    assert match["percent"] == score(ANSWERS_A, ANSWERS_B)[0] and match["icebreaker_by"] == "template"


def test_demo_mode_understands_connect_me_with_people(api):
    reply = api.post("/api/chat", json={"message": "connect me with people", "session_id": "session-carol-01"}).json()
    assert reply["connect"] == {"action": "signup"} and "Sign up to connect" in reply["reply"]


# -- ADK agents: Feedback and Connect People ------------------------------------------


def test_feedback_and_connect_agents_through_adk():
    from app.agent import CafeAgent
    from tests.test_agent import FakeLlm, call, text, transfer

    menu, docs = fresh_menu(), InMemoryDocStore()
    store = OrderStore(menu)
    order = store.create_order(["tw_latte"])
    connect = ConnectService(docs)
    executor = ToolExecutor(menu, store, feedback=FeedbackService(docs, store), connect=connect, demand=DemandLog(docs))
    script = {"steps": [
        transfer("feedback_agent"),
        call("record_feedback", {"order_id": order.order_id, "rating": 2, "sentiment": "negative",
                                 "themes": ["temperature"], "comment": "latte was lukewarm"}),
        text("So sorry it was lukewarm — was it like that as soon as you picked it up?"),
        transfer("connect_agent"),
        call("start_connect", {}),
        text("Tap Sign up to connect first — just a nickname."),
    ], "requests": []}
    agent = CafeAgent(Settings(gemini_api_key="k", gemini_model="gemini-test-model"), executor, llm=FakeLlm(script=script))

    fb = asyncio.run(agent.chat("my latte was lukewarm", [], None, "session-dave-001"))
    assert fb.agent_route == ["Barista AI", "Feedback"] and fb.feedback["saved"]
    assert FeedbackService(docs, store).summary()["themes"] == {"temperature": 1}

    cn = asyncio.run(agent.chat("actually, connect me with people", [], None, "session-dave-001"))
    assert cn.agent_route == ["Feedback", "Connect People"]
    assert cn.connect == {"action": "signup"}  # the tool saw this browser's session, not one the model chose
