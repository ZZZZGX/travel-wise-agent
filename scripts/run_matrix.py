# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""run_matrix.py —— 单条航线的价格矩阵，一条命令跑完。

## 为什么不用 `python -m travelwise`

`python -m travelwise` 走的是 **Python 的包搜索路径**。如果你以前对某个
解释器做过 `pip install`，site-packages 里会留下一份旧代码，而它的优先级
高于你当前所在的目录——于是你以为在跑新版本，实际跑的是几个版本之前的，
表现就是"参数不认识"。

这个脚本把 `src` 目录**显式插到搜索路径最前面**，无论装没装过、
无论从哪个盘运行，跑的都是这个文件夹里的代码。开头会打印实际加载位置，
让这件事一眼可查。

## 花费

每天 1 次调用。--days 7 就是 7 次（约 ¥1.4），跑之前会先报价。
同一天内重复跑同一条航线命中缓存，不重复计费。

## 用法

    python scripts/run_matrix.py 上海 昆明
    python scripts/run_matrix.py 上海 昆明 --days 14 --export xlsx
    python scripts/run_matrix.py 上海 昆明 --provider mock    # 不花钱，看流程
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))          # 必须在 import travelwise 之前

import travelwise                              # noqa: E402
from travelwise.config import Settings, build_flight_provider   # noqa: E402
from travelwise.skills.flight import FlightSkill                # noqa: E402
from travelwise.tools import matrix_export, price_analysis, price_matrix  # noqa: E402

UNIT_PRICE = 0.2


def main() -> int:
    ap = argparse.ArgumentParser(description="单条航线的每航班×每日价格矩阵")
    ap.add_argument("origin", help="出发城市，如 上海")
    ap.add_argument("destination", help="目的城市，如 昆明")
    ap.add_argument("--days", type=int, default=0,
                    help="向后扫几天，默认读 .env 的 TRAVELWISE_MATRIX_DAYS")
    ap.add_argument("--travel-date", help="出行日 YYYY-MM-DD，默认窗口结束后 10 天")
    ap.add_argument("--export", choices=["csv", "xlsx", "html"], default="xlsx")
    ap.add_argument("--provider", choices=["mock", "http"],
                    help="覆盖数据源。mock 不花钱")
    ap.add_argument("--direct-only", action="store_true",
                    help="只看直飞。中转航班便宜但耗时差很多，混在一起比价不公平")
    ap.add_argument("--airline", nargs="*", default=None,
                    help="只看这些航司，可写多个，子串匹配（如 东方 吉祥）")
    ap.add_argument("--exclude-airline", nargs="*", default=None,
                    help="排除这些航司，子串匹配（如 春秋 九元）")
    ap.add_argument("--yes", action="store_true", help="跳过花费确认")
    args = ap.parse_args()

    # 把"到底在跑哪份代码"摆在最前面 —— 这正是 python -m 会踩的坑
    print("代码加载自：%s" % Path(travelwise.__file__).parent)

    settings = Settings.from_env()
    if args.provider:
        settings.flight_provider = args.provider
    days = args.days or settings.matrix_days
    today = date.today()
    travel_date = args.travel_date or (today + timedelta(days=days + 10)).isoformat()

    paid = settings.flight_provider == "http"
    print("航线：%s→%s ｜ 扫描未来 %d 天 ｜ 出行日 %s ｜ 数据源 %s"
          % (args.origin, args.destination, days, travel_date,
             settings.flight_provider))
    if paid:
        print("预计花费：最多 %d 次 ≈ ¥%.1f（缓存命中的部分不计费）"
              % (days, days * UNIT_PRICE))
        if not args.yes:
            try:
                if input("确认？[y/N] ").strip().lower() not in ("y", "yes"):
                    print("已取消。")
                    return 0
            except EOFError:
                print("非交互环境，已取消。加 --yes 可跳过确认。")
                return 0
    else:
        print("（mock 数据源，不花钱，也不代表真实行情）")
    print()

    provider = build_flight_provider(settings, today=today)
    result = FlightSkill(provider).run(
        args.origin, args.destination, travel_date, today=today,
        matrix_days=days, direct_only=args.direct_only,
        airlines=args.airline, exclude_airlines=args.exclude_airline,
        sleep_between=settings.request_interval)

    if not result.get("ok") and result.get("matrix") is None:
        print("失败：%s" % result.get("error"))
        print(result.get("text", ""))
        return 1

    print(result["text"])

    matrix = result["matrix"]
    out_dir = ROOT / "data" / "cache" / "exports"
    path = matrix_export.export(
        matrix, args.export,
        out_dir / ("matrix-%s-%s-%s.%s"
                   % (args.origin, args.destination, today.isoformat(), args.export)))
    print()
    print("已导出：%s" % path)

    vol = price_matrix.volatility(matrix)
    if vol.get("ok"):
        print("波动指标（%s）：cv=%.2f ｜ 低价窗口 %d/%d 天"
              % (vol["basis"], vol["cv"], vol["trough_days"], vol["sample_days"]))

    advice = result.get("per_flight") or price_matrix.per_flight_advice(matrix)
    if advice.get("ok"):
        print("逐航班共识：%d/%d 班在 %s 最便宜（中位跌幅 %.0f%%）"
              % (advice["agree"], advice["total"], advice["consensus_day"],
                 advice["median_saving"]))
    rec = price_analysis.pick_recommendation(result.get("analysis") or {})
    if rec:
        print("建议购票日：%s（提前 %d 天，依据：%s）"
              % (rec["recommended_buy_date"], rec["advance_days"],
                 (result.get("analysis") or {}).get("primary_method", "")))

    if hasattr(provider, "stats"):
        print(provider.stats())
        inner = getattr(provider, "inner", None)
        if hasattr(inner, "cost_report"):
            print(inner.cost_report(UNIT_PRICE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
