# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""fix_env.py —— 自动整理 .env：去重、纠正数据源、指出还差什么。

手工改配置文件最容易出的两件事：删错行、和改完了才发现改的是另一份重复的键。
这个脚本把它变成一条命令，并且**先备份**（.env.bak），改坏了能退回去。

它做四件事：
  1. 同名键去重 —— 保留【最后一个有值的】那份，空的丢掉；
  2. TRAVELWISE_FLIGHT_PROVIDER 缺失或为 mock 时，在你确认下改成 http；
  3. 行尾注释、BOM、export 前缀顺手规范掉；
  4. 列出还空着的必填项，明确告诉你缺哪个。

用法（项目根目录）：
    python scripts/fix_env.py            # 只报告，不改文件
    python scripts/fix_env.py --write    # 真正写入（先备份成 .env.bak）
    python scripts/fix_env.py --write --keep-mock   # 保留 mock 数据源
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travelwise.config import _parse_dotenv_line          # noqa: E402

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"

#: 缺了就跑不起来的键
REQUIRED = {
    "TRAVELWISE_FLIGHT_TOKEN": "阿里云云市场的 AppCode",
    "TRAVELWISE_LLM_API_KEY": "DeepSeek 的 API Key",
}

#: 建议有的键与推荐值（缺失时补上）
DEFAULTS = [
    ("TRAVELWISE_FLIGHT_PROVIDER", "http"),
    ("TRAVELWISE_FLIGHT_CONFIG", "config/flight_api.json"),
    ("TRAVELWISE_FLIGHT_TOKEN", ""),
    ("TRAVELWISE_FLIGHT_TOKEN_BACKUP", ""),
    ("TRAVELWISE_MATRIX_DAYS", "7"),
    ("TRAVELWISE_FLIGHT_CACHE", "1"),
    ("TRAVELWISE_REQUEST_INTERVAL", "0.5"),
    ("TRAVELWISE_REMINDER_PROVIDER", "console"),
    ("TRAVELWISE_ROUTER", "rule"),
    ("TRAVELWISE_LLM_PROVIDER", "openai"),
    ("TRAVELWISE_LLM_BASE_URL", "https://api.deepseek.com"),
    ("TRAVELWISE_LLM_MODEL", "deepseek-chat"),
    ("TRAVELWISE_LLM_API_KEY", ""),
]

COMMENTS = {
    "TRAVELWISE_FLIGHT_PROVIDER": "# 航班数据源：http=真实付费接口，mock=离线假数据（不花钱）",
    "TRAVELWISE_FLIGHT_TOKEN": "# 阿里云云市场 AppCode（不是 AppKey，也不是 AppSecret）",
    "TRAVELWISE_FLIGHT_TOKEN_BACKUP": "# 第二家接口的 AppCode。同一个账号买的就填同一个值",
    "TRAVELWISE_MATRIX_DAYS": "# 向后查几天，一天 1 次调用。7 天 约 1.4 元",
    "TRAVELWISE_FLIGHT_CACHE": "# 当日缓存：同一天重复查同一条航线不再付费",
    "TRAVELWISE_REQUEST_INTERVAL": "# 每次请求之间等几秒，防限流",
    "TRAVELWISE_LLM_PROVIDER": "# DeepSeek 走 OpenAI 兼容协议，这里填 openai",
    "TRAVELWISE_LLM_API_KEY": "# DeepSeek 的 API Key",
}


def read_pairs(path: Path) -> tuple[dict, list]:
    """返回 (去重后的键值, 问题列表)。同名键保留最后一个有值的。"""
    pairs: dict[str, str] = {}
    seen: dict[str, list] = {}
    problems: list[str] = []
    if not path.exists():
        return pairs, ["%s 不存在" % path.name]

    text = path.read_bytes().decode("utf-8-sig", "replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = _parse_dotenv_line(line)
        if not parsed:
            problems.append("第 %d 行不是 KEY=VALUE，已丢弃：%s" % (lineno, stripped[:40]))
            continue
        key, value = parsed
        seen.setdefault(key, []).append(lineno)
        if value or key not in pairs:
            pairs[key] = value

    for key, lines in seen.items():
        if len(lines) > 1:
            problems.append("键 %s 重复 %d 次（第 %s 行），已合并为 1 行"
                            % (key, len(lines), "、".join(str(n) for n in lines)))
    return pairs, problems


def render(pairs: dict) -> str:
    """按固定顺序重写，缺失项补默认值，凭证保持原样。"""
    out = [
        "# TravelWise 配置。凭证只放在这个文件里，config/*.json 只写变量名。",
        "# 写法：值直接跟在 = 后面；同一行不要写注释；不要加引号。",
        "# 存盘选 UTF-8（不要选 UTF-8 with BOM 或 Unicode）。",
        "",
    ]
    written = set()
    for key, default in DEFAULTS:
        value = pairs.get(key, default)
        if key in COMMENTS:
            out.append(COMMENTS[key])
        out.append("%s=%s" % (key, value))
        written.add(key)
    extra = [k for k in pairs if k not in written]
    if extra:
        out += ["", "# ---- 其它（原文件里已有，原样保留）----"]
        out += ["%s=%s" % (k, pairs[k]) for k in extra]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真正写入（先备份 .env.bak）")
    ap.add_argument("--keep-mock", action="store_true",
                    help="保留 mock 数据源，不改成 http")
    args = ap.parse_args()

    if not ENV.exists() and EXAMPLE.exists():
        print("没有 .env，将以 .env.example 为基础生成。")
        pairs, problems = read_pairs(EXAMPLE)
    else:
        pairs, problems = read_pairs(ENV)

    print("=" * 60)
    print("读到 %d 个键" % len(pairs))
    for p in problems:
        print("  [整理] %s" % p)

    if not args.keep_mock and pairs.get("TRAVELWISE_FLIGHT_PROVIDER") != "http":
        print("  [整理] TRAVELWISE_FLIGHT_PROVIDER: %s -> http（要跑真实接口）"
              % pairs.get("TRAVELWISE_FLIGHT_PROVIDER", "(缺失)"))
        pairs["TRAVELWISE_FLIGHT_PROVIDER"] = "http"

    # 配了 Key 却还挂在 scripted（离线回放）上 —— 那模型根本不会被调用
    if pairs.get("TRAVELWISE_LLM_API_KEY") and \
            pairs.get("TRAVELWISE_LLM_PROVIDER", "") in ("", "scripted"):
        print("  [整理] TRAVELWISE_LLM_PROVIDER: %s -> openai"
              "（已填 Key，scripted 是离线回放、不会真的调模型）"
              % (pairs.get("TRAVELWISE_LLM_PROVIDER") or "(缺失)"))
        pairs["TRAVELWISE_LLM_PROVIDER"] = "openai"

    # 两家接口若共用同一个 AppCode，备用那行自动跟上，省得忘了填
    token = pairs.get("TRAVELWISE_FLIGHT_TOKEN", "")
    if token and not pairs.get("TRAVELWISE_FLIGHT_TOKEN_BACKUP"):
        pairs["TRAVELWISE_FLIGHT_TOKEN_BACKUP"] = token
        print("  [整理] 备用 AppCode 留空，已自动填成与主 AppCode 相同")

    print("-" * 60)
    missing = [(k, why) for k, why in REQUIRED.items() if not pairs.get(k)]
    for key, why in REQUIRED.items():
        value = pairs.get(key, "")
        print("  %-32s %s" % (key, ("已填，长度 %d" % len(value)) if value
                              else ">>> 还是空的，需要填：%s" % why))

    if args.write:
        if ENV.exists():
            shutil.copy2(ENV, ROOT / ".env.bak")
            print("\n原文件已备份到 .env.bak")
        io.open(ENV, "w", encoding="utf-8", newline="\r\n").write(render(pairs))
        print("已写入 %s" % ENV)
    else:
        print("\n（这是预览，没有改动任何文件。加 --write 才会真正写入。）")

    if missing:
        print("\n还差 %d 项，用记事本打开 .env 填上：" % len(missing))
        for key, why in missing:
            print("    %s=%s" % (key, why))
        return 1
    print("\n配置齐了，可以跑冒烟测试了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
