"""Customer feedback (written by the Feedback agent or the one-tap pop-up) and the
staff "Room" view: feedback trends plus unmet demand from the recommendation engine."""

from __future__ import annotations

import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DocStore
from .menu_service import CafeError
from .order_service import OrderStore

FEEDBACK_KIND = "feedback"
DEMAND_KIND = "demand"
THEMES = ("taste", "temperature", "wait_time", "service", "value", "accuracy", "ambience", "menu_variety")
SENTIMENTS = ("positive", "neutral", "negative")
MAX_COMMENT = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sentiment_from_rating(rating: int | None) -> str:
    if rating is None:
        return "neutral"
    return "positive" if rating >= 4 else "neutral" if rating == 3 else "negative"


class FeedbackService:
    def __init__(self, docs: DocStore, orders: OrderStore):
        self._docs = docs
        self._orders = orders

    def record(self, *, order_id: str | None = None, rating: int | None = None, sentiment: str | None = None,
               themes: list[str] | None = None, comment: str | None = None, source: str = "chat",
               feedback_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        """Create feedback, or add details to an existing one when feedback_id is given."""
        if order_id:
            self._orders.get(order_id)  # raises OrderNotFoundError for made-up ids
        if rating is not None and not 1 <= rating <= 5:
            raise CafeError("Rating must be between 1 and 5.")
        if sentiment is not None and sentiment not in SENTIMENTS:
            raise CafeError(f"Sentiment must be one of: {', '.join(SENTIMENTS)}.")
        bad = [t for t in themes or [] if t not in THEMES]
        if bad:
            raise CafeError(f"Unknown theme(s) {bad}. Use: {', '.join(THEMES)}.")
        comment = (comment or "").strip()[:MAX_COMMENT] or None

        if feedback_id:
            def merge(current: dict[str, Any] | None) -> dict[str, Any]:
                if current is None:
                    raise CafeError(f"Feedback {feedback_id} not found.")
                merged = dict(current)
                if rating is not None:
                    merged["rating"] = rating
                merged["sentiment"] = sentiment or merged.get("sentiment") or sentiment_from_rating(merged.get("rating"))
                merged["themes"] = sorted(set(merged.get("themes", [])) | set(themes or []))
                if comment:
                    merged["comments"] = [*merged.get("comments", []), comment][-5:]
                merged["updated_at"] = _now().isoformat()
                return merged

            return self._docs.update(FEEDBACK_KIND, feedback_id, merge)

        doc = {
            "id": "FB-" + secrets.token_hex(3).upper(),
            "order_id": order_id,
            "rating": rating,
            "sentiment": sentiment or sentiment_from_rating(rating),
            "themes": sorted(set(themes or [])),
            "comments": [comment] if comment else [],
            "source": source,
            "session_id": session_id,
            "created_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
        }
        self._docs.put(FEEDBACK_KIND, doc["id"], doc)
        return doc

    def given_for_order(self, order_id: str) -> bool:
        return any(f.get("order_id") == order_id for f in self._docs.list(FEEDBACK_KIND, limit=500))

    def summary(self) -> dict[str, Any]:
        items = self._docs.list(FEEDBACK_KIND, limit=300)
        ratings = [f["rating"] for f in items if f.get("rating")]
        return {
            "count": len(items),
            "average_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "sentiment": dict(Counter(f.get("sentiment", "neutral") for f in items)),
            "themes": dict(Counter(t for f in items for t in f.get("themes", [])).most_common(6)),
            "latest": [
                {k: f.get(k) for k in ("id", "order_id", "rating", "sentiment", "themes", "created_at")}
                | {"comment": " / ".join(f.get("comments", []))[:240] or None}
                for f in items[:8]
            ],
        }


def describe_constraints(c: dict[str, Any]) -> str:
    parts = []
    if c.get("dietary_preference"):
        parts.append(str(c["dietary_preference"]))
    if c.get("budget_inr") is not None:
        parts.append(f"≤₹{c['budget_inr']}")
    if c.get("deadline_minutes") is not None:
        parts.append(f"≤{c['deadline_minutes']} min")
    if c.get("want_filling"):
        parts.append("filling")
    if c.get("category"):
        parts.append(str(c["category"]))
    if c.get("max_sweetness_level") is not None:
        parts.append("not too sweet")
    if c.get("require_drink") and c.get("require_food"):
        parts.append("drink + bite")
    if c.get("exclude_allergens"):
        parts.append("no " + "/".join(c["exclude_allergens"]))
    return " · ".join(parts) or "no constraints"


class DemandLog:
    """Records what customers asked the recommendation engine for, and whether it could deliver."""

    def __init__(self, docs: DocStore):
        self._docs = docs

    def record(self, constraints: dict[str, Any], options: int) -> None:
        doc_id = secrets.token_hex(6)
        self._docs.put(DEMAND_KIND, doc_id, {
            "id": doc_id, "constraints": constraints, "options": options, "met": options > 0,
            "at": _now().isoformat(),
        })

    def summary(self, hours: int = 6) -> dict[str, Any]:
        cutoff = _now() - timedelta(hours=hours)
        recent = [d for d in self._docs.list(DEMAND_KIND, limit=500)
                  if datetime.fromisoformat(d["at"]) >= cutoff]
        unmet = Counter(describe_constraints(d["constraints"]) for d in recent if not d["met"])
        return {
            "window_hours": hours,
            "requests": len(recent),
            "unmet": sum(unmet.values()),
            "top_unmet": [{"request": k, "count": v} for k, v in unmet.most_common(5)],
        }
