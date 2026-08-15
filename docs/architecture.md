# TravelWise 架构

## 分层

```
┌──────────────────────────────────────────────────────────────┐
│  入口层    cli.py                                             │
├──────────────────────────────────────────────────────────────┤
│  编排层    orchestrator.py · router.py · state.py            │
│            意图路由 / 参数抽取 / 范围判断 / 合并 / HITL 闸门   │
├──────────────────────────────────────────────────────────────┤
│  技能层    skills/flight.py · skills/destination.py          │
│            只做编排，不碰数据源、不实现算法                    │
├──────────────────────────────────────────────────────────────┤
│  工具层    tools/*.py                                         │
│            确定性计算，无 IO、无平台依赖、可脱网单测           │
├──────────────────────────────────────────────────────────────┤
│  适配层    providers/*.py    ← 与外部世界唯一的接缝            │
│            FlightProvider   : mock │ http（配置驱动）         │
│            ReminderProvider : console │ ics │ json │ mcp      │
├──────────────────────────────────────────────────────────────┤
│  外部      任意航班 API · 日历 · MCP Server · 本地文件         │
└──────────────────────────────────────────────────────────────┘

旁路：analytics/price_history.py —— 领域历史数据积累（非 Memory），
      用于将来验证购票时机启发式到底靠不靠谱。
```

**核心约束：Core 不认识任何具体平台或厂商。** 换数据源只是换一个 Provider 实现；
没有 API Key 时用 Mock，整套流程照样跑通。

## 请求流

```
用户自然语言
     │
     ▼
  Router ──── 意图 / 参数 / scope（确定性规则，作为 LLM 版的 baseline）
     │
     ▼
TravelState ── 显式任务状态，可 dump、可断言
     │
     ├── missing 非空？→ 只挡住对应技能，另一个照常执行
     │
     ├──────────────┬──────────────┐
     ▼              ▼              │
 FlightSkill   DestinationSkill    │
     │              │              │
     ▼              ▼              │
 Provider 取数   名录检索           │
 价格分析        季节 / 链接         │
     │              │              │
     └──────┬───────┘              │
            ▼                      │
          Merge ◄──────────────────┘
            │
       需要副作用？
        ╱      ╲
      No        Yes
       │         │
       │      Preview → 用户确认 → ReminderProvider
       ▼
   TaskStatus 终态
   completed / partial_complete / awaiting_approval / failed
```

## 任务状态机

`current_step` 从自由字符串改为 `TaskStatus` 枚举。原先 `done` 一个值同时表示了
「全办完」「办一半还缺参数」「办完但提醒等确认」三种处境，外部无法据此决定下一步。

```
CREATED → ROUTED → ┬→ OUT_OF_SCOPE
                   ├→ AWAITING_INPUT
                   └→ EXECUTING → ┬→ COMPLETED
                                  ├→ PARTIAL_COMPLETE
                                  ├→ AWAITING_APPROVAL
                                  └→ FAILED
```

`is_terminal()` 与 `awaits_user()` 供多轮续跑判断——为后续 State Resume 预留。

## 关键设计取舍

**为什么路由是规则而不是 LLM。** 三个理由：可脱网零成本复现因而**可被评测**；
作为 LLM 路由的 **baseline**，将来用同一套 Eval 对照才能用数据证明「上 LLM 确实更好」；
中文出行请求的意图信号相当稳定。因此 `router.py` 是长期资产，不是临时代码。

**为什么 Mock 不是玩具。** 它承担 Demo、CI、测试、失败注入、算法验证五项职责。
`FailingFlightProvider` 专门用于验证「工具失败时不得编造」。

**为什么提醒用 .ics。** 「把购票日写进日历」这个需求，用 RFC 5545 开放标准即可满足，
Google / Apple / Outlook 均可导入，无需绑定任何终端厂商私有接口。
