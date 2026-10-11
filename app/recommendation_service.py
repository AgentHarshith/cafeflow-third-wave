"""Time-aware recommendation engine.

Evaluates single items and small combinations against explicit constraints:

* Hard constraints (never relaxed for a "fully compliant" result): availability,
  dietary requirements, excluded allergens, budget.
* Deadline: the estimated wait (same estimator used for real orders, including the
  current queue) must fit the customer's time limit.
* Soft preferences: filling, sweetness cap and category shape what is searched and
  how results are ranked.

When nothing satisfies every constraint, closest alternatives are returned in a
separate list, each labelled with exactly which constraints it misses. Dietary and
allergen requirements are never relaxed, even for alternatives.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

from .menu_service import MenuService, item_public
from .order_service import OrderStore
from .queue_service import estimate_from_backlog
from .schemas import MenuItem

MAX_COMBO_ITEMS = 3
FILLING_TARGET = 4  # minimum combined filling score when the customer wants something filling
MAX_RESULTS = 5
MAX_RESULTS_PER_ITEM = 2  # keeps the shortlist varied instead of 5 tweaks of one combo


@dataclass(frozen=True)
class Constraints:
    budget_inr: int | None = None
    dietary_preference: str | None = None
    deadline_minutes: int | None = None
    want_filling: bool = False
    max_sweetness_level: int | None = None
    category: str | None = None
    exclude_allergens: tuple[str, ...] = ()
    max_items: int = MAX_COMBO_ITEMS
    require_drink: bool = False  # e.g. "a coffee and a bite": exactly one drink in every option
    require_food: bool = False  # at least one food item in every option


def _combo_view(items: tuple[MenuItem, ...], estimate: dict[str, Any], constraints: Constraints) -> dict[str, Any]:
    total = sum(i.price_inr for i in items)
    filling = sum(i.filling_score for i in items)
    wait = estimate["estimated_wait_minutes"]
    unmet: list[str] = []
    if constraints.budget_inr is not None and total > constraints.budget_inr:
        unmet.append(f"over budget by ₹{total - constraints.budget_inr}")
    if constraints.deadline_minutes is not None and wait > constraints.deadline_minutes:
        unmet.append(f"needs ~{wait} min, {wait - constraints.deadline_minutes} min over your {constraints.deadline_minutes}-min limit")
    if constraints.want_filling and filling < FILLING_TARGET:
        unmet.append("lighter than you asked for")
    return {
        "item_ids": [i.id for i in items],
        "items": [item_public(i) for i in items],
        "names": [i.name for i in items],
        "total_price_inr": total,
        "estimated_wait_minutes": wait,
        "per_station_minutes": estimate["per_station_minutes"],
        "queue_delay_minutes": estimate["queue_delay_minutes"],
        "combined_filling_score": filling,
        "budget_left_inr": None if constraints.budget_inr is None else constraints.budget_inr - total,
        "fits_deadline": constraints.deadline_minutes is None or wait <= constraints.deadline_minutes,
        "meets_all_constraints": not unmet,
        "unmet_constraints": unmet,
    }


def _shortfall(combo: dict[str, Any], constraints: Constraints) -> float:
    """How far an option misses: minutes late + 2 per missing filling point + 1 per ₹50 over budget."""
    late = max(0, combo["estimated_wait_minutes"] - (constraints.deadline_minutes or combo["estimated_wait_minutes"]))
    light = max(0, FILLING_TARGET - combo["combined_filling_score"]) if constraints.want_filling else 0
    over = max(0, combo["total_price_inr"] - (constraints.budget_inr or combo["total_price_inr"]))
    return late + 2 * light + over / 50


def _rank_key(combo: dict[str, Any], want_filling: bool) -> tuple:
    if want_filling:
        return (-combo["combined_filling_score"], combo["estimated_wait_minutes"], combo["total_price_inr"], combo["item_ids"])
    return (len(combo["item_ids"]), combo["estimated_wait_minutes"], combo["total_price_inr"], combo["item_ids"])


def _diverse(combos: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    for combo in combos:
        if any(usage.get(i, 0) >= MAX_RESULTS_PER_ITEM for i in combo["item_ids"]):
            continue
        picked.append(combo)
        for i in combo["item_ids"]:
            usage[i] = usage.get(i, 0) + 1
        if len(picked) == limit:
            break
    return picked


def recommend(menu: MenuService, orders: OrderStore, constraints: Constraints) -> dict[str, Any]:
    # Hard filters that are never relaxed: availability, dietary tags, allergens.
    # Category and sweetness are explicit customer requests, so they scope the pool too.
    pool = menu.search(
        dietary_preference=constraints.dietary_preference,
        category=constraints.category,
        max_sweetness_level=constraints.max_sweetness_level,
        exclude_allergens=constraints.exclude_allergens,
    )
    max_items = max(1, min(constraints.max_items, MAX_COMBO_ITEMS))
    backlog = orders.backlog()

    evaluated: list[dict[str, Any]] = []
    for size in range(1, max_items + 1):
        for combo in combinations(pool, size):
            # One drink per person keeps combinations sensible (no espresso + tea + cold brew).
            drinks = sum(1 for i in combo if i.station == "bar")
            if drinks > 1 or (constraints.require_drink and drinks == 0):
                continue
            if constraints.require_food and drinks == len(combo):
                continue
            estimate = estimate_from_backlog(list(combo), backlog)
            evaluated.append(_combo_view(combo, estimate, constraints))

    compliant = sorted(
        (c for c in evaluated if c["meets_all_constraints"]),
        key=lambda c: _rank_key(c, constraints.want_filling),
    )
    options = _diverse(compliant, MAX_RESULTS)

    alternatives: list[dict[str, Any]] = []
    if not options:
        within_budget = [c for c in evaluated if not any(u.startswith("over budget") for u in c["unmet_constraints"])]
        # Relax the deadline / filling first; only relax budget if nothing else is possible.
        fallback_pool = within_budget or evaluated
        alternatives = _diverse(
            sorted(
                fallback_pool,
                key=lambda c: (
                    len(c["unmet_constraints"]),
                    _shortfall(c, constraints),
                    c["total_price_inr"],
                    c["item_ids"],
                ),
            ),
            3,
        )

    if options:
        explanation = (
            f"{len(options)} option(s) satisfy every constraint (checked {len(evaluated)} item combinations)."
        )
    elif not pool:
        explanation = "No available menu item matches the dietary/category/allergen requirements."
    else:
        explanation = (
            "No option satisfies every constraint. Alternatives below are closest matches and each lists what it misses."
        )

    return {
        "constraints_applied": {
            "budget_inr": constraints.budget_inr,
            "dietary_preference": constraints.dietary_preference,
            "deadline_minutes": constraints.deadline_minutes,
            "want_filling": constraints.want_filling,
            "filling_target": FILLING_TARGET if constraints.want_filling else None,
            "max_sweetness_level": constraints.max_sweetness_level,
            "category": constraints.category,
            "exclude_allergens": list(constraints.exclude_allergens),
            "max_items_per_option": max_items,
            "require_drink": constraints.require_drink,
            "require_food": constraints.require_food,
        },
        "current_queue_backlog_minutes": {k: round(v, 1) for k, v in backlog.items()},
        "options": options,
        "alternatives_not_meeting_all_constraints": alternatives,
        "explanation": explanation,
        "estimates_are_approximate": True,
    }
