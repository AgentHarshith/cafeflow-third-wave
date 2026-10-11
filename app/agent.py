"""CaféFlow AI conversational agent.

Gemini (Google Gen AI SDK, API-key auth) decides which backend tool to call via
native function calling; the tools run deterministic Python and their results are
sent back to Gemini in a bounded loop. When Gemini is not configured or fails, a
clearly-labelled rule-based fallback drives the *same* tools, so the demo keeps
working without ever pretending to be a live Gemini answer.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .schemas import ChatResponse, ChatTurn, ToolCallTrace
from .schemas import CafeProfile
from .tools import ToolExecutor, ToolResult

logger = logging.getLogger("cafeflow.agent")

HISTORY_TURNS = 12
VERIFY_CACHE_SECONDS = 300

def build_system_instruction(cafe: CafeProfile) -> str:
    if cafe.fictional:
        who = f'the ordering companion for "{cafe.name}", a fictional demo café (sample menu, prices in Indian rupees ₹).'
    else:
        who = (
            f'an unofficial concept-demo ordering companion for {cafe.name} (prices in Indian rupees ₹). '
            f"You are not affiliated with {cafe.name}; orders here are demo orders and are never sent to the café. "
            "If asked, say so plainly."
        )
    data = (
        f"\nData caveat: {cafe.data_note} Whenever you mention dietary suitability or allergens, add a short "
        "reminder to confirm with café staff."
        if cafe.allergen_data == "indicative"
        else ""
    )
    return f"You are CaféFlow AI, {who}{data}\n" + _INSTRUCTION_BODY


_INSTRUCTION_BODY = """
Help customers find menu items that fit their budget, dietary requirements, taste and time constraints.

Tools:
- recommend_options: the time-aware engine. Use it for ANY request involving budget, time, diet, allergies, "filling" or sweetness. Pass every constraint the customer has stated so far in the conversation (carry earlier constraints forward unless the customer changed them). When a constraint changes, call it again — never reuse earlier results.
- search_menu: browse or look up items (e.g. "what teas do you have?").
- estimate_wait: wait time for a specific set of items.
- prepare_order: when the customer clearly says which items they want to order, create a draft. It does NOT place the order — tell them to tap "Review & confirm" on the draft card to place it.

The latest customer message may be followed by an [App context] note (added by the app, not typed by the customer) listing the option cards currently on screen with their item ids. Use those ids for prepare_order or estimate_wait (e.g. "I'll take option 2"). Never repeat that note or show item ids.

Rules:
- Every item name, price, availability, dietary fact and wait time you mention must come from a tool result. Never invent menu items, prices, order IDs or tool results.
- Never say an order has been placed. Orders exist only after the customer presses Confirm in the app.
- Dietary requirements and allergies are hard constraints; never suggest anything that violates them.
- Wait times are approximate estimates from a simple rule that includes the current order queue — say "about" or "~".
- If no option satisfies everything, say so plainly, then offer the closest alternatives and exactly what each one misses.
- Briefly explain why your top pick fits (budget left, wait vs. deadline, how filling it is) and mention one useful trade-off.
- Never quote internal numbers like filling or sweetness scores; describe them in words ("very filling", "lightly sweet").
- Ask a clarifying question only when you truly cannot proceed.
- Be friendly and concise: at most about 110 words. Use a short bulleted list for options with bold item names. Never show item ids. The app shows option cards with Add buttons under your message, so do not repeat every detail."""

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


class GeminiUnavailable(Exception):
    """Raised when Gemini cannot answer; carries a customer-safe reason."""


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
    return "Could not reach the Gemini API.", True


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class CafeAgent:
    def __init__(self, settings: Settings, executor: ToolExecutor, client_factory: Callable[[], Any] | None = None):
        self.settings = settings
        self.executor = executor
        self.model = settings.gemini_model
        self.status = GeminiStatus(settings.gemini_configured, self.model)
        self.system_instruction = build_system_instruction(executor.menu.cafe)
        self._client_factory = client_factory
        self._client: Any = None
        self._model_discovery_done = False
        self._thinking_enabled = True

    # -- client ----------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from google import genai

                # Explicit AI Studio API-key auth; vertexai=False prevents silently
                # switching to Vertex credentials via GOOGLE_GENAI_USE_VERTEXAI.
                self._client = genai.Client(api_key=self.settings.gemini_api_key, vertexai=False)
        return self._client

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
            logger.warning("Model discovery failed", exc_info=False)
            return None

        def rank(name: str) -> tuple:
            bad = any(t in name for t in ("lite", "image", "tts", "audio", "live", "embedding", "robotics", "computer"))
            unstable = any(t in name for t in ("preview", "exp"))
            version = [int(n) for n in re.findall(r"\d+", name)[:2]]
            return (bad, "flash" not in name, unstable, [-v for v in version])

        names.sort(key=rank)
        return names[0] if names else None

    def _config(self, allow_tools: bool = True):
        from google.genai import types

        tools = [types.Tool(function_declarations=[types.FunctionDeclaration(**d) for d in self.executor.declarations])]
        tool_config = None
        if not allow_tools:
            tool_config = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))
        thinking = None
        if self._thinking_level_supported():
            thinking = types.ThinkingConfig(thinking_level=self.settings.gemini_thinking_level.upper())
        return types.GenerateContentConfig(
            system_instruction=self.system_instruction,
            tools=tools,
            tool_config=tool_config,
            thinking_config=thinking,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def _thinking_level_supported(self) -> bool:
        """thinking_level exists on Gemini 3+ (and the -latest aliases that point at it)."""
        if not self.settings.gemini_thinking_level or not self._thinking_enabled:
            return False
        match = re.match(r"gemini-(\d+)", self.model)
        return self.model.endswith("-latest") or bool(match and int(match.group(1)) >= 3)

    def _generate(self, contents: list, allow_tools: bool = True):
        """One Gemini call with a single retry for transient errors and model fallback on 404."""
        client = self._get_client()
        attempts = 0
        while True:
            attempts += 1
            try:
                response = client.models.generate_content(
                    model=self.model, contents=contents, config=self._config(allow_tools)
                )
                self.status.success(self.model, "Last Gemini request succeeded.")
                return response
            except Exception as exc:  # noqa: BLE001
                detail, retryable = _classify_error(exc)
                logger.warning("Gemini call failed: %s", detail)
                if "model is not available" in detail:
                    replacement = self._discover_model()
                    if replacement and replacement != self.model:
                        logger.info("Switching Gemini model %s -> %s", self.model, replacement)
                        self.model = replacement
                        continue
                if getattr(exc, "code", None) == 400 and self._thinking_level_supported():
                    # Model rejected thinking_level: drop it and retry with the model's default.
                    logger.info("Disabling thinking_level for model %s", self.model)
                    self._thinking_enabled = False
                    continue
                if retryable and attempts < 3:
                    time.sleep(2.0 * attempts)  # 2s, then 4s
                    continue
                self.status.failure(detail)
                raise GeminiUnavailable(detail) from exc

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
                    self.model = replacement
                    return self.verify()
            self.status.failure(detail)
        return self.status.as_dict()

    # -- chat ----------------------------------------------------------------

    def chat(self, message: str, history: list[ChatTurn], shown_options: list[list[str]] | None = None) -> ChatResponse:
        if not self.settings.gemini_configured:
            return self._fallback(message, history, "Gemini API key not configured.")
        try:
            return self._gemini_chat(message, history, shown_options or [])
        except GeminiUnavailable as exc:
            return self._fallback(message, history, str(exc))

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

    def _gemini_chat(self, message: str, history: list[ChatTurn], shown_options: list[list[str]] | None = None) -> ChatResponse:
        from google.genai import types

        contents: list = []
        for turn in history[-HISTORY_TURNS:]:
            if not contents and turn.role != "user":
                continue  # conversations must start with a user turn
            role = "user" if turn.role == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=turn.text)]))
        parts = [types.Part.from_text(text=message)]
        if context := self._screen_context(shown_options or []):
            parts.append(types.Part.from_text(text=context))
        contents.append(types.Content(role="user", parts=parts))

        results: list[ToolResult] = []
        reply = ""
        guard_used = False
        for _ in range(self.settings.max_tool_rounds):
            response = self._generate(contents)
            candidate = (response.candidates or [None])[0]
            content = getattr(candidate, "content", None)
            calls = response.function_calls or []
            if not calls:
                reply = self._text_of(content).strip()
                # Guard against an answer that quotes prices without consulting the menu.
                if not results and not guard_used and "₹" in reply and content is not None:
                    guard_used = True
                    contents.append(content)
                    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=PRICE_GUARD_PROMPT)]))
                    reply = ""
                    continue
                break
            if content is not None:
                contents.append(content)  # keeps function-call parts and thought signatures intact
            parts = []
            for call in calls:
                result = self.executor.execute(call.name, dict(call.args or {}))
                results.append(result)
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=getattr(call, "id", None), name=call.name, response=result.response
                        )
                    )
                )
            contents.append(types.Content(role="user", parts=parts))
        else:
            # Round limit hit: ask for a final answer with tools disabled.
            reply = self._text_of(getattr((self._generate(contents, allow_tools=False).candidates or [None])[0], "content", None)).strip()

        reply = _ECHOED_CONTEXT.sub("", reply).strip()  # never show internal context/ids to the customer
        if not reply:
            reply = (
                "Here's what I found on the menu — see the options below."
                if results
                else "Sorry, I couldn't come up with an answer. Could you rephrase that?"
            )
        return self._build_response(reply, "gemini", results, model=self.model)

    # -- structured payload ----------------------------------------------------

    @staticmethod
    def _build_response(reply: str, mode: str, results: list[ToolResult], model: str | None = None,
                        notice: str | None = None) -> ChatResponse:
        payload: dict[str, Any] = {}
        for result in results:  # later calls override earlier ones of the same kind
            if result.ok:
                payload.update(result.ui)
        return ChatResponse(
            reply=reply,
            mode=mode,  # type: ignore[arg-type]
            model=model,
            notice=notice,
            tool_calls=[ToolCallTrace(name=r.name, arguments=r.arguments, ok=r.ok, summary=r.summary) for r in results],
            recommendations=payload.get("recommendations", []),
            alternatives=payload.get("alternatives", []),
            menu_items=payload.get("menu_items", [])[:8],
            wait_estimate=payload.get("wait_estimate"),
            order_proposal=payload.get("order_proposal"),
        )

    # -- fallback ----------------------------------------------------------------

    def _fallback(self, message: str, history: list[ChatTurn], reason: str) -> ChatResponse:
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
        return self._build_response(reply, "fallback", [result], notice=notice)


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
