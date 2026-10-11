"""Order persistence: Cloud SQL for PostgreSQL in production, in-memory for local dev.

Connection settings come from the environment:
  CLOUD_SQL_CONNECTION_NAME  project:region:instance — on Cloud Run, connects through the
                             built-in Cloud SQL Unix socket (/cloudsql/<name>)
  DB_HOST / DB_PORT          alternative TCP target (e.g. a local Cloud SQL Auth Proxy)
  DB_NAME, DB_USER, DB_PASSWORD
With none of these set, the app keeps orders in memory (they reset on restart).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from .schemas import Order

logger = logging.getLogger("cafeflow.db")


@dataclass(frozen=True)
class DatabaseConfig:
    name: str
    user: str
    password: str
    socket_dir: str | None = None
    host: str | None = None
    port: int = 5432

    @classmethod
    def from_env(cls) -> "DatabaseConfig | None":
        connection_name = os.getenv("CLOUD_SQL_CONNECTION_NAME", "").strip()
        host = os.getenv("DB_HOST", "").strip()
        if not (connection_name or host):
            return None
        return cls(
            name=os.getenv("DB_NAME", "cafeflow").strip(),
            user=os.getenv("DB_USER", "cafeflow_app").strip(),
            password=os.getenv("DB_PASSWORD", ""),
            socket_dir=f"/cloudsql/{connection_name}" if connection_name else None,
            host=host or None,
            port=int(os.getenv("DB_PORT", "5432")),
        )

    def psycopg_conninfo(self) -> str:
        host = self.socket_dir or self.host
        return f"host={host} port={self.port} dbname={self.name} user={self.user} password={self.password} connect_timeout=10"

    def sqlalchemy_url(self) -> str:
        """Async URL for ADK's DatabaseSessionService (SQLAlchemy + asyncpg)."""
        auth = f"{quote(self.user, safe='')}:{quote(self.password, safe='')}"
        if self.socket_dir:
            return f"postgresql+asyncpg://{auth}@/{self.name}?host={quote(self.socket_dir, safe='/:')}"
        return f"postgresql+asyncpg://{auth}@{self.host}:{self.port}/{self.name}"

    def describe(self) -> str:
        return f"postgres {self.socket_dir or f'{self.host}:{self.port}'}/{self.name}"


class OrderRepository(Protocol):
    backend: str

    def add(self, order: Order, station_minutes: dict[str, int]) -> bool: ...
    def get(self, order_id: str) -> Order | None: ...
    def list(self, limit: int = 500) -> list[Order]: ...
    def update(self, order_id: str, mutate: Callable[[Order], Order]) -> Order | None: ...
    def workloads(self) -> list[tuple[str, dict[str, int]]]: ...
    def count_by_status(self) -> dict[str, int]: ...
    def count(self) -> int: ...


class InMemoryOrderRepository:
    backend = "memory"

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._stations: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    def add(self, order: Order, station_minutes: dict[str, int]) -> bool:
        with self._lock:
            if order.order_id in self._orders:
                return False
            self._orders[order.order_id] = order
            self._stations[order.order_id] = station_minutes
            return True

    def get(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def list(self, limit: int = 500) -> list[Order]:
        with self._lock:
            return sorted(self._orders.values(), key=lambda o: o.created_at, reverse=True)[:limit]

    def update(self, order_id: str, mutate: Callable[[Order], Order]) -> Order | None:
        with self._lock:  # atomic read-modify-write
            order = self._orders.get(order_id)
            if order is None:
                return None
            updated = mutate(order)
            self._orders[order_id] = updated
            return updated

    def workloads(self) -> list[tuple[str, dict[str, int]]]:
        with self._lock:
            return [(o.status, self._stations[o.order_id]) for o in self._orders.values()
                    if o.status in ("confirmed", "preparing")]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(o.status for o in self._orders.values()))

    def count(self) -> int:
        with self._lock:
            return len(self._orders)


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    payload         JSONB NOT NULL,
    station_minutes JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status);
CREATE INDEX IF NOT EXISTS orders_created_idx ON orders (created_at DESC);
"""


class PostgresOrderRepository:
    """Orders in Cloud SQL. Payload is the validated Order model as JSONB."""

    backend = "cloudsql"

    def __init__(self, config: DatabaseConfig):
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(config.psycopg_conninfo(), min_size=1, max_size=5, open=True, timeout=15)
        with self._pool.connection() as conn:
            conn.execute(SCHEMA)
        logger.info("Order store connected: %s", config.describe())

    def add(self, order: Order, station_minutes: dict[str, int]) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO orders (order_id, status, payload, station_minutes, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (order_id) DO NOTHING",
                (order.order_id, order.status, order.model_dump_json(), json.dumps(station_minutes),
                 order.created_at, order.updated_at),
            )
            return cur.rowcount == 1

    def get(self, order_id: str) -> Order | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT payload FROM orders WHERE order_id = %s", (order_id,)).fetchone()
        return Order.model_validate(row[0]) if row else None

    def list(self, limit: int = 500) -> list[Order]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT payload FROM orders ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        return [Order.model_validate(r[0]) for r in rows]

    def update(self, order_id: str, mutate: Callable[[Order], Order]) -> Order | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT payload FROM orders WHERE order_id = %s FOR UPDATE", (order_id,)).fetchone()
            if row is None:
                return None
            updated = mutate(Order.model_validate(row[0]))  # may raise -> transaction rolls back
            conn.execute(
                "UPDATE orders SET status = %s, payload = %s, updated_at = %s WHERE order_id = %s",
                (updated.status, updated.model_dump_json(), updated.updated_at, order_id),
            )
            return updated

    def workloads(self) -> list[tuple[str, dict[str, int]]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT status, station_minutes FROM orders WHERE status IN ('confirmed', 'preparing')"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT status, count(*) FROM orders GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

    def count(self) -> int:
        with self._pool.connection() as conn:
            return conn.execute("SELECT count(*) FROM orders").fetchone()[0]


def build_order_repository(config: DatabaseConfig | None) -> OrderRepository:
    return PostgresOrderRepository(config) if config else InMemoryOrderRepository()
