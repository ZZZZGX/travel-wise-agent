# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""Provider 抽象层 —— TravelWise 与外部世界之间唯一的接缝。

设计目标：Core 逻辑不认识任何具体平台。
无论航班数据来自哪家 API、提醒写到哪个系统（手机日历 / .ics 文件 / 某个
MCP Server / 只是打印到控制台），Core 只面对下面这两个接口编程。

这样做的直接收益：
  - 换数据源 = 换一个 FlightProvider 实现，业务逻辑一行不动；
  - 没有任何 API Key 时 = 用 Mock 实现，整套流程照样跑通（CI / 演示 / 测试）；
  - 平台专有能力（如某终端系统的待办、闹钟）退化为众多实现中的一个，
    而不是整个项目的前提。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any


# --------------------------------------------------------------------------
# 统一数据模型：所有 Provider 必须把各家五花八门的返回归一成这些结构
# --------------------------------------------------------------------------

@dataclass
class Flight:
    """一个航班。字段有意保持最小集——只留业务真正用到的。"""

    flight_no: str = ""
    airline: str = ""
    departure_city: str = ""
    arrival_city: str = ""
    departure_airport: str = ""
    arrival_airport: str = ""
    departure_date: str = ""          # YYYY-MM-DD
    departure_time: str = ""          # HH:MM
    arrival_time: str = ""            # HH:MM
    transfer_num: int = 1
    price: float | None = None        # None = 该数据源不含票价

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReminderRequest:
    """一条待创建的提醒。Core 只描述"要提醒什么"，不关心怎么落地。"""

    title: str
    remind_at: datetime
    note: str = ""
    important: bool = False


@dataclass
class ReminderResult:
    """提醒创建结果。

    ok=False 时 message 必须说清失败原因——绝不允许把失败包装成成功。
    """

    ok: bool
    provider: str
    message: str = ""
    location: str = ""                # 落地位置（文件路径 / 外部 id 等），便于用户核实
    payload: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class ProviderError(Exception):
    """Provider 层的可预期错误（网络、鉴权、额度、数据格式等）。

    上层捕获它并【如实告知用户】，不得吞掉后编造数据。
    """


class FlightDataUnavailable(ProviderError):
    """航班数据取不到。区别于"取到了但该航线当天没有航班"（那是空列表，不是异常）。"""


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------

class FlightProvider(ABC):
    """航班数据源接口。"""

    #: 供上层判断能否做价格分析。False 时只能列时刻表。
    supports_price: bool = False

    #: 人类可读的来源名，用于在输出里注明数据出处。
    name: str = "flight-provider"

    @abstractmethod
    def search_flights(self, origin: str, destination: str, day: str | date) -> list[Flight]:
        """返回某航线某出发日的全部航班。

        约定：
          - 取不到数据 → 抛 ProviderError（让上层如实报错）；
          - 取到了但当天无航班 → 返回空列表（这不是错误）。
        """
        raise NotImplementedError


class ReminderProvider(ABC):
    """提醒落地接口。

    任何具体实现（本地文件、.ics、某 MCP Server、某终端系统 API）都在这一层之下，
    Core 永远只调 create()。
    """

    name: str = "reminder-provider"

    @abstractmethod
    def create(self, request: ReminderRequest) -> ReminderResult:
        """创建提醒。失败必须返回 ok=False 并说明原因，不得抛出后被静默忽略。"""
        raise NotImplementedError

    def available(self) -> bool:
        """当前环境是否可用。不可用时上层应降级到别的 Provider 并告知用户。"""
        return True
