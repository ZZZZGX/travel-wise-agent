# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""tracing.py —— 调用链追踪。

面试里几乎必问的一题是：「Agent 跑错了，你怎么定位？」
在这个模块存在之前，本项目的答案只能是"看 print 出来的最终回答"——
而最终回答恰恰是**最不可能告诉你哪里错了**的那个东西：
模型少写了一条链接、工具其实失败了但被含糊带过、第三轮才收敛，
这些在最终回答里全都看不出来。

所以这里落一条 span 流水，每一步都记：

    trace_id      一次运行的唯一标识，把散落的 span 串起来
    span_id/parent 父子关系，能还原成一棵树而不只是一串日志
    timestamp     开始时刻（ISO8601，带时区）
    duration_ms   耗时
    kind          agent | llm | tool | skill
    model         哪个模型（llm span）
    tool          哪个工具（tool span）
    arguments     **摘要**，不是原文（见下）
    status        ok | error | rejected
    tokens        输入 / 输出 / 合计
    cost          按 pricing 表换算的金额，带币种

## 为什么 arguments 只存摘要

两个理由，缺一不可：

  1. **凭证。** 参数里可能混进 token、Key。trace 是要贴进 issue、
     发给同事看的东西，把凭证写进去等于把它公开。这里做主动脱敏。
  2. **体积。** 一份价格矩阵参数几十 KB，全量落盘会让 trace 文件
     大到没人愿意打开——而没人打开的可观测性等于没有可观测性。

摘要保留**足够定位问题**的信息：键名齐全、值截断到可读长度、
长列表只留长度。判断"参数对不对"够用，复现原始请求不够——
后者应当去看业务日志，不该由 trace 承担。

## 落盘

JSONL（一行一个 span）。选它不是因为时髦，而是因为：
追加写不需要重写整个文件，进程被 Ctrl-C 掉也不会丢掉前面的记录——
**恰恰是跑挂了的那次运行最需要看 trace。**
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .llm.pricing import Cost, PriceTable, default_table

# --------------------------------------------------------------------------
# 参数摘要与脱敏
# --------------------------------------------------------------------------

#: 键名里出现这些词就整值打码。宁可多打，也不能漏一个。
_SECRET_HINTS = ("key", "token", "secret", "password", "passwd",
                 "authorization", "auth", "credential", "cookie")

#: 值本身长得像凭证时也打码 —— 有人会把 Key 塞进名字无害的字段里
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9\-_]{8,}|Bearer\s+[A-Za-z0-9\-._~+/]{12,}"
    r"|[A-Za-z0-9]{32,})")

MAX_VALUE_CHARS = 120
MAX_ITEMS = 8


def redact(value: Any) -> Any:
    """把可能是凭证的内容换成占位符。"""
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return "<redacted:%d chars>" % len(value)
    return value


def digest_arguments(args: Any, _depth: int = 0) -> Any:
    """把工具 / 模型参数压成可读、可贴、不含凭证的摘要。"""
    if _depth > 3:
        return "<nested>"
    if isinstance(args, dict):
        out: dict[str, Any] = {}
        for key, value in list(args.items())[:MAX_ITEMS * 2]:
            k = str(key)
            if k.startswith("_"):
                continue                       # 边带数据不进 trace
            if any(h in k.lower() for h in _SECRET_HINTS):
                out[k] = "<redacted>"
                continue
            out[k] = digest_arguments(value, _depth + 1)
        if len(args) > MAX_ITEMS * 2:
            out["…"] = "共 %d 个键" % len(args)
        return out
    if isinstance(args, (list, tuple)):
        if len(args) > MAX_ITEMS:
            return [digest_arguments(v, _depth + 1) for v in args[:MAX_ITEMS]] \
                + ["…共 %d 项" % len(args)]
        return [digest_arguments(v, _depth + 1) for v in args]
    if isinstance(args, str):
        value = redact(args)
        if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + "…（共 %d 字）" % len(value)
        return value
    if isinstance(args, (int, float, bool)) or args is None:
        return args
    return "<%s>" % type(args).__name__


# --------------------------------------------------------------------------
# Span
# --------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_REJECTED = "rejected"        # 被规则挡下（参数不合法 / 待人工确认）


@dataclass
class Span:
    trace_id: str = ""
    span_id: str = ""
    parent_id: str = ""
    name: str = ""
    kind: str = "tool"                 # agent | llm | tool | skill
    timestamp: str = ""                # ISO8601，UTC
    duration_ms: float = 0.0
    model: str = ""
    tool: str = ""
    arguments: Any = None              # 摘要，非原文
    status: str = STATUS_OK
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_amount: float | None = None
    cost_currency: str = ""
    cost_known: bool = True
    attributes: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self) -> Cost:
        return Cost(self.cost_amount, self.cost_currency, self.model,
                    known=self.cost_known)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Span":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# --------------------------------------------------------------------------
# Tracer
# --------------------------------------------------------------------------

class Tracer:
    """一次运行的追踪器。

    默认 **enabled=False**：不开 trace 时所有方法都是几乎零成本的空转，
    这样可以在 agent_loop / orchestrator 里无条件埋点，
    而不必到处写 `if self.tracer:`——那种写法迟早会漏掉一处。
    """

    def __init__(self, enabled: bool = False, sink: "TraceSink | None" = None,
                 trace_id: str = "", price_table: PriceTable | None = None,
                 metadata: dict | None = None):
        self.enabled = bool(enabled)
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.sink = sink
        self.spans: list[Span] = []
        self.price_table = price_table or default_table()
        self.metadata = dict(metadata or {})
        self._stack: list[str] = []
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------
    @property
    def current_parent(self) -> str:
        return self._stack[-1] if self._stack else ""

    def _emit(self, span: Span) -> Span:
        self.spans.append(span)
        if self.sink is not None:
            self.sink.write(span)
        return span

    @contextmanager
    def span(self, name: str, kind: str = "tool", *, tool: str = "",
             model: str = "", arguments: Any = None,
             attributes: dict | None = None) -> Iterator[Span]:
        """开一个 span。异常会被记成 error 状态后**原样重新抛出**——
        追踪不改变程序行为，这是可观测性的底线。
        """
        if not self.enabled:
            yield Span()                        # 空壳，写它没有副作用
            return

        span = Span(
            trace_id=self.trace_id, span_id=uuid.uuid4().hex[:12],
            parent_id=self.current_parent, name=name, kind=kind,
            tool=tool, model=model,
            arguments=digest_arguments(arguments) if arguments is not None else None,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            attributes=dict(attributes or {}))
        started = time.perf_counter()
        self._stack.append(span.span_id)
        try:
            yield span
        except Exception as e:                   # noqa: BLE001
            span.status = STATUS_ERROR
            span.error = "%s: %s" % (type(e).__name__, e)
            raise
        finally:
            self._stack.pop()
            span.duration_ms = round((time.perf_counter() - started) * 1000, 3)
            if span.model and (span.input_tokens or span.output_tokens):
                cost = self.price_table.cost(
                    span.model, span.input_tokens, span.output_tokens)
                span.cost_amount, span.cost_currency = cost.amount, cost.currency
                span.cost_known = cost.known
            self._emit(span)

    # ------------------------------------------------------------------
    def event(self, name: str, kind: str = "agent", **fields) -> Span:
        """记一个零时长的事件点（比如"进入等待用户确认"）。"""
        if not self.enabled:
            return Span()
        span = Span(trace_id=self.trace_id, span_id=uuid.uuid4().hex[:12],
                    parent_id=self.current_parent, name=name, kind=kind,
                    timestamp=datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds"),
                    **{k: v for k, v in fields.items()
                       if k in Span.__dataclass_fields__})
        return self._emit(span)

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        """汇总。给 CLI 末尾那一行，也给 trace viewer 的表头。"""
        llm = [s for s in self.spans if s.kind == "llm"]
        tools = [s for s in self.spans if s.kind == "tool"]
        total_cost = Cost(0.0, "", "", known=True)
        for s in llm:
            total_cost = total_cost + s.cost()
        return {
            "trace_id": self.trace_id,
            "spans": len(self.spans),
            "llm_calls": len(llm),
            "tool_calls": len(tools),
            "tool_errors": sum(1 for s in tools if s.status != STATUS_OK),
            "input_tokens": sum(s.input_tokens for s in self.spans),
            "output_tokens": sum(s.output_tokens for s in self.spans),
            "total_tokens": sum(s.total_tokens for s in self.spans),
            "cost": total_cost.to_dict(),
            "cost_text": total_cost.text(),
            "wall_ms": round((time.perf_counter() - self._t0) * 1000, 1),
            "metadata": self.metadata,
        }

    def close(self) -> None:
        if self.sink is not None:
            self.sink.close(self.summary())


# --------------------------------------------------------------------------
# Sink
# --------------------------------------------------------------------------

class TraceSink:
    """把 span 写到某处。基类什么都不做。"""

    def write(self, span: Span) -> None: ...

    def close(self, summary: dict) -> None: ...


class MemoryTraceSink(TraceSink):
    """只留在内存里，测试用。"""

    def __init__(self):
        self.spans: list[Span] = []
        self.summary: dict = {}

    def write(self, span: Span) -> None:
        self.spans.append(span)

    def close(self, summary: dict) -> None:
        self.summary = summary


class JsonlTraceSink(TraceSink):
    """JSONL 落盘。首行是 meta，尾行是 summary，中间一行一个 span。

    首尾各一行"非 span"的记录，是为了让 `view_trace.py` 不必先扫全文
    才能知道这次跑了多久、花了多少钱——**打开就能看到结论**。
    读取方按 `_type` 字段区分，未知类型直接跳过，向后兼容。
    """

    def __init__(self, path: str | Path, meta: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._write_raw({"_type": "meta", "created_at":
                         datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         **(meta or {})})

    def _write_raw(self, obj: dict) -> None:
        try:
            self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._fh.flush()             # 跑挂的那次最需要 trace，所以每行都刷
        except (OSError, ValueError):
            pass                          # 写不进去不影响主流程

    def write(self, span: Span) -> None:
        self._write_raw({"_type": "span", **span.to_dict()})

    def close(self, summary: dict) -> None:
        self._write_raw({"_type": "summary", **summary})
        try:
            self._fh.close()
        except OSError:
            pass


def load_trace(path: str | Path) -> tuple[dict, list[Span], dict]:
    """读回一份 trace，返回 (meta, spans, summary)。

    容忍坏行：写到一半被 Ctrl-C 的文件最后一行常常是残缺 JSON，
    这时候应该把前面完好的部分显示出来，而不是整份读不出来。
    """
    meta: dict = {}
    summary: dict = {}
    spans: list[Span] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = obj.get("_type")
            if kind == "meta":
                meta = obj
            elif kind == "summary":
                summary = obj
            elif kind == "span":
                spans.append(Span.from_dict(obj))
    return meta, spans, summary


# --------------------------------------------------------------------------

def build_tracer(enabled: bool, out_dir: str | Path | None = None,
                 metadata: dict | None = None) -> Tracer:
    """按开关造 tracer。enabled=False 时不产生任何文件。"""
    if not enabled:
        return Tracer(enabled=False)
    tracer = Tracer(enabled=True, metadata=metadata)
    if out_dir is None:
        from .paths import ensure_cache_dir
        out_dir = ensure_cache_dir("traces")
    if out_dir is None:
        return tracer                     # 目录不可写 → 只留内存，不报错
    path = Path(out_dir) / ("trace-%s-%s.jsonl" % (
        datetime.now().strftime("%Y%m%d-%H%M%S"), tracer.trace_id))
    tracer.sink = JsonlTraceSink(path, meta={"trace_id": tracer.trace_id,
                                             **(metadata or {})})
    tracer.metadata["path"] = str(path)
    return tracer


def env_trace_enabled() -> bool:
    return os.environ.get("TRAVELWISE_TRACE", "0") not in ("0", "", "false", "False")
