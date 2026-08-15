# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""pricing.py —— 把 token 换算成钱。

为什么值得单开一个模块：

一个 Agent 项目最容易被问住的问题不是"准不准"，是"**一次多少钱、一天多少钱**"。
token 数字本身不说明问题——27143 个 token 是贵还是便宜，取决于哪个模型。
所以这里把「模型 → 单价」这张表显式落盘，让每一条 trace、每一轮评测
都能直接给出金额，而不是让人拿着 token 数去翻厂商官网。

## 三条原则

1. **价格是会变的外部事实，所以它是配置，不是常量。**
   内置表只是兜底默认值；`config/llm_pricing.json` 存在时以它为准，
   环境变量 `TRAVELWISE_PRICING` 可以指到别的文件。

2. **不认识的模型返回 None，不返回 0。**
   0 会被当成"免费"混进汇总，是一种静默的假数据。None 会让报表
   明确打出「单价未知」，与项目「禁止假成功」一致。

3. **币种显式携带。** 混着人民币和美元求和是常见事故，
   所以 Cost 里带 currency，跨币种求和会直接拒绝而不是硬加。

单价单位统一为「每 100 万 token 的价格」，与主流厂商定价页一致。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..paths import CONFIG_DIR

#: 内置兜底价格表。数值只是**默认假设**，随时可能过期——
#: 需要准确金额时请用 config/llm_pricing.json 覆盖，并在报告里注明日期。
DEFAULT_PRICING: dict[str, dict] = {
    "claude-sonnet-4-6":   {"input": 3.00, "output": 15.00, "currency": "USD"},
    "claude-haiku-4-5":    {"input": 1.00, "output": 5.00,  "currency": "USD"},
    "gpt-4o-mini":         {"input": 0.15, "output": 0.60,  "currency": "USD"},
    "gpt-4o":              {"input": 2.50, "output": 10.00, "currency": "USD"},
    "deepseek-chat":       {"input": 2.00, "output": 8.00,  "currency": "CNY"},
    "deepseek-reasoner":   {"input": 4.00, "output": 16.00, "currency": "CNY"},
    #: 离线回放不产生任何费用，但**必须显式写成 0**，
    #: 否则会落进"单价未知"，让人以为是漏配。
    "scripted-fixtures":   {"input": 0.0,  "output": 0.0,   "currency": "CNY"},
}


@dataclass(frozen=True)
class Cost:
    """一笔花费。amount 为 None 表示单价未知——**不是 0**。"""

    amount: float | None = None
    currency: str = ""
    model: str = ""
    known: bool = True

    def __add__(self, other: "Cost") -> "Cost":
        if not isinstance(other, Cost):
            return NotImplemented
        # 任一侧未知 → 结果未知。把未知当 0 加进去就是编造。
        if not self.known or not other.known:
            return Cost(None, self.currency or other.currency, "", known=False)
        if self.amount is None:
            return other
        if other.amount is None:
            return self
        if self.currency and other.currency and self.currency != other.currency:
            # 跨币种不硬加。宁可报未知，也不给一个含义不明的数。
            return Cost(None, "MIXED", "", known=False)
        return Cost((self.amount or 0) + (other.amount or 0),
                    self.currency or other.currency, "", known=True)

    def __radd__(self, other):
        if other == 0:
            return self
        return self.__add__(other)

    def text(self) -> str:
        if not self.known or self.amount is None:
            return "单价未知"
        symbol = {"CNY": "¥", "USD": "$"}.get(self.currency, "")
        if self.amount == 0:
            return "%s0（离线回放，不产生费用）" % symbol
        if self.amount < 0.01:
            return "%s%.5f" % (symbol, self.amount)
        return "%s%.4f" % (symbol, self.amount)

    def to_dict(self) -> dict:
        return {"amount": self.amount, "currency": self.currency,
                "known": self.known}


class PriceTable:
    """模型 → 单价。优先级：显式传入 > 配置文件 > 内置默认。"""

    def __init__(self, table: dict | None = None, path: str | Path | None = None):
        self.table = dict(DEFAULT_PRICING)
        self.source = "builtin"
        path = path or os.environ.get("TRAVELWISE_PRICING") \
            or str(CONFIG_DIR / "llm_pricing.json")
        try:
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
            models = loaded.get("models") if isinstance(loaded, dict) else None
            if isinstance(models, dict):
                self.table.update(models)
                self.source = str(path)
        except (OSError, json.JSONDecodeError, TypeError):
            pass                       # 没配置就用内置默认，不报错
        if table:
            self.table.update(table)
            self.source = "explicit"

    def entry(self, model: str) -> dict | None:
        if not model:
            return None
        if model in self.table:
            return self.table[model]
        # 厂商常在模型名后挂日期后缀（claude-sonnet-4-6-20260101），
        # 做一次最长前缀匹配，避免因为一个后缀就整批算不出钱。
        best = ""
        for key in self.table:
            if model.startswith(key) and len(key) > len(best):
                best = key
        return self.table.get(best) if best else None

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Cost:
        entry = self.entry(model)
        if not entry:
            return Cost(None, "", model, known=False)
        amount = (input_tokens / 1_000_000.0) * float(entry.get("input", 0)) \
            + (output_tokens / 1_000_000.0) * float(entry.get("output", 0))
        return Cost(round(amount, 8), entry.get("currency", "CNY"), model, known=True)


#: 进程级默认表，避免每条 span 都去读一次文件
_DEFAULT_TABLE: PriceTable | None = None


def default_table() -> PriceTable:
    global _DEFAULT_TABLE
    if _DEFAULT_TABLE is None:
        _DEFAULT_TABLE = PriceTable()
    return _DEFAULT_TABLE


def estimate(model: str, input_tokens: int, output_tokens: int) -> Cost:
    return default_table().cost(model, input_tokens, output_tokens)
