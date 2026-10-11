import json

import pytest
from pydantic import ValidationError

from app.config import CAFES_DIR
from app.menu_service import InvalidPreferenceError, parse_dietary
from app.recommendation_service import Constraints, recommend
from app.schemas import MenuItem


def test_menu_lookup_returns_real_menu_items(menu):
    raw_ids = {entry["id"] for entry in json.loads((CAFES_DIR / "daily_grind.json").read_text())["items"]}
    items = menu.all_items()
    assert 12 <= len(items) <= 15
    assert {i.id for i in items} == raw_ids
    assert menu.get("cold_brew_01").price_inr == 140
    assert menu.get("does_not_exist") is None


def test_search_results_are_real_available_items(menu):
    results = menu.search()
    assert results, "unfiltered search should return the available menu"
    assert all(menu.get(i.id) == i for i in results)
    assert all(i.available for i in results)
    assert "mango_coconut_smoothie_01" not in {i.id for i in results}  # sold out in sample data


def test_budget_filter_excludes_items_over_budget(menu):
    results = menu.search(budget_inr=120)
    assert results
    assert all(i.price_inr <= 120 for i in results)
    assert "paneer_tikka_sandwich_01" not in {i.id for i in results}


@pytest.mark.parametrize(
    "preference, forbidden_ids",
    [
        ("vegetarian", {"chicken_pesto_sandwich_01"}),
        ("vegan", {"cappuccino_01", "paneer_tikka_sandwich_01", "chicken_pesto_sandwich_01"}),
        ("gluten-free", {"veg_puff_01", "falafel_hummus_wrap_01"}),
        ("dairy-free", {"masala_chai_01", "iced_mocha_01"}),
    ],
)
def test_dietary_filter_excludes_incompatible_items(menu, preference, forbidden_ids):
    results = menu.search(dietary_preference=preference)
    ids = {i.id for i in results}
    assert results
    assert not (ids & forbidden_ids)
    required = parse_dietary(preference)
    assert all(required <= set(i.dietary_tags) for i in results)


def test_combined_dietary_preferences_and_allergens(menu):
    results = menu.search(dietary_preference="vegan, gluten-free", exclude_allergens=["nuts"])
    assert results
    for item in results:
        assert {"vegan", "gluten-free"} <= set(item.dietary_tags)
        assert "tree-nuts" not in item.allergens


def test_unknown_dietary_preference_is_rejected(menu):
    with pytest.raises(InvalidPreferenceError):
        menu.search(dietary_preference="keto-paleo")


def test_menu_data_consistency_is_enforced():
    with pytest.raises(ValidationError):
        MenuItem(
            id="bad_01", name="Bad Vegan Latte", category="Coffee", station="bar", diet_marker="veg", price_inr=150, description="x",
            dietary_tags=("vegan", "vegetarian", "dairy-free"), allergens=("milk",),
            preparation_time_minutes=3, available=True, filling_score=1, sweetness_level=2,
        )


# --- time-aware recommendations ---------------------------------------------


def test_demo_scenario_options_satisfy_every_constraint(menu, store):
    result = recommend(menu, store, Constraints(budget_inr=250, dietary_preference="vegetarian",
                                                deadline_minutes=10, want_filling=True))
    assert result["options"]
    for option in result["options"]:
        assert option["meets_all_constraints"]
        assert option["total_price_inr"] <= 250
        assert option["estimated_wait_minutes"] <= 10
        assert option["combined_filling_score"] >= 4
        assert all("vegetarian" in item["dietary_tags"] for item in option["items"])
        assert option["total_price_inr"] == sum(menu.get(i).price_inr for i in option["item_ids"])


def test_tighter_deadline_recalculates_options(menu, store):
    ten = recommend(menu, store, Constraints(budget_inr=250, dietary_preference="vegetarian",
                                             deadline_minutes=10, want_filling=True))
    five = recommend(menu, store, Constraints(budget_inr=250, dietary_preference="vegetarian",
                                              deadline_minutes=5, want_filling=True))
    assert five["options"]
    assert all(o["estimated_wait_minutes"] <= 5 for o in five["options"])
    ten_ids = {tuple(o["item_ids"]) for o in ten["options"]}
    five_ids = {tuple(o["item_ids"]) for o in five["options"]}
    assert ten_ids != five_ids  # e.g. the 8-minute paneer sandwich drops out
    assert ("paneer_tikka_sandwich_01",) in ten_ids


def test_impossible_constraints_return_labelled_alternatives(menu, store):
    result = recommend(menu, store, Constraints(budget_inr=100, dietary_preference="vegan",
                                                deadline_minutes=2, want_filling=True))
    assert result["options"] == []
    alts = result["alternatives_not_meeting_all_constraints"]
    assert alts
    for alt in alts:
        assert not alt["meets_all_constraints"]
        assert alt["unmet_constraints"]
        # dietary requirements are never relaxed, even for alternatives
        assert all("vegan" in item["dietary_tags"] for item in alt["items"])


def test_queue_load_affects_recommendations(menu, store):
    before = recommend(menu, store, Constraints(dietary_preference="vegetarian", deadline_minutes=10, want_filling=True))
    for _ in range(2):
        store.create_order(["paneer_tikka_sandwich_01"])  # 16 min of kitchen work queued
    after = recommend(menu, store, Constraints(dietary_preference="vegetarian", deadline_minutes=10, want_filling=True))
    assert before["options"]
    # with the kitchen backed up, only bar-only items could fit; none are "filling" enough
    assert all(o["estimated_wait_minutes"] <= 10 for o in after["options"])
    assert len(after["options"]) < len(before["options"])


def test_generic_drink_and_food_words_filter_by_station(menu):
    drinks = menu.search(category="drinks")
    assert drinks and all(i.station == "bar" for i in drinks)
    food = menu.search(category="food")
    assert food and all(i.station == "kitchen" for i in food)
    iced = menu.search(category="cold drink")
    assert iced and all(i.station == "bar" and "iced" in i.serving for i in iced)


def test_category_applies_to_one_half_of_a_drink_and_food_pairing(menu, store):
    result = recommend(menu, store, Constraints(category="iced", require_drink=True, require_food=True, budget_inr=500))
    assert result["options"]
    for option in result["options"]:
        drinks = [i for i in option["items"] if i["station"] == "bar"]
        food = [i for i in option["items"] if i["station"] == "kitchen"]
        assert len(drinks) == 1 and "iced" in drinks[0]["serving"] and food
