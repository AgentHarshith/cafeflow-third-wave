"""Staff menu editing: price and sold-out overrides on top of the printed menu.

Overrides are stored in the document store (Cloud SQL in production), so every Cloud Run
instance picks them up within SYNC_SECONDS; the agents immediately recommend and price
orders with the edited values. "Reset" restores the printed-menu values.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .db import DocStore
from .menu_service import CafeError, MenuService, UnknownItemError, item_public
from .schemas import MenuItem

OVERRIDE_KIND = "menu_override"
SYNC_SECONDS = 5.0


class MenuAdmin:
    def __init__(self, menu: MenuService, docs: DocStore):
        self._menu = menu
        self._docs = docs
        self._lock = threading.Lock()
        self._last_sync = 0.0
        self._since: datetime | None = None
        self.sync(force=True)

    def sync(self, force: bool = False) -> None:
        """Apply overrides saved by any instance since the last sync (rate-limited)."""
        now = time.monotonic()
        if not force and now - self._last_sync < SYNC_SECONDS:
            return
        with self._lock:
            self._last_sync = now
            for item_id, data, updated_at in sorted(self._docs.list_updated_since(OVERRIDE_KIND, self._since),
                                                    key=lambda r: r[2]):
                self._apply(item_id, data)
                self._since = updated_at if self._since is None else max(self._since, updated_at)

    def _apply(self, item_id: str, data: dict[str, Any]) -> None:
        base = self._menu.default(item_id)
        if base is None:
            return
        if data.get("reset"):
            self._menu.set_item(base)
            return
        changes = {k: data[k] for k in ("price_inr", "price_large_inr", "available") if k in data}
        try:
            self._menu.set_item(MenuItem.model_validate({**base.model_dump(), **changes}))
        except ValidationError:
            pass  # never let a bad stored override break the menu

    def update(self, item_id: str, *, price_inr: int | None = None, price_large_inr: int | None = None,
               available: bool | None = None) -> MenuItem:
        base, current = self._menu.default(item_id), self._menu.get(item_id)
        if base is None or current is None:
            raise UnknownItemError(f"Unknown menu item id: {item_id}")
        if price_large_inr is not None and base.price_large_inr is None:
            raise CafeError(f"{base.name} has no Large size on the menu.")
        changes: dict[str, Any] = {}
        if price_inr is not None:
            changes["price_inr"] = price_inr
        if price_large_inr is not None:
            changes["price_large_inr"] = price_large_inr
        if available is not None:
            changes["available"] = available
        try:
            item = MenuItem.model_validate({**current.model_dump(), **changes})
        except ValidationError as exc:
            raise CafeError("; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())) from exc
        self._docs.put(OVERRIDE_KIND, item_id, {
            "price_inr": item.price_inr, "price_large_inr": item.price_large_inr, "available": item.available,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self._menu.set_item(item)
        return item

    def reset(self, item_id: str) -> MenuItem:
        base = self._menu.default(item_id)
        if base is None:
            raise UnknownItemError(f"Unknown menu item id: {item_id}")
        self._docs.put(OVERRIDE_KIND, item_id, {"reset": True, "updated_at": datetime.now(timezone.utc).isoformat()})
        self._menu.set_item(base)
        return base

    def staff_view(self) -> list[dict[str, Any]]:
        rows = []
        for item in self._menu.all_items():
            base = self._menu.default(item.id)
            row = item_public(item)
            row["default_price_inr"] = base.price_inr
            row["default_price_large_inr"] = base.price_large_inr
            row["edited"] = (item.price_inr, item.price_large_inr, item.available) != (
                base.price_inr, base.price_large_inr, base.available)
            rows.append(row)
        return rows
