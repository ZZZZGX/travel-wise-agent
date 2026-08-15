# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""ReminderProvider 的各种实现 —— 取代原先写死的平台专有 MCP 工具。

早期版本把某个终端系统的私有待办 / 闹钟工具名直接写进业务代码，
换个环境就完全不可用。现在改为：Core 只认 ReminderProvider 接口，
下面四个实现按环境挑一个用，都不可用时如实告知，绝不假装成功。

  ConsoleReminderProvider  打印到控制台。永远可用，Demo / CI 默认。
  ICSReminderProvider      写标准 .ics 日历文件（RFC 5545）。
                           Google/Apple/Outlook 日历都能直接导入——
                           这是真正跨平台的"写进日历"，不绑定任何厂商。
  JsonFileReminderProvider 追加到本地 JSON，便于程序化核对与测试断言。
  McpReminderProvider      通用 MCP 适配器：接收一个"调用工具"的可调用对象，
                           工具名可配置。任何 MCP Server（包括原平台的）
                           都能接进来，但 Core 不再依赖任何具体工具名。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .base import ReminderProvider, ReminderRequest, ReminderResult


class ConsoleReminderProvider(ReminderProvider):
    """把提醒打印出来。零依赖、永远可用，作为最终兜底。"""

    name = "console"

    def create(self, request: ReminderRequest) -> ReminderResult:
        text = ("🔔 [提醒] %s\n   时间：%s\n   备注：%s"
                % (request.title,
                   request.remind_at.strftime("%Y-%m-%d %H:%M"),
                   request.note or "-"))
        print(text)
        return ReminderResult(ok=True, provider=self.name,
                              message="提醒已输出到控制台（未写入任何外部系统）",
                              payload={"text": text})


def _ics_escape(text: str) -> str:
    """RFC 5545 要求转义的字符。"""
    return (text.replace("\\", "\\\\").replace(";", r"\;")
                .replace(",", r"\,").replace("\n", r"\n"))


class ICSReminderProvider(ReminderProvider):
    """生成标准 .ics 日历文件（RFC 5545）。

    为什么用它替代平台专有的闹钟/待办：.ics 是通用日历交换格式，
    Google Calendar、Apple 日历、Outlook 全都支持导入。
    "把购票日写进日历"这个真实需求，用一个开放标准就能满足，
    不需要绑定任何一家终端厂商的私有接口。
    """

    name = "ics"

    def __init__(self, output_dir: str = "data/cache/reminders",
                 tzid: str = "Asia/Shanghai"):
        self.output_dir = output_dir
        self.tzid = tzid

    def available(self) -> bool:
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            return os.access(self.output_dir, os.W_OK)
        except OSError:
            return False

    def create(self, request: ReminderRequest) -> ReminderResult:
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            stamp = request.remind_at.strftime("%Y%m%dT%H%M%S")
            # UID 用 uuid 而非时间戳：同一时刻创建多条提醒时，
            # 基于时间的 UID 会重复，日历客户端会把后一条当作前一条的更新而覆盖掉。
            uid = "travelwise-%s@travelwise.local" % uuid.uuid4()
            path = os.path.join(self.output_dir, "reminder-%s-%s.ics" % (stamp, uuid.uuid4().hex[:8]))
            # DTSTAMP 按规范用 UTC（带 Z）；DTSTART 用带 TZID 的本地时间，
            # 不写时区会被不同客户端按各自默认时区解释，导致提醒时间漂移。
            lines = [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//TravelWise//Reminder//CN",
                "CALSCALE:GREGORIAN",
                "BEGIN:VEVENT",
                "UID:%s" % uid,
                "DTSTAMP:%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "DTSTART;TZID=%s:%s" % (self.tzid, stamp),
                "SUMMARY:%s" % _ics_escape(request.title),
                "DESCRIPTION:%s" % _ics_escape(request.note or ""),
                "BEGIN:VALARM",
                "TRIGGER:PT0M",
                "ACTION:DISPLAY",
                "DESCRIPTION:%s" % _ics_escape(request.title),
                "END:VALARM",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\r\n".join(lines) + "\r\n")
            return ReminderResult(
                ok=True, provider=self.name, location=path,
                message="已生成日历文件，可导入 Google / Apple / Outlook 日历",
                payload={"path": path})
        except OSError as e:
            return ReminderResult(ok=False, provider=self.name,
                                  message="写入 .ics 文件失败：%s" % e)


class JsonFileReminderProvider(ReminderProvider):
    """追加写入本地 JSON。便于测试断言与程序化核对。"""

    name = "jsonfile"

    def __init__(self, path: str = "data/cache/reminders/reminders.json"):
        self.path = path

    def create(self, request: ReminderRequest) -> ReminderResult:
        record: dict[str, Any] = {
            "title": request.title,
            "remind_at": request.remind_at.isoformat(timespec="minutes"),
            "note": request.note,
            "important": request.important,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            existing: list = []
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as f:
                        existing = json.load(f) or []
                except (json.JSONDecodeError, OSError):
                    existing = []
            existing.append(record)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            return ReminderResult(ok=True, provider=self.name, location=self.path,
                                  message="提醒已写入本地文件", payload=record)
        except OSError as e:
            return ReminderResult(ok=False, provider=self.name,
                                  message="写入文件失败：%s" % e)


class McpReminderProvider(ReminderProvider):
    """通用 MCP 适配器。

    与原实现的关键差别：**不再把任何工具名写进 Core**。
    调用方注入一个 `tool_caller(tool_name, arguments) -> dict`，
    并通过 tool_name / argument_map 指定该 MCP Server 的具体工具与字段名。

    于是无论对接哪个 MCP Server —— 某终端系统的待办工具、别人的
    `calendar.create_event`、还是你自己写的 —— 都只是构造参数时的配置值。
    """

    name = "mcp"

    def __init__(self, tool_caller: Callable[[str, dict], Any],
                 tool_name: str = "create_reminder",
                 argument_map: dict[str, str] | None = None,
                 time_format: str = "iso",
                 success_predicate: Callable[[Any], bool] | None = None):
        self.tool_caller = tool_caller
        self.tool_name = tool_name
        # 针对特定 MCP Server 的成功判定。不给则走保守的默认规则（见 _interpret）
        self.success_predicate = success_predicate
        # 默认字段名走通用命名；接具体 Server 时按其 schema 改这个映射即可
        self.argument_map = argument_map or {
            "title": "title", "remind_at": "remind_at", "note": "note",
        }
        self.time_format = time_format

    def available(self) -> bool:
        return callable(self.tool_caller)

    def create(self, request: ReminderRequest) -> ReminderResult:
        if not self.available():
            return ReminderResult(ok=False, provider=self.name,
                                  message="未注入 MCP 工具调用器，无法创建提醒")
        when = (request.remind_at.isoformat(timespec="minutes")
                if self.time_format == "iso"
                else request.remind_at.strftime(self.time_format))
        m = self.argument_map
        args = {
            m.get("title", "title"): request.title,
            m.get("remind_at", "remind_at"): when,
        }
        if request.note and "note" in m:
            args[m["note"]] = request.note

        try:
            raw = self.tool_caller(self.tool_name, args)
        except Exception as e:                      # noqa: BLE001 —— 任何异常都要如实上报
            return ReminderResult(
                ok=False, provider=self.name,
                message="MCP 工具「%s」调用失败：%s" % (self.tool_name, e),
                payload={"tool": self.tool_name, "arguments": args})

        ok, reason = self._interpret(raw)
        return ReminderResult(
            ok=ok, provider=self.name,
            message=("MCP 工具「%s」调用成功" % self.tool_name) if ok
                    else ("MCP 工具「%s」未确认成功：%s" % (self.tool_name, reason)),
            payload={"tool": self.tool_name, "arguments": args, "response": raw})

    def _interpret(self, raw) -> tuple[bool, str]:
        """判定 MCP 返回是否代表成功。

        **默认失败（unknown 视为失败）。** 这是刻意的：不同 MCP Server 的返回
        结构五花八门，一旦默认成功，遇到没见过的结构就会向用户宣称"提醒已创建"，
        而实际上可能什么都没发生——这正是本项目最不能犯的错误。

        宁可误报失败让用户去核实，也不能误报成功让用户错过购票日。
        成功条件可通过 success_predicate 针对具体 Server 定制。
        """
        if self.success_predicate is not None:
            try:
                return bool(self.success_predicate(raw)), "自定义判定"
            except Exception as e:                  # noqa: BLE001
                return False, "自定义成功判定函数抛错：%s" % e

        # MCP 规范里的错误标记
        if isinstance(raw, dict):
            if raw.get("isError") or raw.get("error"):
                return False, "返回中带有错误标记：%s" % (raw.get("error") or "isError")
            for key in ("ok", "success"):
                if key in raw:
                    return bool(raw[key]), "字段 %s=%s" % (key, raw[key])
            if "result" in raw or "content" in raw:
                # MCP 标准返回：有 content 且未标记 isError，视为成功
                return True, "返回含 result/content 且未标记错误"
            return False, ("返回结构无法判定成功与否（%s）。请为该 Server 指定 "
                           "success_predicate，或确认其返回 schema。" % list(raw)[:5])

        if raw is None:
            return False, "工具无返回值"
        # 非 dict 返回（字符串/布尔等）同样无法可靠判定
        return False, "返回类型 %s 无法可靠判定，需指定 success_predicate" % type(raw).__name__


def resolve_reminder_provider(kind: str = "console", **kwargs) -> ReminderProvider:
    """按名字取一个提醒 Provider。未知名字回退到 console 并不报错——
    提醒失败不该让整个行程建议崩掉，但降级必须让用户看见（由上层输出）。"""
    kind = (kind or "console").lower()
    if kind == "ics":
        return ICSReminderProvider(**kwargs)
    if kind in ("json", "jsonfile", "file"):
        return JsonFileReminderProvider(**kwargs)
    if kind == "mcp":
        return McpReminderProvider(**kwargs)
    return ConsoleReminderProvider()
