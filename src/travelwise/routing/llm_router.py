# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""LLMRouter —— 用 function calling 做意图路由与参数抽取。

与 RuleRouter 输出同一种 TravelState，因此可以跑同一套 Eval、直接对照。

做法：把「路由」本身声明成一个工具 `plan_travel_request`，强制模型以结构化
参数作答，而不是让它自由生成 JSON 再去解析——后者极易出格式错误。

三条必须坚持的原则（提示词与代码两侧都设了防线）：
  1. **抽不到就留空**，绝不猜。留空会进入 missing，由 Orchestrator 去问用户。
  2. **scope 忠实于用户措辞**，不得因为某城市景点少就自行扩成省。
  3. **模型不可用 / 返回不可解析 → 如实失败**，可选降级到规则路由，
     且降级必须在结果里标记出来（fell_back=True），不静默替换。
"""

from __future__ import annotations

import re
import time
from datetime import date

from ..llm.base import LLMClient, LLMError, Usage
from ..state import TaskStatus, TravelState
from .base import RouteOutcome, Router

SYSTEM_PROMPT = """\
你是出行请求的解析器。把用户的中文出行请求解析成结构化参数，调用 plan_travel_request 工具作答。

意图判定规则：
- 抽到完整航线（出发地 + 目的地）且用户没问玩什么 → 只有 flight。
- 用户只问某地有什么好玩的 → 只有 destination。
- 既问机票又问玩什么 → 两者都要。
- 只提到单个目的地 + 出行动词（去/飞/到），没说要做什么 → 两者都要。
- 火车票、酒店、签证、打车、值机、天气、闲聊 → intents 留空数组，表示超出能力范围。

参数抽取规则：
- 抽不到的字段一律留空，**绝对不要猜测或填入看起来合理的值**。留空会由系统去问用户。
- 日期必须换算成 YYYY-MM-DD。相对时间按给定的"今天"换算。
- origin 和 destination 不能是同一个城市；若抽出来相同，说明抽错了，把 origin 留空。
- place 是"想去玩的地方"，可能与航班到达城市不同。
  例：「飞乌鲁木齐，新疆有什么玩的」→ destination=乌鲁木齐，place=新疆。
- scope 必须忠实反映用户的措辞：说城市就是 city，说省/自治区才是 province。
  **严禁因为某个城市景点可能很少就自行改成 province。**
"""

ROUTE_TOOL = {
    "name": "plan_travel_request",
    "description": "把用户的出行请求解析成结构化参数",
    "parameters": {
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "items": {"type": "string", "enum": ["flight", "destination"]},
                "description": "需要处理的意图。超出能力范围时给空数组",
            },
            "origin": {"type": "string", "description": "出发城市；抽不到就留空"},
            "destination": {"type": "string", "description": "到达城市；抽不到就留空"},
            "travel_date": {"type": "string", "description": "出行日期 YYYY-MM-DD；抽不到就留空"},
            "place": {"type": "string", "description": "想去玩的地名；可能与到达城市不同"},
            "scope": {"type": "string", "enum": ["city", "province"],
                      "description": "玩乐范围，忠实于用户措辞"},
            "travel_month": {"type": "integer", "minimum": 1, "maximum": 12,
                             "description": "用于季节标注的月份；不确定就不给"},
        },
        "required": ["intents"],
    },
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LLMRouter(Router):
    """基于 LLM function calling 的路由器。"""

    name = "llm"

    def __init__(self, client: LLMClient, fallback: Router | None = None,
                 max_tokens: int = 512):
        self.client = client
        self.fallback = fallback
        self.max_tokens = max_tokens
        self.is_synthetic = bool(getattr(client, "is_synthetic", False))

    # ------------------------------------------------------------------
    def route(self, user_request: str, today: date | None = None) -> RouteOutcome:
        text = (user_request or "").strip()
        today = today or date.today()
        started = time.perf_counter()

        if not text:
            # 空输入不必消耗一次模型调用
            return RouteOutcome(state=TravelState(user_request=""), router=self.name,
                                latency_ms=(time.perf_counter() - started) * 1000)

        system = SYSTEM_PROMPT + "\n今天的日期是 %s。" % today.isoformat()
        try:
            response = self.client.complete(
                [{"role": "user", "content": text}],
                system=system, tools=[ROUTE_TOOL],
                tool_choice="plan_travel_request", max_tokens=self.max_tokens)
        except LLMError as e:
            return self._handle_failure(text, today, str(e), started)

        call = response.first_tool_call("plan_travel_request")
        if call is None:
            return self._handle_failure(
                text, today, "模型未调用 plan_travel_request（返回：%s）"
                % (response.text or "空")[:80], started, usage=response.usage)

        state = self._to_state(text, call.arguments or {}, today)
        return RouteOutcome(state=state, router=self.name,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            usage=response.usage,
                            raw_tool_call={"name": call.name, "arguments": call.arguments})

    # ------------------------------------------------------------------
    def _handle_failure(self, text: str, today: date, reason: str,
                        started: float, usage: Usage | None = None) -> RouteOutcome:
        """模型不可用时的处理。

        有 fallback 就降级到规则路由，**但必须标记 fell_back**——
        让上层和评测都能看见"这一条不是 LLM 干的"，避免把降级成绩算进 LLM 头上。
        """
        if self.fallback is not None:
            inner = self.fallback.route(text, today)
            return RouteOutcome(
                state=inner.state, router=self.name,
                latency_ms=(time.perf_counter() - started) * 1000,
                usage=usage or Usage(),
                error="LLM 路由失败已降级到规则路由：%s" % reason, fell_back=True)
        return RouteOutcome(
            state=TravelState(user_request=text), router=self.name,
            latency_ms=(time.perf_counter() - started) * 1000,
            usage=usage or Usage(), error=reason)

    # ------------------------------------------------------------------
    @staticmethod
    def _clean(value) -> str | None:
        if not isinstance(value, str):
            return None
        v = value.strip()
        return v or None

    def _to_state(self, text: str, args: dict, today: date) -> TravelState:
        """把模型给的参数装配成 TravelState，并施加与规则路由一致的兜底约束。

        这些兜底不是不信任模型，而是**不变量应当由代码保证**：
        无论路由由谁来做，"日期必须是 YYYY-MM-DD"、"出发地不能等于目的地"
        这类规则都不该依赖对方自觉。
        """
        raw_intents = args.get("intents")
        intents = [i for i in (raw_intents or []) if i in ("flight", "destination")]
        # 去重并固定顺序：flight 在前，便于与规则路由逐字段比对
        intents = [i for i in ("flight", "destination") if i in intents]

        state = TravelState(user_request=text, intents=intents,
                            current_step=TaskStatus.ROUTED)
        if not intents:
            state.current_step = TaskStatus.OUT_OF_SCOPE
            return state

        state.origin = self._clean(args.get("origin"))
        state.destination = self._clean(args.get("destination"))

        travel_date = self._clean(args.get("travel_date"))
        if travel_date and _DATE_RE.match(travel_date):
            try:
                date.fromisoformat(travel_date)
                state.travel_date = travel_date
            except ValueError:
                state.travel_date = None          # 格式对但日期非法 → 当作没抽到
        # 不合格式的日期一律丢弃，宁可去问用户，也不让脏日期流进业务代码

        if state.origin and state.origin == state.destination:
            state.origin = None                   # 与规则路由同一条兜底

        if "destination" in intents:
            state.place = self._clean(args.get("place")) or state.destination
            scope = self._clean(args.get("scope"))
            state.scope = scope if scope in ("city", "province") else "city"

        missing = []
        if "flight" in intents:
            if not state.origin:
                missing.append("origin")
            if not state.destination:
                missing.append("destination")
            if not state.travel_date:
                missing.append("travel_date")
        if "destination" in intents and not state.place:
            missing.append("place")
        state.missing = missing
        return state
