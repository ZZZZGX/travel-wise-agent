# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""table_refs.py —— 把大表格换成 [T1] 记号，展示前再换回来。

## 和 link_refs 是同一个问题的第二次出现

链接那次的教训是：**确定性数据不该让模型誊写。**
URL 抄一遍烧 2000 token、可能抄错、还撞 max_tokens 把回答截断在半截。

价格矩阵是同一类东西，而且更大：20 个航班 × 30 天 = 600 个数字，
按 markdown 表格算 2500~4000 token。全塞进上下文有三重代价：
  1. 输入侧烧一遍，模型如果誊写输出侧再烧一遍；
  2. 模型抄 600 个数字，抄错任何一个都是**编造票价**；
  3. 直接把 max_tokens 顶爆，回答又被截断。

所以分工照旧：

    工具 → 模型      表格换成 [T1]（约 3 token）+ 一份结构化摘要
    模型 → 用户      代码把 [T1] 换回完整表格

模型负责的是「要不要给这张表、放在哪一段、怎么解读」，
不负责当复印机。这和链接那次一样，**不是放水**：
模型漏写 [T1]，restore() 会把它记进 missing，照样判失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 记号形如 [T1]。与 [L1] 同构，模型学一次会两个。
REF_RE = re.compile(r"\[T(\d+)\]")


@dataclass
class TableStats:
    """还原结果的体检报告。评测据此判定，而不是靠猜。"""

    total: int = 0
    present: list = field(default_factory=list)     # 模型写出来的记号
    missing: list = field(default_factory=list)     # 模型漏掉的记号
    unknown: list = field(default_factory=list)     # 模型编出来的记号

    @property
    def restored(self) -> int:
        return len(self.present)


class TableRefBuilder:
    """跨多次工具调用连续编号，避免撞号。"""

    def __init__(self, start_at: int = 1):
        self._next = start_at
        self.mapping: dict[str, str] = {}

    def add(self, table_text: str) -> str:
        ref = "T%d" % self._next
        self._next += 1
        self.mapping[ref] = table_text
        return "[%s]" % ref


def restore(answer: str, mapping: dict,
            append_missing: bool = True) -> tuple[str, TableStats]:
    """把回答里的 [Tn] 换成完整表格，并统计漏掉 / 编造的记号。

    表格独占整块，所以还原时前后补空行——否则会被上一行正文粘住，
    markdown 渲染不出表格。
    """
    stats = TableStats(total=len(mapping))
    if not answer:
        stats.missing = list(mapping)
        return answer or "", stats

    seen: set = set()

    def repl(m: re.Match) -> str:
        ref = "T" + m.group(1)
        if ref in mapping:
            seen.add(ref)
            return "\n\n" + mapping[ref].strip() + "\n"
        stats.unknown.append(ref)
        return m.group(0)          # 编造的记号原样留着，让它显眼

    out = re.sub(r"\n{3,}", "\n\n", REF_RE.sub(repl, answer))
    stats.present = [r for r in mapping if r in seen]
    stats.missing = [r for r in mapping if r not in seen]

    # 模型漏写了记号 —— 表格是本次回答的主体，不能就这么丢了，兜底附到末尾。
    # 注意这只是**用户侧兜底**：missing 照样记在 stats 里，评测该判失败还是判失败。
    # 评测时传 append_missing=False，测的就是模型自己有没有给全。
    if stats.missing and append_missing:
        out = out.rstrip() + "\n\n" + "\n\n".join(
            mapping[r].strip() for r in stats.missing) + "\n"
    return out, stats
