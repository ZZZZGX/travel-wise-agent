# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""probe_flight_api.py —— 只打 1 次真实接口，验证字段映射是否正确。

## 为什么要有这一步

价格矩阵一次要扫 14~30 天 = 14~30 次额度。如果字段映射错了
（比如把「全价」当成「最低价」、或者航班号取到了空字符串），
你会先烧掉 30 次额度，再拿到一张全是空格或全是同一个价格的表。

所以先花 **1 次** 额度做这件事：
  - 打印接口原始 JSON 的第一条记录（看真实字段名）；
  - 打印解析后的 Flight（看认领对了没有）；
  - 明确指出三个最容易错的地方：航班号、票价、日期格式。

用法：
    set TRAVELWISE_FLIGHT_TOKEN=你的凭证
    python scripts/probe_flight_api.py 上海 成都
    python scripts/probe_flight_api.py SHA CTU --date 2026-09-05
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travelwise.config import Settings, build_flight_provider   # noqa: E402
from travelwise.providers.base import ProviderError             # noqa: E402
from travelwise.tools.price_matrix import row_key               # noqa: E402


def _first_record(raw):
    """从任意结构里捞出第一条看起来像航班的记录。只为打印，不参与业务。"""
    if isinstance(raw, list):
        return raw[0] if raw else None
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]
            if isinstance(v, dict):
                found = _first_record(v)
                if found is not None:
                    return found
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("origin")
    ap.add_argument("destination")
    ap.add_argument("--date", help="出发日 YYYY-MM-DD，默认明天")
    ap.add_argument("--which", help="只探测指定的数据源（config 里的 name），默认逐个都试")
    ap.add_argument("--raw-only", action="store_true",
                    help="只打印原始返回，不做字段体检")
    args = ap.parse_args()
    day = args.date or (date.today() + timedelta(days=1)).isoformat()

    settings = Settings.from_env()
    settings.flight_provider = "http"
    settings.flight_cache = False          # 探测就是要真打一次，不能读缓存
    provider = build_flight_provider(settings)

    # 容错链拆开逐个探测：两家的字段名可能完全不同，混在一起看等于没看
    targets = list(getattr(provider, "providers", [provider]))
    if args.which:
        targets = [p for p in targets if p.name == args.which]
        if not targets:
            print("配置里没有名为「%s」的数据源。" % args.which)
            return 2

    print("将逐个探测 %d 个数据源，每个消耗 1 次额度：%s"
          % (len(targets), "、".join(p.name for p in targets)))
    print("请求：%s → %s ｜ %s" % (args.origin, args.destination, day))

    failures = 0
    for p in targets:
        print()
        print("=" * 64)
        print("数据源：%s ｜ supports_price=%s" % (p.name, p.supports_price))
        print("=" * 64)
        if probe_one(p, args, day) != 0:
            failures += 1
    return 1 if failures else 0


def probe_one(provider, args, day: str) -> int:
    # -- 第一步：原始返回。字段名没看见之前，一切映射都是猜 --
    try:
        raw = provider.fetch_raw(args.origin, args.destination, day)
    except ProviderError as e:
        print("调用失败：%s" % e)
        print("（这是接口返回的真实失败，未做任何推测。）")
        return 1

    print("【原始返回结构】")
    if isinstance(raw, dict):
        print("  顶层键：%s" % list(raw)[:12])
    body = json.dumps(raw, ensure_ascii=False)
    print("  原文前 400 字：%s" % body[:400])

    first = _first_record(raw)
    if isinstance(first, dict):
        print()
        print("【第一条记录的字段名】——照着这些填 config 里的 field_map / price_field")
        for k, v in list(first.items())[:40]:
            print("  %-24s = %s" % (k, str(v)[:48]))
    print("-" * 64)

    if args.raw_only:
        return 0

    # -- 第二步：按当前配置解析。解析不出来说明 list_path / field_map 要显式填 --
    try:
        flights = provider.search_flights(args.origin, args.destination, day)
    except ProviderError as e:
        print("解析失败：%s" % e)
        print("→ 把上面看到的真实字段名填进 config/flight_api.json 的 "
              "response.list_path 与 response.field_map 后重试。")
        return 1

    if not flights:
        print("接口连上了，但当天返回 0 条航班。")
        print("→ 换一个日期或换一条热门航线再试；若始终为空，多半是参数名或城市码不对。")
        return 1

    print("解析出 %d 条航班。第一条：" % len(flights))
    print(json.dumps(flights[0].to_dict(), ensure_ascii=False, indent=2))
    print("-" * 60)

    # 三项体检——每一项错了都会让矩阵静默地失真
    problems: list[str] = []

    no_missing = sum(1 for f in flights if not (f.flight_no or "").strip())
    if no_missing:
        problems.append(
            "有 %d/%d 条**没取到航班号**。矩阵以航班号为行主键，缺失会退化成"
            "「航司@起飞时刻」归并，跨日期对不齐。→ 在 config/flight_api.json 的 "
            "response.field_map 里显式指定 flight_no。" % (no_missing, len(flights)))

    price_missing = sum(1 for f in flights if f.price is None)
    if price_missing == len(flights):
        problems.append(
            "**一条票价都没取到**。→ 检查 supports_price 是否为 true，"
            "以及 response.price_field 是否指向真实的价格字段。")
    elif price_missing:
        problems.append("有 %d/%d 条缺票价，这些格子在矩阵里会是空的。" % (price_missing, len(flights)))

    prices = [f.price for f in flights if f.price is not None]
    if len(set(prices)) == 1 and len(prices) > 2:
        problems.append(
            "所有航班票价完全相同（¥%s）。很可能取到的是**全价/公布运价**而不是"
            "实际最低可售价——那样整张矩阵会是一片常数，看不出任何曲线。"
            "→ 在 price_field 里显式指定「最低价/现价」字段。" % prices[0])

    bad_date = [f.flight_no for f in flights if f.departure_date and f.departure_date != day]
    if bad_date:
        problems.append(
            "返回的出发日期与请求日期不一致（例如 %s）。→ 检查 date_format 配置，"
            "以及接口是否忽略了日期参数直接返回默认数据（那会让矩阵每一列都一样）。"
            % bad_date[0])

    keys = {row_key(f) for f in flights}
    if len(keys) < len(flights):
        problems.append(
            "同一天里有 %d 条航班共用同一个行主键，矩阵里会被折叠成一行（取最低价）。"
            "若这是多舱位报价属于预期，可忽略。" % (len(flights) - len(keys)))

    if problems:
        print("⚠️ 发现 %d 个需要处理的问题：" % len(problems))
        for i, p in enumerate(problems, 1):
            print("  %d) %s" % (i, p))
        return 1

    print("✅ 字段映射看起来正常，可以跑矩阵了：")
    print("   python -m travelwise \"%s到%s\" --provider http --days 7 --export xlsx"
          % (args.origin, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
