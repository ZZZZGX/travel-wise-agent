# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""ToolRegistry —— 把已有的 Python 能力暴露成 LLM 可调用的工具。

设计要点：

1. **不改动任何现有工具。** registry 是一层薄封装：负责声明 schema、校验参数、
   派发调用、把结果整理成模型能读的结构。tools/ 与 skills/ 一行未动。

2. **参数校验在进入业务代码之前。** 模型给的参数是不可信输入，缺字段、类型不对、
   枚举越界都要在这层挡住并返回结构化错误，让模型有机会自我修正——
   而不是把脏数据丢进业务逻辑里炸掉。

3. **有副作用的工具带 requires_approval 标记。** create_reminder 只会构造预览，
   **永不在工具层直接执行**。真正执行必须走 Orchestrator 的人工确认闸门。
   这保证了「接上 LLM 之后 HITL 依然成立」——模型无法绕过它。

4. **失败照实返回。** 工具执行失败返回 ok=False + error，不伪造数据。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import link_refs, price_analysis, price_matrix, table_refs


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]              # JSON Schema
    handler: Callable[..., dict]
    requires_approval: bool = False          # 有副作用 → 只能产出预览

    def to_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


@dataclass
class ToolResult:
    name: str
    ok: bool
    content: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    latency_ms: float = 0.0
    requires_approval: bool = False
    #: 失败的**种类**，不是失败的措辞。ok=True 时为空。
    #:   unknown_tool   叫了不存在的工具        —— 调用方错
    #:   bad_arguments  参数不合法              —— 调用方错
    #:   exception      handler 抛异常          —— 我们自己的代码错
    #:   tool_failed    handler 返回 ok=false   —— 外部依赖错
    #: 加这个字段是因为 tracing 需要区分「被规则挡下」和「真的坏了」，
    #: 而靠 error.startswith("参数不合法") 去认，等于让状态取决于提示语文案：
    #: 哪天有人把那句话改得更友好一点，trace 就悄悄开始误报。
    error_kind: str = ""

    def to_model_payload(self) -> dict[str, Any]:
        """回给模型的内容。失败时明确写成失败，不留想象空间。

        约定：content 里以下划线开头的键是**给代码用的边带数据**（例如链接
        记号表），不回给模型。这样 handler 可以同时产出"给模型看的"和
        "给渲染层用的"两份东西，而不必另开一条通道。
        """
        if not self.ok:
            return {"ok": False, "error": self.error,
                    "note": "工具调用失败。请如实告知用户失败原因，不要编造数据。"}
        payload = {"ok": True,
                   **{k: v for k, v in self.content.items()
                      if not str(k).startswith("_")}}
        if self.requires_approval:
            payload["note"] = ("这是待确认的操作预览，尚未执行。"
                               "必须先向用户展示预览并获得确认。")
        return payload


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------

class ToolArgumentError(ValueError):
    """模型给的参数不合法。"""


_TYPES = {"string": str, "integer": int, "number": (int, float),
          "boolean": bool, "array": list, "object": dict}


def validate_arguments(schema: dict, args: dict) -> dict:
    """按 JSON Schema 校验并归一化参数。只支持项目实际用到的子集。"""
    if not isinstance(args, dict):
        raise ToolArgumentError("参数必须是对象，实得 %s" % type(args).__name__)

    props = schema.get("properties") or {}
    required = schema.get("required") or []
    cleaned: dict[str, Any] = {}

    missing = [k for k in required if k not in args or args[k] in (None, "")]
    if missing:
        raise ToolArgumentError("缺少必需参数：%s" % "、".join(missing))

    for key, value in args.items():
        if key not in props:
            continue                                  # 多余参数忽略，不报错
        spec = props[key]
        if value is None:
            continue
        expected = _TYPES.get(spec.get("type", "string"), str)
        if spec.get("type") == "integer" and isinstance(value, str) and value.isdigit():
            value = int(value)                        # 模型常把整数写成字符串
        if not isinstance(value, expected):
            raise ToolArgumentError(
                "参数 %s 类型应为 %s，实得 %s" % (key, spec.get("type"), type(value).__name__))
        if spec.get("enum") and value not in spec["enum"]:
            raise ToolArgumentError(
                "参数 %s 只能是 %s 之一，实得 %r" % (key, spec["enum"], value))
        if spec.get("type") == "integer":
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolArgumentError("参数 %s 不得小于 %s" % (key, spec["minimum"]))
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolArgumentError("参数 %s 不得大于 %s" % (key, spec["maximum"]))
        cleaned[key] = value
    return cleaned


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

class ToolRegistry:
    """工具注册表：声明 → 校验 → 派发。"""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def to_schemas(self) -> list[dict[str, Any]]:
        """给 LLMClient 的工具声明。"""
        return [t.to_schema() for t in self._tools.values()]

    def call(self, name: str, arguments: dict) -> ToolResult:
        """执行一次工具调用。任何异常都转成 ok=False，不向上抛。"""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(name=name, ok=False, error_kind="unknown_tool",
                              error="不存在的工具「%s」。可用工具：%s"
                                    % (name, "、".join(self.names())))
        started = time.perf_counter()
        try:
            cleaned = validate_arguments(spec.parameters, arguments or {})
            content = spec.handler(**cleaned)
        except ToolArgumentError as e:
            return ToolResult(name=name, ok=False, error="参数不合法：%s" % e,
                              error_kind="bad_arguments",
                              latency_ms=(time.perf_counter() - started) * 1000)
        except Exception as e:                        # noqa: BLE001
            return ToolResult(name=name, ok=False, error_kind="exception",
                              error="%s: %s" % (type(e).__name__, e),
                              latency_ms=(time.perf_counter() - started) * 1000)

        latency = (time.perf_counter() - started) * 1000
        ok = bool(content.get("ok", True)) if isinstance(content, dict) else True
        return ToolResult(name=name, ok=ok,
                          content=content if isinstance(content, dict) else {"result": content},
                          error="" if ok else str(content.get("error") or "工具返回失败"),
                          error_kind="" if ok else "tool_failed",
                          latency_ms=latency,
                          requires_approval=spec.requires_approval)


# --------------------------------------------------------------------------
# 三个工具的 schema 与 handler
# --------------------------------------------------------------------------

_SEARCH_FLIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "origin": {"type": "string", "description": "出发城市中文名，如「上海」"},
        "destination": {"type": "string", "description": "到达城市中文名，如「成都」"},
        "travel_date": {"type": "string",
                        "description": "出行日期，必须是 YYYY-MM-DD。相对时间请先换算再传入"},
        "days": {"type": "integer", "minimum": 0, "maximum": 45,
                 "description": ("要横向对比的天数。填 >0 会扫描未来这么多天，"
                                 "输出「每个航班 × 每个出发日」的价格矩阵（推荐 14）。"
                                 "用户问「哪天买便宜」「各航班价格怎么变」时应当填。"
                                 "注意：每天消耗 1 次查询额度，不要随意填大值")},
        "direct_only": {"type": "boolean", "description": "只看直飞。用户明确说直飞时才填 true"},
    },
    "required": ["origin", "destination", "travel_date"],
}

_SEARCH_DESTINATION_SCHEMA = {
    "type": "object",
    "properties": {
        "place": {"type": "string", "description": "想去玩的地名。注意可能与航班到达城市不同"},
        "scope": {"type": "string", "enum": ["city", "province"],
                  "description": ("检索范围。用户说城市就填 city，说省/自治区才填 province。"
                                  "严禁因为某城市景点少就自行改成 province")},
        "travel_month": {"type": "integer", "minimum": 1, "maximum": 12,
                         "description": "出行月份，用于季节匹配标注。不确定就不要传"},
    },
    "required": ["place", "scope"],
}

_CREATE_REMINDER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "提醒标题，如「购买 上海→成都 机票」"},
        "remind_date": {"type": "string", "description": "提醒日期 YYYY-MM-DD"},
        "remind_time": {"type": "string", "description": "提醒时间 HH:MM，默认 09:00"},
        "note": {"type": "string", "description": "备注，说明为什么建议这天买"},
    },
    "required": ["title", "remind_date"],
}


def build_registry(flight_skill, destination_skill, today: date | None = None,
                   default_days: int = 0, request_interval: float = 0.0) -> ToolRegistry:
    """按已有 skills 组装 registry。

    注意 handler 只是转调 skills —— 业务逻辑完全复用，没有第二套实现。
    """
    today = today or date.today()
    registry = ToolRegistry()

    def search_flights(origin: str, destination: str, travel_date: str,
                       days: int = 0, direct_only: bool = False) -> dict:
        # 模型没指定天数时用部署方设定的默认值（TRAVELWISE_MATRIX_DAYS）。
        # 额度是钱，这个上限该由部署方定，不该由模型临场发挥。
        result = flight_skill.run(origin, destination, travel_date, today=today,
                                  matrix_days=(days or default_days),
                                  direct_only=direct_only,
                                  sleep_between=request_interval)
        # 只把模型需要的部分回传，避免把整份航班明细塞进上下文烧 token
        payload = {
            "ok": result["ok"], "route": result["route"],
            "travel_date": result["travel_date"], "mode": result["mode"],
            "flight_count": len(result.get("flights") or []),
            "report": result["text"],
        }

        # 价格矩阵模式：表格换记号，只给模型一份结构化摘要。
        # 理由同 link_refs —— 600 个价格数字是确定性数据，不该让模型誊写。
        matrix = result.get("matrix")
        if matrix is not None:
            builder = table_refs.TableRefBuilder()
            ref = builder.add(result["matrix_text"])
            payload["report"] = (
                "完整价格矩阵已生成，用记号 %s 表示。把 %s 原样写进回答里，"
                "系统会在展示给用户前替换成完整表格。**不要自己复述表格里的数字。**" % (ref, ref))
            payload["matrix"] = price_matrix.digest(matrix)
            payload["table_ref"] = ref
            payload["_table_map"] = builder.mapping
        if result.get("error"):
            payload["error"] = result["error"]
        analysis = result.get("analysis") or {}
        if analysis.get("ok"):
            chosen = price_analysis.pick_recommendation(analysis) or {}
            payload["recommended_buy_date"] = chosen.get("recommended_buy_date")
            payload["reference_price"] = chosen.get("cheapest_scan_price")
            payload["buy_date_method"] = analysis.get("primary_method")
            payload["price_basis"] = analysis.get("basis_label")
            payload["warnings"] = analysis.get("warnings") or []
            con = analysis.get("consensus")
            if con:
                # 让模型能说出"25/34 班在这天见底"，而不是只报一个日期
                payload["per_flight_consensus"] = {
                    "day": con["cheapest_scan_date"], "agree": con["agree"],
                    "total": con["total"], "median_saving": con.get("median_saving")}
        return payload

    def search_destination(place: str, scope: str, travel_month: int | None = None) -> dict:
        result = destination_skill.run(place, scope=scope, travel_month=travel_month,
                                       today=today)
        # URL 换成 [L1] 记号后再回给模型：一条链接从 ~70 token 降到 ~3 token。
        # 真实 URL 放进 _link_map（下划线开头 = 不回给模型），
        # 由 agent_loop 在最终回答里换回来。
        masked, link_map = link_refs.mask(result["text"] or "")
        payload = {
            "ok": result["ok"], "place": result["place"], "scope": result["scope"],
            "official_count": result.get("official_count", 0),
            # 让模型能说清「这些地点是搜出来的还是名录里的」，
            # 以及二层没启用时**不要假装**做过发现。
            "discovered_count": result.get("discovered_count", 0),
            "discovery_enabled": result.get("discovery_enabled", False),
            "report": masked,
            "link_ref_note": ("报告中的 [L1] [L2] 是链接引用记号。"
                              "需要给出链接时原样写出记号即可，"
                              "系统会在展示给用户前替换成真实网址。"
                              "记号必须逐条给全，且不得编造不存在的记号。"),
            "_link_map": link_map,
        }
        if not result.get("discovery_enabled") and result.get("discovery_reason"):
            payload["discovery_notice"] = result["discovery_reason"]
        if result.get("notice"):
            payload["notice"] = result["notice"]
        if result.get("error"):
            payload["error"] = result["error"]
        return payload

    def create_reminder(title: str, remind_date: str,
                        remind_time: str = "09:00", note: str = "") -> dict:
        """只构造预览，**不执行**。真正写入必须经 Orchestrator 的人工确认闸门。"""
        return {
            "ok": True, "executed": False, "status": "pending_approval",
            "preview": ("准备创建提醒，请确认：\n  标题：%s\n  时间：%s %s\n  备注：%s"
                        % (title, remind_date, remind_time, note or "-")),
            "request": {"title": title, "remind_date": remind_date,
                        "remind_time": remind_time, "note": note},
        }

    registry.register(ToolSpec(
        name="search_flights",
        description=("查询某航线某出行日的航班，并在数据源含票价时给出"
                     "「提前几天买最便宜」的分析与建议购票日。"
                     "参数不全时不要瞎填，先问用户。"),
        parameters=_SEARCH_FLIGHTS_SCHEMA, handler=search_flights))

    registry.register(ToolSpec(
        name="search_destination",
        description=("按城市或省份检索官方 A 级景区名录，返回景区清单、"
                     "搜索入口与季节匹配标注。"
                     "scope 必须忠实反映用户说的范围，不得自行扩大。"),
        parameters=_SEARCH_DESTINATION_SCHEMA, handler=search_destination))

    registry.register(ToolSpec(
        name="create_reminder",
        description=("为建议购票日创建提醒。**该操作有副作用**："
                     "本工具只生成待确认的预览，不会真正写入；"
                     "必须把预览展示给用户并获得明确确认后才能执行。"),
        parameters=_CREATE_REMINDER_SCHEMA, handler=create_reminder,
        requires_approval=True))

    return registry
