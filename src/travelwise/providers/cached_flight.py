# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""cached_flight.py —— 当日缓存层。0.2 元一次的接口，这一层是最直接的省钱手段。

## 缓存键为什么必须含「查询日」

票价随时在变，所以缓存的语义只能是
**「今天问过的这条航线这个出发日，答案是这个」**，不能是
「这条航线这个出发日的价格是这个」。跨天复用就是拿昨天的价格冒充今天的，
和编造数据只差一层自我安慰。

    键 = (run_date, origin, destination, departure_date)
    run_date 变了 → 缓存自动失效，重新花钱。

## 它能省下什么

  - 同一天里重跑（调参、改 prompt、演示、被中断后重来）：0 元；
  - 7 天矩阵 + 出行日单查，出行日若落在窗口内：省 1 次；
  - 换个出行日重算提前量，窗口重叠部分全部命中。

失败**不缓存**——否则一次 429 会把这一天彻底钉死在失败上。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from ..paths import CACHE_DIR, ensure_cache_dir
from .base import Flight, FlightProvider

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_cache (
    run_date   TEXT NOT NULL,
    origin     TEXT NOT NULL,
    destination TEXT NOT NULL,
    dep_date   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    source     TEXT,
    PRIMARY KEY (run_date, origin, destination, dep_date)
);
"""


class CachedFlightProvider(FlightProvider):
    """给任意 FlightProvider 套一层当日缓存。

    缓存目录不可用时**自动退化为直连**——缓存是可选优化，
    不该因为磁盘只读就让整个查询挂掉。
    """

    def __init__(self, inner: FlightProvider, today: date | None = None,
                 db_path: str = "", enabled: bool = True):
        self.inner = inner
        self.today = today or date.today()
        self.name = "%s+cache" % inner.name
        self.supports_price = getattr(inner, "supports_price", False)
        self.hits = 0
        self.misses = 0

        self.db_path = db_path
        if enabled and not db_path:
            d = ensure_cache_dir()
            self.db_path = str((d or CACHE_DIR) / "flight_cache.db") if d else ""
        self.enabled = bool(self.db_path)
        if self.enabled:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.executescript(_SCHEMA)
            except sqlite3.Error:
                self.enabled = False

    # -- 内部 -------------------------------------------------------------
    def _key(self, origin: str, destination: str, day: str) -> tuple:
        return (self.today.isoformat(), origin, destination, day)

    def _get(self, key: tuple) -> list[Flight] | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT payload FROM flight_cache WHERE run_date=? AND origin=? "
                    "AND destination=? AND dep_date=?", key).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        try:
            return [Flight(**item) for item in json.loads(row[0])]
        except (ValueError, TypeError):
            return None          # 缓存格式变了就当没命中，不让旧数据毒化新逻辑

    def _put(self, key: tuple, flights: list[Flight]) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO flight_cache "
                    "(run_date, origin, destination, dep_date, payload, source) "
                    "VALUES (?,?,?,?,?,?)",
                    key + (json.dumps([f.to_dict() for f in flights], ensure_ascii=False),
                           self.inner.name))
        except sqlite3.Error:
            pass                 # 写不进去就算了，不影响本次结果

    # -- 接口实现 ---------------------------------------------------------
    def search_flights(self, origin: str, destination: str, day: str | date) -> list[Flight]:
        iso = day.isoformat() if isinstance(day, date) else str(day)
        if not self.enabled:
            return self.inner.search_flights(origin, destination, day)

        key = self._key(origin, destination, iso)
        cached = self._get(key)
        if cached is not None:
            self.hits += 1
            return cached

        flights = self.inner.search_flights(origin, destination, day)   # 失败照常抛，不缓存
        self.misses += 1
        self._put(key, flights)
        return flights

    def stats(self) -> str:
        if not self.enabled:
            return "缓存未启用（目录不可写）"
        total = self.hits + self.misses
        return ("缓存命中 %d / %d（省下约 ¥%.1f），实际调用 %d 次"
                % (self.hits, total, self.hits * 0.2, self.misses))
