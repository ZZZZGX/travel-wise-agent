# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""session.py —— 多轮会话与状态续跑（Multi-turn State Resume）。

## 在此之前，"缺参数就问"其实只做了一半

原实现能识别出缺什么、能把追问打印出来，但**问完就结束了**：

    用户：我想买张机票
    Agent：还需要你补充：出发城市、目的地城市、出行日期。
    用户：上海到成都，8月28号
    Agent：这条请求看起来不属于机票或目的地范围。   ← 每轮都从零开始

因为每次调用都重新走一遍 `route()`，而 route() 面对的是一句残句。
「问了但接不住」比不问更糟——它把一次可完成的任务变成了死循环。

本模块补上的正是接住的那一半：

    Session.send(text) → 判断这句话是"新任务"还是"对上一轮追问的回话"
                       → 是回话就解析成槽位、合并进已有 state、resume()
                       → 是确认就走 HITL 闸门 approve()

## 四条设计约束

1. **状态可落盘、可跨进程。** 会话存成 JSON，进程重启后 `--session <id>`
   能接着聊。只存内存的多轮，在 CLI 场景里等于没有多轮。

2. **沿用必须显式。** 上一个任务完成后，用户说「那从北京呢」，
   可以沿用上次的目的地和日期——但沿用来的每一项都记进
   `state.carried_over` 并在输出里列出来。静默继承是事故来源。

3. **歧义就问，不猜。** 出发地和目的地都缺、用户只回了「上海」，
   返回追问而不是按顺序硬塞。

4. **确认三态。** 「确认」执行，「算了」取消，**听不懂就再问一次**——
   不把听不懂折叠成任何一边。

## 不做什么

跨会话的长期记忆（"他一般从上海出发"）**不在这里**，也没有假装有。
那是 Memory 的职责，需要它自己的一套持久化与评测。本模块只负责
「同一个任务 / 同一段对话」内部的状态延续，边界清楚。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import slots
from .paths import ensure_cache_dir
from .state import TaskStatus, TravelState
from .tracing import Tracer

#: 新任务的信号词：出现这些，说明用户在开一件新事，而不是回答上一个问题
_NEW_TASK_HINT = re.compile(
    r"(机票|航班|好玩|玩的|景点|景区|攻略|推荐|帮我|我想|查一下|查下|有什么)")

#: 可以从上一个已完成任务沿用的槽位。**只有这三个**——
#: place 不沿用，因为「那从北京呢」里换的是航线，玩的地方多半也变了，
#: 而沿用一个不相干的 place 会安静地给出一份错的景区清单。
CARRYABLE = ("origin", "destination", "travel_date")


@dataclass
class Turn:
    """一轮对话记录。"""

    role: str                       # user | agent
    text: str
    at: str = ""
    kind: str = ""                  # new_task | slot_fill | approval | clarify
    state_step: str = ""
    trace_id: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "text": self.text, "at": self.at,
                "kind": self.kind, "state_step": self.state_step,
                "trace_id": self.trace_id}


@dataclass
class SessionResult:
    """一次 send() 的结果。"""

    state: TravelState
    reply: str = ""
    kind: str = "new_task"          # 这一轮被当成了什么
    resumed: bool = False           # 是否走的续跑路径（而非重新路由）
    slots_filled: dict = field(default_factory=dict)
    needs_user: bool = False
    trace_id: str = ""


class Session:
    """一段对话。持有 agent 与当前 state，负责判断"这句话是什么"。"""

    def __init__(self, agent, session_id: str = "", today: date | None = None,
                 carry_over: bool = True, tracer: Tracer | None = None,
                 want_reminder: bool = False):
        self.agent = agent
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.today = today or getattr(agent, "today", None) or date.today()
        self.carry_over = carry_over
        self.tracer = tracer or Tracer(enabled=False)
        self.want_reminder = want_reminder
        self.state: TravelState | None = None
        self.history: list[Turn] = []
        self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        #: 上一个**已完成**任务的槽位快照，供显式沿用
        self.last_completed: dict[str, Any] = {}

    # ------------------------------------------------------------------
    @property
    def turn_count(self) -> int:
        return sum(1 for t in self.history if t.role == "user")

    def _record(self, role: str, text: str, kind: str = "") -> None:
        self.history.append(Turn(
            role=role, text=text, kind=kind,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            state_step=(self.state.current_step.value if self.state else ""),
            trace_id=self.tracer.trace_id if self.tracer.enabled else ""))

    # ------------------------------------------------------------------
    def classify(self, text: str) -> str:
        """判断这句话该怎么接。返回 new_task | approval | slot_fill。

        判断顺序是刻意的：**先看 state 在等什么**，再看话本身像什么。
        反过来（先看话像不像新任务）会让「帮我查下上海到成都」这种
        既像新任务、又刚好补全了所有缺失槽位的回话被判成新任务，
        白白丢掉上一轮已经确定的意图。
        """
        state = self.state
        if state is None:
            return "new_task"
        if state.is_terminal():
            # 任务已收尾，但用户又说了句「确认」——多半是没看到上一条已经执行完了
            # （网络慢、消息重发、或者单纯手抖）。判成新任务的话，
            # 他会收到一句「这不属于机票或目的地范围」，
            # 比不回答更让人困惑。交给 approve() 去说「已经创建过了」。
            if state.pending_action and slots.parse_approval(text) is not None:
                return "approval"
            return "new_task"
        if state.current_step == TaskStatus.AWAITING_APPROVAL:
            # 等确认时，除非明显是另起一件事，否则都按确认解析
            if slots.parse_approval(text) is not None:
                return "approval"
            return "new_task" if _NEW_TASK_HINT.search(text or "") else "approval"
        if state.current_step == TaskStatus.AWAITING_INPUT:
            return "slot_fill"
        if state.current_step == TaskStatus.PARTIAL_COMPLETE and state.missing:
            return "slot_fill"
        return "new_task"

    # ------------------------------------------------------------------
    def send(self, text: str) -> SessionResult:
        """处理用户的一句话。"""
        text = (text or "").strip()
        kind = self.classify(text)
        self._record("user", text, kind)

        with self.tracer.span("session.send", kind="agent",
                              arguments={"turn": self.turn_count, "kind": kind}):
            if kind == "approval":
                result = self._handle_approval(text)
            elif kind == "slot_fill":
                result = self._handle_slot_fill(text)
            else:
                result = self._handle_new_task(text)

        self.state = result.state
        result.trace_id = self.tracer.trace_id if self.tracer.enabled else ""
        result.reply = self.agent.render(result.state)
        result.needs_user = result.state.awaits_user()
        if result.state.current_step == TaskStatus.COMPLETED:
            self.last_completed = {k: getattr(result.state, k)
                                   for k in CARRYABLE if getattr(result.state, k)}
        self._record("agent", result.reply, kind)
        return result

    # ------------------------------------------------------------------
    def _handle_new_task(self, text: str) -> SessionResult:
        state = self.agent.handle(text, want_reminder=self.want_reminder)
        state.session_id = self.session_id
        state.trace_id = self.tracer.trace_id if self.tracer.enabled else ""

        # 先看用户这句话里有没有**显式说到**的东西。
        # 试跑抓到的真实事故：上一个任务是「上海→成都」，用户接着说
        # 「不对，是从北京飞」——route() 抽不出完整航线，于是三个槽位全缺，
        # 沿用逻辑把「上海」原样填了回去，把用户刚说的「北京」**静默丢掉**。
        # 结论：显式提及永远压过沿用，且被显式提及的槽位不再参与沿用。
        explicit = {k: v for k, v in
                    slots.parse_correction(text, today=self.today).items()
                    if v and k in state.missing}
        if explicit:
            state.apply_slots(explicit, source="reply")

        # 显式沿用：只在**确实缺**且上一个任务里**确实有**的时候补，
        # 补完必须在 carried_over 里留痕并被渲染出来。
        fill = {}
        if self.carry_over and state.missing and self.last_completed:
            fill = {k: v for k, v in self.last_completed.items()
                    if k in state.missing and k in CARRYABLE}
            if fill:
                state.apply_slots(fill, source="carry_over")
        if explicit or fill:
            state = self.agent.dispatch(state, want_reminder=self.want_reminder)
        return SessionResult(state=state, kind="new_task",
                             slots_filled={**explicit, **fill})

    # ------------------------------------------------------------------
    def _handle_slot_fill(self, text: str) -> SessionResult:
        state = self.state
        assert state is not None

        parse = slots.parse_reply(text, state.missing, today=self.today,
                                  intents=state.intents)
        # 显式纠错优先：「不对，是从北京飞」既补也改
        correction = slots.parse_correction(text, today=self.today)

        if parse.ambiguous and not parse.slots and not correction:
            # 歧义不猜。原样停在 AWAITING_INPUT，只多问一句。
            state.warnings = [parse.question()]
            state.current_step = TaskStatus.AWAITING_INPUT
            return SessionResult(state=state, kind="clarify", resumed=True)

        if parse.unparsed and not correction:
            state.warnings = [
                "没听懂「%s」指的是什么。还缺：%s —— 直接写出来就行，"
                "比如「上海」「8月28号」。"
                % (text[:20], "、".join(state.missing) or "无")]
            state.current_step = TaskStatus.AWAITING_INPUT
            return SessionResult(state=state, kind="clarify", resumed=True)

        merged = dict(parse.slots)
        merged.update({k: v for k, v in correction.items() if v})
        applied = state.apply_slots(merged, source="reply")

        self.tracer.event("slot_fill", kind="agent",
                          arguments={"applied": applied, "evidence": parse.evidence},
                          attributes={"missing_after": list(state.missing)})

        state = self.agent.resume(state, want_reminder=self.want_reminder)
        return SessionResult(state=state, kind="slot_fill", resumed=True,
                             slots_filled=merged)

    # ------------------------------------------------------------------
    def _handle_approval(self, text: str) -> SessionResult:
        state = self.state
        assert state is not None
        decision = slots.parse_approval(text)

        if decision is None:
            # 听不懂 ≠ 拒绝，也 ≠ 同意。再问一次，状态原地不动。
            state.warnings = [
                "没听清是要创建还是不要。回「确认」我就创建，回「算了」就取消。"]
            state.current_step = TaskStatus.AWAITING_APPROVAL
            return SessionResult(state=state, kind="clarify", resumed=True)

        state.warnings = []
        state = self.agent.approve(state, approved=decision)
        return SessionResult(state=state, kind="approval", resumed=True)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "today": self.today.isoformat(),
            "carry_over": self.carry_over,
            "want_reminder": self.want_reminder,
            "last_completed": self.last_completed,
            "state": self.state.to_dict() if self.state else None,
            "history": [t.to_dict() for t in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict, agent) -> "Session":
        try:
            today = date.fromisoformat(data.get("today", ""))
        except (TypeError, ValueError):
            today = None
        session = cls(agent, session_id=data.get("session_id", ""), today=today,
                      carry_over=bool(data.get("carry_over", True)),
                      want_reminder=bool(data.get("want_reminder", False)))
        session.created_at = data.get("created_at", session.created_at)
        session.last_completed = dict(data.get("last_completed") or {})
        raw_state = data.get("state")
        session.state = TravelState.from_dict(raw_state) if raw_state else None
        session.history = [Turn(**{k: v for k, v in t.items()
                                   if k in Turn.__dataclass_fields__})
                           for t in (data.get("history") or [])]
        return session


class SessionStore:
    """会话落盘。一个会话一个 JSON 文件。

    为什么是文件而不是 sqlite：这个项目的运行时依赖是**零**，
    而多轮续跑的持久化需求就是"存一个 dict、按 id 读回来"。
    上数据库不会让它更对，只会让 clone 即用这件事变难。
    真要多用户并发时该换，但现在换就是提前优化。
    """

    def __init__(self, directory: str | Path | None = None):
        if directory is None:
            directory = ensure_cache_dir("sessions")
            self.dir = Path(directory) if directory else None
            return
        self.dir = Path(directory)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.dir = None          # 目录建不出来 → 明确表示不可用，不假装存上了

    def available(self) -> bool:
        return self.dir is not None

    def path_for(self, session_id: str) -> Path | None:
        if self.dir is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "")[:64]
        return self.dir / ("session-%s.json" % (safe or "unnamed"))

    def save(self, session: Session) -> Path | None:
        path = self.path_for(session.session_id)
        if path is None:
            return None
        try:
            path.write_text(json.dumps(session.to_dict(), ensure_ascii=False,
                                       indent=1, default=str), encoding="utf-8")
        except OSError:
            return None
        return path

    def load(self, session_id: str, agent) -> Session | None:
        path = self.path_for(session_id)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Session.from_dict(data, agent)

    def list_ids(self) -> list[str]:
        if self.dir is None or not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("session-*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            out.append(p.stem[len("session-"):])
        return out

    def resume_or_new(self, session_id: str, agent, **kwargs) -> Session:
        """有就接着聊，没有就新开。CLI 的 `--session` 走这条。"""
        existing = self.load(session_id, agent) if session_id else None
        if existing is not None:
            return existing
        return Session(agent, session_id=session_id, **kwargs)


def replay(session: Session) -> str:
    """把会话渲染成可读的对话记录。调试多轮问题时第一件要看的东西。"""
    lines = ["会话 %s ｜ %d 轮 ｜ 创建于 %s"
             % (session.session_id, session.turn_count, session.created_at),
             "-" * 60]
    for t in session.history:
        who = "用户" if t.role == "user" else "Agent"
        tag = ("［%s］" % t.kind) if t.kind and t.role == "user" else ""
        body = t.text if len(t.text) <= 400 else t.text[:400] + "…"
        lines.append("%s%s：%s" % (who, tag, body))
        lines.append("")
    if session.state:
        lines.append("当前状态：%s ｜ 还缺：%s"
                     % (session.state.current_step.value,
                        "、".join(session.state.missing) or "无"))
    return "\n".join(lines)
