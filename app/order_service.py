"""Order management: validation, server-side pricing and IDs, status transitions.

Persistence is pluggable (see db.py): Cloud SQL for PostgreSQL on Cloud Run, or an
in-memory store for local development, where orders reset on restart.
"""

from __future__ import annotations

import logging
import secrets
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import InMemoryOrderRepository, OrderRepository
from .menu_service import CafeError, MenuService
from .queue_service import ASSUMPTIONS, estimate_from_backlog, queue_backlog, station_minutes
from .schemas import CustomerPreferences, MenuItem, Order, OrderLine

logger = logging.getLogger("cafeflow.orders")

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
    def __init__(self, menu: MenuService, clock: Callable[[], datetime] = _utcnow,
                 repository: OrderRepository | None = None):
        self._menu = menu
        self._clock = clock
        self._repo: OrderRepository = repository or InMemoryOrderRepository()

    @property
    def backend(self) -> str:
        return self._repo.backend

    # -- queue ---------------------------------------------------------------

    def backlog(self) -> dict[str, float]:
        return queue_backlog(self._repo.workloads())

    def estimate(self, items: list[MenuItem]) -> dict[str, Any]:
        workloads = self._repo.workloads()
        estimate = estimate_from_backlog(items, queue_backlog(workloads))
        estimate["active_orders_in_queue"] = len(workloads)
        estimate["assumptions"] = ASSUMPTIONS
        return estimate

    # -- quoting -------------------------------------------------------------

    def _resolve(self, item_ids: Iterable[str]) -> tuple[list[MenuItem], list[OrderLine], int]:
        ids = list(item_ids)
        if not ids:
            raise CafeError("An order needs at least one item.")
        counts = Counter(ids)
        if any(n > MAX_QUANTITY_PER_ITEM for n in counts.values()):
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

    def create_order(
        self, item_ids: Iterable[str], customer_preferences: CustomerPreferences | None = None
    ) -> Order:
        if self._repo.backend == "memory" and self._repo.count() >= MAX_ORDERS_IN_MEMORY:
            raise OrderLimitError("The demo order store is full. Restart the demo to clear it.")
        items, lines, total = self._resolve(item_ids)
        estimate = estimate_from_backlog(items, self.backlog())
        now = self._clock()
        wait = estimate["estimated_wait_minutes"]
        for _ in range(5):  # server-generated id; retry on the (very unlikely) collision
            order = Order(
                order_id="CF-" + secrets.token_hex(3).upper(),
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
            if self._repo.add(order, station_minutes(items)):
                return order
        raise CafeError("Could not allocate an order id. Please try again.")

    def get(self, order_id: str) -> Order:
        order = self._repo.get(order_id)
        if order is None:
            raise OrderNotFoundError(f"Order {order_id} not found.")
        return order

    def list_orders(self) -> list[Order]:
        return self._repo.list()

    def count_by_status(self) -> dict[str, int]:
        return self._repo.count_by_status()

    def update_status(self, order_id: str, new_status: str) -> Order:
        def transition(order: Order) -> Order:
            if new_status not in ALLOWED_TRANSITIONS[order.status]:
                allowed = sorted(ALLOWED_TRANSITIONS[order.status]) or ["none"]
                raise InvalidTransitionError(
                    f"Cannot move order {order_id} from '{order.status}' to '{new_status}'. "
                    f"Allowed next: {', '.join(allowed)}."
                )
            return order.model_copy(update={"status": new_status, "updated_at": self._clock()})

        updated = self._repo.update(order_id, transition)
        if updated is None:
            raise OrderNotFoundError(f"Order {order_id} not found.")
        return updated
