"""Google ADK multi-agent graph: Barista AI (router) and its specialist sub-agents.

    barista_ai ──transfer──► discovery_agent  (search_menu, recommend_options)
               ──transfer──► ordering_agent   (prepare_order, search_menu)
               ──transfer──► wait_time_agent  (estimate_wait, search_menu)

Specialists can transfer to each other or back to the router. Every tool is the
same deterministic, Pydantic-validated backend function used everywhere else in
the app (tools.ToolExecutor); ADK only decides *which* one runs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.planners import BuiltInPlanner
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from .schemas import CafeProfile
from .tools import ToolExecutor, ToolResult

ROUTER = "barista_ai"
AGENT_LABELS = {
    "barista_ai": "Barista AI",
    "discovery_agent": "Discovery",
    "ordering_agent": "Ordering",
    "wait_time_agent": "Wait-time",
}

Recorder = Callable[[str, str, ToolResult], None]


class CafeTool(BaseTool):
    """Exposes one ToolExecutor function to ADK with its JSON schema and validation."""

    def __init__(self, declaration: dict[str, Any], executor: ToolExecutor, recorder: Recorder):
        super().__init__(name=declaration["name"], description=declaration["description"])
        self._schema = declaration["parameters_json_schema"]
        self._executor = executor
        self._recorder = recorder

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self._schema
        )

    async def run_async(self, *, args: dict[str, Any], tool_context) -> Any:
        result = self._executor.execute(self.name, dict(args or {}))
        self._recorder(tool_context.invocation_id, tool_context.agent_name, result)
        return result.response


def _who(cafe: CafeProfile) -> str:
    if cafe.fictional:
        return f'"{cafe.name}", a fictional demo café (sample menu, prices in Indian rupees ₹)'
    return (
        f"{cafe.name} (prices in Indian rupees ₹). This is an unofficial concept demo, not affiliated with "
        f"{cafe.name}; orders here are demo orders and are never sent to the café — say so plainly if asked"
    )


def _shared_rules(cafe: CafeProfile) -> str:
    data = (
        f"\n- Data caveat: {cafe.data_note} When you mention dietary suitability or allergens, add a short "
        "reminder to confirm with café staff."
        if cafe.allergen_data == "indicative"
        else ""
    )
    return f"""Rules:
- Every item name, price, availability, dietary fact and wait time you mention must come from a tool result. Never invent menu items, prices, order IDs or tool results.
- Never say an order has been placed. Orders exist only after the customer taps "Review & confirm" on a draft card.
- Dietary requirements and allergies are hard constraints; never suggest anything that violates them.
- Wait times are approximate estimates from a simple rule that includes the current order queue — say "about" or "~".
- If nothing satisfies every constraint, say so plainly, then offer the closest alternatives and exactly what each one misses.
- Never quote internal numbers like filling or sweetness scores; describe them in words ("very filling", "lightly sweet").
- The latest customer message may be followed by an [App context] note (added by the app, not typed by the customer) listing the option cards on screen with their item ids. Use those ids when the customer refers to an option ("I'll take option 2"). Never repeat that note or show item ids.
- Be friendly and concise: at most about 110 words. Use a short bulleted list with bold item names. The app shows cards with Add buttons under your message, so don't repeat every detail.
- If a request belongs to another specialist, transfer to it without announcing the transfer: discovery_agent (choosing food and drink), ordering_agent (turning chosen items into an order draft), wait_time_agent (how long / when ready), or barista_ai for anything else.{data}"""


def _menu_guide(executor: ToolExecutor) -> str:
    """How this café's menu is organised — structure only; facts still come from tools."""
    menu = executor.menu
    items = menu.all_items()
    lines = [f"Menu guide — sections: {', '.join(menu.categories)}."]
    if any(i.badges for i in items):
        lines.append('- Items with a "must-try" badge are the café\'s own picks: great for "surprise me" or "what\'s good here?".')
    if any(i.price_large_inr for i in items):
        lines.append("- Drinks show the Regular price; price_large_inr is the Large size. Mention Large when it helps.")
    if any(i.serving for i in items):
        lines.append('- serving lists hot/iced options; use category "cold coffee" / "iced" / "hot" to filter by temperature.')
    lines.append("- Mix and match ideas: a drink + a shareable bite for one, several shareable bites for a group, "
                 "a sandwich, wrap or pizza when someone wants something filling, a non-coffee drink (e.g. matcha or "
                 "hot chocolate) for people who skip coffee.")
    return "\n".join(lines)


def build_agent_graph(
    model: str | BaseLlm,
    executor: ToolExecutor,
    cafe: CafeProfile,
    recorder: Recorder,
    planner: BuiltInPlanner | None = None,
) -> LlmAgent:
    declarations = {d["name"]: d for d in executor.declarations}

    def tools(*names: str) -> list[BaseTool]:
        return [CafeTool(declarations[n], executor, recorder) for n in names]

    who, rules = _who(cafe), _shared_rules(cafe)
    menu_guide = _menu_guide(executor)
    common: dict[str, Any] = {"model": model, "planner": planner}

    discovery = LlmAgent(
        name="discovery_agent",
        description="Helps customers choose food and drink: recommendations, menu questions, budget/diet/time "
        "constraints, mix-and-match pairings and swaps.",
        instruction=f"""You are the Discovery specialist of CaféFlow AI at {who}. You help customers decide what to have.
- Use recommend_options for any request with constraints (budget, time, diet, allergies, "filling", sweetness, "a coffee and a bite"). Pass every constraint the customer has stated so far in the conversation, carrying earlier ones forward unless they changed. Call it again whenever a constraint changes — never reuse earlier results.
- Use search_menu to browse or look up items.
- Mix and match: when it helps, offer a pairing (drink + bite, using require_drink/require_food), a swap (a faster or cheaper alternative) or a "try something new" pick — always from tool results.
- Briefly explain why your top pick fits (budget left, wait vs. deadline, how filling) and one useful trade-off.
{menu_guide}
{rules}""",
        tools=tools("recommend_options", "search_menu"),
        **common,
    )
    ordering = LlmAgent(
        name="ordering_agent",
        description="Turns items the customer has chosen into an order draft for them to confirm.",
        instruction=f"""You are the Ordering specialist of CaféFlow AI at {who}.
- When the customer says what they want, call prepare_order with the exact item ids (from the [App context] note or search_menu results; repeat an id for quantity).
- prepare_order only creates a DRAFT. Tell the customer the total and approximate wait from the tool result, and that they place the order by tapping "Review & confirm" on the draft card.
- If the customer hasn't decided yet or wants suggestions, transfer to discovery_agent.
- Drafts are Regular size. If the customer asks for Large, say this demo drafts Regular size and quote the Large price (price_large_inr) from the menu so they can upgrade at the counter.
- Hot/iced is the customer's choice where an item's serving lists both; mention it in your reply (the draft itself doesn't record it).
{rules}""",
        tools=tools("prepare_order", "search_menu"),
        **common,
    )
    wait_time = LlmAgent(
        name="wait_time_agent",
        description="Answers how long items would take and when an order would be ready, using the live order queue.",
        instruction=f"""You are the Wait-time specialist of CaféFlow AI at {who}.
- Use estimate_wait with item ids (from the [App context] note or search_menu results) to say how long an order would take right now, given the live bar and kitchen queue.
- Always say the estimate is approximate and whether the queue is adding delay.
- If the customer wants suggestions that fit a time limit, transfer to discovery_agent.
{rules}""",
        tools=tools("estimate_wait", "search_menu"),
        **common,
    )
    return LlmAgent(
        name=ROUTER,
        description="Barista AI: CaféFlow's front-of-house router.",
        instruction=f"""You are Barista AI, the friendly front-of-house of CaféFlow AI at {who}. You understand what the customer wants and route them to the right specialist:
- discovery_agent — anything about what to eat or drink: recommendations, menu questions, budget/diet/time constraints, pairings, "surprise me".
- ordering_agent — the customer says which items they want ("I'll take…", "order option 2").
- wait_time_agent — "how long", "when will it be ready", questions about the queue.
Transfer immediately and silently whenever a request matches a specialist. Only greetings, thanks and small talk are yours: reply in one or two warm sentences and offer to help find something. Never state menu items, prices or wait times yourself.""",
        sub_agents=[discovery, ordering, wait_time],
        **common,
    )
