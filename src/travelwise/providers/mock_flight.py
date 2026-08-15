# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""MockFlightProvider —— 不联网、不需要任何 API Key 的航班数据源。

存在意义（不是"玩具"，是基础设施）：
  1. clone 下来立刻能跑完整 Demo，不必先去买接口；
  2. CI 里能跑测试；
  3. 真实 API 挂掉时仍能演示核心算法；
  4. 价格分析的单元测试需要"可预期的价格曲线"才能断言。

价格曲线是【确定性】生成的（同样输入永远同样输出），因此可以写断言。
曲线特征刻意做得贴近真实：
  - 在某个提前天数处见底，两侧回升（V 形）；
  - 周末整体偏贵（制造"星期几噪声"，用来验证同星期几对齐算法确实有用）；
  - 同一天不同航班有价差。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from .base import Flight, FlightProvider

_AIRLINES = [
    ("中国国际航空", "CA"),
    ("东方航空", "MU"),
    ("南方航空", "CZ"),
    ("海南航空", "HU"),
    ("春秋航空", "9C"),
]


def _stable_int(*parts: str) -> int:
    """把任意字符串组合映射成一个稳定的整数（同输入同输出，跨机器一致）。"""
    raw = "|".join(parts).encode("utf-8")
    return int(hashlib.md5(raw).hexdigest()[:8], 16)


class MockFlightProvider(FlightProvider):
    """合成航班数据。价格曲线在 trough_advance 天处见底。"""

    supports_price = True
    name = "mock"

    def __init__(self, today: date | None = None, trough_advance: int = 21,
                 base_price: float = 900.0, flights_per_day: int = 5,
                 supports_price: bool = True):
        self.today = today or date.today()
        self.trough_advance = trough_advance
        self.base_price = base_price
        self.flights_per_day = flights_per_day
        # 允许模拟"只有时刻、没有票价"的数据源，用于测试降级路径
        self.supports_price = supports_price

    # -- 价格模型 ---------------------------------------------------------
    def _day_base_price(self, origin: str, destination: str, day: date) -> float:
        # 注意：V 形提前量曲线已下放到**每个航班**（见 search_flights）——
        # 因为不同航司的曲线本来就不一样，放在这里等于假设整条航线共用一条曲线。
        # 这里只保留对所有航班一致的成分。
        weekend = 180.0 if day.weekday() >= 5 else 0.0    # 周末溢价：制造星期几噪声
        route = _stable_int(origin, destination) % 300    # 航线固有基准，可复现
        return self.base_price + weekend + route

    # -- 接口实现 ---------------------------------------------------------
    def search_flights(self, origin: str, destination: str, day: str | date) -> list[Flight]:
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        day_base = self._day_base_price(origin, destination, d)
        advance = (d - self.today).days

        flights: list[Flight] = []
        for i in range(self.flights_per_day):
            # 【航班身份】只由航线+序号决定，**与日期无关**——同一个航班号天天都在飞。
            # 早先这里把日期也拌进了种子，于是每天都是一批全新航班号：
            # 价格矩阵会退化成对角线（每行只有一个格子），横向曲线根本不成立。
            ident = _stable_int(origin, destination, str(i))
            airline, code = _AIRLINES[(ident // 7) % len(_AIRLINES)]
            dep_hour = 6 + (ident % 15)               # 06:00 ~ 20:00
            dep_min = (ident // 3) % 4 * 15
            duration = 100 + (ident % 90)             # 100~190 分钟
            dep_dt = datetime(d.year, d.month, d.day, dep_hour, dep_min)
            arr_dt = dep_dt + timedelta(minutes=duration)

            # 【周班】末位航班只在一三五飞，用来制造真实的稀疏行（表格里的 —）。
            if i == self.flights_per_day - 1 and d.weekday() not in (0, 2, 4):
                continue

            # 【每家航司自己的曲线】——这正是"只报一个最低价"会抹掉的信息：
            #   低成本航司：越早越便宜，临近陡涨；
            #   全服务航司：中段放低价舱，两头贵。
            if code == "9C":
                curve = max(0, 28 - advance) * 26.0 - 120.0
            elif code in ("CA", "MU"):
                curve = abs(advance - self.trough_advance) * 14.0
            else:
                curve = abs(advance - self.trough_advance) * 22.0
            spread = (ident % 5) * 45.0
            jitter = (_stable_int(str(ident), d.isoformat()) % 7) * 10.0
            price = round(day_base + curve + spread + jitter, 0) if self.supports_price else None

            flights.append(Flight(
                flight_no="%s%d" % (code, 1000 + ident % 8000),
                airline=airline,
                departure_city=origin,
                arrival_city=destination,
                departure_airport="%s机场" % origin,
                arrival_airport="%s机场" % destination,
                departure_date=d.isoformat(),
                departure_time=dep_dt.strftime("%H:%M"),
                arrival_time=arr_dt.strftime("%H:%M"),
                transfer_num=1,
                price=price,
            ))
        flights.sort(key=lambda f: f.departure_time)
        return flights


class FailingFlightProvider(FlightProvider):
    """故意失败的数据源——用于验证「工具失败时不得编造、不得假装成功」。

    这是 Tool Failure Eval 的被测对象，不是摆设。
    """

    supports_price = True
    name = "failing"

    def __init__(self, mode: str = "timeout"):
        self.mode = mode

    def search_flights(self, origin: str, destination: str, day: str | date) -> list[Flight]:
        from .base import ProviderError
        messages = {
            "timeout": "请求超时（15s 内未返回）",
            "http_500": "HTTP 500 服务端错误",
            "auth": "HTTP 401 鉴权失败（凭证无效或额度用尽）",
            "malformed": "返回内容不是合法 JSON，接口可能已变更",
            "empty": "",
        }
        if self.mode == "empty":
            return []          # 连上了但当天真的没航班——这不是错误
        raise ProviderError(messages.get(self.mode, "未知错误"))
