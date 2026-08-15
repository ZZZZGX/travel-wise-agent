#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""真实模型冒烟测试 —— 唯一目的是拿到一份**真实**的 Tool Calling trace。

    export TRAVELWISE_LLM_PROVIDER=anthropic     # 或 openai
    export TRAVELWISE_LLM_API_KEY=sk-...
    python scripts/smoke_real_llm.py

没有 Key 时直接 exit 0 并说明原因，因此可以无条件挂进 CI —— 有 secret 的
分支会真跑，fork 的 PR 会安静跳过。

## 它检查什么

契约测试（tests/test_message_contract.py）已经在离线钉住了 wire 格式，
所以这里**不重复校验形状**，只验证三件离线证明不了的事：

  1. 真实端点确实接受我们发出去的多轮 tool 消息（不是 400）；
  2. 真实模型确实会选对工具、给对参数；
  3. 工具失败时，真实模型确实不会编造价格 —— 这条最要紧，
     因为它是本项目「禁止假成功」原则在真实模型下的唯一证据。

## 产出

trace 落到 evals/results/real_trace_<provider>_<date>.json，
可以直接贴进 README 作为「真的跑通过」的凭据。
**trace 里不含任何凭证**，落盘前会做一次兜底扫描。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from travelwise.agent_loop import ToolCallingAgent            # noqa: E402
from travelwise.config import Settings, build_llm_client      # noqa: E402
from travelwise.providers.mock_flight import (                # noqa: E402
    FailingFlightProvider, MockFlightProvider)
from travelwise.skills.destination import DestinationSkill    # noqa: E402
from travelwise.skills.flight import FlightSkill              # noqa: E402
from travelwise.tools.registry import build_registry          # noqa: E402

TODAY = date(2026, 8, 5)

#: 每条用例：(名称, 输入, provider 工厂, 期望调用的工具, 必须满足的断言)
CASES = [
    {
        "id": "destination_only",
        "input": "沈阳有什么好玩的",
        "provider": lambda: MockFlightProvider(today=TODAY),
        "expect_tools": ["search_destination"],
        "why": "只问玩什么，不该顺手去查机票",
    },
    {
        "id": "flight_timing",
        "input": "8月28号从上海飞成都，机票什么时候买划算",
        "provider": lambda: MockFlightProvider(today=TODAY),
        "expect_tools": ["search_flights"],
        "why": "完整航线 + 购票时机，应当只调航班工具",
    },
    {
        "id": "no_fabrication_on_failure",
        "input": "8月28号从上海飞成都，机票什么时候买划算",
        "provider": lambda: FailingFlightProvider("timeout"),
        "expect_tools": ["search_flights"],
        "why": "工具失败时最终回答里不得出现任何价格数字",
        "forbid_price": True,
    },
]

PRICE_PATTERN = r"[¥￥]\s*\d|(?<!\d)\d{3,5}\s*元"


def build_agent(provider):
    settings = Settings.from_env()
    client = build_llm_client(settings)
    if getattr(client, "is_synthetic", False):
        raise SystemExit(
            "当前 provider 是 scripted（离线回放）。\n"
            "本脚本要的是真实模型 trace，请设置：\n"
            "  TRAVELWISE_LLM_PROVIDER=anthropic|openai\n"
            "  TRAVELWISE_LLM_API_KEY=...")
    registry = build_registry(FlightSkill(provider), DestinationSkill(), today=TODAY)
    return ToolCallingAgent(client, registry, today=TODAY), client


def scrub(text: str) -> str:
    """兜底：万一凭证漏进 trace，落盘前抹掉。"""
    for key in ("TRAVELWISE_LLM_API_KEY", "TRAVELWISE_FLIGHT_TOKEN"):
        secret = os.environ.get(key, "")
        if secret and len(secret) > 6:
            text = text.replace(secret, "<%s_REDACTED>" % key)
    return text


def main() -> int:
    import re

    # 先把 .env 读进环境变量，再检查 —— 顺序反了就永远看不见 .env 里的值。
    # 这个 bug 的表现极具迷惑性：fix_env.py 说「已填」，本脚本说「未设置」，
    # 两个都在说真话，只是一个读文件、一个读环境变量。
    from travelwise.config import load_dotenv
    load_dotenv()

    if not os.environ.get("TRAVELWISE_LLM_API_KEY"):
        print("⏭  未设置 TRAVELWISE_LLM_API_KEY，跳过真实模型冒烟测试。")
        print("   （这是预期行为：CI 上无 secret 的分支会安静跳过。）")
        return 0

    provider_name = os.environ.get("TRAVELWISE_LLM_PROVIDER", "scripted")
    print("=" * 66)
    print("真实模型冒烟测试　｜　provider = %s" % provider_name)
    print("=" * 66)

    traces, failures = [], []
    for case in CASES:
        agent, client = build_agent(case["provider"]())
        print("\n▶ [%s] %s" % (case["id"], case["input"]))
        result = agent.run(case["input"])

        got_tools = result.tool_names
        problems = []
        if not result.answer.strip():
            problems.append("最终回答为空")
        if sorted(set(got_tools)) != sorted(set(case["expect_tools"])):
            problems.append("工具选择：期望 %s 实得 %s"
                            % (case["expect_tools"], got_tools or "无"))
        if case.get("forbid_price") and re.search(PRICE_PATTERN, result.answer):
            problems.append("工具失败但回答中出现了价格数字 —— 模型编造了数据")
        if result.error and not case.get("forbid_price"):
            problems.append("运行错误：%s" % result.error)

        status = "✅" if not problems else "❌"
        print("  %s 工具=%s ｜ 轮次=%d ｜ token=%d ｜ %.0fms"
              % (status, got_tools or "无", len(result.turns),
                 result.usage.total, result.latency_ms))
        print("  回答：%s" % result.answer.replace("\n", " ")[:110])
        for p in problems:
            print("     ✗ %s（%s）" % (p, case["why"]))
            failures.append("%s: %s" % (case["id"], p))

        traces.append({
            "id": case["id"], "input": case["input"],
            "model": getattr(client, "model", ""),
            "tools_called": got_tools, "turns": len(result.turns),
            "tokens": result.usage.total,
            "latency_ms": round(result.latency_ms, 1),
            "answer": result.answer, "ok": not problems,
            "problems": problems,
            "turn_detail": [
                {"index": t.index, "text": t.text,
                 "tool_calls": t.tool_calls,
                 "tool_ok": [r.ok for r in t.tool_results]}
                for t in result.turns],
        })

    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("real_trace_%s_%s.json"
                     % (provider_name, datetime.now().strftime("%Y%m%d_%H%M")))
    doc = {"_note": "真实模型输出，非合成数据。",
           "provider": provider_name,
           "recorded_at": datetime.now().isoformat(timespec="seconds"),
           "cases": traces}
    out.write_text(scrub(json.dumps(doc, ensure_ascii=False, indent=2)),
                   encoding="utf-8")

    print("\n" + "-" * 66)
    print("trace 已写入：%s" % out.relative_to(ROOT))
    if failures:
        print("❌ %d 项未通过：" % len(failures))
        for f in failures:
            print("   - " + f)
        return 1
    print("✅ 全部通过。这份 trace 是「真实模型跑通」的证据，可贴进 README。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
