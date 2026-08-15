# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""fallback_flight.py —— 多数据源按顺序容错。

主源失败就换备源。**只在"失败"时才换，不在"没数据"时换**——
这是两件事：接口返回空列表意味着那天真的没航班，再打一次备源
只会白花钱，还可能拿到一份口径不同的数据混进同一张矩阵里。

记账在这里也很重要：0.2 元一次的接口，你需要知道每一次调用花在了谁身上。
`attempts` 记录每一次尝试（源、成功与否、错误、耗时），`calls_by_provider`
给出汇总，跑完一次矩阵就能对账。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from .base import Flight, FlightProvider, ProviderError


@dataclass
class Attempt:
    provider: str
    day: str
    ok: bool
    error: str = ""
    latency_ms: float = 0.0
    flight_count: int = 0


class FallbackFlightProvider(FlightProvider):
    """把若干 Provider 串成一条链，前一个失败就试下一个。

    supports_price / name 取自链上第一个可用源；只要有**任意**一个源带票价，
    supports_price 就为 True——否则会因为备源不含价而误关掉价格分析。
    """

    def __init__(self, providers: list[FlightProvider], name: str = ""):
        self.providers = [p for p in providers if p is not None]
        if not self.providers:
            raise ValueError("FallbackFlightProvider 至少需要一个数据源")
        self.name = name or ("fallback(%s)" % "→".join(p.name for p in self.providers))
        self.supports_price = any(getattr(p, "supports_price", False) for p in self.providers)
        self.attempts: list[Attempt] = []

    @property
    def calls_by_provider(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.attempts:
            out[a.provider] = out.get(a.provider, 0) + 1
        return out

    def cost_report(self, unit_price: float = 0.2) -> str:
        """对账用。unit_price 单位：元/次。"""
        total = len(self.attempts)
        failed = sum(1 for a in self.attempts if not a.ok)
        lines = ["调用 %d 次（失败 %d 次）≈ ¥%.1f" % (total, failed, total * unit_price)]
        for name, n in sorted(self.calls_by_provider.items(), key=lambda kv: -kv[1]):
            lines.append("  · %s：%d 次" % (name, n))
        return "\n".join(lines)

    def search_flights(self, origin: str, destination: str, day: str | date) -> list[Flight]:
        d = day if isinstance(day, date) else str(day)
        errors: list[str] = []

        for provider in self.providers:
            started = time.perf_counter()
            try:
                flights = provider.search_flights(origin, destination, day)
            except ProviderError as e:
                self.attempts.append(Attempt(
                    provider=provider.name, day=str(d), ok=False, error=str(e),
                    latency_ms=(time.perf_counter() - started) * 1000))
                errors.append("%s：%s" % (provider.name, e))
                continue
            except Exception as e:                       # noqa: BLE001
                # 非预期异常也算这个源失败，但要保留类型名——否则排查时看不出是代码 bug
                self.attempts.append(Attempt(
                    provider=provider.name, day=str(d), ok=False,
                    error="%s: %s" % (type(e).__name__, e),
                    latency_ms=(time.perf_counter() - started) * 1000))
                errors.append("%s：%s: %s" % (provider.name, type(e).__name__, e))
                continue

            self.attempts.append(Attempt(
                provider=provider.name, day=str(d), ok=True,
                latency_ms=(time.perf_counter() - started) * 1000,
                flight_count=len(flights)))
            # 空列表 = 那天没航班，是**有效结果**，不再往下试第二个源浪费额度
            return flights

        raise ProviderError("全部 %d 个数据源都失败了：%s"
                            % (len(self.providers), "；".join(errors)))
