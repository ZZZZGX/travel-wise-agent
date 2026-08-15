# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""state.py —— 一次任务的显式状态。

为什么需要它：原实现把「这次任务进行到哪了」散落在 prompt 文本和函数参数里，
既看不见也测不了。这里用一个 dataclass 集中承载，好处是：

  - 任何时刻都能 dump 出来看（调试 / tracing 的基础）；
  - 路由与技能之间靠它传递，而不是靠约定俗成的参数顺序；
  - 评测时可以直接断言 state 里的字段（origin 抽对没有？scope 是不是被偷偷改了？）。

注意：这里只放【本次任务】的状态，不放跨会话的长期偏好——那是 Memory 的职责，
属于后续阶段，现在不假装已经有了。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """任务状态机。

    从自由字符串换成枚举的原因：`done` 这一个值原本同时表示了
    "全都办完了"、"办了一半还缺参数"、"办完了但提醒还等着用户点确认"
    三种完全不同的处境，外部无法据此决定下一步该做什么。

    继承 str 是为了 asdict() 后仍能直接 JSON 序列化。
    """

    CREATED = "created"
    ROUTED = "routed"
    OUT_OF_SCOPE = "out_of_scope"           # 超出能力范围，已如实告知
    AWAITING_INPUT = "awaiting_input"       # 缺参数，等用户补充
    EXECUTING = "executing"
    PARTIAL_COMPLETE = "partial_complete"   # 一部分办成了，另一部分缺参数或失败
    AWAITING_APPROVAL = "awaiting_approval" # 有副作用操作等用户确认
    COMPLETED = "completed"
    FAILED = "failed"


#: 可以被「一句回话」填写的字段。**只有这五个**。
#: 刻意不含 flight_result / errors 之类——槽位合并是给用户输入用的通道，
#: 让它能写执行结果，等于给了任意输入改写内部状态的口子。
SLOT_FIELDS = ("origin", "destination", "travel_date", "place", "scope")


@dataclass
class TravelState:
    # -- 输入 --
    user_request: str = ""

    # -- 路由与参数抽取结果 --
    intents: list[str] = field(default_factory=list)   # ["flight"] / ["destination"] / 两者
    origin: str | None = None
    destination: str | None = None
    travel_date: str | None = None                     # YYYY-MM-DD
    place: str | None = None                           # 玩乐地名（≠ 落地城市）
    scope: str | None = None                           # "city" | "province"

    # -- 执行结果 --
    flight_result: dict[str, Any] | None = None
    destination_result: dict[str, Any] | None = None

    # -- 流程控制 --
    missing: list[str] = field(default_factory=list)   # 缺失参数，非空则应先问用户
    pending_action: dict[str, Any] | None = None       # 待确认的副作用操作
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    current_step: TaskStatus = TaskStatus.CREATED

    # -- 多轮 --
    #: 所属会话。单轮调用时为空串，不影响任何行为。
    session_id: str = ""
    #: 关联的 trace，便于从一条会话记录跳到那一轮的完整调用链。
    trace_id: str = ""
    #: 槽位 → 来源。`{"origin": "carry_over"}` 表示这个值不是用户这轮说的。
    #: 沿用必须留痕：静默继承上一轮的出发地，是多轮里最难查的一类错。
    carried_over: dict[str, str] = field(default_factory=dict)

    def needs_clarification(self) -> bool:
        return bool(self.missing)

    # ------------------------------------------------------------------
    # 槽位
    # ------------------------------------------------------------------
    def recompute_missing(self) -> list[str]:
        """按当前 intents 重算还缺什么，就地写回 `missing` 并返回。

        这段逻辑原本内联在 `router.py` 里。搬过来的原因是多轮续跑
        必须重算一次：用户补了出发地之后，如果没人重算，`missing`
        里那个 "origin" 会一直挂着，`handle()` 的 `flight_ready`
        判断永远为假 —— 补了参数却依然被追问，正是「问了接不住」的
        另一种表现。

        计算规则放在 state 上而不是两处各写一份，是为了让
        「首轮路由」和「续跑」用的是**同一个**定义。两份定义迟早分叉，
        而分叉出来的症状是「单轮好好的，多轮就不对」——最难查的那种。
        """
        required: list[str] = []
        if "flight" in self.intents:
            required += ["origin", "destination", "travel_date"]
        if "destination" in self.intents:
            required.append("place")
        self.missing = [f for f in required if not getattr(self, f, None)]
        return self.missing

    def apply_slots(self, values: dict[str, Any], source: str = "reply") -> dict[str, Any]:
        """把解析出来的槽位合并进来，返回**真正被写入**的那些。

        三条规则：

        1. **只写空位，不覆盖已确定的值。** 想改已有值属于纠错，
           由调用方先清空对应字段再 apply —— 覆盖是个危险动作，
           不该在合并槽位时顺手发生。
        2. **空值不算数。** `{"origin": None}` 不会把 origin 抹掉，
           否则一次没解析出东西的回话会把上一轮的成果清零。
        3. **非用户直说的来源要留痕。** source != "reply" 时记进
           `carried_over`，渲染层据此把「沿用了什么」明确告诉用户。

        写完自动重算 missing —— 忘记重算是这条链路最容易漏的一步。
        """
        applied: dict[str, Any] = {}
        for key, value in (values or {}).items():
            if key not in SLOT_FIELDS or value in (None, "", []):
                continue
            if getattr(self, key, None):
                continue                      # 已有值：不覆盖
            setattr(self, key, value)
            applied[key] = value
            if source != "reply":
                self.carried_over[key] = source
            else:
                self.carried_over.pop(key, None)
        if applied:
            self.recompute_missing()
        return applied

    def clear_slots(self, keys) -> None:
        """清空指定槽位（纠错前置动作）。同时撤掉它们的沿用记录。"""
        for key in keys:
            if key in SLOT_FIELDS:
                setattr(self, key, None)
                self.carried_over.pop(key, None)
        self.recompute_missing()

    def is_terminal(self) -> bool:
        """是否已经走完——等待用户输入或确认时都【不是】终态。"""
        return self.current_step in (
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.OUT_OF_SCOPE)

    def awaits_user(self) -> bool:
        """是否在等用户回话（补参数或点确认）。多轮续跑时据此判断。"""
        return self.current_step in (
            TaskStatus.AWAITING_INPUT, TaskStatus.AWAITING_APPROVAL)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TravelState":
        """从 `to_dict()` 的产物还原。跨进程续跑靠它。

        三处容错，都是为真实的落盘文件准备的：

        - **未知字段直接忽略。** 旧版本存下的会话文件里可能有本版已删掉的
          字段。为此崩掉毫无意义——用户只是想接着上次聊。
        - **缺失字段走默认值。** 手写的、被截断的、半旧的 JSON 都能读。
        - **枚举认不出就退回 CREATED**，而不是抛异常。一个状态值坏掉时，
          「从头再来一遍」远好过「这个会话再也打不开了」。

        注意这里**不**做业务校验（比如"COMPLETED 却没有结果"）。
        还原就是还原，让坏数据以它本来的样子被看见，
        比在读取时悄悄修正它更容易查出问题出在哪一步。
        """
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}

        step = kwargs.get("current_step")
        if step is not None and not isinstance(step, TaskStatus):
            try:
                kwargs["current_step"] = TaskStatus(step)
            except ValueError:
                kwargs["current_step"] = TaskStatus.CREATED

        # 列表 / 字典字段：容忍 None 和错误类型，一律回落到空容器
        for key, empty in (("intents", []), ("missing", []), ("warnings", []),
                           ("errors", []), ("carried_over", {})):
            val = kwargs.get(key)
            if val is None or not isinstance(val, type(empty)):
                kwargs[key] = type(empty)(empty)

        return cls(**kwargs)
