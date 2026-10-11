"""Agent loop tests with a fake Gemini client (no network, no quota)."""

from __future__ import annotations

from google.genai import errors, types

from app.agent import CafeAgent
from app.config import Settings
from app.schemas import ChatTurn

SETTINGS = Settings(gemini_api_key="test-key-not-real", gemini_model="gemini-test-model", max_tool_rounds=4)


def call_response(name: str, args: dict) -> types.GenerateContentResponse:
    part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])


def text_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))]
    )


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list = []

    def generate_content(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def get(self, *, model):
        return types.Model(name=f"models/{model}")


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def make_agent(executor, script):
    fake = FakeClient(script)
    return CafeAgent(SETTINGS, executor, client_factory=lambda: fake), fake


def function_responses(request) -> list[types.FunctionResponse]:
    return [p.function_response for c in request["contents"] for p in (c.parts or []) if p.function_response]


def test_gemini_function_call_executes_real_tool(executor):
    agent, fake = make_agent(executor, [
        call_response("recommend_options", {"budget_inr": 250, "dietary_preference": "vegetarian",
                                            "deadline_minutes": 10, "want_filling": True}),
        text_response("Top pick: **Vegetable Puff + Eggless Banana Walnut Bread** for ₹200, ready in ~4 min."),
    ])
    result = agent.chat("I'm vegetarian, have ₹250 and need something filling in 10 minutes", [])

    assert result.mode == "gemini"
    assert result.reply.startswith("Top pick")
    assert [t.name for t in result.tool_calls] == ["recommend_options"]
    assert result.tool_calls[0].ok
    assert result.recommendations and all(o["meets_all_constraints"] for o in result.recommendations)
    # the real tool result (not an invented one) was sent back to Gemini
    sent = function_responses(fake.models.requests[1])
    assert sent[0].name == "recommend_options"
    assert sent[0].response["options"][0]["total_price_inr"] <= 250
    assert agent.status.state == "connected"


def test_malformed_tool_arguments_are_rejected_and_reported(executor):
    agent, fake = make_agent(executor, [
        call_response("search_menu", {"budget_inr": "lots", "rm_rf": True}),
        text_response("Sorry, could you tell me your budget in rupees?"),
    ])
    result = agent.chat("cheap stuff", [])
    assert result.tool_calls[0].ok is False
    error = function_responses(fake.models.requests[1])[0].response["error"]
    assert "Invalid arguments" in error


def test_unknown_tool_is_never_executed(executor):
    agent, fake = make_agent(executor, [
        call_response("delete_all_orders", {}),
        text_response("I can't do that."),
    ])
    result = agent.chat("hi", [])
    assert result.tool_calls[0].ok is False
    assert "Unknown tool" in function_responses(fake.models.requests[1])[0].response["error"]


def test_prepare_order_never_places_an_order(executor, store):
    agent, _ = make_agent(executor, [
        call_response("prepare_order", {"item_ids": ["veg_puff_01", "masala_chai_01"]}),
        text_response("Here's your draft — press Confirm to place it."),
    ])
    result = agent.chat("I'll take the puff and a chai", [])
    assert result.order_proposal["order_placed"] is False
    assert result.order_proposal["total_inr"] == 160
    assert store.list_orders() == []


def test_text_reply_without_function_call(executor):
    agent, _ = make_agent(executor, [text_response("Hello! What are you in the mood for?")])
    result = agent.chat("hello", [])
    assert result.mode == "gemini"
    assert result.tool_calls == []
    assert result.reply == "Hello! What are you in the mood for?"


def test_price_quoted_without_tools_triggers_a_tool_call(executor):
    agent, fake = make_agent(executor, [
        text_response("A latte is ₹99."),  # invented price — must not reach the customer
        call_response("search_menu", {"category": "Coffee"}),
        text_response("Our Hazelnut Latte is ₹190."),
    ])
    result = agent.chat("how much is a latte", [])
    assert result.reply == "Our Hazelnut Latte is ₹190."
    assert [t.name for t in result.tool_calls] == ["search_menu"]
    assert len(fake.models.requests) == 3


def test_tool_loop_is_bounded(executor):
    looping = [call_response("search_menu", {}) for _ in range(SETTINGS.max_tool_rounds)]
    agent, fake = make_agent(executor, [*looping, text_response("Here are some options.")])
    result = agent.chat("anything", [])
    assert len(result.tool_calls) == SETTINGS.max_tool_rounds
    final_config = fake.models.requests[-1]["config"]
    assert final_config.tool_config.function_calling_config.mode == "NONE"
    assert result.reply == "Here are some options."


def test_invalid_api_key_falls_back_to_labelled_demo_mode(executor):
    bad_key = errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                 "status": "INVALID_ARGUMENT"}})
    agent, _ = make_agent(executor, [bad_key])
    result = agent.chat("vegan under ₹150", [])
    assert result.mode == "fallback"
    assert "API key" in result.notice
    assert "test-key-not-real" not in result.model_dump_json()
    assert agent.status.state == "error"


def test_rate_limit_is_retried_once_then_succeeds(executor, monkeypatch):
    monkeypatch.setattr("app.agent.time.sleep", lambda _: None)
    limited = errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}})
    agent, _ = make_agent(executor, [limited, text_response("Hi there!")])
    assert agent.chat("hi", []).mode == "gemini"


def test_history_is_passed_as_conversation_context(executor):
    agent, fake = make_agent(executor, [text_response("Sure.")])
    history = [ChatTurn(role="assistant", text="Welcome!"), ChatTurn(role="user", text="I'm vegan"),
               ChatTurn(role="assistant", text="Noted.")]
    agent.chat("Actually, I only have five minutes.", history)
    contents = fake.models.requests[0]["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]  # leading assistant turn dropped
    assert contents[-1].parts[0].text == "Actually, I only have five minutes."


def test_thinking_level_is_dropped_if_model_rejects_it(executor):
    settings = Settings(gemini_api_key="test-key-not-real", gemini_model="gemini-3.8-flash")
    rejected = errors.ClientError(400, {"error": {"code": 400, "message": "thinking_level is not supported",
                                                  "status": "INVALID_ARGUMENT"}})
    fake = FakeClient([rejected, text_response("Hi!")])
    agent = CafeAgent(settings, executor, client_factory=lambda: fake)
    assert agent.chat("hi", []).mode == "gemini"
    first, second = fake.models.requests
    assert first["config"].thinking_config.thinking_level == "LOW"
    assert second["config"].thinking_config is None


def test_on_screen_options_are_sent_as_app_context(executor):
    agent, fake = make_agent(executor, [text_response("Sure!")])
    agent.chat("I'll take option 2", [], [["espresso_01"], ["veg_puff_01", "masala_chai_01"], ["made_up_99"]])
    parts = fake.models.requests[0]["contents"][-1].parts
    assert parts[0].text == "I'll take option 2"
    context = parts[1].text
    assert "Option 2: Vegetable Puff (veg_puff_01) + Masala Chai (masala_chai_01)" in context
    assert "made_up_99" not in context  # ids not on the menu are dropped, names come from the server


def test_echoed_context_never_reaches_the_customer(executor):
    leaked = "Here you go.\n\n[Cards shown — Option 1: Butter Croissant [tw_butter_croissant]]"
    agent, _ = make_agent(executor, [text_response(leaked)])
    assert agent.chat("hi", []).reply == "Here you go."
