"""ADK agent tests with a scripted fake model (no network, no quota).

The fake model replaces Gemini inside the real ADK graph, so these tests exercise the
actual Barista AI router, transfers, sessions, tool execution and fallbacks.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import errors, types

from app.agent import CafeAgent, _thinking_level_supported
from app.config import Settings

SETTINGS = Settings(gemini_api_key="test-key-not-real", gemini_model="gemini-test-model", max_tool_rounds=4)


class FakeLlm(BaseLlm):
    """Plays back scripted model turns and records every request it receives."""

    model: str = "fake-gemini"
    script: Any = None

    async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.script["requests"].append(llm_request)
        step = self.script["steps"].pop(0) if self.script["steps"] else text("(script exhausted)")
        if isinstance(step, Exception):
            raise step
        yield LlmResponse(content=step)


def call(name: str, args: dict) -> types.Content:
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])


def transfer(agent: str) -> types.Content:
    return call("transfer_to_agent", {"agent_name": agent})


def text(value: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part.from_text(text=value)])


def make_agent(executor, steps, settings=SETTINGS):
    script = {"steps": list(steps), "requests": []}
    return CafeAgent(settings, executor, llm=FakeLlm(script=script)), script


def chat(agent, message, session="session-0001", shown=None):
    return asyncio.run(agent.chat(message, [], shown, session))


def function_responses(request) -> list[types.FunctionResponse]:
    return [p.function_response for c in request.contents for p in (c.parts or []) if p.function_response]


def instruction_of(request) -> str:
    return str(request.config.system_instruction)


def test_router_transfers_to_discovery_which_runs_the_real_tool(executor):
    agent, script = make_agent(executor, [
        transfer("discovery_agent"),
        call("recommend_options", {"budget_inr": 250, "dietary_preference": "vegetarian",
                                   "deadline_minutes": 10, "want_filling": True}),
        text("Top pick: **Vegetable Puff + Eggless Banana Walnut Bread** for ₹200, ready in ~4 min."),
    ])
    result = chat(agent, "I'm vegetarian, have ₹250 and need something filling in 10 minutes")

    assert result.mode == "gemini"
    assert result.agent_route == ["Barista AI", "Discovery"]
    assert [(t.name, t.agent, t.ok) for t in result.tool_calls] == [("recommend_options", "Discovery", True)]
    assert result.recommendations and all(o["meets_all_constraints"] for o in result.recommendations)
    assert "Barista AI" in instruction_of(script["requests"][0])
    assert "Discovery specialist" in instruction_of(script["requests"][1])
    # the real tool result (not an invented one) was sent back to Gemini
    sent = function_responses(script["requests"][2])[-1]
    assert sent.name == "recommend_options" and sent.response["options"][0]["total_price_inr"] <= 250
    assert agent.status.state == "connected"


def test_follow_up_goes_straight_to_the_active_specialist(executor):
    agent, script = make_agent(executor, [
        transfer("discovery_agent"),
        call("recommend_options", {"budget_inr": 250, "dietary_preference": "vegetarian", "deadline_minutes": 10}),
        text("Here are some options."),
        call("recommend_options", {"budget_inr": 250, "dietary_preference": "vegetarian", "deadline_minutes": 5}),
        text("With five minutes, these still work."),
    ])
    chat(agent, "vegetarian, ₹250, 10 minutes")
    second = chat(agent, "Actually, I only have five minutes.")
    assert second.agent_route == ["Discovery"]
    assert "Discovery specialist" in instruction_of(script["requests"][3])
    assert second.tool_calls[0].arguments["deadline_minutes"] == 5
    assert all(o["estimated_wait_minutes"] <= 5 for o in second.recommendations)


def test_sessions_are_isolated_per_browser(executor):
    agent, _ = make_agent(executor, [
        transfer("discovery_agent"), text("Discovery here."),
        text("Hi! Barista AI here."),
    ])
    chat(agent, "recommend something", session="session-aaaa")
    other = chat(agent, "hello", session="session-bbbb")
    assert other.agent_route == ["Barista AI"]  # a new browser starts at the router, not Discovery


def test_ordering_agent_drafts_but_never_places_an_order(executor, store):
    agent, _ = make_agent(executor, [
        transfer("ordering_agent"),
        call("prepare_order", {"item_ids": ["veg_puff_01", "masala_chai_01"]}),
        text("Here's your draft — tap Review & confirm to place it."),
    ])
    result = chat(agent, "I'll take the puff and a chai")
    assert result.agent_route == ["Barista AI", "Ordering"]
    assert result.order_proposal["order_placed"] is False
    assert result.order_proposal["total_inr"] == 160
    assert store.list_orders() == []


def test_wait_time_agent_uses_the_queue_aware_estimator(executor, store):
    store.create_order(["paneer_tikka_sandwich_01"])  # 8 min of kitchen work queued
    agent, _ = make_agent(executor, [
        transfer("wait_time_agent"),
        call("estimate_wait", {"item_ids": ["veg_club_sandwich_01"]}),
        text("About 15 minutes right now."),
    ])
    result = chat(agent, "how long for a veg club sandwich?")
    assert result.agent_route == ["Barista AI", "Wait-time"]
    assert result.wait_estimate["estimated_wait_minutes"] == 15  # 8 queued + 7 own prep


def test_malformed_tool_arguments_are_rejected_and_reported(executor):
    agent, script = make_agent(executor, [
        transfer("discovery_agent"),
        call("search_menu", {"budget_inr": "lots", "rm_rf": True}),
        text("Could you tell me your budget in rupees?"),
    ])
    result = chat(agent, "cheap stuff")
    assert result.tool_calls[0].ok is False
    assert "Invalid arguments" in function_responses(script["requests"][2])[-1].response["error"]


def test_unknown_tool_is_never_executed(executor, store):
    agent, _ = make_agent(executor, [call("delete_all_orders", {}), text("I can't do that.")])
    result = chat(agent, "delete everything")
    assert result.reply == "I can't do that."
    assert result.tool_calls == []
    assert store.list_orders() == []


def test_router_handles_small_talk_itself(executor):
    agent, _ = make_agent(executor, [text("Hello! What are you in the mood for?")])
    result = chat(agent, "hello")
    assert result.mode == "gemini"
    assert result.agent_route == ["Barista AI"]
    assert result.tool_calls == []


def test_price_quoted_without_tools_triggers_a_tool_call(executor):
    agent, _ = make_agent(executor, [
        text("A latte is ₹99."),  # invented price — must not reach the customer
        transfer("discovery_agent"),
        call("search_menu", {"category": "Coffee"}),
        text("Our Hazelnut Latte is ₹190."),
    ])
    result = chat(agent, "how much is a latte")
    assert result.reply == "Our Hazelnut Latte is ₹190."
    assert [t.name for t in result.tool_calls] == ["search_menu"]


def test_agent_steps_are_bounded(executor):
    looping = [call("search_menu", {}) for _ in range(20)]
    agent, script = make_agent(executor, [transfer("discovery_agent"), *looping])
    result = chat(agent, "anything")
    assert result.mode == "fallback"  # gave up cleanly instead of looping forever
    assert len(script["requests"]) <= SETTINGS.max_tool_rounds * 2


def test_invalid_api_key_falls_back_to_labelled_demo_mode(executor):
    bad_key = errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                 "status": "INVALID_ARGUMENT"}})
    agent, _ = make_agent(executor, [bad_key])
    result = chat(agent, "vegan under ₹150")
    assert result.mode == "fallback"
    assert "API key" in result.notice
    assert "test-key-not-real" not in result.model_dump_json()
    assert agent.status.state == "error"


def test_on_screen_options_are_sent_as_app_context(executor):
    agent, script = make_agent(executor, [text("Sure!")])
    chat(agent, "I'll take option 2", shown=[["espresso_01"], ["veg_puff_01", "masala_chai_01"], ["made_up_99"]])
    parts = script["requests"][0].contents[-1].parts
    assert parts[0].text == "I'll take option 2"
    context = parts[1].text
    assert "Option 2: Vegetable Puff (veg_puff_01) + Masala Chai (masala_chai_01)" in context
    assert "made_up_99" not in context  # ids not on the menu are dropped, names come from the server


def test_echoed_context_never_reaches_the_customer(executor):
    leaked = "Here you go.\n\n[Cards shown — Option 1: Butter Croissant [tw_butter_croissant]]"
    agent, _ = make_agent(executor, [text(leaked)])
    assert chat(agent, "hi").reply == "Here you go."


def test_thinking_level_only_requested_from_models_that_support_it():
    assert _thinking_level_supported("gemini-3.8-flash", "low")
    assert _thinking_level_supported("gemini-flash-latest", "low")
    assert not _thinking_level_supported("gemini-2.5-flash", "low")
    assert not _thinking_level_supported("gemini-3.8-flash", None)


def test_cards_follow_the_items_named_in_the_reply(executor):
    agent, _ = make_agent(executor, [
        transfer("discovery_agent"),
        call("search_menu", {"category": "drinks"}),
        text("Try the **Classic Cold Brew** — smooth and barely sweet."),
    ])
    result = chat(agent, "a must-try drink?")
    assert [i["id"] for i in result.menu_items] == ["cold_brew_01"]
