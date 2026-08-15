# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""compare_routes.py —— 用数据挑一条"值得展示"的航线，而不是靠猜。

## 要解决的问题

杭州→武汉的价格曲线几乎是平的，提前量分析没有发挥空间。但"哪条航线波动大"
不该拍脑袋——同样是猜，猜错还要花钱重来。这个脚本一次扫几条候选航线，
按波动指标排名，把判断建立在数据上。

## 花费

每条航线 × 每天 = 1 次调用。默认 3 条航线 × 7 天 = 21 次 ≈ ¥4.2。
**跑之前会先报价并要求确认。** 想省钱就把 --days 调小（5 天也够看出差别），
或者一次只比 2 条。

同一天内重复跑同一条航线会命中缓存，不重复计费。

## 用法

    python scripts/compare_routes.py                       # 用内置候选清单
    python scripts/compare_routes.py --days 5
    python scripts/compare_routes.py --routes 上海:三亚 北京:乌鲁木齐 杭州:武汉
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travelwise.config import Settings, build_flight_provider     # noqa: E402
from travelwise.tools import price_matrix                         # noqa: E402

UNIT_PRICE = 0.2

#: 候选清单按"为什么可能波动大"分类，理由写在旁边——
#: 挑航线的依据应该是可复核的假设，不是感觉。
CANDIDATES = [
    ("上海", "三亚", "旅游目的地，季节性极强；无高铁替代"),
    ("北京", "乌鲁木齐", "超长距离，高铁不构成替代，票价基数高"),
    ("杭州", "武汉", "高铁 2.5 小时直杀，对照组：预期很平"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="每条航线扫几天")
    ap.add_argument("--routes", nargs="*", help="形如 出发地:目的地，可给多条")
    ap.add_argument("--yes", action="store_true", help="跳过花费确认")
    args = ap.parse_args()

    settings = Settings.from_env()
    settings.flight_provider = "http"
    days = args.days or settings.matrix_days

    if args.routes:
        routes = []
        for item in args.routes:
            if ":" not in item and "：" not in item:
                print("航线格式应为 出发地:目的地，收到：%s" % item)
                return 2
            origin, dest = item.replace("：", ":").split(":", 1)
            routes.append((origin.strip(), dest.strip(), ""))
    else:
        routes = CANDIDATES

    calls = len(routes) * days
    print("将扫描 %d 条航线 × %d 天 = 最多 %d 次调用 ≈ ¥%.1f"
          % (len(routes), days, calls, calls * UNIT_PRICE))
    print("（同一天内已查过的航线会命中缓存，不重复计费）")
    for origin, dest, why in routes:
        print("  · %s→%s%s" % (origin, dest, ("  —— " + why) if why else ""))

    if not args.yes:
        try:
            if input("确认？[y/N] ").strip().lower() not in ("y", "yes"):
                print("已取消。")
                return 0
        except EOFError:
            print("非交互环境，已取消。加 --yes 可跳过确认。")
            return 0

    today = date.today()
    provider = build_flight_provider(settings, today=today)
    results = []

    for origin, dest, why in routes:
        print()
        print("扫描 %s→%s …" % (origin, dest))
        try:
            m = price_matrix.build_matrix(
                origin, dest, today, provider.search_flights, days=days,
                sleep_between=settings.request_interval)
        except Exception as e:                        # noqa: BLE001
            print("  失败：%s: %s" % (type(e).__name__, str(e)[:120]))
            results.append((origin, dest, why, None))
            continue

        vol = price_matrix.volatility(m)
        if not vol.get("ok"):
            print("  %s" % vol.get("reason"))
            results.append((origin, dest, why, None))
            continue

        print("  实际航班 %d 班（%d 个航班号）｜当日最低价 ¥%s ~ ¥%s"
              % (len(m.rows), m.raw_flight_count,
                 price_matrix._fmt_price(vol["min"]),
                 price_matrix._fmt_price(vol["max"])))
        print("  变异系数 %.2f ｜ 极差 %.0f%% ｜ 低价窗口 %d/%d 天"
              % (vol["cv"], vol["range_ratio"] * 100,
                 vol["trough_days"], vol["sample_days"]))
        results.append((origin, dest, why, vol))

    print()
    print("=" * 72)
    print("按波动排名（变异系数越大 = 越值得挑日子）")
    print("=" * 72)
    ranked = sorted([r for r in results if r[3]],
                    key=lambda r: -r[3]["cv"])
    print("%-16s %8s %8s %10s   %s" % ("航线", "变异系数", "极差", "低价窗口", "判断"))
    for origin, dest, _why, vol in ranked:
        print("%-16s %8.2f %7.0f%% %7d/%-3d  %s"
              % ("%s→%s" % (origin, dest), vol["cv"], vol["range_ratio"] * 100,
                 vol["trough_days"], vol["sample_days"],
                 vol["verdict"].split("：")[0]))

    failed = [r for r in results if not r[3]]
    for origin, dest, _why, _ in failed:
        print("%-16s %s" % ("%s→%s" % (origin, dest), "无有效数据"))

    if hasattr(provider, "stats"):
        print()
        print(provider.stats())
        inner = getattr(provider, "inner", None)
        if hasattr(inner, "cost_report"):
            print(inner.cost_report(UNIT_PRICE))

    if ranked:
        best = ranked[0]
        print()
        print("建议用 %s→%s 做演示。" % (best[0], best[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
