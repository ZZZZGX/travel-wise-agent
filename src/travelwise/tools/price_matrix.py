# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""price_matrix.py —— 「每航班 × 每出发日」的价格矩阵（纯计算，无 IO、无平台依赖）。

## 为什么需要它

原来的输出只给「当日最低价」和「提前 N 天买最便宜」。这个结论隐含了一个
错误前提：**整条航线共享同一条价格曲线。**

实际上不是。每家航司的航线结构、放舱节奏、竞争策略都不同：
  - 春秋这类低成本航司常常越早越便宜，接近出发日陡涨；
  - 两舱占比高的大航司，某些日子反而在中段放低价舱；
  - 同一天不同班次（早班 / 红眼）价差可以到 40%。

把它们压成一个 min()，用户看到的是「某天有人卖 780」，但拿不到
**「哪个航班在哪天 780」**，也看不出这个 780 是常态还是孤点。

所以这里把同一批数据换一种组织方式：

    行 = 航班（航班号）    列 = 出发日    单元格 = 该航班当日最低价

一行就是一条独立的价格曲线，横着扫能看趋势，竖着扫能比同日各航班。

## 三个必须诚实处理的坑

1. **空格 ≠ 没航班。** 单元格为空有两种截然不同的原因：那天该航班不飞
   （周班），或者那天的查询失败了。前者是事实，后者是数据缺口。
   混为一谈就等于用「没数据」冒充「不存在」。→ 每一列带 status。
2. **航班号是行的主键，而它可能不稳。** 数据源若在不同日期给同一物理航班
   不同编号（或字段缺失），矩阵会退化成对角线：每行只有一个格子。
   → 计算覆盖率，低于阈值时明确警告，不假装矩阵有效。
3. **矩阵会很宽。** 30 天 × 20 班 = 600 个格子。全塞给模型就是第二次
   「38 条链接撞 max_tokens」。→ 表格用记号 [T1] 传给模型，
   由代码在展示前换回来（见 table_refs.py）。

本模块【不联网】：接收一个 fetcher 回调，可完全脱网单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

#: 行覆盖率低于此值时判定「航班号不足以作为行主键」，输出警告而非假装矩阵可用。
LOW_COVERAGE_RATIO = 0.34


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class DayColumn:
    """矩阵的一列 = 一个出发日。"""

    day: date
    advance: int
    status: str = "ok"                 # ok | no_flight | failed
    error: str = ""
    flight_count: int = 0
    min_price: float | None = None

    @property
    def label(self) -> str:
        return "%02d-%02d %s" % (self.day.month, self.day.day, WEEKDAY_CN[self.day.weekday()])

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.day.isoformat(), "advance": self.advance,
                "status": self.status, "error": self.error,
                "flight_count": self.flight_count, "min_price": self.min_price}


@dataclass
class FlightRow:
    """矩阵的一行 = 一个航班在整个窗口内的价格曲线。"""

    key: str
    flight_no: str = ""
    airline: str = ""
    departure_time: str = ""
    arrival_time: str = ""
    transfer_num: int = 1
    prices: dict[str, float] = field(default_factory=dict)   # ISO 日期 -> 价格

    #: 同一架飞机的其它销售航班号（共享代码）。
    #: [(航班号, 航司, 该号窗口内最低价), ...]，按最低价升序。
    codeshares: list = field(default_factory=list)
    #: 每一天最便宜的那个航班号 —— 合并后仍然要能回答"那这天该买哪个号"
    cheapest_no: dict = field(default_factory=dict)

    @property
    def is_merged(self) -> bool:
        return bool(self.codeshares)

    # -- 派生指标（都基于已有格子，缺格不参与计算，也不插值） --
    @property
    def observed(self) -> int:
        return len(self.prices)

    @property
    def min_price(self) -> float | None:
        return min(self.prices.values()) if self.prices else None

    @property
    def max_price(self) -> float | None:
        return max(self.prices.values()) if self.prices else None

    @property
    def best_date(self) -> str:
        if not self.prices:
            return ""
        return min(self.prices.items(), key=lambda kv: kv[1])[0]

    @property
    def swing(self) -> float | None:
        """波动幅度 = 最高 − 最低。反映这个航班「值不值得挑日子」。"""
        if len(self.prices) < 2:
            return None
        return round(self.max_price - self.min_price, 2)

    def coverage(self, valid_days: int) -> float:
        return (self.observed / valid_days) if valid_days else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"flight_no": self.flight_no, "airline": self.airline,
                "departure_time": self.departure_time, "arrival_time": self.arrival_time,
                "transfer_num": self.transfer_num, "prices": dict(self.prices),
                "observed": self.observed, "min_price": self.min_price,
                "max_price": self.max_price, "best_date": self.best_date,
                "swing": self.swing}


@dataclass
class PriceMatrix:
    origin: str = ""
    destination: str = ""
    today: date | None = None
    columns: list[DayColumn] = field(default_factory=list)
    rows: list[FlightRow] = field(default_factory=list)
    api_calls: int = 0
    #: 合并共享代码**之前**的航班号条数。与 len(rows) 的差就是被合并掉的销售号。
    raw_flight_count: int = 0
    failed_days: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def route(self) -> str:
        return "%s→%s" % (self.origin, self.destination)

    @property
    def valid_columns(self) -> list[DayColumn]:
        """真正拿到数据的列。失败列不算——它们是缺口，不是观测。"""
        return [c for c in self.columns if c.status == "ok"]

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    def cell(self, row: FlightRow, col: DayColumn) -> float | None:
        return row.prices.get(col.day.isoformat())


# --------------------------------------------------------------------------
# 取数与聚合
# --------------------------------------------------------------------------

def _price_of(f: Any) -> float | None:
    price = getattr(f, "price", None)
    if price is None and isinstance(f, dict):
        price = f.get("price", f.get("ticket_price"))
    return float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None


def _attr(f: Any, name: str, default: Any = "") -> Any:
    v = getattr(f, name, None)
    if v is None and isinstance(f, dict):
        v = f.get(name, default)
    return default if v is None else v


def row_key(f: Any) -> str:
    """行主键：航班号优先。

    航班号是同一物理航班跨日期的唯一稳定标识。缺失时退化为
    「航司+起飞时刻」——这是**降级**不是等价，会在覆盖率里体现出来。
    """
    no = str(_attr(f, "flight_no", "")).replace(" ", "").upper()
    if no:
        return no
    return "%s@%s" % (_attr(f, "airline", "?"), _attr(f, "departure_time", "??:??"))


def _airline_allowed(f, airlines, exclude_airlines) -> bool:
    """航司白名单 / 黑名单。子串匹配，"春秋" 能命中 "春秋航空"。

    有人就是不坐某几家（座位窄、不含餐、改签贵），把它们的价格留在表里
    只会干扰结论——最便宜那格点不了，等于没有。
    """
    name = "%s %s" % (_attr(f, "airline", ""), _attr(f, "flight_no", ""))
    if exclude_airlines and any(x and x in name for x in exclude_airlines):
        return False
    if airlines:
        return any(x and x in name for x in airlines)
    return True


def build_matrix(origin: str, destination: str, today, fetcher: Callable,
                 days: int = 14, direct_only: bool = False,
                 skip_today: bool = True, sleep_between: float = 0.0,
                 merge_codeshare: bool = True,
                 airlines: list | None = None,
                 exclude_airlines: list | None = None) -> PriceMatrix:
    """逐日取数并组装矩阵。

    fetcher(origin, destination, "YYYY-MM-DD") -> list[Flight|dict]
      - 抛异常  → 该列标记 failed（数据缺口，如实记录，不当成「无航班」）
      - 空列表  → 该列标记 no_flight（那天确实没航班，这不是错误）

    每天 1 次调用，days 天就是 days 次额度。这里不做提前停止：
    矩阵要求列是齐的，中途停会让不同航班的曲线长度不一致而无法横向比较。

    sleep_between：串行请求之间的间隔秒数。付费接口通常有 QPS 限制，
    连打 30 次很容易触发 429——那会变成一整片 × 列，比慢几秒糟得多。
    """
    today = today if isinstance(today, date) else date.fromisoformat(str(today))
    m = PriceMatrix(origin=origin, destination=destination, today=today)
    rows: dict[str, FlightRow] = {}
    start = 1 if skip_today else 0

    for i, advance in enumerate(range(start, start + max(1, days))):
        d = today + timedelta(days=advance)
        col = DayColumn(day=d, advance=advance)
        if sleep_between > 0 and i > 0:
            time.sleep(sleep_between)
        try:
            flights = fetcher(origin, destination, d.isoformat())
            m.api_calls += 1
        except Exception as e:                       # noqa: BLE001
            col.status = "failed"
            col.error = "%s: %s" % (type(e).__name__, e)
            m.api_calls += 1
            m.failed_days.append(d.isoformat())
            m.columns.append(col)
            continue

        day_prices: list[float] = []
        for f in flights or []:
            if direct_only and int(_attr(f, "transfer_num", 1) or 1) != 1:
                continue
            if not _airline_allowed(f, airlines, exclude_airlines):
                continue
            price = _price_of(f)
            if price is None or price <= 0:
                continue
            key = row_key(f)
            row = rows.get(key)
            if row is None:
                row = FlightRow(
                    key=key,
                    flight_no=str(_attr(f, "flight_no", "")),
                    airline=str(_attr(f, "airline", "")),
                    departure_time=str(_attr(f, "departure_time", "")),
                    arrival_time=str(_attr(f, "arrival_time", "")),
                    transfer_num=int(_attr(f, "transfer_num", 1) or 1),
                )
                rows[key] = row
            iso = d.isoformat()
            # 同一航班同一天出现多条（多舱位）→ 取最低，与「当日最低价」口径一致
            if iso not in row.prices or price < row.prices[iso]:
                row.prices[iso] = price
            day_prices.append(price)

        col.flight_count = len(day_prices)
        if day_prices:
            col.min_price = min(day_prices)
        else:
            col.status = "no_flight" if col.status == "ok" else col.status
        m.columns.append(col)

    m.rows = sorted(rows.values(),
                    key=lambda r: (r.min_price if r.min_price is not None else 1e9,
                                   r.departure_time))
    m.raw_flight_count = len(m.rows)
    if merge_codeshare:
        merge_codeshares(m)
    m.warnings = _diagnose(m)
    return m


def _trough_days(prices: list, tolerance: float = 0.05) -> int:
    lo = min(prices)
    return sum(1 for p in prices if p <= lo * (1 + tolerance))


def _median(prices: list) -> float:
    xs = sorted(prices)
    n = len(xs)
    if not n:
        return 0.0
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def panel_view(m: PriceMatrix, direct_only: bool = True,
               min_rows: int = 3) -> dict:
    """构造「可比面板」：只保留在每个有效日都有价的航班，逐日给出中位价。

    ## 为什么要中位价，而不是最低价

    实测踩到的第二个坑。上海→昆明的面板最低价曲线几乎是平的（cv=0.07），
    但同一批航班的**中位价**曲线是一条干净的 U 型（cv=0.13，底在 08-20）：

        日期      面板最低   面板中位
        08-15      730       1074
        08-20      580        788   ← 底
        08-21      580        930

    原因是 `min()` 永远被最便宜的那一班定住。春秋、9C 这类低成本航司常年
    贴着地板价、几乎不动，于是整条最低价曲线跟着它不动，剩下 30 多班的
    涨跌全被吃掉。**「最低价」回答的是"运气最好能买到多少钱"，
    「中位价」回答的是"一张正常能订到的票现在值多少钱"**——
    后者才是"哪天买"这个决策要看的量。

    最低价曲线仍然算、仍然报，作为对照。
    """
    valid_days = [c.day.isoformat() for c in m.valid_columns]
    rows = [r for r in m.rows
            if all(day in r.prices for day in valid_days)
            and not (direct_only and (r.transfer_num or 1) > 1)]

    envelope = [c.min_price for c in m.valid_columns if c.min_price is not None]
    if len(rows) < min_rows or len(valid_days) < 1:
        return {"ok": False, "days": valid_days, "rows": rows,
                "panel_size": len(rows), "median": [], "panel_min": [],
                "envelope": envelope,
                "reason": "全窗口都在飞%s的航班不足 %d 班，没有可比面板"
                          % ("的直飞" if direct_only else "", min_rows)}

    median = [_median([r.prices[day] for r in rows]) for day in valid_days]
    panel_min = [min(r.prices[day] for r in rows) for day in valid_days]
    return {"ok": True, "days": valid_days, "rows": rows,
            "panel_size": len(rows), "median": median,
            "panel_min": panel_min, "envelope": envelope,
            "direct_only": direct_only}


def _cv(prices: list) -> float:
    mean = sum(prices) / len(prices)
    if not mean:
        return 0.0
    var = sum((p - mean) ** 2 for p in prices) / len(prices)
    return (var ** 0.5) / mean


def volatility(m: PriceMatrix) -> dict:
    """量化这条航线的价格曲线"值不值得挑日子"。

    ## 为什么不能直接用「当日最低价」这条曲线

    这是实测数据打脸的一处。上海→昆明 86 班里有 53 班**不是每天都飞**，
    于是「当日最低价」是在一个**每天都在变的航班集合**上取 min：

        08-17 的 ¥580 来自 CZ8804+CZ3997（中转，只在 3 天出现）
        08-19 的 ¥580 来自 CZ8886+CZ3901（只在这 1 天出现）

    这不是同一件商品在不同日期的价格，是不同商品的价格摆在一起比。
    按它算出来"低价窗口 5/7 天"，而用户扫一眼表格数出来的是 3 天左右——
    **用户是对的**，因为他看的是那些天天都在飞、能横向比较的行。

    所以主口径改成**可比面板**：只用在所有有效日都有价的航班。
    全量口径仍然算、仍然报，但只作为参考，并在两者背离时说明原因。

    三个指标：
      - `cv`（变异系数）：波动幅度，与价格基数无关，可跨航线比；
      - `range_ratio`（极差 / 最低价）；
      - `trough_days`：处于最低价 ±5% 的天数。**决策价值取决于低价是否稀缺**——
        低价窗口 5 天说明随便挑一天都差不多，1 天才谈得上挑日子。
    """
    valid_days = [c.day.isoformat() for c in m.valid_columns]
    if len(valid_days) < 3:
        return {"ok": False, "reason": "有效样本不足 3 天，不做波动判断"}

    all_curve = [c.min_price for c in m.valid_columns if c.min_price is not None]

    # 可比面板的**中位价**曲线。用 min 会被最便宜那一班定住（见 panel_view）。
    panel = panel_view(m)
    panel_curve = panel["median"] if panel.get("ok") else []
    panel_rows = panel["rows"]

    primary = panel_curve or all_curve
    basis = ("可比面板中位价（全窗口都在飞的 %d 班直飞）" % panel["panel_size"]) if panel_curve \
        else "全部航班的当日最低价（可比面板不足 3 班）"
    if len(primary) < 3:
        return {"ok": False, "reason": "有效样本不足 3 天，不做波动判断"}

    lo, hi = min(primary), max(primary)
    mean = sum(primary) / len(primary)
    cv = _cv(primary)
    trough_days = _trough_days(primary)
    wide_trough = trough_days >= max(3, len(primary) * 0.5)

    if wide_trough:
        verdict = ("低价窗口太宽（%d/%d 天都在最低价附近）：早买晚买差别不大，"
                   "挑日子省不下什么。" % (trough_days, len(primary)))
    elif cv >= 0.25:
        verdict = "波动大且低价日稀缺：挑日子能省下明显的钱，这条航线最能体现提前量分析的价值"
    elif cv >= 0.12:
        verdict = "波动中等、低价日不算多：挑日子有一定收益"
    else:
        verdict = "价格几乎不动：这条航线没有可优化的空间"

    result = {"ok": True, "basis": basis, "cv": round(cv, 3), "min": lo, "max": hi,
              "mean": round(mean, 1),
              "range_ratio": round((hi - lo) / lo, 3) if lo else 0.0,
              "trough_days": trough_days, "sample_days": len(primary),
              "panel_size": len(panel_rows), "total_flights": len(m.rows),
              "verdict": verdict}

    if panel_curve and all_curve:
        result["all_min"] = min(all_curve)
        result["all_trough_days"] = _trough_days(all_curve)
        result["all_cv"] = round(_cv(all_curve), 3)
        if result["all_trough_days"] != trough_days:
            result["divergence"] = (
                "全量最低价口径算出的低价窗口是 %d 天，可比面板中位价是 %d 天。差异来自那些"
                "**只在部分日期出现**的航班（含中转），它们在某天压低了当日最低价，"
                "但那不是同一班飞机在不同日期的价格，跨日期比较不成立。"
                % (result["all_trough_days"], trough_days))
    return result


def per_flight_advice(m: PriceMatrix, direct_only: bool = True) -> dict:
    """每班飞机各算各的最低点，再看它们的共识。

    ## 为什么必须按航班算

    实测打脸的一处：上海→昆明的「当日最低价」曲线看着很平（低价窗口 4~5 天），
    但把 32 班全窗口直飞逐个拆开看，最低点的分布是：

        08-17: 8 班 ｜ 08-18: 14 班 ｜ 08-19: 17 班 ｜ **08-20: 23 班**

    23/32 班在同一天见底 —— 信号非常强，只是被聚合的 min() 抹掉了。
    聚合曲线之所以失真，是因为它每天在不同的航班集合上取 min，
    某天冒出来的一班中转特价就能把整条线拉平。

    **每班飞机是各自独立的商品，有各自的收益管理策略，就该各自算。**
    顺带还解决了另一个问题：用户不坐某家航司时，只要看自己那几行就行，
    不必被别人的价格干扰结论。

    ## 并列最低怎么处理

    一班飞机可能有好几天同价并列最低，这些天全部计入分布——
    "这天买不吃亏"本来就是可以有多个答案的。所以各日计数之和会大于航班数。
    """
    valid_days = [c.day.isoformat() for c in m.valid_columns]
    if len(valid_days) < 3:
        return {"ok": False, "reason": "有效样本不足 3 天"}

    rows = []
    used: list[FlightRow] = []
    for r in m.rows:
        if direct_only and r.transfer_num and r.transfer_num > 1:
            continue
        if not all(day in r.prices for day in valid_days):
            continue          # 不是每天都飞的，横向比较不成立
        prices = [r.prices[day] for day in valid_days]
        lo = min(prices)
        mean = sum(prices) / len(prices)
        best_dates = [day for day in valid_days if r.prices[day] <= lo * 1.001]
        used.append(r)
        rows.append({
            "flight_no": r.flight_no or r.key, "airline": r.airline,
            "departure_time": r.departure_time, "arrival_time": r.arrival_time,
            "cv": round(_cv(prices), 3), "min_price": lo, "max_price": max(prices),
            "mean_price": round(mean, 1),
            "best_dates": best_dates,
            "saving_pct": round((mean - lo) / mean * 100, 1) if mean else 0.0,
        })

    if len(rows) < 3:
        return {"ok": False, "reason": "全窗口都在飞的航班不足 3 班，无法做逐航班对比"}

    distribution = {day: 0 for day in valid_days}
    for item in rows:
        for day in item["best_dates"]:
            distribution[day] += 1

    consensus_day = max(distribution, key=lambda d: (distribution[d], d))
    agree = distribution[consensus_day]

    # 共识日那天这批航班的中位价 ——「那天大概多少钱」要有个数，
    # 用中位数而不是最低价，理由同 panel_view。
    median_on_day = _median([r.prices[consensus_day] for r in used])

    cvs = sorted(item["cv"] for item in rows)
    median_cv = cvs[len(cvs) // 2]
    savings = sorted(item["saving_pct"] for item in rows)
    median_saving = savings[len(savings) // 2]

    return {"ok": True, "flights": rows, "distribution": distribution,
            "consensus_day": consensus_day, "agree": agree, "total": len(rows),
            "median_price_on_consensus": median_on_day,
            "median_cv": median_cv, "median_saving": median_saving,
            "valid_days": valid_days}


def merge_codeshares(m: PriceMatrix) -> PriceMatrix:
    """把共享代码航班合并成一行。

    ## 为什么必须做

    一条航线返回 17 个"航班"，按起降时刻一分组就会发现只有 4 组：
    同一分钟起飞、同一分钟到达 = **同一架飞机**。这是代码共享：
    长龙执飞，川航 / 南航 / 东航 / 厦航挂自己的航班号在卖票。

    不合并的话有两处直接错：
      - "17 个航班"是虚的，用户以为选择很多，其实只有 4 班飞机；
      - "按航司"表会说"东方航空 6 个航班 ¥300"，可飞机是长龙的。

    合并之后反而露出了更有用的信息：**同一架飞机，不同航司卖不同价**
    （07:00 那班 ¥300 / ¥300 / ¥400）。这正是"只报一个最低价"会抹掉、
    而用户真正想看的东西——只是维度从"航司策略"变成了"销售渠道差价"。

    ## 分组键为什么是起降时刻

    起飞和到达都精确到同一分钟，还是同一条航线同一天——这不可能是巧合。
    只用起飞时刻不够：两班飞机同点起飞、不同时间到达是可能的。
    这里**不猜哪家是实际承运人**（数据里没有这个字段），
    只按窗口内最低价挑一个代表号，其余原样列在 codeshares 里。
    """
    groups: dict[tuple, list[FlightRow]] = {}
    for row in m.rows:
        # 时刻缺失的行不参与合并——没有依据就不合，宁可多一行
        if not row.departure_time or not row.arrival_time:
            groups[("__solo__", row.key)] = [row]
            continue
        groups.setdefault((row.departure_time, row.arrival_time, row.transfer_num),
                          []).append(row)

    merged: list[FlightRow] = []
    for key, rows in groups.items():
        if len(rows) == 1:
            merged.append(rows[0])
            continue

        # 真正的共享代码是**同一天同时出现**的多个航班号。
        # 如果这些号在日期上完全不重叠，那更可能是数据源的航班号不稳定
        # （每天换一个号）——那种情况必须留着让 _diagnose 报出来，
        # 而不是靠"按时刻合并"把问题盖掉，凑出一条好看却虚假的曲线。
        total = sum(len(r.prices) for r in rows)
        union = len({iso for r in rows for iso in r.prices})
        if total <= union:
            merged.extend(rows)
            continue

        ordered = sorted(rows, key=lambda r: (r.min_price if r.min_price is not None
                                              else 1e9, r.flight_no))
        lead = ordered[0]
        prices: dict[str, float] = {}
        cheapest_no: dict[str, str] = {}
        for row in ordered:
            for iso, price in row.prices.items():
                if iso not in prices or price < prices[iso]:
                    prices[iso] = price
                    cheapest_no[iso] = row.flight_no or row.key

        merged.append(FlightRow(
            key="|".join(sorted(r.key for r in ordered)),
            flight_no=lead.flight_no, airline=lead.airline,
            departure_time=lead.departure_time, arrival_time=lead.arrival_time,
            transfer_num=lead.transfer_num, prices=prices,
            cheapest_no=cheapest_no,
            codeshares=[(r.flight_no or r.key, r.airline, r.min_price) for r in ordered],
        ))

    m.rows = sorted(merged, key=lambda r: (r.min_price if r.min_price is not None
                                           else 1e9, r.departure_time))
    return m


def _diagnose(m: PriceMatrix) -> list[str]:
    """把「矩阵在什么条件下不可信」讲清楚，而不是让用户自己发现。"""
    w: list[str] = []
    valid = len(m.valid_columns)

    if m.failed_days:
        w.append("有 %d 天查询失败（%s），这些列是**数据缺口**，不代表当天没有航班。"
                 % (len(m.failed_days), "、".join(m.failed_days[:5])
                    + ("…" if len(m.failed_days) > 5 else "")))

    empty = [c.day.isoformat() for c in m.columns if c.status == "no_flight"]
    if empty:
        w.append("有 %d 天数据源返回空（%s）：该航线当天可能确实无航班。"
                 % (len(empty), "、".join(empty[:5]) + ("…" if len(empty) > 5 else "")))

    if not m.rows:
        w.append("窗口内没有任何带票价的航班，无法构建价格矩阵。")
        return w

    if valid:
        covs = sorted(r.coverage(valid) for r in m.rows)
        median = covs[len(covs) // 2]
        if median < LOW_COVERAGE_RATIO:
            w.append(
                "航班行覆盖率偏低（中位数 %.0f%%）：多数航班号只在少数几天出现。"
                "可能是该航线本身多为非每日航班，也可能是数据源的航班号在不同日期不一致——"
                "后者会让「同一行 = 同一航班」的前提失效，横向价格曲线不可比。建议人工抽查两天核对航班号。"
                % (median * 100))

    transfers = [r for r in m.rows if r.transfer_num and r.transfer_num > 1]
    if transfers and len(transfers) >= 0.2 * len(m.rows):
        w.append("有 %d/%d 班是中转（航班号形如 A+B）。它们常常更便宜，"
                 "但耗时差得多，和直飞放在一起比「当日最低价」并不公平。"
                 "只想看直飞加 --direct-only。" % (len(transfers), len(m.rows)))

    single = [r for r in m.rows if r.observed == 1]
    if single and len(single) == len(m.rows) and valid > 1:
        w.append("每个航班号都只出现过一天，矩阵退化为对角线，横向对比无意义。")

    # 每一列都一模一样 —— 几乎可以确定接口忽略了出发日参数（depDate 这类字段
    # 常常是"选填、默认当天"，填错名字就被静默丢弃），或者返回的是公布运价而非实时价。
    # 不查出来的话，你会拿到一张看起来很正常、其实每列都是同一天的表。
    if valid > 2 and m.rows:
        vectors = {tuple(sorted((c.day.isoformat(), m.cell(r, c)) for c in m.valid_columns
                                if m.cell(r, c) is not None)) for r in m.rows}
        flat = [set(v for k, v in vec) for vec in vectors]
        if flat and all(len(s) == 1 for s in flat):
            w.append(
                "每个航班在所有日期上的价格完全相同。这**几乎不可能是真实行情**，"
                "更可能是：① 接口忽略了出发日参数（检查 params.date_key 拼写与 date_format），"
                "或 ② 返回的是公布运价 / 全价而不是实时最低可售价（检查 response.price_field）。"
                "在排除之前不要采信这张表。")

    return w


# --------------------------------------------------------------------------
# 渲染：给人看的宽表
# --------------------------------------------------------------------------

def _fmt_price(p: float | None) -> str:
    if p is None:
        return ""
    return str(int(p)) if float(p).is_integer() else ("%.1f" % p)


def render_matrix(m: PriceMatrix, max_rows: int = 12, cols_per_block: int = 15,
                  show_summary: bool = True) -> str:
    """渲染成 Markdown 宽表。

    行按「窗口内最低价」升序——用户最先看到的就是最值得买的那几班。
    单元格里的 * 标记该航班自己的最低点，一眼能看出每条曲线的谷底在哪天。
    """
    if not m.columns:
        return "【%s】未扫描任何日期。" % m.route

    L: list[str] = ["【%s ｜ 未来 %d 天 · 每航班价格矩阵】"
                    % (m.route, len(m.columns))]
    merged_count = sum(1 for r in m.rows if r.is_merged)
    if merged_count and m.raw_flight_count:
        flight_desc = ("实际航班 %d 班（接口返回 %d 个航班号，其中 %d 班存在共享代码，已合并）"
                       % (len(m.rows), m.raw_flight_count, merged_count))
    else:
        flight_desc = "航班 %d 个" % len(m.rows)
    L.append("扫描区间：%s ~ %s（消耗 %d 次查询额度）｜有效 %d 天 / 失败 %d 天｜%s"
             % (m.columns[0].day.isoformat(), m.columns[-1].day.isoformat(),
                m.api_calls, len(m.valid_columns), len(m.failed_days), flight_desc))
    L.append("")

    if not m.rows:
        L.append("（窗口内没有任何带票价的航班）")
        for warn in m.warnings:
            L.append("- " + warn)
        return "\n".join(L)

    rows = m.rows[:max_rows]
    hidden = len(m.rows) - len(rows)

    # 列太多时分块渲染，避免一行几百字符在终端里折成一团
    blocks = [m.columns[i:i + cols_per_block]
              for i in range(0, len(m.columns), cols_per_block)]

    for bi, block in enumerate(blocks):
        if len(blocks) > 1:
            L.append("**第 %d 段：%s ~ %s**"
                     % (bi + 1, block[0].day.isoformat(), block[-1].day.isoformat()))
        header = "| 航班 | 航司 | 起飞 | " + " | ".join(c.label for c in block) + " |"
        sep = "|---|---|---|" + "---|" * len(block)
        L.append(header)
        L.append(sep)

        for r in rows:
            cells = []
            for c in block:
                if c.status == "failed":
                    cells.append("×")
                    continue
                v = m.cell(r, c)
                if v is None:
                    cells.append("—")
                else:
                    cells.append(_fmt_price(v) + ("*" if r.best_date == c.day.isoformat() else ""))
            label = r.flight_no or r.key
            if r.is_merged:
                label += " +%d" % (len(r.codeshares) - 1)
            L.append("| %s | %s | %s | %s |"
                     % (label, r.airline or "-",
                        r.departure_time or "-", " | ".join(cells)))

        # 汇总行：当日最低（跨所有航班，包含未列出的行）
        mins = []
        for c in block:
            if c.status == "failed":
                mins.append("×")
            elif c.min_price is None:
                mins.append("—")
            else:
                mins.append(_fmt_price(c.min_price))
        L.append("| **当日最低** | | | %s |" % " | ".join(mins))
        L.append("")

    L.append("图例：数字 = 该航班当日最低票价（元）｜`*` 该航班自己的最低点｜"
             "`—` 当天无此航班｜`×` 当天查询失败（数据缺口，非无航班）")
    if merged_count:
        L.append("航班号后的 `+N` = 同一架飞机还有 N 个共享代码航班号在卖，"
                 "格子里取的是**这几个号里最便宜的**；各号差价见下面的「共享代码」表。")
    if hidden > 0:
        L.append("（另有 %d 班未列出：终端只显示最便宜的 %d 行，"
                 "**导出的 xlsx / csv 里是全的**）" % (hidden, max_rows))

    if show_summary:
        L.append("")
        L.append("**每航班要点（按窗口内最低价升序）**")
        L.append("")
        L.append("| 航班 | 航司 | 起飞 | 最低价 | 该班最便宜的日子 | 最高价 | 波动 | 变异系数 | 有价天数 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            prices = list(r.prices.values())
            cv_text = "%.2f" % _cv(prices) if len(prices) >= 3 else "-"
            lo = r.min_price
            best_days = ([d for d, p in sorted(r.prices.items()) if p <= lo * 1.001]
                         if lo else [])
            best_text = "、".join(d[5:] for d in best_days[:4]) or "-"
            if len(best_days) > 4:
                best_text += " 等%d天" % len(best_days)
            L.append("| %s | %s | %s | ¥%s | %s | ¥%s | %s | %s | %d/%d |"
                     % (r.flight_no or r.key, r.airline or "-", r.departure_time or "-",
                        _fmt_price(lo), best_text, _fmt_price(r.max_price),
                        ("¥" + _fmt_price(r.swing)) if r.swing is not None else "-",
                        cv_text, r.observed, len(m.valid_columns)))
        L.append("")
        L.extend(_codeshare_detail(m))
        L.extend(_airline_summary(m))

    advice = per_flight_advice(m)
    if advice.get("ok"):
        L.append("")
        L.append("**逐航班的最低点落在哪天**"
                 "（只统计全窗口都在飞的 %d 班直飞——不是每天都飞的航班无法横向比）"
                 % advice["total"])
        L.append("")
        L.append("| 出发日 | 有几班在这天最便宜 | |")
        L.append("|---|---|---|")
        peak = advice["agree"]
        for day, count in advice["distribution"].items():
            d = date.fromisoformat(day)
            bar = "█" * count if count else ""
            mark = "  ← 最集中" if count == peak and count else ""
            L.append("| %02d-%02d %s | %d | %s%s"
                     % (d.month, d.day, WEEKDAY_CN[d.weekday()], count, bar, mark))
        L.append("")
        L.append("- **共识：%d/%d 班在 %s 最便宜**，逐航班中位跌幅 %.0f%%（相对各自 7 日均价）"
                 % (advice["agree"], advice["total"], advice["consensus_day"],
                    advice["median_saving"]))
        L.append("- 逐航班中位变异系数 %.2f" % advice["median_cv"])
        L.append("- 一班飞机可能有几天并列最低，都算进去了，所以各日计数之和大于航班数。")

        if advice["agree"] >= advice["total"] * 0.5:
            L.append("- 半数以上航班在同一天见底，这个信号是可用的。"
                     "**注意它来自逐航班统计，聚合的「当日最低价」曲线会把它抹平**"
                     "——那条曲线每天在不同的航班集合上取 min，某天冒出来一班特价"
                     "就能拉平整条线。")
        else:
            L.append("- 各航班的低点比较分散，没有形成共识：这条航线更适合"
                     "**先选定航班、再看那一行**，而不是找一个「全航线最佳日」。")

    vol = volatility(m)
    if vol.get("ok"):
        L.append("")
        L.append("**整体波动（参考）**：口径 %s｜变异系数 %.2f｜"
                 "最低价 ±5%% 区间内 %d/%d 天"
                 % (vol["basis"], vol["cv"], vol["trough_days"], vol["sample_days"]))
        if vol.get("divergence"):
            L.append("")
            L.append("- %s" % vol["divergence"])

    if m.warnings:
        L.append("")
        L.append("⚠️ 注意：")
        L.extend("- " + w for w in m.warnings)

    L.append("")
    L.append("说明：表内为**基准最低可售价**（航司放出的最低舱位价），随舱位实时变动；"
             "**不含**飞猪 / 携程 / 美团等平台的优惠券、立减与会员权益，"
             "实际支付金额可能更低。本表用来判断「哪天、哪班便宜」，"
             "最终成交价请以下单平台为准。")
    return "\n".join(L)


def _codeshare_detail(m: PriceMatrix) -> list[str]:
    """同一架飞机、不同航司卖不同价——合并之后才看得见的信息。"""
    merged = [r for r in m.rows if r.is_merged]
    if not merged:
        return []

    L = ["**共享代码明细（同一架飞机，不同航司在卖）**", "",
         "| 起降时刻 | 各航班号（航司）与窗口内最低价 | 最大差价 | 各日最低来自 |",
         "|---|---|---|---|"]
    for r in merged:
        prices = [p for _, _, p in r.codeshares if p is not None]
        gap = ("¥" + _fmt_price(max(prices) - min(prices))) if len(prices) > 1 else "-"
        detail = " ｜ ".join("%s(%s) ¥%s" % (no, airline or "?", _fmt_price(price))
                             for no, airline, price in r.codeshares)
        sources = set(r.cheapest_no.values())
        if len(sources) == 1:
            from_no = sources.pop()
        else:
            # 最便宜的号逐日在变 —— 这本身就是有用的信息，不能抹平成一个号
            from_no = "随日期变化（%s）" % "/".join(sorted(sources))
        L.append("| %s→%s | %s | %s | %s |"
                 % (r.departure_time, r.arrival_time, detail, gap, from_no))
    L.append("")
    L.append("同一架飞机不同航班号价差可观时，直接买最便宜那个号即可——"
             "座位、时刻、机型完全相同。")
    L.append("")
    return L


def _airline_summary(m: PriceMatrix) -> list[str]:
    """按**销售**航司聚合。合并共享代码后，这张表回答的是
    「哪家渠道卖得便宜」，而不是「哪家航司的飞机便宜」——
    实际承运人数据源没给，不猜。"""
    by_airline: dict[str, list[FlightRow]] = {}
    for r in m.rows:
        if r.is_merged:
            # 合并行按各销售号分别归到自己的航司名下，否则统计会失真
            for no, airline, price in r.codeshares:
                proxy = FlightRow(key=no, flight_no=no, airline=airline,
                                  departure_time=r.departure_time)
                if price is not None:
                    proxy.prices = {r.best_date or "?": price}
                by_airline.setdefault(airline or "未知航司", []).append(proxy)
        else:
            by_airline.setdefault(r.airline or "未知航司", []).append(r)
    if len(by_airline) < 2:
        return []

    L = ["**按销售航司（同一架飞机可能有多家在卖）**", "",
         "| 航司 | 在卖的航班号数 | 最低价 | 最低出现在 |", "|---|---|---|---|"]
    items = []
    for name, rs in by_airline.items():
        prices = [r.min_price for r in rs if r.min_price is not None]
        if not prices:
            continue
        best_row = min(rs, key=lambda r: r.min_price if r.min_price is not None else 1e9)
        items.append((min(prices), name, len(rs), best_row.best_date))
    for price, name, count, best_date in sorted(items):
        L.append("| %s | %d | ¥%s | %s |" % (name, count, _fmt_price(price), best_date or "-"))
    return L


# --------------------------------------------------------------------------
# 摘要：给模型看的压缩版
# --------------------------------------------------------------------------

def digest(m: PriceMatrix, top_n: int = 5) -> dict[str, Any]:
    """给 LLM 的压缩摘要。

    模型不需要 600 个格子——它需要知道「有哪些结论、哪里有坑」，
    完整表格由代码渲染给用户（见 table_refs）。这和链接换记号是同一个分工原则：
    **确定性数据不该由模型誊写。**
    """
    valid = len(m.valid_columns)
    top = []
    for r in m.rows[:top_n]:
        item = {"flight_no": r.flight_no or r.key, "airline": r.airline,
                "departure_time": r.departure_time,
                "min_price": r.min_price, "best_date": r.best_date,
                "max_price": r.max_price, "swing": r.swing,
                "observed_days": r.observed}
        if r.is_merged:
            item["codeshare_numbers"] = [no for no, _, _ in r.codeshares]
        top.append(item)

    cheapest_col = None
    valid_cols = [c for c in m.valid_columns if c.min_price is not None]
    if valid_cols:
        c = min(valid_cols, key=lambda x: x.min_price)
        cheapest_col = {"date": c.day.isoformat(), "advance": c.advance,
                        "min_price": c.min_price}

    codeshare_note = ""
    if m.raw_flight_count and m.raw_flight_count > len(m.rows):
        codeshare_note = ("接口返回 %d 个航班号，按起降时刻合并后实际只有 %d 班飞机"
                          "（代码共享：同一架飞机多家航司在卖，价格不同）。"
                          "不要把航班号数量说成航班数量。"
                          % (m.raw_flight_count, len(m.rows)))

    return {
        "route": m.route,
        "codeshare_note": codeshare_note,
        "scan_range": [m.columns[0].day.isoformat(), m.columns[-1].day.isoformat()] if m.columns else [],
        "days_scanned": len(m.columns), "valid_days": valid,
        "failed_days": m.failed_days, "api_calls": m.api_calls,
        "flight_count": len(m.rows),
        "top_flights": top,
        "cheapest_day_overall": cheapest_col,
        "volatility": volatility(m),
        "warnings": m.warnings,
    }


def to_scan(m: PriceMatrix, direct_only: bool = True) -> list[dict]:
    """把矩阵折叠回 price_analysis 需要的「价格 ~ 提前天数」曲线。

    关键点：**复用同一批数据，不再发一次请求。** 提前量分析与价格矩阵
    是同一次扫描的两种视图，额度只花一份。

    每一天给三个价：

      - `panel_median`：可比面板的中位价 —— **提前量分析的主口径**；
      - `panel_min`：可比面板的最低价；
      - `min_price`：全部航班的当日最低价（含只飞几天的中转特价），仅作对照。

    只有 `panel_median` 是跨日期可比的：它每天都在**同一批航班**上取数。
    `min_price` 每天在不同的航班集合上取 min，某天冒出来一班只飞那天的
    中转特价就能把整条曲线拉平——上海→昆明就是这么被拉平的。
    """
    panel = panel_view(m, direct_only=direct_only)
    by_day = {}
    if panel.get("ok"):
        for i, day in enumerate(panel["days"]):
            by_day[day] = (panel["median"][i], panel["panel_min"][i])

    scan = []
    for c in m.columns:
        if c.status != "ok" or c.min_price is None:
            continue
        median, pmin = by_day.get(c.day.isoformat(), (None, None))
        scan.append({"date": c.day, "advance": c.advance,
                     "weekday": c.day.weekday(), "min_price": c.min_price,
                     "panel_median": median, "panel_min": pmin,
                     "panel_size": panel["panel_size"]})
    return scan
