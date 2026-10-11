from app.queue_service import estimate_from_backlog, queue_backlog, station_minutes


def test_estimates_are_non_negative_and_deterministic(menu, store):
    items = [menu.get("veg_puff_01"), menu.get("masala_chai_01")]
    first = store.estimate(items)
    second = store.estimate(items)
    assert first == second
    assert first["estimated_wait_minutes"] >= 0
    assert first["approximate"] is True
    assert first["assumptions"]


def test_bar_and_kitchen_work_in_parallel(menu):
    puff, chai = menu.get("veg_puff_01"), menu.get("masala_chai_01")  # kitchen 3, bar 4
    estimate = estimate_from_backlog([puff, chai], queue_backlog([]))
    assert estimate["estimated_wait_minutes"] == 4  # max, not 3 + 4


def test_same_station_items_are_sequential(menu):
    puff, bread = menu.get("veg_puff_01"), menu.get("banana_walnut_bread_01")  # both kitchen: 3 + 1
    assert station_minutes([puff, bread]) == {"kitchen": 4}
    assert estimate_from_backlog([puff, bread], queue_backlog([]))["estimated_wait_minutes"] == 4


def test_backlog_counts_only_active_work():
    backlog = queue_backlog([
        ("confirmed", {"kitchen": 8}),
        ("preparing", {"kitchen": 6, "bar": 4}),  # counted at 50%
        ("ready", {"kitchen": 9}),  # finished: not counted
        ("completed", {"bar": 5}),
        ("cancelled", {"bar": 5}),
    ])
    assert backlog == {"kitchen": 11.0, "bar": 2.0}


def test_queue_increases_wait_and_completion_clears_it(menu, store):
    sandwich = [menu.get("paneer_tikka_sandwich_01")]
    empty_wait = store.estimate(sandwich)["estimated_wait_minutes"]
    order = store.create_order(["veg_club_sandwich_01"])  # 7 min kitchen
    assert store.estimate(sandwich)["estimated_wait_minutes"] == empty_wait + 7
    store.update_status(order.order_id, "preparing")
    assert store.estimate(sandwich)["estimated_wait_minutes"] == empty_wait + 4  # ceil(3.5)
    store.update_status(order.order_id, "ready")
    assert store.estimate(sandwich)["estimated_wait_minutes"] == empty_wait


def test_drinks_are_not_delayed_by_kitchen_queue(menu, store):
    store.create_order(["chicken_pesto_sandwich_01"])
    estimate = store.estimate([menu.get("cold_brew_01")])
    assert estimate["estimated_wait_minutes"] == menu.get("cold_brew_01").preparation_time_minutes
