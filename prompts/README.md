# Prompts

TravelWise 的 **Agent 指令**，与代码分离存放。

平台无关的纯 Markdown：没有厂商专有 frontmatter、没有"必须用某平台工具"的约束、
没有写死的私有工具名。可直接用作任意 LLM 的 system prompt，或 Claude Skills /
Cursor Rules 等指令文件。

> ⚠️ **这些文件不是业务规则的权威来源。** 权威来源是
> [`../docs/decision-policy.md`](../docs/decision-policy.md)，本目录的指令由它派生。
> 改规则时请先改规范，再改 Eval，最后同步代码与本目录，避免出现
> 「Prompt 写 A、代码实现 B」的漂移。

| 指令 | 对应代码 |
|---|---|
| `orchestrator.md` | `router.py` + `orchestrator.py` |
| `flight_analyst.md` | `skills/flight.py` + `tools/price_analysis.py` |
| `destination_curator.md` | `skills/destination.py` + `tools/destination_search.py` |
