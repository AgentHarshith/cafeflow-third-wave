"""Every café profile in data/cafes must load, validate and render cleanly."""

import pytest
from fastapi.testclient import TestClient

from app.config import CAFES_DIR, Settings
from app.main import create_app
from app.menu_service import MenuService
from app.order_service import OrderStore
from app.recommendation_service import Constraints, recommend

PROFILES = sorted(p.stem for p in CAFES_DIR.glob("*.json"))


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_loads_and_validates(profile):
    menu = MenuService.from_file(CAFES_DIR / f"{profile}.json")
    items = menu.all_items()
    assert menu.cafe.id == profile
    assert 10 <= len(items) <= 30, "keep menus small enough for exhaustive combination search"
    assert {i.station for i in items} == {"bar", "kitchen"}
    assert any(i.available for i in items)


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_pages_render_without_placeholders(profile):
    client = TestClient(create_app(Settings(gemini_api_key=None, cafe_profile=profile)))
    for path in ("/", "/staff"):
        page = client.get(path)
        assert page.status_code == 200
        assert "{{" not in page.text
    assert client.get("/health").json()["cafe_profile"] == profile


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_supports_the_demo_scenario(profile):
    menu = MenuService.from_file(CAFES_DIR / f"{profile}.json")
    result = recommend(menu, OrderStore(menu), Constraints(budget_inr=250, dietary_preference="vegetarian",
                                                           deadline_minutes=10, want_filling=True))
    assert result["options"], "the judge demo prompt must have at least one compliant option"


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("phrase", ["cold drink", "drinks", "food", "coffee"])
def test_common_category_phrases_resolve(profile, phrase):
    menu = MenuService.from_file(CAFES_DIR / f"{profile}.json")
    assert menu.parse_category(phrase)


def test_real_cafe_profiles_are_labelled_unofficial():
    for profile in PROFILES:
        cafe = MenuService.from_file(CAFES_DIR / f"{profile}.json").cafe
        if not cafe.fictional:
            assert "unofficial" in cafe.disclaimer.lower()
            assert cafe.allergen_data == "indicative"
            assert cafe.sources, "real-café data must cite where it came from"
