def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["menu_items"] == 15
    assert "key" not in str(body).lower().replace("configured", "")


def test_starts_without_gemini_credentials(client):
    status = client.get("/api/status").json()
    assert status["gemini"]["state"] == "not_configured"
    assert client.get("/").status_code == 200
    assert client.get("/staff").status_code == 200


def test_menu_endpoint_is_labelled_sample_data(client):
    body = client.get("/api/menu").json()
    assert body["cafe"]["sample_data"] is True
    assert len(body["items"]) == 15


def test_order_requires_explicit_confirmation(client):
    response = client.post("/api/orders", json={"item_ids": ["espresso_01"]})
    assert response.status_code == 400
    assert client.get("/api/orders").json()["orders"] == []


def test_client_cannot_set_price_or_order_id(client):
    response = client.post(
        "/api/orders", json={"item_ids": ["espresso_01"], "confirmed": True, "total_inr": 1, "order_id": "CF-HACKED"}
    )
    assert response.status_code == 422


def test_confirmed_order_appears_in_staff_api(client):
    created = client.post("/api/orders", json={"item_ids": ["veg_puff_01", "masala_chai_01"], "confirmed": True})
    assert created.status_code == 201
    order = created.json()
    assert order["total_inr"] == 160
    listing = client.get("/api/orders").json()
    assert [o["order_id"] for o in listing["orders"]] == [order["order_id"]]
    assert listing["active_count"] == 1


def test_invalid_ids_return_400(client):
    response = client.post("/api/orders", json={"item_ids": ["nope_01"], "confirmed": True})
    assert response.status_code == 400
    assert "Unknown menu item" in response.json()["detail"]


def test_status_transitions_via_api(client):
    order_id = client.post("/api/orders", json={"item_ids": ["espresso_01"], "confirmed": True}).json()["order_id"]
    bad = client.patch(f"/api/orders/{order_id}/status", json={"status": "ready"})
    assert bad.status_code == 409
    assert client.patch(f"/api/orders/{order_id}/status", json={"status": "preparing"}).status_code == 200
    assert client.patch(f"/api/orders/{order_id}/status", json={"status": "ready"}).json()["status"] == "ready"
    assert client.patch(f"/api/orders/{order_id}/status", json={"status": "teleported"}).status_code == 422
    assert client.patch("/api/orders/CF-000000/status", json={"status": "preparing"}).status_code == 404


def test_quote_is_calculated_server_side(client):
    quote = client.post("/api/orders/quote", json={"item_ids": ["paneer_tikka_sandwich_01", "cold_brew_01"]}).json()
    assert quote["total_inr"] == 340
    assert quote["wait_estimate"]["estimated_wait_minutes"] == 8


def test_chat_falls_back_to_labelled_demo_mode_without_key(client):
    body = client.post("/api/chat", json={"message": "I'm vegetarian, have ₹250 and need something filling in 10 minutes"}).json()
    assert body["mode"] == "fallback"
    assert "Demo mode" in body["reply"] and "Demo mode" in body["notice"]
    assert body["recommendations"]
    assert body["tool_calls"][0]["name"] == "recommend_options"
    assert body["tool_calls"][0]["arguments"]["deadline_minutes"] == 10


def test_chat_fallback_carries_constraints_across_turns(client):
    history = [{"role": "user", "text": "I'm vegetarian, have ₹250, and need something filling in 10 minutes."},
               {"role": "assistant", "text": "..."}]
    body = client.post("/api/chat", json={"message": "Actually, I only have five minutes.", "history": history}).json()
    args = body["tool_calls"][0]["arguments"]
    assert args["deadline_minutes"] == 5 and args["budget_inr"] == 250 and args["dietary_preference"] == "vegetarian"
    assert all(o["estimated_wait_minutes"] <= 5 for o in body["recommendations"])


def test_chat_validates_input(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 801}).status_code == 422
