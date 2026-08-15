# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""
data_accumulator.py —— 长期价格积累（辅轨 / long-scale）

定位（按你最终确认的理论）：
  这不是"给单次推荐用"的——你的单快照启发式(price_analyzer)自己就够用。
  这条轨道是【并行的、以年为单位的低频后台任务】：每天 1-2 次，
  为每条航线记录"某发起日、查询某出发日、该出发日最低价、提前天数"，
  攒够 1-2 年后，用真实历史反推"这条航线典型的最佳提前量到底是几天"，
  再回过头校验甚至替换单快照启发式。

  理论前提（你的背书）：城市规模/发展速度决定航线日均客流大体稳定，
  航司据此固定排班；航线没扩、航班没加，则"提前几天买最便宜"的趋势相对稳定。
  → 常规日（非春运/黄金周）覆盖 300~365 天即算可用 scale。

每条记录四要素（严格对应你的描述）：
  run_date            = 发起查询的那天（今天）
  queried_dep_date    = 被查询的出发日期
  advance_days        = (queried_dep_date - run_date)  提前天数
  price               = 该出发日当天起飞机票的价格（存每航班 + 当日最低）

特性：
  - 纯后台脚本，不经过 LLM，不消耗 Token。
  - 用标准库 sqlite3，落到 data/cache/price_history.db。
  - 幂等：同 (run_date, 航线, 出发日, 航班号) 只留一条（REPLACE）。
"""

import os
import sqlite3

from ..paths import DATA_DIR
import argparse
from datetime import date, timedelta

_DB_PATH = os.environ.get(
    "TRAVELWISE_PRICE_DB",
    str(DATA_DIR / "cache" / "price_history.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    run_date          TEXT NOT NULL,   -- 发起查询日（今天）YYYY-MM-DD
    departure         TEXT NOT NULL,   -- 出发地码
    arrival           TEXT NOT NULL,   -- 目的地码
    queried_dep_date  TEXT NOT NULL,   -- 被查询的出发日期
    advance_days      INTEGER NOT NULL,-- 提前天数
    flight_no         TEXT NOT NULL,   -- 航班号（当日最低价行用 __MIN__）
    airline           TEXT,
    transfer_num      INTEGER,
    ticket_price      REAL NOT NULL,
    is_day_min        INTEGER NOT NULL DEFAULT 0,  -- 1 = 该出发日的最低价汇总行
    PRIMARY KEY (run_date, departure, arrival, queried_dep_date, flight_no)
);
CREATE INDEX IF NOT EXISTS idx_route_dep
    ON price_snapshots (departure, arrival, queried_dep_date);
"""


def _connect(db_path=_DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def record_route(departure, arrival, fetcher, run_date=None,
                 horizon_days=90, direct_only=False, db_path=_DB_PATH):
    """
    为一条航线扫描未来 horizon_days 天，把每航班价格 + 当日最低写入库。
    fetcher(dep, arr, "YYYY-MM-DD") -> list[flight dict]
    返回 (写入航班行数, 覆盖天数)
    """
    run_date = run_date or date.today()
    conn = _connect(db_path)
    rows = 0
    days = 0
    try:
        # 整条航线一个事务：普通航班行与 __MIN__ 汇总行要么一起落库、要么整体回滚，
        # 避免"写了部分航班却没写 __MIN__"这类中途失败导致的脏/不一致状态。
        with conn:  # 上下文成功即 COMMIT，抛异常即 ROLLBACK
            for adv in range(1, horizon_days + 1):
                d = run_date + timedelta(days=adv)
                try:
                    flights = fetcher(departure, arrival, d.isoformat())
                except Exception:
                    # 单日取数失败不阻断整条航线的积累（已写入的其他天不受影响）
                    continue
                prices = []
                for f in flights:
                    p = f.get("ticket_price")
                    if not isinstance(p, (int, float)):
                        continue
                    if direct_only and f.get("transfer_num", 1) != 1:
                        continue
                    prices.append(p)
                    conn.execute(
                        "REPLACE INTO price_snapshots VALUES (?,?,?,?,?,?,?,?,?,0)",
                        (run_date.isoformat(), departure, arrival, d.isoformat(), adv,
                         f.get("flight_no") or "?", f.get("airline"),
                         f.get("transfer_num", 1), p))
                    rows += 1
                if prices:
                    conn.execute(
                        "REPLACE INTO price_snapshots VALUES (?,?,?,?,?,?,?,?,?,1)",
                        (run_date.isoformat(), departure, arrival, d.isoformat(), adv,
                         "__MIN__", None, None, min(prices)))
                    days += 1
    finally:
        conn.close()
    return rows, days


def typical_best_advance(departure, arrival, db_path=_DB_PATH):
    """
    从历史积累里估计这条航线的"典型最佳提前量"。
    做法：对每一个 run_date（每次快照），取当日最低价所在的 advance_days，
    汇总成分布，返回中位数 + 样本数。样本太少时如实说明。
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """SELECT run_date, advance_days, ticket_price
               FROM price_snapshots
               WHERE departure=? AND arrival=? AND is_day_min=1
               ORDER BY run_date, ticket_price""",
            (departure, arrival))
        best_by_run = {}
        for run_date, adv, price in cur.fetchall():
            if run_date not in best_by_run or price < best_by_run[run_date][1]:
                best_by_run[run_date] = (adv, price)
    finally:
        conn.close()

    advances = sorted(a for a, _ in best_by_run.values())
    n = len(advances)
    if n == 0:
        return {"ok": False, "reason": "该航线暂无历史积累数据", "samples": 0}
    median = advances[n // 2] if n % 2 else (advances[n // 2 - 1] + advances[n // 2]) / 2
    confidence = "低（样本 < 30，仅供参考）" if n < 30 else "中" if n < 120 else "较高"
    return {"ok": True, "route": "%s→%s" % (departure, arrival),
            "median_best_advance": median, "samples": n, "confidence": confidence}


def _load_routes(path):
    routes = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) >= 2:
                routes.append((parts[0], parts[1]))
    return routes


if __name__ == "__main__":
    # CLI：后台低频跑。示例：
    #   python data_accumulator.py --routes routes.txt --horizon 90
    #   python data_accumulator.py --stats SHA PEK
    from price_fetcher import query_flights  # 真实运行时用真 fetcher

    ap = argparse.ArgumentParser(description="机票价格长期积累（辅轨）")
    ap.add_argument("--routes", help="航线清单文件，每行：出发码 到达码")
    ap.add_argument("--horizon", type=int, default=90, help="每条航线向后扫描天数")
    ap.add_argument("--direct-only", action="store_true", help="只记直飞")
    ap.add_argument("--stats", nargs=2, metavar=("DEP", "ARR"),
                    help="查看某航线的典型最佳提前量")
    args = ap.parse_args()

    if args.stats:
        print(typical_best_advance(args.stats[0], args.stats[1]))
    elif args.routes:
        for dep, arr in _load_routes(args.routes):
            r, d = record_route(dep, arr, query_flights,
                                horizon_days=args.horizon,
                                direct_only=args.direct_only)
            print("%s→%s 已记录 %d 航班行 / 覆盖 %d 天" % (dep, arr, r, d))
    else:
        ap.print_help()
