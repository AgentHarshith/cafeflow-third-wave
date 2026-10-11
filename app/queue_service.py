"""Deterministic preparation-time estimator.

Heuristic (approximate, not an ML model):

* The café has two stations that work in parallel: the **bar** (drinks) and the
  **kitchen** (food); each menu item names its station explicitly. Each station is staffed by one
  person and prepares its items one after another.
* Station backlog = prep minutes still queued at that station from active orders:
  ``confirmed`` orders count in full, ``preparing`` orders count at 50% (assumed
  half done). ``ready``/``completed``/``cancelled`` orders add nothing, so finished
  work is never double-counted.
* For a new order, each station it uses finishes at
  ``backlog(station) + new prep minutes at that station``; the order is ready when
  its slowest station finishes:

      estimated_wait = max over used stations (backlog + new_station_minutes)

  rounded up to whole minutes. Same inputs always give the same answer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from .schemas import MenuItem

STATIONS = ("bar", "kitchen")
ACTIVE_WORKLOAD_FACTOR: dict[str, float] = {"confirmed": 1.0, "preparing": 0.5}

ASSUMPTIONS = [
    "Approximate estimate from a fixed rule, not a trained model or live kitchen data.",
    "Bar (drinks) and kitchen (food) work in parallel; items at the same station are made one at a time.",
    "Queued work from active orders is added first: confirmed orders in full, orders being prepared at 50%.",
]


def station_minutes(items: Iterable[MenuItem]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in items:
        totals[item.station] = totals.get(item.station, 0) + item.preparation_time_minutes
    return totals


def queue_backlog(active_orders: Iterable[tuple[str, Mapping[str, int]]]) -> dict[str, float]:
    """Sum remaining station workload from (status, station_minutes) pairs."""
    backlog = {station: 0.0 for station in STATIONS}
    for status, minutes in active_orders:
        factor = ACTIVE_WORKLOAD_FACTOR.get(status, 0.0)
        for station, value in minutes.items():
            backlog[station] += value * factor
    return backlog


def estimate_from_backlog(items: list[MenuItem], backlog: Mapping[str, float]) -> dict[str, Any]:
    new_minutes = station_minutes(items)
    per_station = {
        station: math.ceil(backlog.get(station, 0.0) + minutes) for station, minutes in new_minutes.items()
    }
    wait = max(per_station.values(), default=0)
    return {
        "estimated_wait_minutes": wait,
        "own_prep_minutes": max(new_minutes.values(), default=0),
        "queue_delay_minutes": wait - max(new_minutes.values(), default=0),
        "per_station_minutes": per_station,
        "station_backlog_minutes": {k: round(v, 1) for k, v in backlog.items()},
        "approximate": True,
    }
