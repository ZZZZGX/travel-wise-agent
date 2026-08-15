# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""Router 抽象 —— 让规则路由与 LLM 路由可以互换、可以对照。

关键约束：`travelwise/router.py` **一行未改**。RuleRouter 只是把它包起来，
补上计时与用量统计，好让两者能在同一张表里比较。

为什么保留规则路由（它不是临时代码）：
  - 零模型成本、稳定、可复现，因此能进 CI、能跑回归；
  - 它是 LLM 路由的 **baseline**——没有基线，"上了 LLM 更好"就只是感觉；
  - 真实场景里可以做兜底：LLM 不可用时降级到规则，而不是直接不可用。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from ..llm.base import Usage
from ..state import TravelState


@dataclass
class RouteOutcome:
    """一次路由的结果 + 可观测指标。

    指标是对照实验的全部意义所在：只比准确率而不看延迟与成本，
    没法回答"这个提升值不值这个价"。
    """

    state: TravelState
    router: str = ""
    latency_ms: float = 0.0
    usage: Usage = field(default_factory=Usage)
    error: str = ""
    fell_back: bool = False                  # 是否发生过降级
    raw_tool_call: dict | None = None        # LLM 实际请求的工具与参数


class Router(ABC):
    """路由器接口。两种实现必须给出结构完全一致的 TravelState。"""

    name: str = "router"
    #: 产出是否为合成数据（离线回放）。对照报告据此打警示标记。
    is_synthetic: bool = False

    @abstractmethod
    def route(self, user_request: str, today: date | None = None) -> RouteOutcome:
        raise NotImplementedError


class RuleRouter(Router):
    """包装既有的确定性规则路由。**不修改 router.py。**"""

    name = "rule"

    def route(self, user_request: str, today: date | None = None) -> RouteOutcome:
        from ..router import route as rule_route          # 延迟导入，避免循环依赖

        started = time.perf_counter()
        try:
            state = rule_route(user_request, today)
            error = ""
        except Exception as e:                            # noqa: BLE001
            state = TravelState(user_request=user_request or "")
            error = "%s: %s" % (type(e).__name__, e)
        return RouteOutcome(state=state, router=self.name,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            usage=Usage(), error=error)   # 规则路由 token 成本恒为 0
