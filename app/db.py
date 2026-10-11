"""Persistence: Cloud SQL for PostgreSQL in production, in-memory for local dev.

Connection settings come from the environment:
  CLOUD_SQL_CONNECTION_NAME  project:region:instance — on Cloud Run, connects through the
                             built-in Cloud SQL Unix socket (/cloudsql/<name>)
  DB_HOST / DB_PORT          alternative TCP target (e.g. a local Cloud SQL Auth Proxy)
  DB_NAME, DB_USER, DB_PASSWORD
With none of these set, everything is kept in memory (and resets on restart).

Two stores share one small connection pool (db-f1-micro allows ~25 connections, and
Cloud Run may run up to 3 instances):
  * orders  — one row per order, status transitions row-locked
  * docs    — small JSON documents for feedback, menu price overrides, Connect People
              profiles and requests, and demand signals
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote

from .schemas import Order

logger = logging.getLogger("cafeflow.db")

POOL_MAX = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

CREATE TABLE IF NOT EXISTS docs (
    kind       TEXT NOT NULL,
    id         TEXT NOT NULL,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS docs_kind_updated_idx ON docs (kind, updated_at DESC);
"""


class Database:
    """One psycopg pool shared by the order repository and the document store."""

    def __init__(self, config: DatabaseConfig):
        from psycopg_pool import ConnectionPool

        self.config = config
        self.pool = ConnectionPool(config.psycopg_conninfo(), min_size=1, max_size=POOL_MAX, open=True, timeout=15)
        with self.pool.connection() as conn:
            conn.execute(SCHEMA)
        logger.info("Connected to %s", config.describe())


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


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


class PostgresOrderRepository:
    """Orders in Cloud SQL. Payload is the validated Order model as JSONB."""

    backend = "cloudsql"

    def __init__(self, database: Database):
        self._pool = database.pool

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


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

Mutator = Callable[[dict[str, Any] | None], dict[str, Any] | None]


class DocStore(Protocol):
    backend: str

    def put(self, kind: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def get(self, kind: str, doc_id: str) -> dict[str, Any] | None: ...
    def list(self, kind: str, limit: int = 200) -> list[dict[str, Any]]: ...
    def update(self, kind: str, doc_id: str, mutate: Mutator) -> dict[str, Any] | None: ...
    def delete(self, kind: str, doc_id: str) -> bool: ...
    def list_updated_since(self, kind: str, since: datetime | None) -> list[tuple[str, dict[str, Any], datetime]]: ...


class InMemoryDocStore:
    backend = "memory"

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], tuple[dict[str, Any], datetime]] = {}
        self._lock = threading.RLock()

    def put(self, kind: str, doc_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._docs[(kind, doc_id)] = (json.loads(json.dumps(data, default=str)), _now())

    def get(self, kind: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._docs.get((kind, doc_id))
            return json.loads(json.dumps(found[0])) if found else None

    def list(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = [(d, t) for (k, _), (d, t) in self._docs.items() if k == kind]
        rows.sort(key=lambda r: r[1], reverse=True)
        return [json.loads(json.dumps(d)) for d, _ in rows[:limit]]

    def update(self, kind: str, doc_id: str, mutate: Mutator) -> dict[str, Any] | None:
        with self._lock:  # atomic read-modify-write
            current = self.get(kind, doc_id)
            updated = mutate(current)
            if updated is not None:
                self.put(kind, doc_id, updated)
            return updated

    def delete(self, kind: str, doc_id: str) -> bool:
        with self._lock:
            return self._docs.pop((kind, doc_id), None) is not None

    def list_updated_since(self, kind: str, since: datetime | None) -> list[tuple[str, dict[str, Any], datetime]]:
        with self._lock:
            return [(i, json.loads(json.dumps(d)), t) for (k, i), (d, t) in self._docs.items()
                    if k == kind and (since is None or t > since)]


class PostgresDocStore:
    backend = "cloudsql"

    def __init__(self, database: Database):
        self._pool = database.pool

    def put(self, kind: str, doc_id: str, data: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO docs (kind, id, data) VALUES (%s, %s, %s) "
                "ON CONFLICT (kind, id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (kind, doc_id, json.dumps(data, default=str)),
            )

    def get(self, kind: str, doc_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM docs WHERE kind = %s AND id = %s", (kind, doc_id)).fetchone()
        return row[0] if row else None

    def list(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT data FROM docs WHERE kind = %s ORDER BY updated_at DESC LIMIT %s",
                                (kind, limit)).fetchall()
        return [r[0] for r in rows]

    def update(self, kind: str, doc_id: str, mutate: Mutator) -> dict[str, Any] | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT data FROM docs WHERE kind = %s AND id = %s FOR UPDATE",
                               (kind, doc_id)).fetchone()
            updated = mutate(row[0] if row else None)  # may raise -> transaction rolls back
            if updated is not None:
                conn.execute(
                    "INSERT INTO docs (kind, id, data) VALUES (%s, %s, %s) "
                    "ON CONFLICT (kind, id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                    (kind, doc_id, json.dumps(updated, default=str)),
                )
            return updated

    def delete(self, kind: str, doc_id: str) -> bool:
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM docs WHERE kind = %s AND id = %s", (kind, doc_id)).rowcount == 1

    def list_updated_since(self, kind: str, since: datetime | None) -> list[tuple[str, dict[str, Any], datetime]]:
        with self._pool.connection() as conn:
            if since is None:
                rows = conn.execute("SELECT id, data, updated_at FROM docs WHERE kind = %s", (kind,)).fetchall()
            else:
                rows = conn.execute("SELECT id, data, updated_at FROM docs WHERE kind = %s AND updated_at > %s",
                                    (kind, since)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]


def build_storage(config: DatabaseConfig | None) -> tuple[OrderRepository, DocStore]:
    if config is None:
        return InMemoryOrderRepository(), InMemoryDocStore()
    database = Database(config)
    return PostgresOrderRepository(database), PostgresDocStore(database)
