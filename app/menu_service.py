"""Menu catalogue: loading, validation, lookup and structured filtering.

Everything café-specific (categories, stations, copy, theme) comes from the profile
file in data/cafes/<id>.json, so the same engine serves any café.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .schemas import CafeProfile, MenuItem


class CafeError(Exception):
    """Base class for expected, user-facing business errors."""


class UnknownItemError(CafeError):
    pass


class UnavailableItemError(CafeError):
    pass


class InvalidPreferenceError(CafeError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[\s_]+", "-", text.strip().lower())


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("ches") or word.endswith("shes"):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


# Accepted spellings -> required dietary tags. Matching is done only against the
# structured `dietary_tags` field of each item.
_DIETARY_SYNONYMS: dict[str, set[str]] = {
    "vegetarian": {"vegetarian"},
    "veg": {"vegetarian"},
    "veggie": {"vegetarian"},
    "pure-veg": {"vegetarian"},
    "vegan": {"vegan"},
    "plant-based": {"vegan"},
    "dairy-free": {"dairy-free"},
    "no-dairy": {"dairy-free"},
    "lactose-free": {"dairy-free"},
    "gluten-free": {"gluten-free"},
    "no-gluten": {"gluten-free"},
    "celiac": {"gluten-free"},
}
_NO_DIETARY_FILTER = {"", "none", "any", "no-preference", "non-veg", "non-vegetarian", "nonveg", "omnivore"}

# Words that map to a station rather than a named category.
_STATION_WORDS = {
    "drink": "bar", "drinks": "bar", "beverage": "bar", "beverages": "bar",
    "food": "kitchen", "meal": "kitchen", "eat": "kitchen", "bite": "kitchen", "snack-food": "kitchen",
}
# Common customer words -> words that appear in category names.
_CATEGORY_WORD_ALIASES = {"chai": "tea", "pastry": "bakery", "sweet": "dessert", "shareable": "bite", "share": "bite"}
_TEMPERATURE_WORDS = {"cold": "iced", "iced": "iced", "ice": "iced", "chilled": "iced", "hot": "hot", "warm": "hot"}
_CATEGORY_STOPWORDS = {"drink", "drinks", "item", "items", "something", "a", "an", "some", "the", "beverage"}

_ALLERGEN_SYNONYMS: dict[str, str] = {
    "milk": "milk", "dairy": "milk", "lactose": "milk",
    "gluten": "gluten", "wheat": "gluten",
    "nut": "tree-nuts", "nuts": "tree-nuts", "tree-nut": "tree-nuts", "tree-nuts": "tree-nuts",
    "walnut": "tree-nuts", "cashew": "tree-nuts", "hazelnut": "tree-nuts", "almond": "tree-nuts",
    "peanut": "peanuts", "peanuts": "peanuts",
    "sesame": "sesame",
    "soy": "soy", "soya": "soy",
    "egg": "egg", "eggs": "egg",
}


def parse_dietary(preference: str | None) -> frozenset[str]:
    """Turn a free-text dietary preference into the set of tags every item must carry.

    Accepts comma/"and"-separated combinations such as "vegan, gluten free".
    """
    if preference is None:
        return frozenset()
    required: set[str] = set()
    for raw in re.split(r",|/|\band\b|\+", preference.lower()):
        key = _norm(raw)
        if key in _NO_DIETARY_FILTER:
            continue
        if key not in _DIETARY_SYNONYMS:
            supported = "vegetarian, vegan, dairy-free, gluten-free (or none)"
            raise InvalidPreferenceError(f"Unsupported dietary preference '{raw.strip()}'. Supported: {supported}.")
        required |= _DIETARY_SYNONYMS[key]
    return frozenset(required)


def parse_allergens(allergens: Iterable[str] | None) -> frozenset[str]:
    result: set[str] = set()
    for raw in allergens or ():
        key = _norm(raw)
        if key not in _ALLERGEN_SYNONYMS:
            raise InvalidPreferenceError(f"Unknown allergen '{raw}'.")
        result.add(_ALLERGEN_SYNONYMS[key])
    return frozenset(result)


def split_temperature(phrase: str | None) -> tuple[str | None, str | None]:
    """'cold coffee' -> ('coffee', 'iced'); 'iced' -> (None, 'iced'); 'sandwich' -> ('sandwich', None)."""
    if not phrase:
        return None, None
    words = re.split(r"[\s\-/,]+", phrase.lower().strip())
    temps = {_TEMPERATURE_WORDS[w] for w in words if w in _TEMPERATURE_WORDS}
    rest = " ".join(w for w in words if w and w not in _TEMPERATURE_WORDS)  # category words are parsed later
    return (rest or None), (temps.pop() if len(temps) == 1 else None)


def item_public(item: MenuItem) -> dict[str, Any]:
    """Serializable view of an item for tools and the frontend."""
    return {
        "id": item.id,
        "name": item.name,
        "category": item.category,
        "station": item.station,
        "diet_marker": item.diet_marker,
        "price_inr": item.price_inr,
        "description": item.description,
        "dietary_tags": list(item.dietary_tags),
        "allergens": list(item.allergens),
        "preparation_time_minutes": item.preparation_time_minutes,
        "available": item.available,
        "filling_score": item.filling_score,
        "sweetness_level": item.sweetness_level,
        "price_large_inr": item.price_large_inr,
        "serving": list(item.serving),
        "badges": list(item.badges),
    }


class MenuService:
    def __init__(self, items: Iterable[MenuItem], cafe: CafeProfile):
        self._items: dict[str, MenuItem] = {}
        for item in items:
            if item.id in self._items:
                raise ValueError(f"Duplicate menu item id: {item.id}")
            self._items[item.id] = item
        self.cafe = cafe
        # Category order as it first appears in the menu file.
        self.categories: list[str] = list(dict.fromkeys(i.category for i in self._items.values()))

    @classmethod
    def from_file(cls, path: Path) -> "MenuService":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        items = [MenuItem.model_validate(entry) for entry in raw["items"]]
        return cls(items, CafeProfile.model_validate(raw["cafe"]))

    def all_items(self) -> list[MenuItem]:
        return list(self._items.values())

    def get(self, item_id: str) -> MenuItem | None:
        return self._items.get(item_id)

    # -- categories --------------------------------------------------------------

    def parse_category(self, category: str | None) -> frozenset[str]:
        """Resolve a customer phrase ("cold drink", "sandwich", "food") to menu categories."""
        if category is None or not category.strip():
            return frozenset()
        key = _norm(category)
        for name in self.categories:
            if key == _norm(name):
                return frozenset({name})
        if key in _STATION_WORDS:
            station = _STATION_WORDS[key]
            return frozenset(i.category for i in self._items.values() if i.station == station)

        words = [_CATEGORY_WORD_ALIASES.get(w, w) for w in re.split(r"[\s\-&/,]+", category.lower()) if w]
        wanted = {_singular(w) for w in words if w not in _CATEGORY_STOPWORDS}
        if wanted:
            matches = set()
            for name in self.categories:
                tokens = {_singular(t) for t in re.split(r"[\s&/,]+", name.lower()) if t}
                if wanted <= tokens:
                    matches.add(name)
            if matches:
                return frozenset(matches)
            if wanted <= {"coffee", "drink", "beverage"}:  # menus that group coffees as "Classics", "Specials"...
                return frozenset(i.category for i in self._items.values() if i.station == "bar")
        raise InvalidPreferenceError(
            f"Unknown category '{category}'. Use one of: {', '.join(self.categories)}, 'drinks' or 'food'."
        )

    # -- lookup ------------------------------------------------------------------

    def require_available(self, item_ids: Iterable[str]) -> list[MenuItem]:
        """Resolve ids to items, rejecting unknown or unavailable ones (order-safe)."""
        resolved: list[MenuItem] = []
        unknown = []
        unavailable = []
        for item_id in item_ids:
            item = self._items.get(item_id)
            if item is None:
                unknown.append(item_id)
            elif not item.available:
                unavailable.append(item.name)
            else:
                resolved.append(item)
        if unknown:
            raise UnknownItemError(f"Unknown menu item id(s): {', '.join(sorted(set(unknown)))}")
        if unavailable:
            raise UnavailableItemError(f"Currently unavailable: {', '.join(sorted(set(unavailable)))}")
        return resolved

    def search(
        self,
        *,
        budget_inr: int | None = None,
        dietary_preference: str | None = None,
        max_prep_time_minutes: int | None = None,
        category: str | None = None,
        min_filling_score: int | None = None,
        max_sweetness_level: int | None = None,
        exclude_allergens: Iterable[str] | None = None,
    ) -> list[MenuItem]:
        """Return available items matching every provided filter, cheapest first."""
        required_tags = parse_dietary(dietary_preference)
        rest, temperature = split_temperature(category)
        if temperature and rest in ("coffee", "coffees", "drink", "drinks", "beverage", "beverages"):
            # "iced coffee" means any iced drink at the bar, whatever section the menu files it under
            categories = frozenset(i.category for i in self._items.values() if i.station == "bar")
        else:
            categories = self.parse_category(rest)
        blocked = parse_allergens(exclude_allergens)
        matches = [
            item
            for item in self._items.values()
            if item.available
            and required_tags <= set(item.dietary_tags)
            and not (blocked & set(item.allergens))
            and (not categories or item.category in categories)
            and (temperature is None or temperature in item.serving)
            and (budget_inr is None or item.price_inr <= budget_inr)
            and (max_prep_time_minutes is None or item.preparation_time_minutes <= max_prep_time_minutes)
            and (min_filling_score is None or item.filling_score >= min_filling_score)
            and (max_sweetness_level is None or (item.sweetness_level or 0) <= max_sweetness_level)
        ]
        return sorted(matches, key=lambda i: (i.price_inr, i.preparation_time_minutes, i.id))
