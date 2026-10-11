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
Browser (vanilla HTML/CSS/JS)              FastAPI container on Cloud Run (asia-south1)
 ├─ /        customer chat + cart ─► /api/chat ─► Google ADK runner
 ├─ /staff   staff dashboard                       Barista AI (router LlmAgent)
 └─ confirm modal ─► /api/orders                     ├─transfer─► Discovery  (recommend_options, search_menu)
                                                     ├─transfer─► Ordering   (prepare_order, search_menu)
                                                     └─transfer─► Wait-time  (estimate_wait, search_menu)
                                                              │  Gemini 3.8 Flash via Gemini API (key in Secret Manager)
                                                              ▼
                                   tools.py — Pydantic-validated, allow-listed backend functions
                                   recommendation · queue · menu · order services
                                              │
                                              ▼
                     Cloud SQL for PostgreSQL: orders + ADK conversation sessions
                     Cloud Logging: structured JSON events (chat_turn, order_created, …)
```

| File | Role |
| --- | --- |
| `app/adk_agents.py` | ADK agent graph: Barista AI router plus Discovery / Ordering / Wait-time specialists and their prompts |
| `app/agent.py` | Runs ADK turns, per-browser sessions, connection status, labelled rule-based fallback |
| `app/tools.py` | Function declarations, argument validation, tool execution |
| `app/recommendation_service.py` | Time-aware mix-and-match engine |
| `app/queue_service.py` | Deterministic wait estimator |
| `app/order_service.py` | Server-side totals and IDs, status transitions |
| `app/db.py` | Order persistence: Cloud SQL (PostgreSQL) or in-memory for local dev |
| `app/logging_setup.py` | JSON logs for Cloud Logging |
| `app/menu_service.py` | Menu loading, consistency validation, structured and hot/iced-aware filtering |
| `app/data/cafes/*.json` | Café profiles: Third Wave Coffee (42 items from the printed menu) and fictional The Daily Grind |
| `app/static/` | Customer app and staff dashboard (no build step) |
| `.github/workflows/deploy.yml` | Tests, then builds, pushes and deploys to Cloud Run |

### ADK agents

- **Barista AI** (router) greets the customer and transfers each request to a specialist. It never states prices or waits itself.
- **Discovery** recommends and explores the menu with mix-and-match pairings, using the time-aware engine and the café's "must try" picks.
- **Ordering** turns chosen items into a **draft**. Only the customer's Confirm button creates the order.
- **Wait-time** gives queue-aware readiness estimates.

Follow-up messages go straight to the active specialist, and specialists hand off to each other as needed. Sessions are stored in Cloud SQL through ADK's `DatabaseSessionService`, so a conversation survives restarts and multiple instances.

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

81 tests cover menu filtering, dietary safety, recommendations (including the 10 → 5
minute recalculation), wait determinism, order validation, status transitions and the
API. The ADK agent tests run the real router graph with a scripted fake model, so they
cost no quota.

The Postgres integration tests (orders, transitions, backlog, and ADK sessions shared
across instances) run when `TEST_DB_HOST` is set, as in CI:

```bash
docker run -d --name cafeflow-pg -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=cafeflow_test -p 55432:5432 postgres:17-alpine
TEST_DB_HOST=localhost TEST_DB_PORT=55432 TEST_DB_PASSWORD=dev pytest -q
```

---

## Google Cloud deployment

Project `thirdwavecoffeecompanion`, region `asia-south1`. Every resource was created with gcloud:

| Resource | Name | Purpose |
| --- | --- | --- |
| Cloud Run service | `cafeflow-ai` | The app (min 1 instance during the event to avoid cold starts; max 3) |
| Cloud SQL (PostgreSQL 17, `db-f1-micro`) | `cafeflow-db` / database `cafeflow` / user `cafeflow_app` | Orders and ADK sessions |
| Secret Manager | `gemini-api-key`, `cafeflow-db-password` | Injected into Cloud Run as env vars; never in code or GitHub |
| API key | "CafeFlow AI Gemini (Cloud Run)" | Restricted to the Gemini API (`generativelanguage.googleapis.com`) |
| Service account | `cafeflow-run` | Runtime identity: `cloudsql.client`, `logging.logWriter`, and secret access on the two secrets only |
| Service account | `cafeflow-deployer` | CI identity: `run.admin`, Artifact Registry writer on one repo, act-as `cafeflow-run` only |
| Artifact Registry | `cafeflow` (Docker) | Images tagged by commit SHA |
| Workload Identity Federation | pool `github`, provider `github-oidc` | Keyless GitHub → GCP auth, limited to `AgentHarshith/cafeflow-third-wave` |

### CI/CD (GitHub Actions)

`.github/workflows/deploy.yml` runs on every push to `main`:

1. **Test:** the full suite, including Postgres integration tests against a `postgres:17` service container.
2. **Build and push** the Docker image to Artifact Registry.
3. **Deploy** to Cloud Run with the Cloud SQL connection, secrets and runtime service account.
4. **Smoke test** `/health`, which must report `storage: cloudsql`.

Repository variables (not secrets) are `GCP_PROJECT_ID`, `GCP_REGION`, `WIF_PROVIDER`, `DEPLOYER_SA`, `RUNTIME_SA` and `CLOUD_SQL_CONNECTION_NAME`.

### Logs

On Cloud Run the app writes JSON logs that Cloud Logging parses. Filter events in Logs Explorer:

```
resource.type="cloud_run_revision" AND resource.labels.service_name="cafeflow-ai" AND jsonPayload.event="chat_turn"
```

Each `chat_turn` records the mode, agent route, tools and latency. Customer message text is never logged.

### After the event

```bash
gcloud run services update cafeflow-ai --min-instances 0 --region asia-south1   # stop paying for an idle instance
gcloud sql instances patch cafeflow-db --activation-policy NEVER                  # stop the database
```

---

## Limitations (by design for a rapid prototype)

- **Local development without a database keeps orders in memory.** The deployed app stores them in Cloud SQL.
- **No authentication.** Anyone with the URL can open the staff dashboard and change order statuses.
- **No payments and no POS integration.** Orders are demo records only.
- **Wait times are approximate.** They come from a fixed heuristic, not live kitchen data or ML.
- **Menus are not live.** The Third Wave Coffee profile is transcribed from the café's printed menu, so availability is not live. Drafts are Regular size. Allergen and vegan details are indicative only. The Daily Grind is fictional.
