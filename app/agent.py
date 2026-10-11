"""CaféFlow AI conversational agent, built on Google ADK.

Barista AI (an ADK router agent) understands the customer and transfers to the
right specialist sub-agent (see adk_agents.py). Gemini decides which deterministic
backend tool to call; the tools run validated Python and their results go back to
Gemini. When Gemini is not configured or fails, a clearly-labelled rule-based
fallback drives the *same* tools, so the demo keeps working without ever pretending
to be a live Gemini answer.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.planners import BuiltInPlanner
from google.adk.runners import RunConfig, Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .adk_agents import AGENT_LABELS, ROUTER, build_agent_graph
from .config import Settings
from .schemas import ChatResponse, ChatTurn, ToolCallTrace
from .tools import ToolExecutor, ToolResult

logger = logging.getLogger("cafeflow.agent")

APP_NAME = "cafeflow"
HISTORY_TURNS = 12
VERIFY_CACHE_SECONDS = 300
MAX_SESSIONS = 500

_ECHOED_CONTEXT = re.compile(r"\s*\[(?:App context|Cards shown).*\Z", re.S)

PRICE_GUARD_PROMPT = (
    "Check: you mentioned prices or menu details without calling a tool in this turn. "
    "Call the appropriate tool now and answer only from its results."
)


# ---------------------------------------------------------------------------
# Connection status (drives the header indicator honestly)
# ---------------------------------------------------------------------------


class GeminiStatus:
    def __init__(self, configured: bool, model: str):
        self._lock = threading.Lock()
        self.state = "unverified" if configured else "not_configured"
        self.model = model
        self.detail = "API key present; no successful request yet." if configured else "GEMINI_API_KEY is not set."
        self.last_success_at: datetime | None = None
        self.last_success_monotonic: float | None = None

    def success(self, model: str, how: str) -> None:
        with self._lock:
            self.state, self.model, self.detail = "connected", model, how
            self.last_success_at = datetime.now(timezone.utc)
            self.last_success_monotonic = time.monotonic()

    def failure(self, detail: str) -> None:
        with self._lock:
            self.state, self.detail = "error", detail

    def recently_verified(self) -> bool:
        return (
            self.state == "connected"
            and self.last_success_monotonic is not None
            and time.monotonic() - self.last_success_monotonic < VERIFY_CACHE_SECONDS
        )

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "model": self.model,
                "detail": self.detail,
                "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            }


def _classify_error(exc: Exception) -> tuple[str, bool]:
    """Return (safe description, retryable). Never includes the API key or raw payloads."""
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "")
        message = str(getattr(exc, "message", "") or "").lower()
        if code in (401, 403) or "api key" in message or "api_key" in message or status == "PERMISSION_DENIED":
            return "Gemini rejected the API key (invalid, restricted or lacking access).", False
        if code == 404:
            return "The configured Gemini model is not available to this API key.", False
        if code == 429:
            return "Gemini rate limit or quota reached.", True
        if code and code >= 500:
            return f"Gemini service error ({code}).", True
        return f"Gemini request failed ({code} {status}).".replace(" )", ")"), False
    if type(exc).__name__ == "LlmCallsLimitExceededError":
        return "The agent used too many steps for one message.", False
    return "Could not reach the Gemini API.", True


def _thinking_level_supported(model: str, level: str | None) -> bool:
    """thinking_level exists on Gemini 3+ (and the -latest aliases that point at it)."""
    if not level:
        return False
    match = re.match(r"gemini-(\d+)", model)
    return model.endswith("-latest") or bool(match and int(match.group(1)) >= 3)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class CafeAgent:
    def __init__(self, settings: Settings, executor: ToolExecutor, llm: BaseLlm | None = None):
        self.settings = settings
        self.executor = executor
        self.model = settings.gemini_model
        self.status = GeminiStatus(settings.gemini_configured, self.model)
        self._llm_override = llm
        self._client: Any = None
        self._model_discovery_done = False
        self._turn_results: dict[str, list[tuple[str, ToolResult]]] = {}
        self._sessions: OrderedDict[str, None] = OrderedDict()
        self._session_service = InMemorySessionService()
        self._runner: Runner | None = None

    # -- Gemini client / model ---------------------------------------------------

    def _get_client(self):
        if self._client is None:
            from google import genai

            # Explicit AI Studio API-key auth; vertexai=False prevents silently switching to
            # Vertex credentials. Transient 429/5xx errors are retried with backoff.
            self._client = genai.Client(
                api_key=self.settings.gemini_api_key,
                vertexai=False,
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(
                        attempts=3, initial_delay=1.5, max_delay=6.0, http_status_codes=[429, 500, 502, 503, 504]
                    )
                ),
            )
        return self._client

    def _model_object(self) -> BaseLlm:
        if self._llm_override is not None:
            return self._llm_override
        return Gemini(model=self.model, client=self._get_client())

    def _planner(self) -> BuiltInPlanner | None:
        level = self.settings.gemini_thinking_level
        if self._llm_override is not None or not _thinking_level_supported(self.model, level):
            return None
        return BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_level=level.upper()))

    def _record(self, invocation_id: str, agent_name: str, result: ToolResult) -> None:
        self._turn_results.setdefault(invocation_id, []).append((agent_name, result))

    def _get_runner(self) -> Runner:
        if self._runner is None:
            root = build_agent_graph(self._model_object(), self.executor, self.executor.menu.cafe, self._record,
                                     planner=self._planner())
            self._runner = Runner(app_name=APP_NAME, agent=root, session_service=self._session_service)
        return self._runner

    def _discover_model(self) -> str | None:
        """Pick a generateContent-capable Flash model when the configured one 404s."""
        if self._model_discovery_done:
            return None
        self._model_discovery_done = True
        try:
            names = []
            for m in self._get_client().models.list():
                actions = getattr(m, "supported_actions", None) or []
                name = (getattr(m, "name", "") or "").removeprefix("models/")
                if "generateContent" in actions and name.startswith("gemini"):
                    names.append(name)
        except Exception:  # noqa: BLE001
            logger.warning("Model discovery failed")
            return None

        def rank(name: str) -> tuple:
            bad = any(t in name for t in ("lite", "image", "tts", "audio", "live", "embedding", "robotics", "computer"))
            unstable = any(t in name for t in ("preview", "exp"))
            version = [int(n) for n in re.findall(r"\d+", name)[:2]]
            return (bad, "flash" not in name, unstable, [-v for v in version])

        names.sort(key=rank)
        return names[0] if names else None

    def verify(self) -> dict[str, Any]:
        """Make a lightweight real API request (models.get) to confirm key + model."""
        if not self.settings.gemini_configured or self.status.recently_verified():
            return self.status.as_dict()
        try:
            self._get_client().models.get(model=self.model)
            self.status.success(self.model, "Verified with a live Gemini API request.")
        except Exception as exc:  # noqa: BLE001
            detail, _ = _classify_error(exc)
            if "model is not available" in detail:
                replacement = self._discover_model()
                if replacement:
                    self.model, self._runner = replacement, None  # rebuild agents on the new model
                    return self.verify()
            self.status.failure(detail)
        return self.status.as_dict()

    # -- sessions --------------------------------------------------------------

    async def _ensure_session(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions.move_to_end(session_id)
            return
        existing = await self._session_service.get_session(app_name=APP_NAME, user_id=session_id, session_id=session_id)
        if existing is None:
            await self._session_service.create_session(app_name=APP_NAME, user_id=session_id, session_id=session_id)
        self._sessions[session_id] = None
        while len(self._sessions) > MAX_SESSIONS:
            old, _ = self._sessions.popitem(last=False)
            await self._session_service.delete_session(app_name=APP_NAME, user_id=old, session_id=old)

    # -- chat ----------------------------------------------------------------

    async def chat(
        self,
        message: str,
        history: list[ChatTurn],
        shown_options: list[list[str]] | None = None,
        session_id: str | None = None,
    ) -> ChatResponse:
        session_id = session_id or uuid.uuid4().hex
        if not self.settings.gemini_configured and self._llm_override is None:
            return self._fallback(message, history, "Gemini API key not configured.", session_id)
        try:
            return await self._adk_chat(message, shown_options or [], session_id)
        except Exception as exc:  # noqa: BLE001 - any agent failure degrades to the labelled fallback
            detail, _ = _classify_error(exc)
            logger.warning("Agent turn failed: %s", detail)
            self.status.failure(detail)
            return self._fallback(message, history, detail, session_id)

    @staticmethod
    def _text_of(content) -> str:
        if content is None or not getattr(content, "parts", None):
            return ""
        return "".join(p.text for p in content.parts if getattr(p, "text", None) and not getattr(p, "thought", False))

    def _screen_context(self, shown_options: list[list[str]]) -> str | None:
        """Describe on-screen cards using names from the real menu (unknown ids are dropped)."""
        lines = []
        for n, ids in enumerate(shown_options[:12], start=1):
            items = [self.executor.menu.get(i) for i in ids]
            if ids and all(items):
                lines.append(f"Option {n}: " + " + ".join(f"{i.name} ({i.id})" for i in items))
        if not lines:
            return None
        return "[App context — option cards currently on screen: " + "; ".join(lines) + "]"

    async def _run_turn(self, session_id: str, content: types.Content) -> tuple[str, list[str], list[tuple[str, ToolResult]]]:
        """Run one ADK invocation; return (final text, agents involved, tool results)."""
        runner = self._get_runner()
        route: list[str] = []
        reply = ""
        invocation_ids: set[str] = set()
        run_config = RunConfig(max_llm_calls=max(4, self.settings.max_tool_rounds * 2))
        async for event in runner.run_async(user_id=session_id, session_id=session_id, new_message=content,
                                            run_config=run_config):
            invocation_ids.add(event.invocation_id)
            if event.author and event.author != "user" and (not route or route[-1] != event.author):
                route.append(event.author)
            if event.is_final_response():
                text = self._text_of(event.content).strip()
                if text:
                    reply = text
        results = [r for inv in invocation_ids for r in self._turn_results.pop(inv, [])]
        return reply, route, results

    async def _adk_chat(self, message: str, shown_options: list[list[str]], session_id: str) -> ChatResponse:
        await self._ensure_session(session_id)
        parts = [types.Part.from_text(text=message)]
        if context := self._screen_context(shown_options):
            parts.append(types.Part.from_text(text=context))

        reply, route, results = await self._run_turn(session_id, types.Content(role="user", parts=parts))
        # Guard against an answer that quotes prices without consulting the menu.
        if not results and "₹" in reply:
            guard = types.Content(role="user", parts=[types.Part.from_text(text=PRICE_GUARD_PROMPT)])
            reply, more_route, results = await self._run_turn(session_id, guard)
            route += [a for a in more_route if not route or a != route[-1]]

        self.status.success(self.model, "Last Gemini request succeeded.")
        reply = _ECHOED_CONTEXT.sub("", reply).strip()  # never show internal context/ids to the customer
        if not reply:
            reply = (
                "Here's what I found on the menu — see the options below."
                if results
                else "Sorry, I couldn't come up with an answer. Could you rephrase that?"
            )
        return self._build_response(reply, "gemini", results, model=self.model, route=route or [ROUTER],
                                    session_id=session_id)

    # -- structured payload ----------------------------------------------------

    @staticmethod
    def _build_response(reply: str, mode: str, results: list[tuple[str | None, ToolResult]], model: str | None = None,
                        notice: str | None = None, route: list[str] | None = None,
                        session_id: str | None = None) -> ChatResponse:
        payload: dict[str, Any] = {}
        searched: dict[str, dict[str, Any]] = {}
        for _, result in results:  # later calls override earlier ones of the same kind
            if result.ok:
                payload.update(result.ui)
                for item in result.ui.get("menu_items", []):
                    searched.setdefault(item["id"], item)
        # Show cards for the items the reply actually talks about, so "option 1" means what the customer read.
        mentioned = [i for i in searched.values() if i["name"].lower() in reply.lower()]
        if mentioned:
            payload["menu_items"] = mentioned
        return ChatResponse(
            reply=reply,
            mode=mode,  # type: ignore[arg-type]
            model=model,
            notice=notice,
            session_id=session_id,
            agent_route=[AGENT_LABELS.get(a, a) for a in (route or [])],
            tool_calls=[
                ToolCallTrace(name=r.name, arguments=r.arguments, ok=r.ok, summary=r.summary,
                              agent=AGENT_LABELS.get(agent, agent) if agent else None)
                for agent, r in results
            ],
            recommendations=payload.get("recommendations", []),
            alternatives=payload.get("alternatives", []),
            menu_items=payload.get("menu_items", [])[:8],
            wait_estimate=payload.get("wait_estimate"),
            order_proposal=payload.get("order_proposal"),
        )

    # -- fallback ----------------------------------------------------------------

    def _fallback(self, message: str, history: list[ChatTurn], reason: str, session_id: str | None = None) -> ChatResponse:
        constraints: dict[str, Any] = {}
        for turn in [*history[-HISTORY_TURNS:], ChatTurn(role="user", text=message)]:
            if turn.role == "user":
                constraints.update(parse_constraints(turn.text))

        result = self.executor.execute("recommend_options", constraints)
        if not result.ok and constraints.get("category"):
            constraints.pop("category")  # phrase didn't match this café's categories; search everything
            result = self.executor.execute("recommend_options", constraints)
        reply = _fallback_reply(constraints, result)
        notice = f"Demo mode — {reason} Responses use rule-based parsing, not Gemini."
        return self._build_response(reply, "fallback", [(None, result)], notice=notice, session_id=session_id)


# ---------------------------------------------------------------------------
# Rule-based fallback parsing (demo mode only)
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
}
_NUM = r"(\d{1,3}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"


def _to_int(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def parse_constraints(text: str) -> dict[str, Any]:
    t = text.lower().replace(",", " ")
    found: dict[str, Any] = {}

    if m := re.search(_NUM + r"\s*-?\s*(?:min|mins|minute|minutes)\b", t):
        found["deadline_minutes"] = _to_int(m.group(1))
    elif "half an hour" in t:
        found["deadline_minutes"] = 30
    elif re.search(r"\b(an|one) hour\b", t):
        found["deadline_minutes"] = 60

    money = re.search(r"(?:₹|rs\.?|inr|rupees?)\s*(\d{2,5})|(\d{2,5})\s*(?:₹|rs\b|inr|rupees?|bucks)", t) or re.search(
        r"(?:under|below|within|budget(?: of| is)?|have|max|upto|up to)\s*(\d{2,5})\b(?!\s*-?\s*min)", t
    )
    if money:
        found["budget_inr"] = int(money.group(1) or money.group(2))

    diets = []
    if "vegan" in t:
        diets.append("vegan")
    elif re.search(r"\b(vegetarian|veg|veggie)\b", t) and not re.search(r"\bnon[- ]?veg", t):
        diets.append("vegetarian")
    if re.search(r"gluten[- ]?free|no gluten|celiac", t):
        diets.append("gluten-free")
    if re.search(r"dairy[- ]?free|lactose|no dairy|no milk", t):
        diets.append("dairy-free")
    if diets:
        found["dietary_preference"] = ", ".join(diets)
    elif re.search(r"\bnon[- ]?veg", t):
        found["dietary_preference"] = None

    if re.search(r"filling|hungry|starving|meal|lunch|breakfast|substantial|heavy", t):
        found["want_filling"] = True
    if re.search(r"not (too |that |very )?sweet|less sweet|no sugar|unsweet|sugar[- ]?free", t):
        found["max_sweetness_level"] = 2

    if re.search(r"cold (drink|coffee|beverage)|iced|chilled", t):
        found["category"] = "cold"
    elif re.search(r"\b(sandwich|wrap)", t):
        found["category"] = "sandwich"
    elif re.search(r"\b(dessert|cake|brownie|cookie)", t):
        found["category"] = "dessert"
    elif re.search(r"\b(tea|chai)\b", t):
        found["category"] = "tea"
    elif re.search(r"\bcoffee\b", t):
        found["category"] = "coffee"

    drink_words = r"(coffee|drink|tea|chai|latte|cappuccino|brew)"
    food_words = r"(bite|snack|food|eat|sandwich|croissant|pastry|dessert|breakfast)"
    if re.search(drink_words + r".{0,25}\b(and|with|plus|\+)\b.{0,25}" + food_words, t) or re.search(
        food_words + r".{0,25}\b(and|with|plus|\+)\b.{0,25}" + drink_words, t
    ):
        found["require_drink"] = True
        found["require_food"] = True
        found.pop("category", None)  # a mixed order spans categories
    if re.search(r"(no|without|allergic to|allergy to)\s+nuts?|nut allergy|nut-free", t):
        found["exclude_allergens"] = ["tree-nuts", "peanuts"]
    return found


def _fallback_reply(constraints: dict[str, Any], result: ToolResult) -> str:
    understood = []
    if constraints.get("dietary_preference"):
        understood.append(constraints["dietary_preference"])
    if constraints.get("budget_inr") is not None:
        understood.append(f"budget ₹{constraints['budget_inr']}")
    if constraints.get("deadline_minutes") is not None:
        understood.append(f"ready within {constraints['deadline_minutes']} min")
    if constraints.get("want_filling"):
        understood.append("filling")
    if constraints.get("max_sweetness_level") is not None:
        understood.append("not too sweet")
    if constraints.get("category"):
        understood.append(constraints["category"].lower())

    lines = ["**Demo mode** (Gemini not connected) — rule-based matching on the real menu."]
    lines.append("I understood: " + (" · ".join(understood) if understood else "no specific constraints") + ".")
    if not result.ok:
        lines.append("Sorry — I couldn't search the menu for that request.")
        return "\n\n".join(lines)

    options = result.response.get("options", [])
    alternatives = result.response.get("alternatives_not_meeting_all_constraints", [])
    if options:
        top = options[0]
        lines.append(
            f"Top pick: **{' + '.join(top['names'])}** — ₹{top['total_price_inr']}, ready in about "
            f"{top['estimated_wait_minutes']} min. {len(options)} option(s) fit everything; see the cards below."
        )
    elif alternatives:
        alt = alternatives[0]
        lines.append(
            "Nothing on the menu meets every constraint. Closest: "
            f"**{' + '.join(alt['names'])}** (₹{alt['total_price_inr']}, ~{alt['estimated_wait_minutes']} min) — "
            + "; ".join(alt["unmet_constraints"]) + "."
        )
    else:
        lines.append("No available item matches those dietary/allergen requirements.")
    lines.append("Wait times are approximate.")
    return "\n\n".join(lines)
