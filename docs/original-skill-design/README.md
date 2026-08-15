# 原始 Agent Skill 设计（V1 · 历史资料）

本目录保留 TravelWise 在**卓易科技 OPC / Agent Skill 训练营**时期的原始设计文档，
用于说明项目来源与架构演进，**不参与当前代码运行**。

| 文件 | 说明 |
|---|---|
| `travelwise-orchestrator.SKILL.md` | 总控 Skill：意图路由、参数抽取、Scope 控制、结果合并 |
| `flight-price-analyzer.SKILL.md` | 机票 Skill：取数、价格分析、提醒构造 |
| `destination-curator.SKILL.md` | 目的地 Skill：名录检索、季节判断、搜索入口 |
| `data-storage-notes.txt` | 原始数据存储说明 |

## 演进对照

| V1（平台 Skill） | V2（平台无关 Core） |
|---|---|
| `SKILL.md` 前置元数据 + 平台激活配置 | `prompts/*.md` 纯 Markdown，任意 LLM 可用 |
| 平台 `http` 工具取数 | `providers/http_flight.py`（标准库 urllib） |
| 平台私有待办 / 闹钟工具 | `providers/reminders.py`（console / ics / json / mcp） |
| `price_analyzer.py` | `tools/price_analysis.py` |
| `price_fetcher.py` | `providers/http_flight.py` |
| `reminder_builder.py` | `ReminderProvider` + `FlightSkill.build_reminder` |
| `spot_filter.py` | `tools/destination_search.py` |
| `spot_cache_manager.py` | `tools/spot_repository.py` |
| `social_search.py` | `tools/search_links.py` |
| `season_detector.py` | `tools/season.py` |
| `city_codes.py` | `tools/city_codes.py` |
| `data_accumulator.py` | `analytics/price_history.py` |

V1 中体现的产品判断（不擅自扩大范围、只给搜索结果页、空季节不冒充全年、
副作用需确认）已全部固化进 `docs/decision-policy.md` 与 `evals/cases.json`。
