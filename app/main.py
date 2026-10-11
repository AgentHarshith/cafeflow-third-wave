"""CaféFlow AI — FastAPI application (single container for Cloud Run)."""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import CafeAgent
from .config import STATIC_DIR, Settings, load_settings
from .connect_service import ConnectService
from .db import DatabaseConfig, build_storage
from .feedback_service import DemandLog, FeedbackService
from .menu_admin import MenuAdmin
from .logging_setup import configure_logging, event
from .menu_service import CafeError, MenuService, item_public
from .order_service import InvalidTransitionError, OrderLimitError, OrderNotFoundError, OrderStore
from .schemas import (
    CafeProfile, ChatRequest, ConnectAnswerRequest, ConnectDeclineRequest, ConnectProfileRequest,
    ConnectSessionRequest, DelayRequest, MenuItemUpdate,
)
from .schemas import  ChatResponse, CreateOrderRequest, Order, QuoteRequest, StatusUpdateRequest
from .tools import ToolExecutor

configure_logging()
logger = logging.getLogger("cafeflow")


# Feedback pop-up chips -> themes the staff "Room" panel aggregates.
CHIP_THEMES = {"Great taste": "taste", "Took too long": "wait_time", "Too sweet": "taste", "Not hot enough": "temperature",
               "Friendly staff": "service", "Wrong item": "accuracy", "Pricey": "value"}


class RateLimiter:
    """Tiny per-client sliding window so a public demo URL can't burn Gemini quota."""

    def __init__(self, limit_per_minute: int):
        self.limit = limit_per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


_DEFAULT_MARK = (
    '<svg viewBox="0 0 32 32"><path d="M8 12h12v6a6 6 0 0 1-6 6h0a6 6 0 0 1-6-6z" fill="currentColor"/>'
    '<path d="M20 13.5h1.6a2.7 2.7 0 0 1 0 5.4H20" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/>'
    '<path d="M11.5 5.5c-.8 1 .8 1.8 0 3M15 5.5c-.8 1 .8 1.8 0 3" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg>'
)
_SAFE_CSS_VAR = re.compile(r"^--[a-z0-9-]{1,40}$")
_SAFE_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def render_page(filename: str, cafe: CafeProfile) -> str:
    """Fill the café-specific placeholders in a static page (all values HTML-escaped)."""
    esc = html.escape
    theme = ";".join(f"{k}:{v}" for k, v in cafe.theme.items() if _SAFE_CSS_VAR.match(k) and _SAFE_COLOR.match(v))
    logo_file = STATIC_DIR / cafe.logo.removeprefix("/static/") if cafe.logo else None
    if logo_file and logo_file.is_file():
        mark = f'<img src="{esc(cafe.logo)}" alt="{esc(cafe.logo_alt or cafe.name)}" />'
        mark_class = "brand-mark has-logo"
    else:
        mark, mark_class = _DEFAULT_MARK, "brand-mark"
    for_line = "" if cafe.fictional else f"for {cafe.name} · "
    replacements = {
        "{{THEME_STYLE}}": f"<style>:root{{{theme}}}</style>" if theme else "",
        "{{BRAND_MARK_CLASS}}": mark_class,
        "{{BRAND_MARK}}": mark,
        "{{BRAND_FOR}}": esc(for_line),
        "{{CAFE_NAME}}": esc(cafe.name),
        "{{DISCLAIMER}}": esc(cafe.disclaimer),
        "{{WELCOME_TITLE}}": esc(cafe.welcome_title),
        "{{WELCOME_BODY}}": esc(cafe.welcome_body),
        "{{QUICK_PROMPTS}}": "".join(f'<button class="chip" type="button">{esc(p)}</button>' for p in cafe.quick_prompts),
        "{{CAFE_JSON}}": json.dumps(
            {"name": cafe.name, "fictional": cafe.fictional, "data_note": cafe.data_note,
             "allergen_data": cafe.allergen_data}
        ).replace("</", "<\\/"),
    }
    page = (STATIC_DIR / filename).read_text(encoding="utf-8")
    for token, value in replacements.items():
        page = page.replace(token, value)
    return page


def create_app(settings: Settings | None = None, llm=None, use_database: bool = True) -> FastAPI:
    settings = settings or load_settings()
    menu = MenuService.from_file(settings.menu_path)
    db_config = DatabaseConfig.from_env() if use_database else None
    order_repo, docs = build_storage(db_config)
    orders = OrderStore(menu, repository=order_repo)
    menu_admin = MenuAdmin(menu, docs)
    feedback = FeedbackService(docs, orders)
    demand = DemandLog(docs)
    connect = ConnectService(docs, icebreaker=lambda a, b, shared: agent.icebreaker(a, b, shared))
    executor = ToolExecutor(menu, orders, feedback=feedback, connect=connect, demand=demand)
    session_service = None
    if db_config:  # ADK conversation sessions live in Cloud SQL too (small pool: db-f1-micro)
        from google.adk.sessions import DatabaseSessionService

        session_service = DatabaseSessionService(db_url=db_config.sqlalchemy_url(), pool_pre_ping=True,
                                                 pool_size=2, max_overflow=1)
    agent = CafeAgent(settings, executor, llm=llm, session_service=session_service)
    limiter = RateLimiter(settings.chat_rate_limit_per_minute)

    app = FastAPI(
        title="CaféFlow AI",
        description="Your café visit, intelligently coordinated. (Demo with simulated café data.)",
        version="1.0.0",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )
    app.state.settings, app.state.menu, app.state.orders, app.state.agent = settings, menu, orders, agent

    @app.middleware("http")
    async def _no_stale_assets(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            await run_in_threadpool(menu_admin.sync)  # pick up staff price edits from any instance (rate-limited)
        # Pages and assets revalidate (ETag) on every load, so a redeploy is never masked by cache.
        response = await call_next(request)
        if request.url.path in ("/", "/staff") or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # -- error handling: clear messages, never stack traces --------------------

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        problems = [f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "Invalid request.", "problems": problems})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Something went wrong on our side. Please try again."})

    def require_staff(x_staff_pin: str | None = Header(default=None)) -> None:
        if settings.staff_pin and not hmac.compare_digest((x_staff_pin or "").encode(), settings.staff_pin.encode()):
            raise HTTPException(401, "Staff PIN required.")

    staff = [Depends(require_staff)]

    def _business_error(exc: CafeError) -> HTTPException:
        if isinstance(exc, OrderNotFoundError):
            return HTTPException(404, str(exc))
        if isinstance(exc, InvalidTransitionError):
            return HTTPException(409, str(exc))
        if isinstance(exc, OrderLimitError):
            return HTTPException(503, str(exc))
        return HTTPException(400, str(exc))

    # -- pages -----------------------------------------------------------------

    pages = {name: render_page(name, menu.cafe) for name in ("index.html", "staff.html")}

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def customer_page():
        return pages["index.html"]

    @app.get("/staff", include_in_schema=False, response_class=HTMLResponse)
    def staff_page():
        return pages["staff.html"]

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- health & diagnostics ---------------------------------------------------

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "app": "cafeflow-ai",
            "environment": settings.app_env,
            "cafe_profile": menu.cafe.id,
            "menu_items": len(menu.all_items()),
            "storage": orders.backend,
            "gemini": {"configured": settings.gemini_configured, "state": agent.status.state},
        }

    @app.get("/api/status")
    def status():
        persistence = "Cloud SQL (PostgreSQL)" if orders.backend == "cloudsql" else "in-memory (resets on restart)"
        return {"gemini": agent.status.as_dict(), "demo": True, "persistence": persistence}

    @app.post("/api/status/verify")
    def verify_status():
        return {"gemini": agent.verify()}

    # -- menu ------------------------------------------------------------------

    @app.get("/api/menu")
    def get_menu():
        return {"cafe": menu.cafe.model_dump(exclude={"theme"}), "categories": menu.categories,
                "items": [item_public(i) for i in menu.all_items()]}

    # -- chat ------------------------------------------------------------------

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request):
        client_key = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        if not limiter.allow(client_key):
            raise HTTPException(429, "Too many messages — please wait a minute and try again.")
        started = time.monotonic()
        extra_context, saved = None, None
        if body.feedback_form:
            form = body.feedback_form
            try:
                saved = await run_in_threadpool(lambda: feedback.record(
                    order_id=form.order_id, rating=form.rating, comment=form.comment, source="form",
                    themes=sorted({CHIP_THEMES[c] for c in form.chips if c in CHIP_THEMES}), session_id=body.session_id))
            except CafeError as exc:
                raise _business_error(exc) from exc
            extra_context = (
                f"[App context — the customer's feedback form for order {form.order_id} is already saved as "
                f"{saved['id']}: rating {form.rating}/5" + (f", chips: {', '.join(form.chips)}" if form.chips else "")
                + ". Respond as the Feedback specialist; call record_feedback with that feedback_id only if they add "
                "new details.]"
            )
            event(logger, "feedback_saved", f"Feedback {saved['id']} saved", feedback_id=saved["id"],
                  order_id=form.order_id, rating=form.rating, sentiment=saved["sentiment"], source="form")
        response = await agent.chat(body.message.strip(), body.history, body.shown_options, body.session_id,
                                    extra_context=extra_context)
        if saved:
            response.feedback = {"id": saved["id"], "saved": True}
            if response.mode == "fallback":  # no Gemini: answer the feedback, don't recommend food
                response.reply = ("Thank you — your feedback is saved and the café team will see it."
                                  + (" Sorry it wasn't great; we'll pass this on." if form.rating <= 2 else ""))
                response.recommendations, response.alternatives, response.tool_calls = [], [], []
        event(logger, "chat_turn", "Chat turn handled", mode=response.mode, agents=response.agent_route,
              tools=[f"{t.agent or '-'}:{t.name}:{'ok' if t.ok else 'error'}" for t in response.tool_calls],
              latency_ms=round((time.monotonic() - started) * 1000), model=response.model)
        return response

    # -- orders ----------------------------------------------------------------

    @app.post("/api/orders/quote")
    def quote(body: QuoteRequest):
        try:
            return orders.quote(body.item_ids)
        except CafeError as exc:
            raise _business_error(exc) from exc

    @app.post("/api/orders", response_model=Order, status_code=201)
    def create_order(body: CreateOrderRequest):
        if body.confirmed is not True:
            raise HTTPException(400, "Explicit customer confirmation is required before an order is created.")
        try:
            order = orders.create_order(body.item_ids, body.customer_preferences)
        except CafeError as exc:
            raise _business_error(exc) from exc
        event(logger, "order_created", f"Order {order.order_id} created", order_id=order.order_id,
              items=order.item_count, total_inr=order.total_inr, estimated_wait_minutes=order.estimated_wait_minutes)
        return order

    @app.get("/api/orders", dependencies=staff)
    def list_orders():
        all_orders = orders.list_orders()
        counts = orders.count_by_status()
        return {
            "orders": [o.model_dump(mode="json") for o in all_orders],
            "counts": counts,
            "active_count": sum(counts.get(s, 0) for s in ("confirmed", "preparing", "ready")),
            "queue_backlog_minutes": {k: round(v, 1) for k, v in orders.backlog().items()},
        }

    @app.get("/api/orders/{order_id}", response_model=Order)
    def get_order(order_id: str):
        try:
            return orders.get(order_id)
        except CafeError as exc:
            raise _business_error(exc) from exc

    @app.get("/api/orders/{order_id}/track")
    def track_order(order_id: str):
        """Customer-side tracking: status, delay notice and whether feedback was already given."""
        try:
            order = orders.get(order_id)
        except CafeError as exc:
            raise _business_error(exc) from exc
        return {"order": order.model_dump(mode="json"), "feedback_given": feedback.given_for_order(order_id)}

    @app.post("/api/orders/{order_id}/delay", response_model=Order, dependencies=staff)
    def delay_order(order_id: str, body: DelayRequest):
        try:
            order = orders.get(order_id)
            message = agent.delay_message(order, body.minutes)
            updated = orders.delay(order_id, body.minutes, message)
        except CafeError as exc:
            raise _business_error(exc) from exc
        event(logger, "order_delayed", f"Order {order_id} delayed {body.minutes} min", order_id=order_id,
              minutes=body.minutes, total_delay=updated.delay_minutes)
        return updated

    @app.patch("/api/orders/{order_id}/status", response_model=Order, dependencies=staff)
    def update_status(order_id: str, body: StatusUpdateRequest):
        try:
            updated = orders.update_status(order_id, body.status)
        except CafeError as exc:
            raise _business_error(exc) from exc
        event(logger, "order_status_changed", f"Order {order_id} -> {updated.status}", order_id=order_id,
              status=updated.status)
        return updated

    # -- staff: PIN check, menu editing, "Room" insights -------------------------

    @app.post("/api/staff/login", dependencies=staff)
    def staff_login():
        return {"ok": True, "pin_required": bool(settings.staff_pin)}

    @app.get("/api/staff/menu", dependencies=staff)
    def staff_menu():
        return {"categories": menu.categories, "items": menu_admin.staff_view()}

    @app.patch("/api/staff/menu/{item_id}", dependencies=staff)
    def staff_update_item(item_id: str, body: MenuItemUpdate):
        try:
            item = menu_admin.update(item_id, **body.model_dump())
        except CafeError as exc:
            raise _business_error(exc) from exc
        event(logger, "menu_updated", f"Menu item {item_id} updated", item_id=item_id, price_inr=item.price_inr,
              price_large_inr=item.price_large_inr, available=item.available)
        return item_public(item)

    @app.post("/api/staff/menu/{item_id}/reset", dependencies=staff)
    def staff_reset_item(item_id: str):
        try:
            item = menu_admin.reset(item_id)
        except CafeError as exc:
            raise _business_error(exc) from exc
        event(logger, "menu_updated", f"Menu item {item_id} reset", item_id=item_id, reset=True)
        return item_public(item)

    def room_snapshot() -> dict:
        counts = orders.count_by_status()
        return {"feedback": feedback.summary(), "demand": demand.summary(), "orders": counts}

    @app.get("/api/staff/room", dependencies=staff)
    def staff_room():
        return room_snapshot()

    @app.post("/api/staff/brief", dependencies=staff)
    def staff_brief():
        snapshot = room_snapshot()
        return {"brief": agent.shift_brief(snapshot), "snapshot": snapshot}

    # -- Connect People ------------------------------------------------------------

    def _connect(fn):
        try:
            return fn()
        except CafeError as exc:
            raise _business_error(exc) from exc

    @app.post("/api/connect/profile")
    def connect_profile(body: ConnectProfileRequest):
        profile = _connect(lambda: connect.register(body.session_id, body.nickname, body.table))
        event(logger, "connect_signup", "Connect People sign-up", table=body.table)
        return profile

    @app.post("/api/connect/leave")
    def connect_leave(body: ConnectSessionRequest):
        connect.leave(body.session_id)
        return {"left": True}

    @app.post("/api/connect/state")
    def connect_state(body: ConnectSessionRequest):
        return connect.state(body.session_id)

    @app.post("/api/connect/request")
    def connect_request(body: ConnectSessionRequest):
        result = _connect(lambda: connect.start(body.session_id))
        if not result["needs_signup"]:
            event(logger, "connect_request", "Connect request started", notified=result["people_notified"])
        return result

    @app.post("/api/connect/answer")
    def connect_answer(body: ConnectAnswerRequest):
        result = _connect(lambda: connect.answer(body.session_id, body.request_id, body.answers))
        if result["matches_created"]:
            event(logger, "connect_match", "Connect People match created", matches=result["matches_created"])
        return result

    @app.post("/api/connect/decline")
    def connect_decline(body: ConnectDeclineRequest):
        connect.decline(body.session_id, body.request_id)
        return {"declined": True}

    logger.info(
        "CaféFlow AI ready: %s profile, %d menu items, storage %s, Gemini %s (model %s)",
        menu.cafe.id,
        len(menu.all_items()),
        orders.backend,
        "configured" if settings.gemini_configured else "NOT configured — demo fallback mode",
        settings.gemini_model,
    )
    return app


app = create_app()
