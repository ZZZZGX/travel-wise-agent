# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""run_evals.py —— 评测执行器。

    python evals/run_evals.py            # 跑全部
    python evals/run_evals.py routing    # 只跑某一类
    python evals/run_evals.py --json     # 机器可读输出（接 CI）

与 tests/ 的分工：
  tests/  是"这段代码有没有坏"的单元回归（断言实现细节）。
  evals/  是"这个 Agent 表现好不好"的行为评测（断言外部可观察行为），
          用例写在 cases.json 里，可以只加数据不改代码——
          这样后续换成 LLM 路由时，同一套用例可以直接对比新旧实现。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from travelwise.orchestrator import TravelWiseAgent            # noqa: E402
from travelwise.providers.mock_flight import (                 # noqa: E402
    FailingFlightProvider, MockFlightProvider)
from travelwise.router import route                            # noqa: E402
from travelwise.state import TaskStatus                        # noqa: E402

TODAY = date(2026, 8, 5)
CASES = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))
HARD = json.loads((Path(__file__).parent / "hard_cases.json").read_text(encoding="utf-8"))

# --------------------------------------------------------------------------
# 路由器注入点（Phase 1 新增）
#
# 默认仍是原来的规则路由 `route`，所以 `python evals/run_evals.py` 的行为
# 与之前逐字节一致——用例、断言、期望值一律未改。
# 传入 --router llm 时才换成 LLMRouter，从而用【同一套用例】做对照。
# --------------------------------------------------------------------------
_ROUTE = route


def set_router(router) -> None:
    """把评测使用的路由换成任意 Router 实现（None = 恢复规则路由）。"""
    global _ROUTE
    if router is None:
        _ROUTE = route
    else:
        _ROUTE = lambda text, today=None: router.route(text, today).state


class Outcome:
    def __init__(self, case_id: str, passed: bool, detail: str = ""):
        self.case_id = case_id
        self.passed = passed
        self.detail = detail


# --------------------------------------------------------------------------
def eval_intent_routing() -> list[Outcome]:
    out = []
    for c in CASES["intent_routing"]:
        got = sorted(_ROUTE(c["input"], TODAY).intents)
        want = sorted(c["expect_intents"])
        out.append(Outcome(c["id"], got == want,
                           "期望 %s，实得 %s" % (want or "无", got or "无")))
    return out


def eval_parameter_extraction() -> list[Outcome]:
    out = []
    for c in CASES["parameter_extraction"]:
        state = _ROUTE(c["input"], TODAY)
        problems = []
        for field, want in (c.get("expect") or {}).items():
            got = getattr(state, field, None)
            if got != want:
                problems.append("%s: 期望 %s 实得 %s" % (field, want, got))
        if "expect_missing" in c:
            if sorted(state.missing) != sorted(c["expect_missing"]):
                problems.append("missing: 期望 %s 实得 %s"
                                % (sorted(c["expect_missing"]), sorted(state.missing)))
        out.append(Outcome(c["id"], not problems, "；".join(problems) or "OK"))
    return out


def eval_scope_control() -> list[Outcome]:
    out = []
    for c in CASES["scope_control"]:
        state = _ROUTE(c["input"], TODAY)
        problems = []
        for key, field in (("expect_scope", "scope"), ("expect_place", "place"),
                           ("expect_destination", "destination")):
            if key in c and getattr(state, field, None) != c[key]:
                problems.append("%s: 期望 %s 实得 %s" % (field, c[key], getattr(state, field, None)))
        out.append(Outcome(c["id"], not problems, "；".join(problems) or "OK"))
    return out


def eval_tool_failure() -> list[Outcome]:
    out = []
    for c in CASES["tool_failure"]:
        agent = TravelWiseAgent(FailingFlightProvider(c["mode"]), today=TODAY)
        state = agent.handle(c["input"])
        res = state.flight_result or {}
        problems = []
        if res.get("ok") != c["expect_ok"]:
            problems.append("ok: 期望 %s 实得 %s" % (c["expect_ok"], res.get("ok")))
        if c.get("expect_error_contains") and c["expect_error_contains"] not in (res.get("error") or ""):
            problems.append("错误信息未包含「%s」" % c["expect_error_contains"])
        if c.get("forbid_fabrication"):
            # 失败时绝不能有航班或分析结论被"补"出来
            if res.get("flights") or res.get("analysis"):
                problems.append("失败却产出了航班/分析——疑似编造")
        out.append(Outcome(c["id"], not problems, "；".join(problems) or "OK"))
    return out


def eval_hitl() -> list[Outcome]:
    out = []
    for c in CASES["human_in_the_loop"]:
        seen = {}

        def approval(preview, _c=c, _s=seen):
            _s["preview"] = preview
            return bool(_c["approval"])

        agent = TravelWiseAgent(
            MockFlightProvider(today=TODAY),
            approval_callback=None if c["approval"] is None else approval,
            today=TODAY)
        state = agent.handle(c["input"], want_reminder=c.get("want_reminder", False))
        pa = state.pending_action or {}
        executed = bool(pa.get("result", {}).get("ok"))

        problems = []
        if executed != c["expect_executed"]:
            problems.append("执行状态: 期望 %s 实得 %s" % (c["expect_executed"], executed))
        if c.get("expect_preview") and "确认" not in seen.get("preview", ""):
            problems.append("未在执行前展示预览")
        out.append(Outcome(c["id"], not problems, "；".join(problems) or "OK"))
    return out


def eval_edge_cases() -> list[Outcome]:
    out = []
    for c in CASES["edge_cases"]:
        problems = []
        try:
            agent = TravelWiseAgent(MockFlightProvider(today=TODAY), today=TODAY)
            state = agent.handle(c["input"])
            TravelWiseAgent.render(state)          # 渲染也不能崩
            if "expect_scope" in c and state.scope != c["expect_scope"]:
                problems.append("scope: 期望 %s 实得 %s" % (c["expect_scope"], state.scope))
            if "expect_intents" in c and sorted(state.intents) != sorted(c["expect_intents"]):
                problems.append("intents: 期望 %s 实得 %s"
                                % (sorted(c["expect_intents"]), sorted(state.intents)))
        except Exception as e:                      # noqa: BLE001
            problems.append("抛出异常：%s: %s" % (type(e).__name__, e))
        out.append(Outcome(c["id"], not problems, "；".join(problems) or "OK"))
    return out


def judge_hard(case: dict, state) -> list[str]:
    """判一条难例。返回问题列表，空列表 = 通过。

    抽成函数是为了给 `compare_routers.py` 共用：对照实验必须和评测
    用**同一把尺子**，否则「LLM 路由提升了多少」这个数字是拿两把不同的
    尺子量出来的，没有意义。
    """
    problems = []
    if case.get("expect_out_of_scope"):
        if state.current_step != TaskStatus.OUT_OF_SCOPE:
            problems.append("应判超范围，实得 intents=%s" % (state.intents or "无"))
    if "expect_intents" in case:
        got, want = sorted(state.intents), sorted(case["expect_intents"])
        if got != want:
            problems.append("intents: 期望 %s 实得 %s" % (want, got or "无"))
    for field, want in (case.get("expect") or {}).items():
        got = getattr(state, field, None)
        if got != want:
            problems.append("%s: 期望 %s 实得 %s" % (field, want, got))
    if "expect_missing" in case:
        if sorted(state.missing) != sorted(case["expect_missing"]):
            problems.append("missing: 期望 %s 实得 %s"
                            % (sorted(case["expect_missing"]), sorted(state.missing)))
    for word in case.get("expect_notice") or []:
        if not any(word in w for w in state.warnings):
            problems.append("应当提到「%s」，但没提" % word)
    if case.get("expect_clarify"):
        # 「去问」的形态有好几种：标为缺失、给出警告、或直接判超范围。
        # 只要不是**默默替用户决定**，都算过。
        asked = (state.missing or state.warnings
                 or state.current_step == TaskStatus.OUT_OF_SCOPE)
        if not asked:
            problems.append("应当要求澄清，实得 place=%s（替用户做了选择）" % state.place)
    return problems


def iter_hard_cases():
    """(组名, 用例) 逐条给出。跳过 `_` 开头的说明块。"""
    for group, cases in HARD.items():
        if not group.startswith("_"):
            for case in cases:
                yield group, case


def eval_hard() -> list[tuple[str, Outcome]]:
    """难例组。一个通用判定器吃掉所有断言字段。

    不为每类难例单写一个 evaluator，是因为难例本来就是**跨类**的：
    「别给我查机票，我就想知道成都有什么好玩的」同时考意图、槽位和否定。
    分到六个 suite 里，反而看不出它到底难在哪。
    """
    out: list[tuple[str, Outcome]] = []
    for group, case in iter_hard_cases():
        problems = judge_hard(case, _ROUTE(case["input"], TODAY))
        out.append((group, Outcome(case["id"], not problems,
                                   "；".join(problems) or "OK")))
    return out


SUITES = {
    "routing": ("Intent Routing", eval_intent_routing),
    "params": ("Parameter Extraction", eval_parameter_extraction),
    "scope": ("Scope Control", eval_scope_control),
    "failure": ("Tool Failure Handling", eval_tool_failure),
    "hitl": ("Human-in-the-loop", eval_hitl),
    "edge": ("Edge Cases", eval_edge_cases),
}


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    router_mode = "rule"
    if "--router" in argv:
        idx = argv.index("--router")
        if idx + 1 < len(argv):
            router_mode = argv[idx + 1]
            argv = argv[:idx] + argv[idx + 2:]

    synthetic = False
    if router_mode == "llm":
        from travelwise.config import Settings, build_router
        settings = Settings.from_env()
        settings.router = "llm"
        router = build_router(settings)
        synthetic = bool(getattr(router, "is_synthetic", False))
        set_router(router)

    picked = [a for a in argv if not a.startswith("-")]
    suites = {k: v for k, v in SUITES.items() if not picked or k in picked}

    report, total, passed = {}, 0, 0
    for key, (label, fn) in suites.items():
        results = fn()
        ok = sum(1 for r in results if r.passed)
        total += len(results)
        passed += ok
        report[key] = {
            "label": label, "passed": ok, "total": len(results),
            "failures": [{"id": r.case_id, "detail": r.detail} for r in results if not r.passed],
        }

    # 难例组：跑，但**不参与退出码**。理由见 hard_cases.json 开头。
    hard = eval_hard() if not picked else []
    hard_pass = sum(1 for _g, r in hard if r.passed)
    hard_report: dict = {}
    for group, r in hard:
        bucket = hard_report.setdefault(group, {"passed": 0, "total": 0, "failures": []})
        bucket["total"] += 1
        if r.passed:
            bucket["passed"] += 1
        else:
            bucket["failures"].append({"id": r.case_id, "detail": r.detail})

    if as_json:
        print(json.dumps({"summary": {"passed": passed, "total": total},
                          "suites": report,
                          "hard": {"passed": hard_pass, "total": len(hard),
                                   "groups": hard_report}},
                         ensure_ascii=False, indent=2))
        return 0 if passed == total else 1

    print("=" * 62)
    print("TravelWise Evaluation Suite　｜　router = %s" % router_mode)
    if synthetic:
        print("⚠️  当前 LLM 客户端为离线回放（合成数据），"
              "结果只验证管道通畅，\n    不代表真实模型能力。")
    print("=" * 62)
    for key, data in report.items():
        mark = "✅" if data["passed"] == data["total"] else "❌"
        print("\n%s %-26s %d/%d" % (mark, data["label"], data["passed"], data["total"]))
        for f in data["failures"]:
            print("     ✗ %s —— %s" % (f["id"], f["detail"]))
    rate = (passed / total * 100) if total else 0.0
    print("\n" + "-" * 62)
    print("回归闸门：%d/%d 通过（%.1f%%）　—— 红了就是退化，必须修" % (passed, total, rate))

    if hard:
        hard_rate = hard_pass / len(hard) * 100
        print("")
        print("=" * 62)
        print("难例组　%d/%d（%.0f%%）　—— 不参与退出码" % (hard_pass, len(hard), hard_rate))
        print("=" * 62)
        for group, data in hard_report.items():
            mark = "✅" if data["passed"] == data["total"] else "· "
            print("\n%s %-14s %d/%d" % (mark, group, data["passed"], data["total"]))
            for f in data["failures"]:
                print("     ✗ %s —— %s" % (f["id"], f["detail"]))
        print("")
        print("-" * 62)
        print("难例组不是待修的 bug 列表，是**规则路由的能力边界**。")
        print("它存在的意义是让「换成 LLM 路由值不值」这个问题有数字可答——")
        print("回归组两边都是满分，比不出任何东西。")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
