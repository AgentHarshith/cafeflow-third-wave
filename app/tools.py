"""Backend tools exposed to Gemini via native function calling.

Every call is validated with a Pydantic model before any Python runs, and only the
four functions registered here can be executed — model output is never evaluated
as code. Ordering is split on purpose: the model may *prepare* an order draft, but
only the customer's explicit confirmation in the UI (POST /api/orders) creates it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .menu_service import CafeError, MenuService, item_public
from .order_service import OrderStore
from .recommendation_service import Constraints, recommend

logger = logging.getLogger("cafeflow.tools")


# ---------------------------------------------------------------------------
# Argument schemas (validation)
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchMenuArgs(_Args):
    budget_inr: int | None = Field(default=None, ge=0, le=100_000)
    dietary_preference: str | None = Field(default=None, max_length=60)
    max_prep_time_minutes: int | None = Field(default=None, ge=0, le=240)
    category: str | None = Field(default=None, max_length=40)
    min_filling_score: int | None = Field(default=None, ge=1, le=5)
    max_sweetness_level: int | None = Field(default=None, ge=0, le=5)
    exclude_allergens: list[str] = Field(default_factory=list, max_length=7)


class RecommendOptionsArgs(_Args):
    budget_inr: int | None = Field(default=None, ge=0, le=100_000)
    dietary_preference: str | None = Field(default=None, max_length=60)
    deadline_minutes: int | None = Field(default=None, ge=1, le=240)
    want_filling: bool = False
    max_sweetness_level: int | None = Field(default=None, ge=0, le=5)
    category: str | None = Field(default=None, max_length=40)
    exclude_allergens: list[str] = Field(default_factory=list, max_length=7)
    max_items: int = Field(default=3, ge=1, le=3)
    require_drink: bool = False
    require_food: bool = False


class ItemIdsArgs(_Args):
    item_ids: list[str] = Field(min_length=1, max_length=12)


class RecordFeedbackArgs(_Args):
    order_id: str | None = Field(default=None, pattern=r"^CF-[0-9A-F]{6}$")
    feedback_id: str | None = Field(default=None, pattern=r"^FB-[0-9A-F]{6}$")
    rating: int | None = Field(default=None, ge=1, le=5)
    sentiment: Literal["positive", "neutral", "negative"] | None = None
    themes: list[Literal["taste", "temperature", "wait_time", "service", "value", "accuracy", "ambience",
                         "menu_variety"]] = Field(default_factory=list, max_length=8)
    comment: str | None = Field(default=None, max_length=600)


class NoArgs(_Args):
    pass


# ---------------------------------------------------------------------------
# Gemini function declarations (JSON Schema)
# ---------------------------------------------------------------------------

_DIETARY_DESC = "Dietary requirement: vegetarian, vegan, dairy-free, gluten-free, or a combination like 'vegan, gluten-free'. Omit if none."
_ALLERGEN_DESC = "Allergens to exclude, e.g. ['tree-nuts', 'milk', 'gluten', 'sesame']."


def build_function_declarations(categories: list[str]) -> list[dict[str, Any]]:
    _CATEGORY_DESC = f"Optional category or phrase: {', '.join(categories)}, 'drinks', 'food', or e.g. 'cold drink'."
    return [
        {
            "name": "recommend_options",
            "description": (
                "Time-aware recommendation engine. Evaluates single items and small combinations from the real menu "
                "against budget, dietary needs, deadline (using the live order queue) and preferences. Returns options "
                "that satisfy every constraint, or clearly-labelled closest alternatives. Use this for any request with "
                "constraints such as budget, time, diet or 'filling'. Call it again whenever a constraint changes."
            ),
            "parameters_json_schema": {
                "type": "object",
                "properties": {
                    "budget_inr": {"type": "integer", "description": "Maximum total spend in rupees."},
                    "dietary_preference": {"type": "string", "description": _DIETARY_DESC},
                    "deadline_minutes": {"type": "integer", "description": "Order must be ready within this many minutes."},
                    "want_filling": {"type": "boolean", "description": "True if the customer wants something filling / a meal."},
                    "max_sweetness_level": {"type": "integer", "description": "0-5 cap on sweetness; e.g. 2 for 'not too sweet'."},
                    "category": {"type": "string", "description": _CATEGORY_DESC},
                    "exclude_allergens": {"type": "array", "items": {"type": "string"}, "description": _ALLERGEN_DESC},
                    "max_items": {"type": "integer", "description": "Max items per option (1-3). Default 3."},
                    "require_drink": {"type": "boolean", "description": "True when the customer wants a drink in the order (e.g. 'a coffee and a bite'). Each option then has exactly one drink."},
                    "require_food": {"type": "boolean", "description": "True when the customer wants something to eat in the order."},
                },
            },
        },
        {
            "name": "search_menu",
            "description": (
                "Search the actual sample menu with optional filters. Returns matching available items with real prices, "
                "prep times, dietary tags and allergens. Returns an empty list if nothing matches."
            ),
            "parameters_json_schema": {
                "type": "object",
                "properties": {
                    "budget_inr": {"type": "integer", "description": "Maximum price per item in rupees."},
                    "dietary_preference": {"type": "string", "description": _DIETARY_DESC},
                    "max_prep_time_minutes": {"type": "integer", "description": "Maximum preparation time per item."},
                    "category": {"type": "string", "description": _CATEGORY_DESC},
                    "min_filling_score": {"type": "integer", "description": "1-5; 4+ means filling."},
                    "max_sweetness_level": {"type": "integer", "description": "0-5 cap on sweetness."},
                    "exclude_allergens": {"type": "array", "items": {"type": "string"}, "description": _ALLERGEN_DESC},
                },
            },
        },
        {
            "name": "estimate_wait",
            "description": (
                "Estimate how long a specific set of items would take if ordered now, including the current order queue. "
                "Deterministic, approximate heuristic."
            ),
            "parameters_json_schema": {
                "type": "object",
                "properties": {
                    "item_ids": {"type": "array", "items": {"type": "string"}, "description": "Menu item ids (repeat an id for quantity)."},
                },
                "required": ["item_ids"],
            },
        },
        {
            "name": "prepare_order",
            "description": (
                "Prepare an order DRAFT for the customer to review. Validates items, computes the real total and wait. "
                "This does NOT place the order: the customer must press Confirm in the app. Use only when the customer "
                "clearly says which items they want to order."
            ),
            "parameters_json_schema": {
                "type": "object",
                "properties": {
                    "item_ids": {"type": "array", "items": {"type": "string"}, "description": "Menu item ids (repeat an id for quantity)."},
                },
                "required": ["item_ids"],
            },
        },
        {
            "name": "record_feedback",
            "description": (
                "Save customer feedback for the café team. Use a new record for new feedback; pass feedback_id "
                "(from the [App context] note) to add details to feedback already saved from the pop-up form."
            ),
            "parameters_json_schema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order ID like CF-1A2B3C, if the feedback is about an order."},
                    "feedback_id": {"type": "string", "description": "Existing feedback id like FB-1A2B3C to add details to."},
                    "rating": {"type": "integer", "description": "1-5 if the customer gave a rating."},
                    "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
                    "themes": {"type": "array", "items": {"type": "string", "enum": [
                        "taste", "temperature", "wait_time", "service", "value", "accuracy", "ambience",
                        "menu_variety"]}},
                    "comment": {"type": "string", "description": "Short summary in the customer's own words."},
                },
            },
        },
        {
            "name": "start_connect",
            "description": (
                "Opt-in Connect People: notify other signed-up customers in the café that this customer would "
                "like to connect. Returns needs_signup if the customer hasn't signed up yet."
            ),
            "parameters_json_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "connect_status",
            "description": "This customer's Connect People status: people online, responses and match percentages.",
            "parameters_json_schema": {"type": "object", "properties": {}},
        },
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    name: str
    arguments: dict[str, Any]
    ok: bool
    response: dict[str, Any]  # sent back to Gemini
    summary: str  # one line for the UI activity log
    ui: dict[str, Any] = field(default_factory=dict)  # structured data for frontend cards


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "category", "diet_marker", "price_inr", "preparation_time_minutes", "filling_score",
            "sweetness_level", "dietary_tags", "allergens")
    out = {k: item[k] for k in keys}
    for optional in ("price_large_inr", "serving", "badges"):  # only when the menu has them
        if item.get(optional):
            out[optional] = item[optional]
    return out


def _compact_combo(combo: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in combo.items() if k not in ("items", "per_station_minutes")}
    out["items"] = [_compact_item(i) for i in combo["items"]]
    return out


class ToolExecutor:
    def __init__(self, menu: MenuService, orders: OrderStore, *, feedback=None, connect=None, demand=None):
        self.menu = menu
        self.orders = orders
        self.feedback = feedback  # FeedbackService
        self.connect = connect  # ConnectService
        self.demand = demand  # DemandLog
        self._session_id: str | None = None
        self.declarations = build_function_declarations(menu.categories)
        cafe = menu.cafe
        # Real-café profiles carry indicative (unofficial) dietary/allergen data: say so in every result.
        self._data_note = cafe.data_note if cafe.allergen_data == "indicative" else None
        self._handlers = {
            "search_menu": (SearchMenuArgs, self._search_menu),
            "recommend_options": (RecommendOptionsArgs, self._recommend_options),
            "estimate_wait": (ItemIdsArgs, self._estimate_wait),
            "prepare_order": (ItemIdsArgs, self._prepare_order),
            "record_feedback": (RecordFeedbackArgs, self._record_feedback),
            "start_connect": (NoArgs, self._start_connect),
            "connect_status": (NoArgs, self._connect_status),
        }

    @property
    def tool_names(self) -> list[str]:
        return list(self._handlers)

    def execute(self, name: str, raw_args: dict[str, Any] | None, session_id: str | None = None) -> ToolResult:
        """Run one tool. session_id comes from the server (ADK session), never from the model."""
        raw_args = dict(raw_args or {})
        if name not in self._handlers:
            return ToolResult(name, raw_args, False, {"error": f"Unknown tool '{name}'."}, "unknown tool")
        schema, handler = self._handlers[name]
        try:
            args = schema.model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors())
            return ToolResult(name, raw_args, False, {"error": f"Invalid arguments: {problems}"}, "rejected: invalid arguments")
        clean = args.model_dump(exclude_defaults=True)
        try:
            self._session_id = session_id
            response, summary, ui = handler(args)
            return ToolResult(name, clean, True, response, summary, ui)
        except CafeError as exc:
            return ToolResult(name, clean, False, {"error": str(exc)}, f"error: {exc}")
        except Exception:  # noqa: BLE001 - never leak internals to the model or customer
            logger.exception("Tool %s failed", name)
            return ToolResult(name, clean, False, {"error": "The tool failed unexpectedly."}, "error: tool failed")

    # -- handlers --------------------------------------------------------------

    def _search_menu(self, args: SearchMenuArgs):
        items = [item_public(i) for i in self.menu.search(**args.model_dump())]
        response = {"count": len(items), "items": [_compact_item(i) for i in items]}
        if not items:
            response["note"] = "No menu items match all filters."
        if self._data_note:
            response["data_note"] = self._data_note
        return response, f"{len(items)} matching item(s)", {"menu_items": items}

    def _recommend_options(self, args: RecommendOptionsArgs):
        constraints = Constraints(**{**args.model_dump(), "exclude_allergens": tuple(args.exclude_allergens)})
        result = recommend(self.menu, self.orders, constraints)
        response = {
            **{k: v for k, v in result.items() if k not in ("options", "alternatives_not_meeting_all_constraints")},
            "options": [_compact_combo(c) for c in result["options"]],
            "alternatives_not_meeting_all_constraints": [
                _compact_combo(c) for c in result["alternatives_not_meeting_all_constraints"]
            ],
        }
        if self._data_note:
            response["data_note"] = self._data_note
        options = result["options"]
        if self.demand is not None:
            try:
                self.demand.record(args.model_dump(exclude_defaults=True), len(options))
            except Exception:  # noqa: BLE001 - analytics must never break a recommendation
                logger.warning("Could not record demand signal")
        if options:
            fastest = min(c["estimated_wait_minutes"] for c in options)
            summary = f"{len(options)} option(s) meet every constraint · fastest ~{fastest} min"
        else:
            summary = f"no fully compliant option · {len(result['alternatives_not_meeting_all_constraints'])} alternative(s)"
        ui = {"recommendations": options, "alternatives": result["alternatives_not_meeting_all_constraints"]}
        return response, summary, ui

    def _estimate_wait(self, args: ItemIdsArgs):
        quote = self.orders.quote(args.item_ids)
        estimate = quote["wait_estimate"]
        response = {"item_ids": args.item_ids, "total_inr": quote["total_inr"], **estimate}
        return response, f"~{estimate['estimated_wait_minutes']} min (approximate)", {"wait_estimate": response}

    def _prepare_order(self, args: ItemIdsArgs):
        quote = self.orders.quote(args.item_ids)
        draft = {
            "status": "awaiting_customer_confirmation",
            "order_placed": False,
            "item_ids": args.item_ids,
            "lines": quote["lines"],
            "total_inr": quote["total_inr"],
            "estimated_wait_minutes": quote["wait_estimate"]["estimated_wait_minutes"],
            "next_step": "Customer must press 'Confirm order' in the app. No order exists yet.",
        }
        return draft, f"draft ₹{quote['total_inr']} · awaiting confirmation", {"order_proposal": draft}

    def _record_feedback(self, args: RecordFeedbackArgs):
        if self.feedback is None:
            raise CafeError("Feedback is not available right now.")
        saved = self.feedback.record(**args.model_dump(), source="chat", session_id=self._session_id)
        response = {"saved": True, "feedback_id": saved["id"], "sentiment": saved["sentiment"],
                    "themes": saved["themes"], "note": "Visible to the café team in the staff dashboard."}
        return response, f"saved {saved['id']} · {saved['sentiment']}", {"feedback": response}

    def _start_connect(self, args: NoArgs):
        if self.connect is None or not self._session_id:
            raise CafeError("Connect People is not available right now.")
        result = self.connect.start(self._session_id)
        if result["needs_signup"]:
            response = {"needs_signup": True, "next_step": "Customer taps 'Sign up to connect' (nickname + optional table)."}
            return response, "needs sign-up", {"connect": {"action": "signup"}}
        response = {**result, "questions": "go-to coffee, milk, today's vibe",
                    "next_step": "Customer answers the 3 quick questions; matches appear when others answer."}
        ui = {"connect": {"action": "answer", "request_id": result["request_id"]}} if not result["answered"] else {}
        return response, f"{result['people_notified']} people notified", ui

    def _connect_status(self, args: NoArgs):
        if self.connect is None or not self._session_id:
            raise CafeError("Connect People is not available right now.")
        state = self.connect.state(self._session_id)
        response = {
            "signed_up": state["signed_up"],
            "people_online": state["people_online"],
            "my_request": state["my_request"],
            "matches": [{"percent": m["percent"], "with": m["with_profile"], "shared": m["shared"]}
                        for m in state["matches"]],
        }
        return response, f"{len(state['matches'])} match(es)", {}
