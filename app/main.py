"""CaféFlow AI — FastAPI application (single container for Cloud Run)."""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import CafeAgent
from .config import STATIC_DIR, Settings, load_settings
from .menu_service import CafeError, MenuService, item_public
from .order_service import InvalidTransitionError, OrderLimitError, OrderNotFoundError, OrderStore
from .schemas import CafeProfile, ChatRequest, ChatResponse, CreateOrderRequest, Order, QuoteRequest, StatusUpdateRequest
from .tools import ToolExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cafeflow")


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


def create_app(settings: Settings | None = None, llm=None) -> FastAPI:
    settings = settings or load_settings()
    menu = MenuService.from_file(settings.menu_path)
    orders = OrderStore(menu)
    executor = ToolExecutor(menu, orders)
    agent = CafeAgent(settings, executor, llm=llm)
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
            "orders_in_memory": len(orders.list_orders()),
            "gemini": {"configured": settings.gemini_configured, "state": agent.status.state},
        }

    @app.get("/api/status")
    def status():
        return {"gemini": agent.status.as_dict(), "demo": True, "persistence": "in-memory (resets on restart)"}

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
        return await agent.chat(body.message.strip(), body.history, body.shown_options, body.session_id)

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
        logger.info("Order %s created: %d item(s), ₹%d", order.order_id, order.item_count, order.total_inr)
        return order

    @app.get("/api/orders")
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

    @app.patch("/api/orders/{order_id}/status", response_model=Order)
    def update_status(order_id: str, body: StatusUpdateRequest):
        try:
            return orders.update_status(order_id, body.status)
        except CafeError as exc:
            raise _business_error(exc) from exc

    logger.info(
        "CaféFlow AI ready: %s profile, %d menu items, Gemini %s (model %s)",
        menu.cafe.id,
        len(menu.all_items()),
        "configured" if settings.gemini_configured else "NOT configured — demo fallback mode",
        settings.gemini_model,
    )
    return app


app = create_app()
