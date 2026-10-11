# CaféFlow AI

> Your café visit, intelligently coordinated.

A conversational café companion, shown here as an **unofficial concept demo for Third
Wave Coffee**, with a fictional café, **The Daily Grind**, as a fallback profile. Customers describe their budget, diet and time constraints in plain language. A
**Gemini** agent uses native function calling to query a deterministic backend that
searches the real menu, evaluates item combinations, and estimates readiness against
the live order queue. Customers explicitly confirm before an order is created, and
staff manage orders from a dashboard. The whole thing runs as one container on
**Google Cloud Run**.

**Demo only:** in-memory orders, no payments, no POS integration. The Third Wave Coffee
profile is **not an official Third Wave Coffee app** and is not affiliated with them. No
orders are sent to the café.

## Café profiles

Each café is one JSON file in `app/data/cafes/`. It holds the identity, copy, theme
colours, logo path and menu. Select a café with `CAFE_PROFILE`.

| Profile | What it is |
| --- | --- |
| `third_wave` (default) | 23 real items from a public Third Wave Coffee delivery listing (Bengaluru, Oct 2026), with prices and veg/egg/non-veg markers. Prep times are CaféFlow estimates. Vegan, dairy-free, gluten-free and allergen details are **indicative**, inferred from standard recipes, and the UI and agent tell customers to confirm with staff. Availability is not live. Sources are listed in the file. |
| `daily_grind` | Fictional sample café (15 items, one sold out). Useful as a neutral fallback. |

Every item declares its `station` (`bar` or `kitchen`) and its Indian `diet_marker`
(`veg`, `egg` or `non-veg`). Categories, theme and copy are data, so adding a café needs
no code changes. `tests/test_profiles.py` validates every profile file automatically.

---

## What makes it different: time-aware ordering

"I'm vegetarian, have ₹250, and need something filling in 10 minutes":

1. Gemini extracts the constraints and calls `recommend_options(budget_inr=250, dietary_preference="vegetarian", deadline_minutes=10, want_filling=true)`.
2. The backend filters on **hard constraints** (availability, dietary tags, allergens, budget). It then evaluates every single item and every combination of up to 3 items (at most one drink each), about 575 candidates in all.
3. Each candidate gets a wait estimate from the **same estimator used for real orders**, which includes the current queue. Candidates that miss the deadline are excluded.
4. Results are ranked by preference (filling score, then wait, then price) and diversified, then sent back to Gemini, which explains the trade-offs.
5. "Actually, I only have five minutes" makes Gemini call the tool again with the new deadline. The 8-minute sandwich drops out.
6. If nothing satisfies every constraint, the closest alternatives come back in a **separate list**, and each one lists exactly what it misses. Dietary and allergen rules are never relaxed.

### Wait-time heuristic (deterministic, approximate)

- Two stations work **in parallel**: the **bar** (coffee, tea, cold drinks) and the **kitchen** (sandwiches, snacks). Each makes its items one at a time.
- Station backlog = prep minutes from active orders. `confirmed` orders count in full, `preparing` orders count at 50%, and `ready`, `completed` and `cancelled` orders count as nothing.
- `estimated_wait = max over the order's stations (station backlog + this order's prep minutes at that station)`, rounded up.

This is a fixed rule, not a trained model, and the UI always labels waits as approximate.

## Architecture

```
Browser (vanilla HTML/CSS/JS)          single FastAPI container (Cloud Run)
 ├─ /         customer chat + cart  ─► /api/chat ──► agent.py ──► Gemini API (google-genai, function calling)
 ├─ /staff    staff dashboard              │            │  bounded loop (max 6 rounds)
 └─ confirm modal ─► /api/orders           │            ▼
                                           │        tools.py  (Pydantic-validated, allow-listed)
                                           ▼            │
                              order_service.py ◄────────┤
                              queue_service.py          ├─ menu_service.py ◄── data/menu.json
                              recommendation_service.py ┘
```

| File | Role |
| --- | --- |
| `app/main.py` | FastAPI app factory, endpoints, error handling, chat rate limit |
| `app/agent.py` | Gemini loop, connection status, labelled rule-based fallback |
| `app/tools.py` | Function declarations, argument validation, tool execution |
| `app/recommendation_service.py` | Time-aware combination engine |
| `app/queue_service.py` | Deterministic wait estimator |
| `app/order_service.py` | In-memory order store, server-side totals and IDs, status transitions |
| `app/menu_service.py` | Menu loading, consistency validation, structured filtering |
| `app/data/menu.json` | 15 sample items (one deliberately sold out) |
| `app/static/` | Customer app and staff dashboard (no build step) |

### Tools Gemini can call

| Tool | Purpose |
| --- | --- |
| `recommend_options` | Time-aware engine: budget, diet, deadline, filling, sweetness, allergens, category |
| `search_menu` | Filtered menu lookup; returns `[]` when nothing matches |
| `estimate_wait` | Queue-aware estimate for specific item IDs |
| `prepare_order` | Builds a **draft** order with the server-side total and wait. It **cannot place orders.** |

**The model never places orders.** The backend `create_order` runs only behind
`POST /api/orders`, which the UI calls after the customer presses **Confirm & place
order**. That request must include `confirmed: true`. The server revalidates the items,
checks availability, recalculates the total and wait, and generates the order ID. Extra
fields such as `total_inr` or `order_id` are rejected with 422.

### API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness and basic status (no secrets) |
| GET | `/` , `/staff` | Customer app, staff dashboard |
| GET | `/api/menu` | Sample menu |
| GET | `/api/status` · POST `/api/status/verify` | Gemini connection state (verify makes a real `models.get` call) |
| POST | `/api/chat` | `{message, history[]}` → reply, mode (`gemini`/`fallback`), tool trace, cards |
| POST | `/api/orders/quote` | Server-side price and wait for a cart (changes nothing) |
| POST | `/api/orders` | Create an order. Requires `confirmed: true`. Returns 201 with the order ID |
| GET | `/api/orders` | Orders, counts, queue backlog |
| PATCH | `/api/orders/{id}/status` | `confirmed → preparing → ready → completed`, or `confirmed → cancelled`. Other transitions return 409 |

### Honest status indicator

The header shows **Gemini connected** only after a real API request has succeeded in
this server session (a chat call, or the `models.get` verification on page load).
Without a key it shows **Demo mode**. Fallback replies are badged "Demo mode ·
rule-based". They use the same deterministic tools, but parse constraints with simple
rules instead of Gemini.

---

## Run locally

Requires Python 3.11+.

```bash
cd cafeflow-ai
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # then put your key in GEMINI_API_KEY
uvicorn app.main:app --reload --port 8090
```

Open http://localhost:8090 (customer) and http://localhost:8090/staff (staff).

The app starts without a key and runs in labelled demo mode.

### Gemini configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `GEMINI_API_KEY` | (none) | Google AI Studio key: https://aistudio.google.com/apikey. Server-side only, never sent to the browser. |
| `GEMINI_MODEL` | `gemini-flash-latest` | Google's alias for the current Flash model. Pin a stable model for reproducibility. To list what your key can use, run `python scripts/list_models.py`. If the configured model returns 404, the app picks an available Flash model automatically. |
| `CAFE_PROFILE` | `third_wave` | Which `app/data/cafes/<id>.json` to serve |
| `GEMINI_THINKING_LEVEL` | `low` | Reasoning effort for Gemini 3+ models (`minimal`, `low`, `medium`, `high` or `default`). `low` keeps replies at about 5 s. |
| `APP_ENV` | `development` | `production` hides `/docs` |
| `MAX_TOOL_ROUNDS` | `6` | Upper bound on function-calling rounds per message |
| `CHAT_RATE_LIMIT_PER_MINUTE` | `20` | Per-client limit on `/api/chat` |

Authentication uses Gemini API keys from AI Studio (`genai.Client(api_key=..., vertexai=False)`).
Vertex AI is not used, and the explicit `vertexai=False` prevents silently mixing the two.

### Tests

```bash
pytest -q
```

69 tests cover menu filtering, dietary safety, recommendations (including the 10 → 5
minute recalculation), wait determinism, order validation, status transitions, the
API, and the agent loop. The agent tests use a fake Gemini client, so they cost no
quota.

---

## Deploy to Cloud Run

Use the Google account and project that hold your credits. Billing must be enabled on the project.

```bash
# 1. Authenticate and select the project
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud billing projects describe YOUR_PROJECT_ID   # billingEnabled: true

# 2. Enable the required APIs
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

# 3. Store the Gemini key in Secret Manager (paste the key on stdin, then Ctrl-D)
gcloud secrets create gemini-api-key --replication-policy=automatic --data-file=-

# 4. Let the Cloud Run runtime service account read the secret
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# 5. Build from the Dockerfile and deploy
cd cafeflow-ai
gcloud run deploy cafeflow-ai --source . --region asia-south1 \
  --allow-unauthenticated --max-instances 1 --memory 512Mi \
  --set-env-vars APP_ENV=production \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest

# 6. Verify
URL=$(gcloud run services describe cafeflow-ai --region asia-south1 --format='value(status.url)')
curl -s "$URL/health"
curl -s -X POST "$URL/api/status/verify"
```

`--max-instances 1` keeps every request on the same in-memory order store, so
customers and staff see the same orders. The `.gcloudignore` keeps `.env` and `.venv`
out of the upload.

---

## Limitations (by design for a rapid prototype)

- **Orders are in memory.** They are lost when the server or Cloud Run instance restarts or scales to zero, and are not shared across instances. Production would need a database (for example Cloud SQL or Firestore).
- **No authentication.** Anyone with the URL can open the staff dashboard and change order statuses.
- **No payments and no POS integration.** Orders are demo records only.
- **Wait times are approximate.** They come from a fixed heuristic, not live kitchen data or ML.
- **Menus are not live.** The Third Wave Coffee profile uses public third-party listings that vary by outlet and may be out of date. Its allergen data is indicative only. The Daily Grind is fictional.
