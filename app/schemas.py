"""Pydantic models shared by the services, the agent tools and the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DietMarker = Literal["veg", "egg", "non-veg"]  # the standard Indian menu marker
Station = Literal["bar", "kitchen"]  # bar = drinks, kitchen = food; the two work in parallel
DietaryTag = Literal["vegetarian", "vegan", "dairy-free", "gluten-free"]
Allergen = Literal["milk", "gluten", "tree-nuts", "peanuts", "sesame", "soy", "egg"]
OrderStatus = Literal["confirmed", "preparing", "ready", "completed", "cancelled"]

ItemId = str
ItemIdStr = Annotated[str, Field(pattern=r"^[a-z0-9_]{2,64}$")]


class MenuItem(BaseModel):
    """One sample-menu item. Dietary claims are explicit data, never inferred from names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ItemId = Field(pattern=r"^[a-z0-9_]{2,64}$")
    name: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=40)
    station: Station
    diet_marker: DietMarker
    price_inr: int = Field(ge=1, le=2000)
    description: str = Field(max_length=300)
    dietary_tags: tuple[DietaryTag, ...]
    allergens: tuple[Allergen, ...]
    preparation_time_minutes: int = Field(ge=0, le=60)
    available: bool
    filling_score: int = Field(ge=1, le=5)
    sweetness_level: int | None = Field(default=None, ge=0, le=5)

    @model_validator(mode="after")
    def _tags_match_allergens(self) -> "MenuItem":
        tags, allergens = set(self.dietary_tags), set(self.allergens)
        problems = []
        if "vegan" in tags and not {"vegetarian", "dairy-free"} <= tags:
            problems.append("vegan items must also be tagged vegetarian and dairy-free")
        if "vegan" in tags and allergens & {"milk", "egg"}:
            problems.append("vegan items cannot contain milk or egg")
        if "dairy-free" in tags and "milk" in allergens:
            problems.append("dairy-free items cannot contain milk")
        if "gluten-free" in tags and "gluten" in allergens:
            problems.append("gluten-free items cannot contain gluten")
        # Indian vegetarian convention: egg is not vegetarian.
        if "vegetarian" in tags and "egg" in allergens:
            problems.append("vegetarian items cannot contain egg")
        if (self.diet_marker == "veg") != ("vegetarian" in tags):
            problems.append("diet_marker 'veg' must match the vegetarian tag")
        if self.diet_marker == "egg" and "egg" not in allergens:
            problems.append("egg-marked items must list egg as an allergen")
        if problems:
            raise ValueError(f"{self.id}: " + "; ".join(problems))
        return self


class CafeProfile(BaseModel):
    """Café identity, copy and theme. Lives next to the menu in data/cafes/<id>.json."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    name: str
    fictional: bool
    currency: Literal["INR"] = "INR"
    sample_data: bool = True
    notice: str
    disclaimer: str
    logo: str | None = None
    logo_alt: str | None = None
    theme: dict[str, str] = Field(default_factory=dict)
    welcome_title: str
    welcome_body: str
    quick_prompts: list[str] = Field(min_length=1, max_length=6)
    allergen_data: Literal["sample", "indicative"] = "sample"
    data_note: str
    sources: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class CustomerPreferences(BaseModel):
    """Optional context saved with an order. Deliberately holds no personal data."""

    model_config = ConfigDict(extra="forbid")

    dietary_preference: str | None = Field(default=None, max_length=40)
    budget_inr: int | None = Field(default=None, ge=0, le=100_000)
    deadline_minutes: int | None = Field(default=None, ge=1, le=240)


class OrderLine(BaseModel):
    item_id: ItemId
    name: str
    category: str
    unit_price_inr: int
    quantity: int
    line_total_inr: int


class Order(BaseModel):
    order_id: str
    status: OrderStatus
    lines: list[OrderLine]
    item_count: int
    total_inr: int
    estimated_wait_minutes: int
    estimated_ready_at: datetime
    created_at: datetime
    updated_at: datetime
    customer_preferences: CustomerPreferences | None = None


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: list[ItemId] = Field(min_length=1, max_length=12)
    confirmed: bool = Field(
        default=False,
        description="Must be true: set only after the customer explicitly confirms in the UI.",
    )
    customer_preferences: CustomerPreferences | None = None


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: list[ItemId] = Field(min_length=1, max_length=12)


class StatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=800)
    history: list[ChatTurn] = Field(default_factory=list, max_length=30)
    # Item ids of the option cards currently on screen, so "I'll take option 2" can be resolved.
    shown_options: list[list[ItemIdStr]] = Field(default_factory=list, max_length=12)


class ToolCallTrace(BaseModel):
    name: str
    arguments: dict[str, Any]
    ok: bool
    summary: str


class ChatResponse(BaseModel):
    reply: str
    mode: Literal["gemini", "fallback"]
    model: str | None = None
    notice: str | None = None
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    menu_items: list[dict[str, Any]] = Field(default_factory=list)
    wait_estimate: dict[str, Any] | None = None
    order_proposal: dict[str, Any] | None = None
