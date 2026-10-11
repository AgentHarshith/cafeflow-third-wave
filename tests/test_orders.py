import re

import pytest

from app.menu_service import UnavailableItemError, UnknownItemError
from app.order_service import InvalidTransitionError, OrderNotFoundError


def test_order_creation_returns_generated_id_and_server_total(store, menu):
    order = store.create_order(["veg_puff_01", "veg_puff_01", "masala_chai_01"])
    assert re.fullmatch(r"CF-[0-9A-F]{6}", order.order_id)
    assert order.total_inr == 2 * menu.get("veg_puff_01").price_inr + menu.get("masala_chai_01").price_inr
    assert order.status == "confirmed"
    assert order.item_count == 3
    puff_line = next(line for line in order.lines if line.item_id == "veg_puff_01")
    assert puff_line.quantity == 2 and puff_line.line_total_inr == 160
    assert order.estimated_wait_minutes >= 0
    assert order.estimated_ready_at > order.created_at or order.estimated_wait_minutes == 0


def test_order_ids_are_unique(store):
    ids = {store.create_order(["espresso_01"]).order_id for _ in range(50)}
    assert len(ids) == 50


def test_invalid_menu_ids_are_rejected(store):
    with pytest.raises(UnknownItemError):
        store.create_order(["espresso_01", "free_lunch_99"])
    assert store.list_orders() == []


def test_unavailable_items_cannot_be_ordered(store):
    with pytest.raises(UnavailableItemError):
        store.create_order(["mango_coconut_smoothie_01"])
    assert store.list_orders() == []


def test_valid_status_transitions(store):
    order = store.create_order(["cappuccino_01"])
    for status in ("preparing", "ready", "completed"):
        order = store.update_status(order.order_id, status)
        assert order.status == status


@pytest.mark.parametrize("path", [["ready"], ["preparing", "confirmed"], ["cancelled", "preparing"]])
def test_invalid_status_transitions_are_rejected(store, path):
    order = store.create_order(["cappuccino_01"])
    with pytest.raises(InvalidTransitionError):
        for status in path:
            store.update_status(order.order_id, status)


def test_unknown_order_status_update(store):
    with pytest.raises(OrderNotFoundError):
        store.update_status("CF-000000", "preparing")
