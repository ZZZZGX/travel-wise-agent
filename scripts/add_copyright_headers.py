#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""add_copyright_headers.py —— 给每个源文件加上版权与授权声明。

    python scripts/add_copyright_headers.py --author "你的名字" --dry-run
    python scripts/add_copyright_headers.py --author "你的名字" --write

## 为什么每个文件都要加

只在根目录放一个 LICENSE 是不够的。代码会被**逐文件**复制走——
有人拷走 `price_analysis.py` 塞进自己的产品，那个文件里如果没有任何声明，
他事后主张「不知道有许可限制」是有说服力的。

反过来，每个文件头部都写着授权限制时：
  - 对方要商用，必须**主动删掉**这几行；
  - 删除权利管理信息，在法律上是比单纯侵权更重的情节。

**把条款写得越显眼，对你越有利。** 这一点和「设陷阱」的直觉相反，
但法院看的是对方是否「应知或明知」，藏起来只会帮对方脱责。

## 只加，不覆盖

已经有版权头的文件会被跳过，不会重复叠加。
CRLF 文件保持 CRLF —— 否则 git 会把整个文件标成全量改写。
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMPLATE = """# Copyright (c) {year} {author}. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""

#: 这些目录不碰
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist",
             ".pytest_cache", "node_modules"}

#: 判断「已经有版权头了」的标记
MARKER = "Copyright (c)"


def iter_sources(root: Path):
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS or part.endswith(".egg-info")
               for part in path.parts):
            continue
        yield path


def add_header(path: Path, header: str, write: bool) -> str:
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    if MARKER in text[:600]:
        return "已有，跳过"

    lines = text.split("\n")
    insert_at = 0

    # shebang 必须留在第一行，编码声明必须在前两行 —— 版权头插在它们后面
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    if len(lines) > insert_at and "coding" in lines[insert_at]:
        insert_at += 1

    new_lines = lines[:insert_at] + header.rstrip("\n").split("\n") + lines[insert_at:]
    new_text = "\n".join(new_lines)

    if write:
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text.replace("\n", "\r\n") if crlf else new_text)
    return "已加" if write else "将加"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="给源文件加版权与授权声明")
    ap.add_argument("--author", required=True, help="版权所有人姓名")
    ap.add_argument("--year", default="2026")
    ap.add_argument("--write", action="store_true", help="真正写入（默认只预览）")
    args = ap.parse_args(argv)

    header = TEMPLATE.format(year=args.year, author=args.author)

    print("=" * 62)
    print("版权头　｜　%s %s" % (args.year, args.author))
    print("=" * 62)
    print(header)
    print("-" * 62)

    counts = {"将加": 0, "已加": 0, "已有，跳过": 0}
    for path in iter_sources(ROOT):
        status = add_header(path, header, args.write)
        counts[status] = counts.get(status, 0) + 1
        if status != "已有，跳过":
            print("  %-12s %s" % (status, path.relative_to(ROOT).as_posix()))

    print("-" * 62)
    for k, v in counts.items():
        if v:
            print("  %s：%d 个文件" % (k, v))

    if not args.write:
        print("")
        print("（预览，未写入。加 --write 真正执行。）")
        print("执行后请务必跑一次测试：python -m unittest discover -s tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
