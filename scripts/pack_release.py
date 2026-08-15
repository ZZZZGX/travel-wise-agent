# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""pack_release.py —— 打一个不会夹带凭证的分发包。

    python scripts/pack_release.py                  # 生成 dist/travelwise-agent-vX.Y.Z.zip
    python scripts/pack_release.py --dry-run        # 只列出会被排除的东西

## 为什么需要这个脚本

`.gitignore` 里明明写着 `.env`，可发出去的 zip 里还是躺着一份明文 API Key。
原因很朴素：**`.gitignore` 只管 git，不管 `zip -r`。**
直接压缩工作目录时，被忽略的文件一个不落地全进去了。

这类事故的共同点是「安全措施建在了错误的层」：真正会被别人拿到的是 zip，
而防护建在 git 上。所以这里补上缺的那一层——打包走这个脚本，
它按 `.gitignore` 的规则筛，并且对疑似凭证的文件**硬拦截**，
就算有人往 .gitignore 里手滑删了一行也拦得住。

## 兜底的黑名单

按 .gitignore 过滤是主路径。但 .gitignore 是可以被改坏的，
所以另有一份不依赖它的 DENY 列表：`.env`、`*.pem`、`config/flight_api.json` 等。
两道筛子的关系是「或」——任一条命中就排除。
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 不依赖 .gitignore 的兜底黑名单。宁可多排除，也不要漏一个 Key。
DENY_PATTERNS = [
    ".env", ".env.*", "!.env.example",
    "*.pem", "*.key", "secrets.*",
    "config/flight_api.json", "config/api_keys.json",
    "**/__pycache__/**", "*.pyc",
    "*.egg-info/**", ".git/**", ".venv/**", "venv/**",
    "dist/**", "build/**", ".pytest_cache/**",
    "data/cache/**", "evals/results/**",
]

#: 打包完成后再扫一遍内容，命中即报错退出——最后一道保险。
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|[A-Za-z0-9_\-]{32,}\s*$)", re.M)
#: 键名像凭证 **且** 值长得像裸密钥（不带引号括号的长串）才算命中。
#: 只看键名会把 `_SECRET_HINTS = (...)` 这种正常的 Python 常量误报成泄密——
#: 一个天天误报的扫描器，用不了几次就会被人加 `--force` 绕过去，
#: 那时它就彻底失效了。所以宁可让规则窄一点，也要保住它的可信度。
SECRET_KEY_RE = re.compile(
    r"^\s*[A-Za-z_]*(API_KEY|TOKEN|SECRET|PASSWORD|APIKEY)[A-Za-z_]*"
    r"\s*=\s*[A-Za-z0-9_\-]{16,}\s*$", re.M)


def load_gitignore() -> list[str]:
    path = ROOT / ".gitignore"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def matches(rel: str, patterns: list[str]) -> bool:
    """极简 gitignore 匹配：够用，且**偏保守**。

    没实现完整的 gitignore 语义（那需要一个小型解析器）。
    取舍是：宁可误伤把某个该进包的文件排除掉，也不要漏放一个 .env。
    误伤看得见（包里少东西），漏放看不见（Key 已经发出去了）。
    """
    negated = False
    hit = False
    for pat in patterns:
        neg = pat.startswith("!")
        p = pat[1:] if neg else pat
        p = p.rstrip("/")
        candidates = [rel, "/" + rel]
        ok = any(fnmatch.fnmatch(c, p) or fnmatch.fnmatch(c, "*/" + p)
                 or fnmatch.fnmatch(c, p + "/*") or fnmatch.fnmatch(c, "*/" + p + "/*")
                 for c in candidates)
        # 目录前缀命中：data/cache/ 应当排掉 data/cache/x/y.json
        if not ok and (rel == p or rel.startswith(p + "/")):
            ok = True
        if ok:
            if neg:
                negated = True
            else:
                hit = True
    return hit and not negated


def read_version() -> str:
    text = (ROOT / "src" / "travelwise" / "_version.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "0.0.0"


def collect() -> tuple[list[Path], list[tuple[Path, str]]]:
    patterns = load_gitignore()
    included: list[Path] = []
    excluded: list[tuple[Path, str]] = []

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if matches(rel, DENY_PATTERNS):
            excluded.append((path, "黑名单"))
        elif matches(rel, patterns):
            excluded.append((path, ".gitignore"))
        else:
            included.append(path)
    return included, excluded


def audit(paths: list[Path]) -> list[str]:
    """对将要打包的文本文件做一次凭证扫描。"""
    findings = []
    for p in paths:
        if p.suffix.lower() in {".png", ".jpg", ".zip", ".db", ".xlsx", ".csv"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel.endswith(".example") or ".example" in rel:
            continue
        if SECRET_KEY_RE.search(text) and rel != "scripts/pack_release.py":
            for line in text.splitlines():
                m = SECRET_KEY_RE.match(line)
                if m and not line.rstrip().endswith("="):
                    val = line.split("=", 1)[1].strip()
                    if len(val) >= 16 and not val.startswith("<"):
                        findings.append("%s → %s" % (rel, line.split("=", 1)[0].strip()))
    return findings


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="打一个不夹带凭证的分发包")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    included, excluded = collect()
    version = read_version()

    print("=" * 62)
    print("打包 travelwise-agent v%s" % version)
    print("=" * 62)
    print("  纳入 %d 个文件，排除 %d 个" % (len(included), len(excluded)))

    sensitive = [(p, why) for p, why in excluded
                 if p.name.startswith(".env") or "flight_api.json" in p.name
                 or p.suffix in {".pem", ".key"}]
    if sensitive:
        print("  已挡下的凭证类文件：")
        for p, why in sensitive:
            print("     · %-34s（%s）" % (p.relative_to(ROOT).as_posix(), why))

    findings = audit(included)
    if findings:
        print("-" * 62)
        print("❌ 待打包的文件里仍疑似含有凭证，已中止：")
        for f in findings:
            print("     · %s" % f)
        print("   处理掉再打包。不要用 --force 绕过——这里没有 --force。")
        return 1

    if args.dry_run:
        print("-" * 62)
        print("（dry-run，未写文件）")
        return 0

    out = Path(args.out) if args.out else (
        ROOT / "dist" / ("travelwise-agent-v%s.zip" % version))
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in included:
            z.write(p, "travelwise-agent/" + p.relative_to(ROOT).as_posix())

    print("-" * 62)
    print("✅ %s（%.1f MB）" % (out, out.stat().st_size / 1e6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
