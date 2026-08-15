# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""check_consistency.py —— 让「README 自称的数字」变成一个会失败的测试。

    python scripts/check_consistency.py          # 校验，不一致则退出码 1
    python scripts/check_consistency.py --fix    # 直接把 README 改对
    python scripts/check_consistency.py --json   # 机器可读

## 为什么需要它

这个仓库通篇在讲「诚实边界」——不宣称没做到的事、不把降级说成成功。
可它自己的 README 里写着 112 项测试，实跑是 235 项；badge 写 v0.3.0，
pyproject 写 0.1.0，`__init__` 又写 0.8.0。**一个连自己版本号都对不上的仓库，
没有资格谈诚实。**

问题不在于当初谁写错了，而在于当时的机制是「靠人记得同步四个地方」。
人不会记得。所以这里换一种机制：

  - 版本号只有 `_version.py` 一处可以手写；
  - 测试数 / 评测数一律**实跑一遍**再和 README 比对；
  - 对不上就退出码 1，CI 变红。

漂移从此是一次构建失败，而不是一次没人注意到的疏忽。

## 为什么评测强制走离线口径

`.env` 里配了真实 Key 的机器上，agent evals 会去打真模型：数字随网络、
随模型心情变，还要花钱。README badge 要的是**任何人 clone 下来都能复现**
的那个数字，所以这里强制 `TRAVELWISE_LLM_PROVIDER=scripted`。
真机跑出来的结果属于另一个口径，记在 docs/real-model-runs.md，不进 badge。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SRC = ROOT / "src"


# ---------------------------------------------------------------- 采集事实

def read_version() -> tuple[str, str]:
    """从 _version.py 读版本号。

    用正则而不是 import：import `travelwise` 会连带执行 `__init__`，
    把 orchestrator 整条依赖链拉起来。校验版本号不该需要整个包能跑。
    """
    text = (SRC / "travelwise" / "_version.py").read_text(encoding="utf-8")
    ver = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    stage = re.search(r'^STAGE\s*=\s*"([^"]+)"', text, re.M)
    if not ver:
        raise SystemExit("✗ _version.py 里找不到 VERSION")
    return ver.group(1), (stage.group(1) if stage else "")


def _run(args: list[str], extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def _extract_json(out: str, what: str) -> dict:
    """从可能被污染的 stdout 里抠出 JSON。

    `--json` 本该是干净的机器可读输出，但 console reminder provider 会在
    评测过程中往 stdout 打提醒卡片。与其要求所有 provider 都懂得闭嘴，
    这里退一步：从第一个行首 `{` 开始解析。
    真要治本，应该让 provider 写 stderr —— 记在 README 的已知问题里。
    """
    idx = out.find("\n{")
    start = 0 if out.lstrip().startswith("{") else (idx + 1 if idx >= 0 else -1)
    if start < 0:
        raise SystemExit("✗ %s 没有输出可解析的 JSON：\n%s" % (what, out[-600:]))
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError as exc:
        raise SystemExit("✗ %s 的 JSON 解析失败：%s\n%s" % (what, exc, out[-600:]))


def count_tests() -> int:
    """实跑单元测试，返回用例总数。测试若不全绿，直接判失败。"""
    p = _run(["-m", "unittest", "discover", "-s", "tests", "-v"])
    m = re.search(r"^Ran (\d+) tests?", p.stderr or "", re.M)
    if not m:
        raise SystemExit("✗ 无法解析 unittest 输出：\n" + (p.stderr or "")[-800:])
    if "\nOK" not in (p.stderr or "") and "OK" != (p.stderr or "").strip()[-2:]:
        if p.returncode != 0:
            raise SystemExit(
                "✗ 测试没有全绿，此时统计数量没有意义。先修测试：\n"
                + (p.stderr or "")[-800:])
    return int(m.group(1))


def count_router_evals() -> tuple[int, int]:
    p = _run(["evals/run_evals.py", "--json"])
    data = _extract_json(p.stdout, "run_evals.py")
    s = data["summary"]
    return s["passed"], s["total"]


def count_hard_evals() -> tuple[int, int]:
    """难例组的通过数。

    它**不是**一个应当变成满分的数字，但同样必须准：README 上写「19 条难例
    过了 4 条」而实跑是 6 条，和吹牛没有区别——方向相反的吹牛也是吹牛。
    """
    p = _run(["evals/run_evals.py", "--json"])
    data = _extract_json(p.stdout, "run_evals.py")
    h = data.get("hard") or {"passed": 0, "total": 0}
    return h["passed"], h["total"]


def count_agent_evals() -> tuple[int, int]:
    # 强制离线口径：badge 必须是 clone 下来就能复现的数字
    p = _run(["evals/run_agent_evals.py", "--json"],
             {"TRAVELWISE_LLM_PROVIDER": "scripted"})
    data = _extract_json(p.stdout, "run_agent_evals.py")
    passed, total = data["total"]
    return passed, total


# ---------------------------------------------------------------- 比对规则

def build_rules(facts: dict) -> list[tuple]:
    """(说明, 正则, 期望值)。正则必须恰好有一个捕获组，即那个数字。"""
    v, stage = facts["version"], facts["stage"]
    tests = str(facts["tests"])
    r_pass, r_total = facts["router_evals"]
    h_pass, h_total = facts["hard_evals"]
    a_pass, a_total = facts["agent_evals"]

    return [
        ("status badge",
         r"(?<=badge/status-v)([0-9][^-\)]*?)(?=-)",
         "%s%%20%s" % (v, stage)),
        ("tests badge",
         r"(?<=badge/tests-)(\d+)(?=%20passing)",
         tests),
        ("router evals badge",
         r"(?<=badge/router%20evals-)(\d+%2F\d+)(?=-)",
         "%d%%2F%d" % (r_pass, r_total)),
        ("router hard badge",
         r"(?<=badge/router%20hard-)(\d+%2F\d+)(?=-)",
         "%d%%2F%d" % (h_pass, h_total)),
        ("难例组条数",
         r"(?<=难例组（)(\d+)(?= 条）)",
         str(h_total)),
        ("agent evals badge",
         r"(?<=badge/agent%20evals-)(\d+%2F\d+)(?=-)",
         "%d%%2F%d" % (a_pass, a_total)),
        ("快速开始里的测试数",
         r"(?<=discover -s tests    # )(\d+)(?= 项测试)",
         tests),
        ("目录树里的测试数",
         r"(?<=# )(\d+)(?= 项单元测试)",
         tests),
        ("能力清单里的测试数",
         r"(?<=单元测试（)(\d+)(?= 项）)",
         tests),
        ("能力清单里的回归用例数",
         r"(?<=回归闸门 6 类 )(\d+)(?= 用例)",
         str(r_total)),
        ("能力清单里的难例数",
         r"(?<=难例组 6 类 )(\d+)(?= 用例）)",
         str(h_total)),
        ("两台仪器对照表 · 回归",
         r"(?<=\| 全绿（)(\d+/\d+)(?=）)",
         "%d/%d" % (r_pass, r_total)),
        ("两台仪器对照表 · 难例",
         r"(?<=不全绿（)(\d+/\d+)(?=）)",
         "%d/%d" % (h_pass, h_total)),
        ("Roadmap 里的难例条数",
         r"(?<=hard_cases\.json`，)(\d+)(?= 条)",
         str(h_total)),
        ("Roadmap 里的难例通过数",
         r"(?<=规则路由 )(\d+/\d+)(?=）)",
         "%d/%d" % (h_pass, h_total)),
    ]


REAL_BEGIN = "<!-- real-model-run:begin -->"
REAL_END = "<!-- real-model-run:end -->"


def latest_real_run() -> str:
    """从 docs/real-model-runs.md 取最近一次真机记录，生成 README 该显示的那行。

    没有记录时**必须**显示"尚无记录"。这是整个机制的关键：
    默认值是「没有证据」而不是「已通过」。上一版 README 的错误方向恰好相反——
    它默认了一个结论，然后由人负责在结论失效时去改它，而人不会去改。
    """
    doc = ROOT / "docs" / "real-model-runs.md"
    if doc.is_file():
        m = re.search(r"^##\s*(\d{4}-\d{2}-\d{2})\s*[｜|]\s*(.+?)\s*$",
                      doc.read_text(encoding="utf-8"), re.M)
        if m:
            date_s, model = m.group(1), m.group(2).strip("　 |｜")
            return ("**最近一次真机验证：%s ｜ 模型 `%s`** —— "
                    "完整结果见 [`docs/real-model-runs.md`](docs/real-model-runs.md)。"
                    % (date_s, model))
    return ("**最近一次真机验证：尚无记录** —— "
            "跑 `python scripts/record_real_run.py` 生成。")


def check(fix: bool = False) -> tuple[list[dict], dict]:
    version, stage = read_version()
    facts = {
        "version": version,
        "stage": stage,
        "tests": count_tests(),
        "router_evals": count_router_evals(),
        "hard_evals": count_hard_evals(),
        "agent_evals": count_agent_evals(),
    }

    text = README.read_text(encoding="utf-8")
    problems: list[dict] = []

    for label, pattern, expected in build_rules(facts):
        found = re.findall(pattern, text)
        if not found:
            problems.append({"where": label, "found": None, "expected": expected,
                             "why": "README 里找不到这个位置（模板被改动过？）"})
            continue
        for got in set(found):
            if got != expected:
                problems.append({"where": label, "found": got, "expected": expected,
                                 "why": ""})
        if fix:
            text = re.sub(pattern, expected.replace("\\", "\\\\"), text)

    # ---- 真机验证记录块 ----
    expected_line = latest_real_run()
    facts["real_model_run"] = expected_line
    block = re.search(re.escape(REAL_BEGIN) + r"\n(.*?)\n" + re.escape(REAL_END),
                      text, re.S)
    if not block:
        problems.append({
            "where": "真机验证记录块", "found": None, "expected": "标记对",
            "why": "README 里缺少 %s / %s 标记" % (REAL_BEGIN, REAL_END)})
    elif block.group(1).strip() != expected_line:
        problems.append({
            "where": "真机验证记录块", "found": block.group(1).strip()[:24] + "…",
            "expected": expected_line[:24] + "…",
            "why": "应与 docs/real-model-runs.md 的最新一条一致"})
        if fix:
            text = (text[:block.start(1)] + expected_line + text[block.end(1):])

    # pyproject 必须走 dynamic，不许再出现手写字面量
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if re.search(r'^version\s*=\s*"', pyproject, re.M):
        problems.append({
            "where": "pyproject.toml", "found": "硬编码 version",
            "expected": 'dynamic = ["version"]',
            "why": "版本号只能写在 _version.py，pyproject 应当 dynamic 读取"})

    init = (SRC / "travelwise" / "__init__.py").read_text(encoding="utf-8")
    if re.search(r'^__version__\s*=\s*"', init, re.M):
        problems.append({
            "where": "src/travelwise/__init__.py", "found": "硬编码 __version__",
            "expected": "from ._version import VERSION",
            "why": "同上：三处各写各的就是上一版翻车的原因"})

    if fix and text != README.read_text(encoding="utf-8"):
        README.write_text(text, encoding="utf-8")

    return problems, facts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="校验 README 自称的数字与实跑结果一致")
    ap.add_argument("--fix", action="store_true", help="直接改写 README 中的数字")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    problems, facts = check(fix=args.fix)

    if args.as_json:
        print(json.dumps({"facts": facts, "problems": problems},
                         ensure_ascii=False, indent=2))
        return 0 if (args.fix or not problems) else 1

    print("=" * 62)
    print("一致性校验　｜　版本 %s-%s" % (facts["version"], facts["stage"]))
    print("=" * 62)
    print("  实跑单元测试        %d 项" % facts["tests"])
    print("  Router 回归闸门     %d/%d" % facts["router_evals"])
    print("  Router 难例组       %d/%d" % facts["hard_evals"])
    print("  Agent 评测（离线）  %d/%d" % facts["agent_evals"])
    print("-" * 62)

    if not problems:
        print("✅ README 自称的数字与实跑结果一致。")
        return 0

    if args.fix:
        print("🔧 已修正 %d 处：" % len(problems))
    else:
        print("❌ 发现 %d 处不一致：" % len(problems))
    for p in problems:
        print("   · %-22s README 写 %-12s 应为 %s"
              % (p["where"], p["found"], p["expected"]))
        if p["why"]:
            print("     %s" % p["why"])
    if not args.fix:
        print("-" * 62)
        print("   跑 `python scripts/check_consistency.py --fix` 自动改写 README。")
        print("   注意：pyproject / __init__ 的问题需要手改，--fix 不碰代码。")
    return 0 if args.fix else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
