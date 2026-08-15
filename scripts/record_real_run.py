#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""record_real_run.py —— 把一次真机跑的结果，落成一份可复现的记录。

    export TRAVELWISE_LLM_PROVIDER=openai         # DeepSeek 走 OpenAI 兼容协议
    export TRAVELWISE_LLM_API_KEY=sk-...
    export TRAVELWISE_LLM_BASE_URL=https://api.deepseek.com
    export TRAVELWISE_LLM_MODEL=deepseek-chat
    python scripts/record_real_run.py

## 为什么不直接手写进 README

README 里曾经写着「尚未用真实模型跑过」，而那时作者其实已经用 DeepSeek 跑通了。
反过来也一样危险：改成「已跑通」之后，这句话就再也不会自己失效了——
哪怕半年后模型换了、接口变了、早就跑不通，README 依然信誓旦旦。

**手写的结论没有保质期，这是它最大的问题。**

所以这里的做法是：真机结果由脚本生成，带日期、带模型名、带具体数字，
写进 `docs/real-model-runs.md`；README 只放一句指向它的链接和最近一次的日期。
读的人能看到「上一次验证是什么时候、用的什么模型、结果如何」，
自己判断这个结论还新不新鲜——而不是被一句没有时间戳的断言糊弄过去。

## 没有 Key 时

直接退出并说明，不写任何文件。**不会**留下一条「跳过」记录假装跑过。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "real-model-runs.md"

HEADER = """# 真实模型运行记录

> 本文件由 `python scripts/record_real_run.py` 生成，**不要手改**。
> 每条记录对应一次真机跑，带日期和模型名——因为「跑通过」是有保质期的结论。
>
> 离线口径（`ScriptedLLMClient` 回放）的数字见 README badge，二者不是一回事：
> 离线证明的是管道通畅，真机证明的是**模型本身守不守规矩**。

"""


def env_summary() -> dict:
    return {
        "provider": os.environ.get("TRAVELWISE_LLM_PROVIDER", ""),
        "model": os.environ.get("TRAVELWISE_LLM_MODEL", ""),
        "base_url": os.environ.get("TRAVELWISE_LLM_BASE_URL", ""),
    }


def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def scrub(text: str) -> str:
    """兜底：任何形似 Key 的串都不许进文档。"""
    text = re.sub(r"sk-[A-Za-z0-9\-_]{8,}", "sk-***", text)
    key = os.environ.get("TRAVELWISE_LLM_API_KEY", "")
    if key:
        text = text.replace(key, "***")
    return text


def main() -> int:
    # 先把 .env 读进环境变量，再检查 —— 顺序反了就永远看不见 .env 里的值。
    # 这个 bug 的表现极具迷惑性：fix_env.py 说「已填」，本脚本说「未设置」，
    # 两个都在说真话，只是一个读文件、一个读环境变量。
    from travelwise.config import load_dotenv
    load_dotenv()

    if not os.environ.get("TRAVELWISE_LLM_API_KEY"):
        print("✗ 未设置 TRAVELWISE_LLM_API_KEY。")
        print("  这个脚本的全部意义就是产出真机证据，没有 Key 时不写任何记录——")
        print("  留一条『已跳过』的记录，比没有记录更容易被误读成『跑过了』。")
        return 1

    info = env_summary()
    print("=" * 66)
    print("真机记录　｜　provider=%s  model=%s" % (info["provider"], info["model"]))
    print("=" * 66)

    print("\n▶ 1/2  冒烟测试（scripts/smoke_real_llm.py）")
    smoke = _run(["scripts/smoke_real_llm.py"])
    print(scrub(smoke.stdout)[-1500:])
    smoke_ok = smoke.returncode == 0

    print("\n▶ 2/2  Agent 评测（真机口径）")
    evals = _run(["evals/run_agent_evals.py", "--json"])
    try:
        idx = evals.stdout.find("\n{")
        start = 0 if evals.stdout.lstrip().startswith("{") else idx + 1
        data = json.loads(evals.stdout[start:])
    except Exception as exc:  # noqa: BLE001
        print("✗ 无法解析评测输出：%s" % exc)
        print(scrub(evals.stdout)[-800:])
        return 1

    passed, total = data["total"]
    print("   %d/%d 通过｜token %d" % (passed, total, data.get("tokens", 0)))

    # ------------------------------------------------------------ 写记录
    lines = ["## %s　｜　%s" % (date.today().isoformat(),
                               info["model"] or info["provider"]), ""]
    lines.append("| 项 | 结果 |")
    lines.append("|---|---|")
    lines.append("| Provider | `%s` |" % (info["provider"] or "-"))
    lines.append("| Model | `%s` |" % (info["model"] or "-"))
    if info["base_url"]:
        lines.append("| Base URL | `%s` |" % info["base_url"])
    lines.append("| 端到端冒烟 | %s |" % ("✅ 通过" if smoke_ok else "❌ 未通过"))
    lines.append("| Agent 评测（真机） | %d/%d（%.1f%%） |"
                 % (passed, total, passed / total * 100 if total else 0))
    lines.append("| Token 消耗 | %d |" % data.get("tokens", 0))
    lines.append("")

    lines.append("各维度：")
    lines.append("")
    lines.append("| 维度 | 通过 |")
    lines.append("|---|---|")
    for s in data.get("suites", []):
        if s["total"] == 0:
            lines.append("| %s | n/a（本次未适用） |" % s["name"])
        else:
            lines.append("| %s | %d/%d |" % (s["name"], s["passed"], s["total"]))
    lines.append("")

    fails = [f for s in data.get("suites", []) for f in s.get("failures", [])]
    if fails:
        lines.append("未通过的用例：")
        lines.append("")
        for f in fails:
            lines.append("- **%s**　%s" % (f["id"], scrub(str(f.get("input", "")))[:60]))
            for p in f.get("problems", []):
                lines.append("  - %s" % scrub(str(p)))
        lines.append("")
    else:
        lines.append("本次无未通过用例。")
        lines.append("")

    lines.append("---")
    lines.append("")

    entry = "\n".join(lines)
    DOC.parent.mkdir(parents=True, exist_ok=True)
    old = DOC.read_text(encoding="utf-8") if DOC.is_file() else ""
    body = old[len(HEADER):] if old.startswith(HEADER) else old
    # 新记录插到最前面：读的人最关心的是「最近一次」
    DOC.write_text(HEADER + entry + body, encoding="utf-8")

    print("\n" + "-" * 66)
    print("✅ 记录已写入 %s" % DOC.relative_to(ROOT))
    print("   README 的『最近一次真机验证』日期请跑 check_consistency.py --fix 同步。")
    return 0 if smoke_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
