# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""link_refs.py —— 把 URL 换成 [L1] [L2] 这样的引用记号，展示前再换回来。

## 为什么需要它

工具返回的搜索链接是 percent-encoded 的，一条就要 50~80 个 token。
苏州有 38 条，光让模型把链接誊写一遍就要 2000+ token —— 撞 max_tokens
上限，回答被截断在半截。

但这里有个更根本的问题：**URL 是确定性数据，让模型逐字抄是错的分工。**
抄一遍要烧 token、增延迟，还可能抄错一个字符让链接彻底失效。
模型该做的是"决定给哪几条、怎么组织"，不是当复印机。

所以：

    工具 → 模型      URL 换成 [L1] [L2]（一条约 3 token）
    模型 → 用户      代码把 [L1] 换回真实 URL

## 这不是在给评测放水

模型仍然必须把每个 [Ln] 逐条写出来，漏一个照样被 link_preservation 判失败。
变的只是每条链接的成本，不是"链接由谁负责给全"这个责任归属。

同时还多了一个原来抓不到的检查：模型如果编出一个 [L99]，
restore() 会把它作为 unknown 报出来 —— 相当于"编造链接"的探测器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 记号形如 [L1]。用方括号是因为它在中文正文里视觉上足够突兀，模型不容易漏抄。
REF_RE = re.compile(r"\[L(\d+)\]")

#: 同时覆盖 http(s) 和 App 深链（xhsdiscover://）
URL_RE = re.compile(r"(?:https?|xhsdiscover)://[^\s\)\]｜|、，,。；;\"'<>]+")


@dataclass
class RestoreStats:
    """还原结果的体检报告。评测据此判定，而不是靠猜。"""

    total: int = 0            # 工具给出的记号总数
    total_primary: int = 0    # 其中的主链接（http 网页版）数量
    present: list = field(default_factory=list)      # 模型写出来的记号
    missing: list = field(default_factory=list)      # 模型漏掉的记号
    missing_primary: list = field(default_factory=list)
    unknown: list = field(default_factory=list)      # 模型编出来的记号

    @property
    def restored(self) -> int:
        return len(self.present)


def is_primary(url: str) -> bool:
    """主链接 = 任何浏览器都能打开的网页版。

    App 深链（xhsdiscover://）只在装了 App 的移动端有效，属于附加入口；
    红线只卡主链接，否则会因为要求过苛而变成噪音。
    """
    return url.startswith("http")


def mask(text: str, start_at: int = 1) -> tuple[str, dict]:
    """把 text 里的 URL 换成记号。

    返回 (换过的文本, {"L1": url, ...})。相同 URL 复用同一个记号。
    start_at 让多次调用（多个工具结果）能接着编号而不撞号。
    """
    mapping: dict[str, str] = {}
    by_url: dict[str, str] = {}
    counter = [start_at]

    def repl(m: re.Match) -> str:
        url = m.group(0)
        if url not in by_url:
            ref = "L%d" % counter[0]
            counter[0] += 1
            by_url[url] = ref
            mapping[ref] = url
        return "[%s]" % by_url[url]

    return URL_RE.sub(repl, text or ""), mapping


def restore(answer: str, mapping: dict) -> tuple[str, RestoreStats]:
    """把回答里的 [Ln] 换回真实 URL，并统计漏掉 / 编造的记号。"""
    stats = RestoreStats(
        total=len(mapping),
        total_primary=sum(1 for u in mapping.values() if is_primary(u)))
    if not answer:
        stats.missing = list(mapping)
        stats.missing_primary = [r for r, u in mapping.items() if is_primary(u)]
        return answer or "", stats

    seen: set = set()

    def repl(m: re.Match) -> str:
        ref = "L" + m.group(1)
        if ref in mapping:
            seen.add(ref)
            return mapping[ref]
        stats.unknown.append(ref)
        return m.group(0)      # 编造的记号原样留着，让它显眼

    out = REF_RE.sub(repl, answer)
    stats.present = [r for r in mapping if r in seen]
    stats.missing = [r for r in mapping if r not in seen]
    stats.missing_primary = [r for r in stats.missing if is_primary(mapping[r])]
    return out, stats
