# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""dump_city_codes.py —— 把城市三字码表导出成 txt / json，方便人工核对与增补。

**不消耗任何额度，不联网。** 表本来就在 data/source/city_codes.csv 里，
这个脚本只是把它换个格式打出来，顺便做一次体检：
  - 有没有重复的三字码（两个城市指向同一个码 = 至少有一个是错的）；
  - 有没有格式不对的码（不是 3 个大写字母）；
  - 常用城市抽查。

用法：
    python scripts/dump_city_codes.py                 # 打印到屏幕
    python scripts/dump_city_codes.py --out city_codes.txt
    python scripts/dump_city_codes.py --check 上海 乌鲁木齐 喀什
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travelwise.tools import city_codes                # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="输出文件路径（.txt 或 .json）")
    ap.add_argument("--check", nargs="*", help="抽查这些城市名能否解析")
    args = ap.parse_args()

    rows = city_codes._read_rows()
    table = city_codes.load_table()

    if args.check:
        for name in args.check:
            code = city_codes.try_resolve(name)
            print("%-12s -> %s" % (name, code or "查不到（需要在 csv 里增补）"))
        return 0

    # -- 体检：重复码与格式错误会让你查到错误的城市，而且不会报错 --
    by_code: dict[str, list[str]] = {}
    bad: list[str] = []
    for row in rows:
        name = (row.get("城市名") or "").strip()
        code = (row.get("三字码") or "").strip().upper()
        if not (len(code) == 3 and code.isalpha()):
            bad.append("%s -> %r" % (name, code))
        by_code.setdefault(code, []).append(name)

    dupes = {c: names for c, names in by_code.items() if len(names) > 1}

    lines = ["# 城市三字码表（来源：data/source/city_codes.csv）",
             "# 共 %d 个城市，%d 个可匹配键（含别名与去后缀形式）" % (len(rows), len(table)),
             ""]
    for row in sorted(rows, key=lambda r: (r.get("省份") or "", r.get("城市名") or "")):
        lines.append("%-3s  %-10s  %s"
                     % ((row.get("三字码") or "").strip(),
                        (row.get("城市名") or "").strip(),
                        (row.get("省份") or "").strip()))

    if dupes:
        lines += ["", "# ⚠️ 重复的三字码（同一个码对应多个城市，至少有一个是错的）："]
        lines += ["#   %s -> %s" % (c, "、".join(n)) for c, n in dupes.items()]
    if bad:
        lines += ["", "# ⚠️ 格式不对的码（必须是 3 个大写字母）："] + ["#   " + b for b in bad]

    text = "\n".join(lines)

    if args.out:
        path = Path(args.out)
        if path.suffix.lower() == ".json":
            payload = {(r.get("城市名") or "").strip(): (r.get("三字码") or "").strip()
                       for r in rows}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.write_text(text, encoding="utf-8")
        print("已导出 %d 个城市 → %s" % (len(rows), path))
        if dupes or bad:
            print("⚠️ 发现 %d 个重复码、%d 个格式错误，详见文件末尾。" % (len(dupes), len(bad)))
        return 0

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
