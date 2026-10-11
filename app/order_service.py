"""In-memory demo order store.

Orders live only in this process's memory: they are lost when the server or the
Cloud Run instance restarts, and are not shared between instances. This is a
deliberate rapid-prototype limitation (see README).
"""

from __future__ import annotations

import secrets
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from .menu_service import CafeError, MenuService
from .queue_service import ASSUMPTIONS, estimate_from_backlog, queue_backlog, station_minutes
from .schemas import CustomerPreferences, MenuItem, Order, OrderLine

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "confirmed": {"preparing", "cancelled"},
    "preparing": {"ready"},
    "ready": {"completed"},
    "completed": set(),
    "cancelled": set(),
}
ACTIVE_STATUSES = ("confirmed", "preparing", "ready")
MAX_ORDERS_IN_MEMORY = 500
MAX_QUANTITY_PER_ITEM = 5


class OrderNotFoundError(CafeError):
    pass


class InvalidTransitionError(CafeError):
    pass


class OrderLimitError(CafeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderStore:
    def __init__(self, menu: MenuService, clock: Callable[[], datetime] = _utcnow):
        self._menu = menu
        self._clock = clock
        self._orders: dict[str, Order] = {}
        self._station_minutes: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    # -- queue ---------------------------------------------------------------

    def backlog(self) -> dict[str, float]:
        with self._lock:
            return queue_backlog(
                (order.status, self._station_minutes[order.order_id]) for order in self._orders.values()
            )

    def estimate(self, items: list[MenuItem]) -> dict[str, Any]:
        estimate = estimate_from_backlog(items, self.backlog())
        counts = self.count_by_status()
        estimate["active_orders_in_queue"] = counts.get("confirmed", 0) + counts.get("preparing", 0)
        estimate["assumptions"] = ASSUMPTIONS
        return estimate

    # -- quoting -------------------------------------------------------------

    def _resolve(self, item_ids: Iterable[str]) -> tuple[list[MenuItem], list[OrderLine], int]:
        ids = list(item_ids)
        if not ids:
            raise CafeError("An order needs at least one item.")
        counts = Counter(ids)
        too_many = [i for i, n in counts.items() if n > MAX_QUANTITY_PER_ITEM]
        if too_many:
            raise CafeError(f"At most {MAX_QUANTITY_PER_ITEM} of each item per demo order.")
        items = self._menu.require_available(ids)
        lines: list[OrderLine] = []
        for item_id, quantity in counts.items():
            item = self._menu.get(item_id)
            assert item is not None  # guaranteed by require_available
            lines.append(
                OrderLine(
                    item_id=item.id,
                    name=item.name,
                    category=item.category,
                    unit_price_inr=item.price_inr,
                    quantity=quantity,
                    line_total_inr=item.price_inr * quantity,
                )
            )
        total = sum(line.line_total_inr for line in lines)
        return items, lines, total

    def quote(self, item_ids: Iterable[str]) -> dict[str, Any]:
        """Server-side price and wait for a prospective order. Changes nothing."""
        items, lines, total = self._resolve(item_ids)
        return {
            "lines": [line.model_dump() for line in lines],
            "item_count": len(items),
            "total_inr": total,
            "wait_estimate": self.estimate(items),
        }

    # -- orders --------------------------------------------------------------

    def _new_order_id(self) -> str:
        while True:
            order_id = "CF-" + secrets.token_hex(3).upper()
            if order_id not in self._orders:
                return order_id

    def create_order(
        self, item_ids: Iterable[str], customer_preferences: CustomerPreferences | None = None
    ) -> Order:
        with self._lock:
            if len(self._orders) >= MAX_ORDERS_IN_MEMORY:
                raise OrderLimitError("The demo order store is full. Restart the demo to clear it.")
            items, lines, total = self._resolve(item_ids)
            estimate = estimate_from_backlog(items, self.backlog())
            now = self._clock()
            wait = estimate["estimated_wait_minutes"]
            order = Order(
                order_id=self._new_order_id(),
                status="confirmed",
                lines=lines,
                item_count=len(items),
                total_inr=total,
                estimated_wait_minutes=wait,
                estimated_ready_at=now + timedelta(minutes=wait),
                created_at=now,
                updated_at=now,
                customer_preferences=customer_preferences,
            )
            self._orders[order.order_id] = order
            self._station_minutes[order.order_id] = station_minutes(items)
            return order

    def get(self, order_id: str) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise OrderNotFoundError(f"Order {order_id} not found.")
            return order

    def list_orders(self) -> list[Order]:
        with self._lock:
            return sorted(self._orders.values(), key=lambda o: o.created_at, reverse=True)

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(order.status for order in self._orders.values()))

    def update_status(self, order_id: str, new_status: str) -> Order:
        with self._lock:
            order = self.get(order_id)
            if new_status not in ALLOWED_TRANSITIONS[order.status]:
                allowed = sorted(ALLOWED_TRANSITIONS[order.status]) or ["none"]
                raise InvalidTransitionError(
                    f"Cannot move order {order_id} from '{order.status}' to '{new_status}'. "
                    f"Allowed next: {', '.join(allowed)}."
                )
            updated = order.model_copy(update={"status": new_status, "updated_at": self._clock()})
            self._orders[order_id] = updated
            return updated
