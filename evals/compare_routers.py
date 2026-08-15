# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""compare_routers.py —— Rule Baseline VS LLM Agent，同一套用例的对照实验。

    python evals/compare_routers.py                     # 离线回放（验证管道）
    python evals/compare_routers.py --json              # 机器可读，接 CI
    TRAVELWISE_LLM_PROVIDER=anthropic \\
    TRAVELWISE_LLM_API_KEY=sk-... \\
    python evals/compare_routers.py                     # 真实模型对照

为什么要有这个脚本：
    "换成 LLM 之后更好了" 是个需要被证明的命题，不是感觉。
    本脚本用**同一批用例**分别跑两个路由器，并列出：

        意图准确率 / 参数准确率 / 范围准确率 / 延迟 / Token 成本 / 降级次数

    只看准确率而不看延迟与成本，无法回答"这个提升值不值这个价"。
    如果 LLM 没有明显提升，**就不要为了"Agent 感"替换掉规则路由**。

⚠️ 关于离线回放的诚实说明：
    默认的 ScriptedLLMClient 回放的是人工录制的合成响应，且录制基准就来自
    规则路由，因此两者成绩必然接近——**这不构成模型能力的证据**。
    它证明的只是"LLM 路由的解析与调度管道是通的"。
    要得到有意义的对照数字，必须配置真实模型。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from travelwise.config import Settings, build_llm_client, build_router  # noqa: E402
from travelwise.routing.base import RuleRouter                          # noqa: E402

TODAY = date(2026, 8, 5)
CASES = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_evals import iter_hard_cases, judge_hard          # noqa: E402


@dataclass
class Metrics:
    router: str = ""
    synthetic: bool = False
    intent_hit: int = 0
    intent_total: int = 0
    param_hit: int = 0
    param_total: int = 0
    scope_hit: int = 0
    scope_total: int = 0
    latency_ms: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fell_back: int = 0
    errors: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    #: 难例组单独计。它**不并进** total_hit —— 回归组是「有没有坏」，
    #: 难例组是「做到哪一步」，加在一起会得到一个两头都说不清的百分比。
    hard_hit: int = 0
    hard_total: int = 0

    @property
    def total_hit(self) -> int:
        return self.intent_hit + self.param_hit + self.scope_hit

    @property
    def total_cases(self) -> int:
        return self.intent_total + self.param_total + self.scope_total

    @property
    def accuracy(self) -> float:
        return self.total_hit / self.total_cases * 100 if self.total_cases else 0.0

    @property
    def hard_accuracy(self) -> float:
        return self.hard_hit / self.hard_total * 100 if self.hard_total else 0.0

    @property
    def avg_latency(self) -> float:
        return self.latency_ms / self.calls if self.calls else 0.0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def rate(self, hit: int, total: int) -> str:
        return "%d/%d (%.1f%%)" % (hit, total, hit / total * 100 if total else 0.0)


def _record(m: Metrics, outcome) -> None:
    m.calls += 1
    m.latency_ms += outcome.latency_ms
    m.input_tokens += outcome.usage.input_tokens
    m.output_tokens += outcome.usage.output_tokens
    if outcome.fell_back:
        m.fell_back += 1
    if outcome.error and not outcome.fell_back:
        m.errors.append(outcome.error)


def evaluate(router) -> Metrics:
    m = Metrics(router=router.name,
                synthetic=bool(getattr(router, "is_synthetic", False)))

    # --- 意图路由 ---
    for c in CASES["intent_routing"]:
        outcome = router.route(c["input"], TODAY)
        _record(m, outcome)
        m.intent_total += 1
        got, want = sorted(outcome.state.intents), sorted(c["expect_intents"])
        if got == want:
            m.intent_hit += 1
        else:
            m.failures.append({"suite": "intent", "id": c["id"],
                               "detail": "期望 %s 实得 %s" % (want or "无", got or "无")})

    # --- 参数抽取 ---
    for c in CASES["parameter_extraction"]:
        outcome = router.route(c["input"], TODAY)
        _record(m, outcome)
        m.param_total += 1
        problems = []
        for field_name, want in (c.get("expect") or {}).items():
            got = getattr(outcome.state, field_name, None)
            if got != want:
                problems.append("%s: 期望 %s 实得 %s" % (field_name, want, got))
        if "expect_missing" in c and sorted(outcome.state.missing) != sorted(c["expect_missing"]):
            problems.append("missing: 期望 %s 实得 %s"
                            % (sorted(c["expect_missing"]), sorted(outcome.state.missing)))
        if problems:
            m.failures.append({"suite": "params", "id": c["id"], "detail": "；".join(problems)})
        else:
            m.param_hit += 1

    # --- 范围控制 ---
    for c in CASES["scope_control"]:
        outcome = router.route(c["input"], TODAY)
        _record(m, outcome)
        m.scope_total += 1
        problems = []
        for key, field_name in (("expect_scope", "scope"), ("expect_place", "place"),
                                ("expect_destination", "destination")):
            if key in c and getattr(outcome.state, field_name, None) != c[key]:
                problems.append("%s: 期望 %s 实得 %s"
                                % (field_name, c[key], getattr(outcome.state, field_name, None)))
        if problems:
            m.failures.append({"suite": "scope", "id": c["id"], "detail": "；".join(problems)})
        else:
            m.scope_hit += 1

    # --- 难例组 ---
    # 这才是对照实验真正有信息量的地方：回归组两边都接近满分，
    # 差值落在噪声里；能不能在这 19 条上拉开距离，才是「值不值得换」的答案。
    for _group, c in iter_hard_cases():
        outcome = router.route(c["input"], TODAY)
        _record(m, outcome)
        m.hard_total += 1
        problems = judge_hard(c, outcome.state)
        if problems:
            m.failures.append({"suite": "hard", "id": c["id"],
                               "detail": "；".join(problems)})
        else:
            m.hard_hit += 1
    return m


def _verdict(rule: Metrics, llm: Metrics) -> list[str]:
    """给出结论。没有明显提升就明说不值得替换。"""
    lines = []
    # 判据取**难例组**。回归组两边都接近满分，那里的差值只是噪声——
    # 拿它下结论，等于用一把量不出差别的尺子去说「没差别」。
    delta = llm.hard_accuracy - rule.hard_accuracy
    if llm.synthetic:
        lines.append("⚠️ 本次 LLM 侧使用离线回放的合成响应，且录制基准来自规则路由，")
        lines.append("   因此准确率接近是必然的，**不能据此判断模型好坏**。")
        lines.append("   结论：本次只证明了 Tool Calling 管道通畅。")
        lines.append("   要得到有意义的对照，请配置 TRAVELWISE_LLM_PROVIDER 与 API Key 重跑。")
        if llm.fell_back:
            lines.append("")
            lines.append("   另：LLM 侧发生了 %d 次降级到规则路由（录音里没有这些用例的响应），"
                         % llm.fell_back)
            lines.append("   **这部分数字实际上是规则路由跑出来的**。难例组尤其如此——")
            lines.append("   表里那一行 llm 的成绩，此刻和 rule 是同一个东西，不是巧合。")
        return lines

    if delta > 2:
        lines.append("✅ LLM 路由在难例组上高出 %.1f 个百分点（%s vs %s）。"
                     % (delta, llm.rate(llm.hard_hit, llm.hard_total),
                        rule.rate(rule.hard_hit, rule.hard_total)))
        lines.append("   代价：平均延迟 %.1fms vs %.1fms，Token 消耗 %d vs 0。"
                     % (llm.avg_latency, rule.avg_latency, llm.tokens))
        lines.append("   建议：值得采用，但保留规则路由作为 LLM 不可用时的降级路径。")
    elif delta < -2:
        lines.append("❌ LLM 路由在难例组上反而低 %.1f 个百分点，且额外消耗 %d tokens。"
                     % (-delta, llm.tokens))
        lines.append("   建议：**不要替换**。先看失败用例，可能是提示词而非模型的问题。")
    else:
        lines.append("➖ 两者准确率相当（差 %.1f 个百分点），但 LLM 额外消耗 %d tokens、"
                     "平均慢 %.1fms。" % (abs(delta), llm.tokens,
                                      llm.avg_latency - rule.avg_latency))
        lines.append("   建议：**不值得替换规则路由**——难例组上也没拉开距离，")
        lines.append("   说明差距不在「规则 vs 模型」，而在提示词或用例设计。")

    if llm.fell_back:
        lines.append("注意：LLM 路由发生了 %d 次降级到规则路由，这部分成绩不应算作 LLM 的。"
                     % llm.fell_back)
    return lines


def render(rule: Metrics, llm: Metrics) -> str:
    L = ["=" * 74,
         "Router 对照实验　｜　Rule Baseline  VS  LLM Agent",
         "=" * 74, ""]
    if llm.synthetic:
        L += ["⚠️  LLM 侧为【离线回放的合成数据】，非真实模型输出。",
              "    下列数字仅验证管道通畅，不代表模型能力。", ""]

    L.append("%-22s %-22s %-22s" % ("指标", "rule (baseline)", "llm"))
    L.append("-" * 74)
    rows = [
        ("意图路由准确率", rule.rate(rule.intent_hit, rule.intent_total),
         llm.rate(llm.intent_hit, llm.intent_total)),
        ("参数抽取准确率", rule.rate(rule.param_hit, rule.param_total),
         llm.rate(llm.param_hit, llm.param_total)),
        ("范围控制准确率", rule.rate(rule.scope_hit, rule.scope_total),
         llm.rate(llm.scope_hit, llm.scope_total)),
        ("回归组准确率", "%.1f%%" % rule.accuracy, "%.1f%%" % llm.accuracy),
        ("难例组准确率", rule.rate(rule.hard_hit, rule.hard_total),
         llm.rate(llm.hard_hit, llm.hard_total)),
        ("平均延迟", "%.2f ms" % rule.avg_latency, "%.2f ms" % llm.avg_latency),
        ("Token 消耗", "0", "%d (in %d / out %d)" % (llm.tokens, llm.input_tokens,
                                                    llm.output_tokens)),
        ("降级次数", "-", str(llm.fell_back)),
        ("调用失败", str(len(rule.errors)), str(len(llm.errors))),
    ]
    for name, a, b in rows:
        L.append("%-22s %-22s %-22s" % (name, a, b))

    for m in (rule, llm):
        if m.failures:
            L += ["", "【%s 未通过的用例】" % m.router]
            L += ["  ✗ %-8s %-8s %s" % (f["suite"], f["id"], f["detail"]) for f in m.failures]
        if m.errors:
            L += ["", "【%s 调用错误】" % m.router]
            L += ["  ! " + e for e in dict.fromkeys(m.errors)]

    L += ["", "-" * 74, "结论"]
    L += ["  " + line for line in _verdict(rule, llm)]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    settings = Settings.from_env()
    settings.router = "llm"
    client = build_llm_client(settings)
    llm_router = build_router(settings, client)

    rule_metrics = evaluate(RuleRouter())
    llm_metrics = evaluate(llm_router)

    if "--json" in argv:
        def dump(m: Metrics) -> dict:
            return {"router": m.router, "synthetic": m.synthetic,
                    "accuracy": round(m.accuracy, 2),
                    "intent": [m.intent_hit, m.intent_total],
                    "params": [m.param_hit, m.param_total],
                    "scope": [m.scope_hit, m.scope_total],
                    "avg_latency_ms": round(m.avg_latency, 3),
                    "tokens": m.tokens, "fell_back": m.fell_back,
                    "failures": m.failures}
        print(json.dumps({"model": getattr(client, "model", ""),
                          "rule": dump(rule_metrics), "llm": dump(llm_metrics)},
                         ensure_ascii=False, indent=2))
    else:
        print(render(rule_metrics, llm_metrics))

    # 合成数据下不做通过与否的判定——那样的绿灯没有意义
    if llm_metrics.synthetic:
        return 0
    return 0 if llm_metrics.accuracy >= rule_metrics.accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
