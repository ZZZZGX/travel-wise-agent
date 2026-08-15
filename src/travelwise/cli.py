# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""cli.py —— 命令行入口。

    python -m travelwise "月底从上海飞成都，四川有什么好玩的"
    python -m travelwise --demo
    python -m travelwise "下周去大连" --reminder --reminder-provider ics

默认 mock 数据源：不需要任何 API Key 就能跑通完整 Agent 流程。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import (Settings, build_flight_provider, build_llm_client,
                     build_reminder_provider, build_router,
                     build_web_search_provider)
from .orchestrator import TravelWiseAgent

def _build_destination_skill(settings: Settings):
    """按配置造目的地技能。搜索源没配就是 Null——二层如实标记未启用。"""
    from .skills.destination import DestinationSkill
    return DestinationSkill(build_web_search_provider(settings),
                            scenes=settings.scene_list(),
                            min_mentions=settings.search_min_mentions)


DEMO_CASES = [
    ("单意图 · 机票", "8月28号从上海飞成都，机票什么时候买划算", False),
    ("单意图 · 目的地", "沈阳有什么好玩的", False),
    ("双意图 + 范围区分", "8月28号飞乌鲁木齐，新疆有什么玩的", False),
    ("缺参数 → 应追问", "我想买张机票", False),
    ("副作用 → 需确认", "8月28号从北京飞广州，帮我设个购票提醒", True),
    ("超范围 → 应拒绝", "帮我订个酒店", False),
]


def _confirm(preview: str) -> bool:
    """交互式确认。非交互环境（管道 / CI）一律视为未确认——不默认执行副作用。"""
    print("\n" + preview)
    if not sys.stdin.isatty():
        print("（非交互环境，默认不执行）")
        return False
    try:
        return input("确认创建？[y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def build_agent(args) -> TravelWiseAgent:
    settings = Settings.from_env()
    if args.provider:
        settings.flight_provider = args.provider
    if args.reminder_provider:
        settings.reminder_provider = args.reminder_provider

    approval = None
    if args.reminder:
        approval = (lambda _p: True) if args.yes else _confirm

    from .skills.destination import DestinationSkill
    agent = TravelWiseAgent(
        tracer=_make_tracer(args, "orchestrator"),
        flight_provider=build_flight_provider(settings),
        destination_skill=_build_destination_skill(settings),
        matrix_days=(settings.matrix_days if getattr(args, "days", None) is None
                     else args.days),
        request_interval=settings.request_interval,
        reminder_provider=build_reminder_provider(settings),
        approval_callback=approval,
        today=date.fromisoformat(args.today) if args.today else None,
    )
    # --router llm 时把编排器的路由换成 LLM 版；规则路由仍是默认与降级路径
    if getattr(args, "router", "rule") == "llm":
        settings.router = "llm"
        agent.router = build_router(settings)
    return agent


def _make_tracer(args, mode: str):
    """按 `--trace` / 环境变量造 tracer。没开就是空转，不建目录也不写文件。"""
    from .tracing import build_tracer, env_trace_enabled
    enabled = bool(getattr(args, "trace", False)) or env_trace_enabled()
    return build_tracer(enabled, metadata={
        "mode": mode,
        "request": (getattr(args, "request", "") or "")[:120],
        "today": getattr(args, "today", "") or "",
    })


def _report_trace(tracer) -> None:
    """把这次的 trace 落在哪、里面有什么，一行说清。

    不打印这一行，等于做了可观测性但没告诉任何人怎么看——
    上一版 `tracing.py` 写好了却没人调，就是这么发生的。
    """
    if tracer is None or not tracer.enabled:
        return
    s = tracer.summary()
    tracer.close()
    print("\n[trace] %s ｜ %d span ｜ 工具 %d（失败 %d）｜ token %d ｜ %s ｜ %.0fms"
          % (s["trace_id"], s["spans"], s["tool_calls"], s["tool_errors"],
             s["total_tokens"], s["cost_text"], s["wall_ms"]))
    path = tracer.metadata.get("path")
    if path:
        print("        %s" % path)
        print("        看图：python scripts/view_trace.py --latest --open")


def _report_cost(agent) -> None:
    """把这次花了多少钱说清楚。付费接口不该让人事后翻账单才知道。"""
    provider = getattr(agent, "flight_skill", None)
    provider = getattr(provider, "provider", None)
    if provider is None:
        return
    if hasattr(provider, "stats"):                       # 缓存层
        print("\n[缓存] " + provider.stats())
        provider = getattr(provider, "inner", None)
    if hasattr(provider, "cost_report"):                 # 容错链
        print("[额度] " + provider.cost_report())


def _maybe_export(args, state) -> None:
    """把矩阵导成 csv / xlsx / html。**全程零 token**，模型不参与。"""
    fmt = getattr(args, "export", None)
    if not fmt:
        return
    result = getattr(state, "flight_result", None) or {}
    matrix = result.get("matrix")
    if matrix is None:
        print("\n[导出] 本次没有价格矩阵可导出（--days 为 0，或机票查询未成功）。")
        return
    from .tools import matrix_export
    from .paths import ensure_cache_dir
    if args.export_path:
        path = args.export_path
    else:
        out_dir = ensure_cache_dir("exports")
        if out_dir is None:
            print("\n[导出] 缓存目录不可写，请用 --export-path 指定位置。")
            return
        path = matrix_export.default_path(matrix, fmt, out_dir)
    print("\n[导出] 已生成：%s" % matrix_export.export(matrix, fmt, path))


def run_demo(args) -> None:
    print("=" * 68)
    print("TravelWise Demo —— 数据源：%s（无需 API Key）" % (args.provider or "mock"))
    print("=" * 68)
    for i, (label, request, want_reminder) in enumerate(DEMO_CASES, 1):
        agent = build_agent(args)
        print("\n\n【场景 %d / %s】用户：%s" % (i, label, request))
        print("-" * 68)
        state = agent.handle(request, want_reminder=want_reminder)
        print("意图=%s  范围=%s  缺失=%s"
              % (state.intents or "无", state.scope or "-", state.missing or "无"))
        print()
        print(TravelWiseAgent.render(state))


def _run_agent_loop(args) -> int:
    """LLM 自主选工具的闭环模式。"""
    from .agent_loop import ToolCallingAgent
    from .skills.destination import DestinationSkill
    from .skills.flight import FlightSkill
    from .tools.registry import build_registry

    settings = Settings.from_env()
    if args.provider:
        settings.flight_provider = args.provider
    today = date.fromisoformat(args.today) if args.today else date.today()

    registry = build_registry(
        FlightSkill(build_flight_provider(settings, today=today)),
        _build_destination_skill(settings),
        today=today,
        default_days=(settings.matrix_days if getattr(args, "days", None) is None
                      else args.days),
        request_interval=settings.request_interval)
    client = build_llm_client(settings)
    if getattr(client, "is_synthetic", False):
        print("⚠️  当前使用离线回放的合成响应，仅用于验证链路；"
              "真实效果请配置 TRAVELWISE_LLM_PROVIDER 与 API Key。\n")

    tracer = _make_tracer(args, "agent-loop")
    result = ToolCallingAgent(client, registry, today=today, tracer=tracer).run(
        args.request)
    print(result.answer)
    if result.tool_names:
        print("\n[调用工具] " + " → ".join(result.tool_names))
    print("[轮次] %d ｜ [Token] %d ｜ [耗时] %.0fms"
          % (len(result.turns), result.usage.total, result.latency_ms))
    if result.pending_approval:
        print("[待确认] %d 项副作用操作等待你确认后才会执行" % len(result.pending_approval))
    _report_trace(tracer)
    return 0 if result.ok else 1


def _run_replay(args) -> int:
    from .session import SessionStore, replay
    store = SessionStore()
    if not store.available():
        print("会话目录不可用（无法写入 data/cache/sessions/），没有可回放的记录。")
        return 1
    session = store.load(args.replay, build_agent(args))
    if session is None:
        ids = store.list_ids()
        print("找不到会话「%s」。" % args.replay)
        if ids:
            print("现有会话：%s" % "、".join(ids[:10]))
        return 1
    print(replay(session))
    return 0


def _run_session(args) -> int:
    """多轮会话。`--session ID` 单发一句，`--chat` 进交互。

    单发也要落盘：CLI 场景里「上午问一半，下午接着聊」是常态，
    只存内存的多轮在这里等于没有多轮。
    """
    from .session import Session, SessionStore

    agent = build_agent(args)
    store = SessionStore()
    if not store.available():
        # 存不了就明说，而不是跑一半发现记录没了
        print("⚠️  会话目录不可写，本次对话不会被保存（退出即丢）。\n")

    session_id = args.session or ""
    if store.available() and session_id:
        session = store.resume_or_new(session_id, agent, want_reminder=args.reminder)
    else:
        session = Session(agent, session_id=session_id, want_reminder=args.reminder)

    def one(text: str) -> None:
        result = session.send(text)
        print(result.reply)
        if store.available():
            store.save(session)

    if not args.chat:
        if not args.request:
            print("用 --session 时需要给一句话，或者加 --chat 进交互模式。")
            return 1
        one(args.request)
        if session.state and session.state.awaits_user():
            print("\n（等你回话。接着用：travelwise --session %s \"<你的回答>\"）"
                  % session.session_id)
        _report_trace(agent.tracer)
        return 0

    print("多轮会话 %s —— 输入 exit / quit 退出。" % session.session_id)
    if args.request:
        print("\n你：%s" % args.request)
        one(args.request)
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in {"exit", "quit", "q"}:
            break
        if not text:
            continue
        one(text)
    if store.available():
        print("\n会话已保存：travelwise --session %s 可继续，--replay %s 可回看。"
              % (session.session_id, session.session_id))
    _report_trace(agent.tracer)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="travelwise", description="TravelWise —— 出行决策 Agent（机票时机 + 目的地策展）")
    p.add_argument("request", nargs="?", help="自然语言出行请求")
    p.add_argument("--demo", action="store_true", help="跑内置场景演示（含失败与边界用例）")
    p.add_argument("--provider", choices=["mock", "http"], help="航班数据源（默认 mock）")
    p.add_argument("--reminder", action="store_true", help="需要购票提醒（会先预览并请求确认）")
    p.add_argument("--reminder-provider", choices=["console", "ics", "json"],
                   help="提醒落地方式（默认 console）")
    p.add_argument("--yes", action="store_true", help="自动确认副作用操作（仅供演示 / 测试）")
    p.add_argument("--days", type=int, default=None,
                   help=("扫描未来 N 天并输出「每航班 × 每出发日」价格矩阵；"
                         "每天消耗 1 次查询额度。默认读 TRAVELWISE_MATRIX_DAYS（7）。0 = 关闭矩阵"))
    p.add_argument("--export", choices=["csv", "xlsx", "html"],
                   help="把价格矩阵另存为文件（零 token，由代码直接生成）")
    p.add_argument("--export-path", help="导出文件路径，默认写到 data/cache/exports/")
    p.add_argument("--today", help="覆盖“今天”，格式 YYYY-MM-DD（便于复现与测试）")
    p.add_argument("--json", action="store_true", help="输出结构化 state 而非文本")
    p.add_argument("--router", choices=["rule", "llm"], default="rule",
                   help="路由方式：rule（默认，零成本基线）| llm（function calling）")
    p.add_argument("--agent-loop", action="store_true",
                   help="用 LLM 自主选工具的 Tool Calling 循环，替代固定编排")
    p.add_argument("--session", metavar="ID",
                   help="多轮会话 ID。同一个 ID 可跨进程接着聊（状态落盘到 data/cache/sessions/）")
    p.add_argument("--chat", action="store_true",
                   help="进入交互式多轮对话（输入 exit 退出）")
    p.add_argument("--trace", action="store_true",
                   help="记录调用链 trace（JSONL，写到 data/cache/traces/）")
    p.add_argument("--replay", metavar="ID",
                   help="打印某个会话的完整对话记录，然后退出")
    args = p.parse_args(argv)

    if args.demo:
        run_demo(args)
        return 0
    if args.replay:
        return _run_replay(args)
    if args.session or args.chat:
        return _run_session(args)
    if not args.request:
        p.print_help()
        return 1

    if args.agent_loop:
        return _run_agent_loop(args)

    agent = build_agent(args)
    state = agent.handle(args.request, want_reminder=args.reminder)

    if args.json:
        import json
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(TravelWiseAgent.render(state))

    _report_cost(agent)
    _maybe_export(args, state)
    _report_trace(agent.tracer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
