# TravelWise（请先阅读notice）

![status](https://img.shields.io/badge/status-v0.8.0%20beta-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![tests](https://img.shields.io/badge/tests-351%20passing-brightgreen)
![evals](https://img.shields.io/badge/router%20evals-32%2F32-brightgreen)
![hard](https://img.shields.io/badge/router%20hard-4%2F19-orange)
![agent-evals](https://img.shields.io/badge/agent%20evals-14%2F14-brightgreen)
![deps](https://img.shields.io/badge/runtime%20deps-none-brightgreen)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

> An evaluation-driven AI travel agent —— 平台无关、开箱即跑、带评测集的出行决策智能体。

TravelWise 把出行规划里最耗神的两件事自动化：**「机票该提前几天买」** 和 **「目的地到底玩什么」**。

它不是一个聊天机器人壳子，而是一个完整的 Agent 工程实践：意图路由、参数抽取、范围控制、工具调用、失败降级、人工确认闸门，以及一套**可回归运行的评测集**。

```bash
git clone <your-repo-url> && cd travelwise-agent
python -m unittest discover -s tests    # 351 项测试
python evals/run_evals.py               # Router 评测：回归闸门 32 条 + 难例组（19 条）
python evals/run_agent_evals.py         # Agent 评测：9 项指标
PYTHONPATH=src python -m travelwise --demo
```

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [架构](#架构)
- [快速开始](#快速开始)
- [Agent 工作流](#agent-工作流)
- [可靠性设计](#可靠性设计)
- [评测](#评测)
- [接入真实数据源](#接入真实数据源)
- [项目结构](#项目结构)
- [已实现 / 未实现](#已实现--未实现)
- [Roadmap](#roadmap)
- [接口购买指南](docs/api-purchase.md)
- [原理、稳定性与已知缺陷](docs/principles-and-limits.md)
---

## 它解决什么问题

**痛点一 · 机票蹲守。** 用户真正耗神的不是「查」这个动作，而是「每天打开看有没有降价」的持续蹲守——平台把比价成本转嫁给了个人。

TravelWise 的做法是从**预测**转向**确定性查询**：横向扫描近期出发日，读出这条航线「价格 ~ 提前天数」的形状，取谷底提前量 N，平移到出行日：

```
建议购票日 = 出行日 − N
```

几周盯盘压缩成一次到点确认。

结论取自**逐航班共识**（每班各自算最低点，看多数落在哪天），而不是「当日最低价」曲线的最低点——
后者每天在不同的航班集合上取 min，一班只飞那天的中转特价就能把整条曲线拉平。
聚合口径统一用**可比面板中位价**：只算全窗口每天都在飞的直飞航班，逐日取中位数。

**痛点二 · 流量噪声。** 现有攻略普遍是「互联网声量大就推荐什么」，缺少季节与人流的考量。TravelWise 以官方 A 级景区名录为候选池绕开流量排序，再用**二层场景发现**兜住非官方网红打卡地：先搜「城市 + 打卡 / 出片 / 小众景点 / citywalk」，把搜索结果的标题摘要读一遍，从中**归纳出地名**（翠湖公园、钱局街、海埂大坝……），再按地名各给一个入口，每个地点都附上「被几条结果提到」和出处标题。

一层给的是「昆明打卡」这个话题的搜索页——用户还得自己读几十篇帖子挑地方；二层给的是**已经收敛过的地点清单**，用户只点自己感兴趣的那个。**两层都只返回搜索结果页链接、不返回具体帖子**——AI 不做筛选，就没有选错的风险；没配搜索源时输出会明写「二层发现未启用」，不会拿模型编的地名顶上。

---

## 架构

```
                         用户自然语言请求
                                │
                                ▼
                    ┌───────────────────────┐
                    │      Router           │  意图路由 / 参数抽取 / 范围判断
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │    TravelState        │  显式任务状态（可 dump、可断言）
                    └───────────┬───────────┘
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
      ┌───────────────┐                   ┌───────────────┐
      │  Flight Skill │                   │  Dest. Skill  │
      └───────┬───────┘                   └───────┬───────┘
              │                                   │
              ▼                                   ▼
      价格分析 / 时刻渲染                  名录检索 / 季节 / 链接
              │                                   │
              └─────────────────┬─────────────────┘
                                ▼
                        Orchestrator Merge
                                │
                        需要副作用操作？
                          ╱            ╲
                        No              Yes
                         │               │
                        END        Preview → 用户确认
                                         │
                                         ▼
                              ┌──────────────────────┐
                              │  ReminderProvider    │
                              │  console / ics /     │
                              │  json / mcp          │
                              └──────────────────────┘

  ┌──────────────────────────────────────────────────────────┐
  │  Provider 层 —— 与外部世界唯一的接缝                        │
  │  FlightProvider:  mock │ http（任意厂商，配置驱动）          │
  │  ReminderProvider: console │ ics │ json │ mcp（通用适配器） │
  └──────────────────────────────────────────────────────────┘
```

**核心设计：所有外部依赖都在 Provider 接口之后。** Core 不认识任何具体平台或厂商——换数据源只是换一个实现，业务逻辑一行不动；没有 API Key 时用 Mock，整套流程照样跑通。

---

## 快速开始

环境要求：**Python 3.10+**，无需安装任何第三方包。

```bash
# 1. 跑测试与评测（验证一切正常）
python -m unittest discover -s tests -v
python evals/run_evals.py

# 2. 跑内置场景演示（含失败与边界用例）
PYTHONPATH=src python -m travelwise --demo

# 3. 提问
PYTHONPATH=src python -m travelwise "8月28号从上海飞成都，机票什么时候买划算"
PYTHONPATH=src python -m travelwise "沈阳有什么好玩的"
PYTHONPATH=src python -m travelwise "月底飞乌鲁木齐，新疆有什么玩的"

# 4. 带购票提醒（会先预览并请求确认）
PYTHONPATH=src python -m travelwise "8月28号从北京飞广州" --reminder --reminder-provider ics

# 5. 结构化输出（便于接入其它系统）
PYTHONPATH=src python -m travelwise "沈阳有什么好玩的" --json
```

### 三种运行模式

本项目刻意保留三条并存的执行路径，而不是用"更像 Agent"的那条替换掉前面的 ——
**没有基线，"上了 LLM 更好"就只是感觉。**

| 模式 | 组成 | 命令 | 特点 |
|---|---|---|---|
| **1 · Deterministic Baseline** | Rule Router + 固定编排 | `python -m travelwise "沈阳有什么好玩的"` | 零模型成本、可复现，作为回归基线 |
| **2 · LLM Router** | LLM 抽参 + 固定编排 | `... --router llm` | 解析更鲁棒，编排仍确定 |
| **3 · Agent Loop** | LLM 自主选工具闭环 | `... --agent-loop` | 最灵活，成本与不确定性也最高 |

三者跑**同一套** Skill 与 Tool，因此可以横向对照：

```bash
python evals/run_evals.py --router llm    # 同一批用例换 LLM 路由
python evals/compare_routers.py           # 准确率 / 延迟 / Token / 降级次数
python evals/run_agent_evals.py           # Agent 级：选工具 / 参数 / 红线
```

### Router 评测

Router 评测长期停在 32/32。说明**用例已经不能区分任何东西了**——
拿它去对照规则路由和 LLM 路由，只会得到 32/32 对 32/32，答不出「换成 LLM 值不值这个价」。

所以 `evals/` 下现在有两台仪器，它们回答的是不同的问题：

| | `cases.json` | `hard_cases.json` |
|---|---|---|
| 叫法 | 回归闸门 | 能力基线 |
| 问题 | 有没有**坏**？ | 现在做到**哪一步**？ |
| 常态 | 全绿（32/32） | 不全绿（4/19） |
| 退出码 | 参与 | **不参与** |

难例组不进闸门，是因为闸门长期红着，最后一定会被人 disable 掉，
连带那些真正该守的回归用例一起失效。反过来，把难例塞进闸门再把期望值改软
（「反正现在做不到，就按现在的行为写吧」），那是把评测集降格成快照——
它从此只能证明「行为没变」，再也不能证明「行为是对的」。

**难例组的期望值写的是正确行为，不是当前行为。**

六类难例：改写、噪声、否定与冲突、超出范围、日期表达、歧义。举三条现在还红着的：

```
H-S1  从上海飞成都的高铁票什么时候买划算   → 应判超范围，实得 intents=['flight']
H-C1  别给我查机票，我就想知道成都有什么好玩的 → 显式否定被无视，凭空多一个机票意图
H-A1  朝阳有什么好玩的                    → 北京朝阳区 / 辽宁朝阳市 / 长春朝阳区，直接选了一个
```

这三条都指向同一件事：**关键词匹配没有「否定」和「范围」的概念**。
句子里出现「机票」就判机票意图，哪怕前面写着「别给我查机票」。
这正是规则路由的能力边界，也正是 LLM 路由应当拿出证据来的地方——
现在这个证据有了分母。


**一、槽位里塞进用户没说过的值。** 「帮我看看这两天飞成都的票价走势，我从上海出发」
抽出的是 `origin="帮我看看这两天"`、`destination="成都的票价走势"`，
而且 `missing` 是空的——系统认为参数齐了，转头拿这坨东西去查航班。
根因不是正则写得糙，而是**同一条规则住在两个地方**：`slots.py` 里的多轮补槽位
会用城市码表校验，首轮的 `route()` 不会。现在谓词下沉到 `router.py`，两边共用一份，
并且顺序改成「先尽力救、再判缺失」：`成都的机票` → 取最长可识别子串 → `成都`；
`帮我看看这两天` → 救不回来 → 判缺失，去问。

**二、「下周五」解析成周三。** `REL_DATE` 里有 `下周: 7`，而「下周五」含子串「下周」，
于是直接 `today + 7` ——和今天同一个星期几。用户说周五，系统按周三查价，还查得理直气壮。
同一个 bug 让「下下周」也被「下周」吃掉，差整整一周。现在星期几先于泛化词匹配，
并且长键优先。`tests/test_router_hardening.py` 里有一条把七个星期几全扫一遍——
逐条写死日期只能证明那几条对，扫一遍才证明规则本身对。

### Agent 级评测：九项指标，而不是一个百分比

原来这里报的是「17/17 通过」。这个数字有三个毛病：

1. **把不同性质的东西加在了一起。** 工具选错和编造价格都算「一条不通过」，
   可前者是能力问题、后者是安全问题。混成一个百分比之后，
   红线被稀释成了 1/17 的权重。
2. **只有通过率，没有代价。** 一个 100% 通过但每次跑 6 轮、烧 8000 token 的 Agent，
   和 100% 通过、2 轮、1200 token 的 Agent，在这个数字下长得一模一样。
3. **探测器自检占了 5/17。** 那是「尺子准不准」的检查，不是模型的分数——
   等于用一把没校准的尺子量出来的成绩里，有 29% 是这把尺子在给自己打分。

现在拆成九项，分三组：

| 组 | 指标 | 判定方式 |
|---|---|---|
| **质量** | 工具选择 / 工具参数 / 任务完成 / **失败恢复** | 通过率 |
| **安全**（红线） | 不编造 / 人工确认 / 链接保全 | **全过才算过**，不看平均分 |
| **代价** | 延迟 p50·p95 / Token / 成本 | 报数值，不判红绿 |

两条容易被忽略的规矩：

- **分母为 0 时报 `n/a`，不报 100%。** 一项指标没有任何适用用例，说明它
  *没被测*，而不是「全过了」。离线跳过 `link_preservation` 时正是这种处境。
- **探测器自检降级成前置闸门。** 尺子不准时直接停下、退出码 2，
  而不是继续量完再报一个漂亮的百分比。

**新增的 `failure_recovery` 与 `no_fabrication` 是两回事：**

```
no_fabrication    工具挂了，它有没有编数字？  —— 看的是「有没有多说」
failure_recovery  工具挂了，它有没有收住场？  —— 看的是「有没有少做」
```

一句「查询失败」没编任何数字（不编造 ✅），但把本可交付的目的地清单
一起丢了（失败恢复 ❌）。两者可以一个过一个不过，所以必须分开测。

### 多轮：追问之后，得接得住

「缺参数就问」原先只做了一半 —— 能问，但问完就结束了：

```
用户：我想买张机票
Agent：还需要你补充：出发城市、目的地城市、出行日期。
用户：上海到成都，8月28号
Agent：这条请求看起来不属于机票或目的地范围。      ← 每轮都从零路由
```

**问了但接不住，比压根不问更糟** —— 它把一次可完成的任务变成了死循环。
补上的那一半在 `session.py` + `TravelState.apply_slots()` + `Agent.resume()`：

```bash
# 两次独立的命令，同一个任务
PYTHONPATH=src python -m travelwise --session trip-1 "我想买张机票"
PYTHONPATH=src python -m travelwise --session trip-1 "上海到成都，8月28号"

PYTHONPATH=src python -m travelwise --chat          # 交互式
PYTHONPATH=src python -m travelwise --replay trip-1 # 回看整段对话
```

四条约束，每条都有对应的测试：

| 约束 | 具体表现 |
|---|---|
| **状态可跨进程** | 落盘成 JSON，上午问一半下午接着聊。只存内存的多轮，在 CLI 里等于没有多轮 |
| **沿用必须显式** | 沿用来的槽位记进 `state.carried_over`，并在回复里列出来。静默继承是事故来源 |
| **歧义就问，不猜** | 出发地和目的地都缺、用户只回「上海」→ 再问一次，不按顺序硬塞 |
| **确认三态** | 确认 / 取消 / **听不懂再问一次** —— 不把听不懂折叠成任何一边 |

一个真实事故：上一轮是「上海→成都」，用户说「不对，是从北京飞」，
路由抽不出完整航线于是三个槽位全缺，沿用逻辑把「上海」原样填回去，
**把用户刚说的「北京」静默丢掉**。结论写进了代码：显式提及永远压过沿用，
且被显式提及的槽位不再参与沿用（`tests/test_session_resume.py`
的 `test_explicit_mention_beats_carry_over` 钉住这条）。

### 调用链追踪

「Agent 跑错了，你怎么定位？」——在这套埋点存在之前，本项目的答案只能是
「看最终回答」。而最终回答恰恰是**最不可能告诉你哪里错了**的那个东西：
模型少写了一条链接、工具其实失败了但被含糊带过、第三轮才收敛、
提醒到底写出去没有——这些在最终回答里全都看不出来。

```bash
python -m travelwise --trace "8月28号从上海飞乌鲁木齐，新疆有什么玩的"
python scripts/view_trace.py --latest --open      # 渲染成一页 HTML
python scripts/view_trace.py --list               # 现有的 trace
```

两条执行路径都埋了（`orchestrator` 的固定编排、`agent_loop` 的模型自主选工具），
span 串成一棵树：

```
agent.handle                 3.3ms  step=partial_complete
  route                      1.0ms  router=rule  intents=[flight, destination]
  dispatch                   1.6ms  delivered=[destination]  errors=1  partial=true
    skill.flight             0.0ms  ✗ 请求超时（15s 内未返回）
    skill.destination        1.4ms
```

三个不太显然的取舍：

**`dispatch` 在这里是绿的，红的是 `skill.flight`。** 本项目最看重的行为是
「一个技能失败不拖垮另一个」，如果部分交付在 trace 上和整轮全崩长得一模一样，
那这条行为就等于没被观测到。所以父节点记 `partial=true` 而不是标红，
红留给真正出事的那一格——位置更准。

**状态分 `ok` / `error` / `rejected` 三档，第三档不是「轻一点的失败」。**
参数不合法、叫了不存在的工具，是调用方（模型）用错了、被代码挡下了；
接口超时是外部真的坏了。看 trace 的人第一件想知道的就是这个分岔：
该去改 prompt，还是该去看接口。判断依据取 `ToolResult.error_kind`
而不是错误文案——靠 `startswith("参数不合法")` 去认，等于让状态取决于某句提示语的措辞。

**人工确认闸门单独记事件。** `hitl.awaiting_approval` → `hitl.approve` → `reminder.create`
三个记号缺一不可：一次运行到底有没有真的写出去、是谁点的头、还是根本没人点，
这是最终回答里绝对看不出来、而又最要紧的事。用户点了「取消」时，
trace 上不会出现 `reminder.create`——这一点由测试钉死。

**参数在写入时就已脱敏**（`tracing.digest_arguments`）：键名含 key / token / secret 的整值打码，
形似凭证的值按长度替换，长列表只留长度。trace 是要贴进 issue、发给同事的东西，
脱敏必须发生在落盘那一刻——等到渲染时再做，那份没脱敏的 JSONL 已经在磁盘上了。

不开 `--trace` 时 tracer 是空转的：不建目录、不写文件、不多花一次 uuid。
可观测性不该让没开它的人付钱。

### 接真实模型

上面所有数字的默认口径都是**离线回放**（`ScriptedLLMClient` 播录好的响应）。
它能证明管道通畅，但证明不了模型本身守不守规矩 —— 录音里的回答是写死的，
不管模型多爱编价格，离线都不会翻车。要拿到有意义的结论，得接真机：

```bash
export TRAVELWISE_LLM_PROVIDER=openai      # DeepSeek 走 OpenAI 兼容协议
export TRAVELWISE_LLM_API_KEY=sk-...
export TRAVELWISE_LLM_BASE_URL=https://api.deepseek.com
export TRAVELWISE_LLM_MODEL=deepseek-chat

python scripts/smoke_real_llm.py           # 无 Key 时自动跳过，可直接进 CI
python scripts/record_real_run.py          # 冒烟 + 评测，结果落成带日期的记录
python evals/compare_routers.py            # 这时的数字才第一次有意义
```

<!-- real-model-run:begin -->
**最近一次真机验证：尚无记录** —— 跑 `python scripts/record_real_run.py` 生成。
<!-- real-model-run:end -->

这一行由 `scripts/check_consistency.py` 依据 [`docs/real-model-runs.md`](docs/real-model-runs.md)
自动同步，**不要手改**。手写的「已跑通」没有保质期：模型换了、接口变了，
那句话依然会挂在那里，而这正是上一版 README 出错的方式 ——
它在作者早已用 DeepSeek 跑通之后，仍写着「尚未用真实模型跑过」。
一句没有时间戳的断言，错的方向可以是任意一边。

### 消息契约测试：把"只有真机才能发现的错"变成 CI 能抓的错

`ScriptedLLMClient` 不校验历史消息形状，所以 Agent Loop 用什么格式回填工具结果，
离线测试一律看不出来 —— 这正是本项目踩过的坑：**90 项测试全绿，但真机第一次
多轮调用就会被 400 打回**。

修法是把厂商协议差异收进 `LLMClient.serialize_messages()`，Agent Loop 只产出
内部的 `UserMessage / AssistantMessage / ToolResultMessage`；再用
`tests/test_message_contract.py` 拦截 HTTP 出口，逐字段断言真正会发出去的 payload：

| | assistant 轮 | 工具结果轮 |
|---|---|---|
| **Anthropic** | `content` 是数组，含 `{"type":"tool_use","id","name","input"}` | 仍是 `role:"user"`，content 数组里放 `tool_result`，同轮结果**合并成一条** |
| **OpenAI** | `tool_calls[]` 须有 `id` / `type:"function"` / `arguments` 是 **JSON 字符串** | 每个结果一条 `role:"tool"` 消息，带 `tool_call_id` |

`tool_use_id` 两侧必须配对 —— 这是真机最常见的 400，也由契约测试钉住。

也可以安装后直接用 `travelwise` 命令：

```bash
pip install -e .
travelwise --demo
```

### 输出示例

```
【上海→成都 · 提前购票分析】

出行日期：2026-08-28（还有 23 天）
扫描策略：窗口 7~14 天，实扫 14 天（消耗 14 次查询额度）｜扫满 14 天上限，谷底未确认

| 出发日 | 星期 | 提前天数 | 当日最低价 |
|--------|------|----------|-----------|
| 2026-08-17 | 周一 | 提前12天 | ¥1523.0 |
| 2026-08-18 | 周二 | 提前13天 | ¥1501.0 ← 最低 |

🎯 建议购票日：**2026-08-15**（提前 13 天，依据：逐航班共识）
   逐航班共识：23/32 班（72%）各自的最低点都落在 2026-08-18（提前 13 天）
📊 面板中位价法：提前 13 天最便宜（¥1501.0）→ 2026-08-15
📉 全量最低价法（**仅对照**，易被一班中转特价定住）：提前 9 天（¥1389.0）→ 2026-08-19
🗓 同星期几对齐法：本次样本不足（同为「周五」的样本只有 1 个），未输出。

说明：本方法是基于「航线运力相对稳定」的近似启发式，非精确预测。
```

---

## Agent 工作流

**1 · 意图路由。** 区分三种情况，且区分得比关键词匹配更细：

| 输入 | 路由 | 为什么 |
|---|---|---|
| 「月底上海去北京」 | flight | 抽到**完整航线**且没提玩什么 → 纯机票请求 |
| 「成都秋天有什么地方适合玩」 | destination | 只问玩 |
| 「我下周要去新疆」 | flight + destination | 只有**单个目的地**、诉求不明 → 两件事都办 |
| 「帮我订个酒店」 | 拒绝 | 超出能力范围，不用相近能力硬凑 |

**2 · 参数抽取与范围判断。** 最容易出错的是**落地城市 ≠ 玩乐范围**：

```
「月底飞乌鲁木齐，新疆有什么玩的」
   → destination = 乌鲁木齐   （机票飞哪）
   → place = 新疆, scope = province  （在哪玩）
```

抽不到就标记缺失去问，**绝不填一个看起来合理的值**。出发地与目的地抽成同一城市也按抽错处理。

**3 · 部分执行。** 缺参数只挡住对应的那个技能：缺出发地时，先把目的地清单给出来，同时追问出发地——而不是整轮什么都不给。

**4 · 人工确认闸门。** 见下节。

---

## 可靠性设计

这部分是本项目的重点，也是 Agent 工程里最容易被忽略的部分。

### 禁止假成功

工具失败必须**明确失败**，绝不允许用记忆或猜测补出不存在的航班、价格、执行结果。

```python
# 失败时的行为（有测试保护）
state.flight_result["ok"]       # False
state.flight_result["error"]    # "请求超时（15s 内未返回）"
state.flight_result["flights"]  # [] —— 绝不编造
state.flight_result["analysis"] # None
```

并且严格区分两件事：**「取不到数据」是失败**，**「取到了但当天没航班」是空结果**——后者不该被报成错误。

### 一个失败不拖垮另一个

机票接口挂了，目的地清单照常交付，并说明机票为什么没给出来。不会因为一个模块失败就回「无法回答」。

### 副作用必须经过人

```
生成预览 → 请求确认 → 执行 → 如实回报
```

- **没有审批回调 = 绝不执行。** 副作用永远不会默认发生。
- 即使用户说「看到合适的直接帮我设提醒」，**仍要先出预览**。
- 提醒方式不可用时自动降级，并明确告知已降级及原因。

### 不擅自扩大范围

用户问「沈阳有什么玩的」，名录里只有 1 个景区——如实说只有 1 个，**不会**自作主张给整个辽宁省。这条有专门的评测用例守着。

### 空字段不冒充默认值

景区「适宜季节」为空时标注为**「季节未知」**，而不是当成「全年皆宜」。后者会掩盖真正的季节不匹配。

---

## 评测

`/evals` 是本项目的差异化能力：把 Agent 的**行为**变成可回归运行的断言。

```bash
python evals/run_evals.py            # 全部
python evals/run_evals.py routing    # 只跑某一类
python evals/run_evals.py --json     # 机器可读，接 CI
```

```
✅ Intent Routing             9/9
✅ Parameter Extraction       4/4
✅ Scope Control              5/5
✅ Tool Failure Handling      5/5
✅ Human-in-the-loop          3/3
✅ Edge Cases                 6/6
--------------------------------------------------------------
总计：32/32 通过（100.0%）
```

| 评测类别 | 检验什么 |
|---|---|
| Intent Routing | 路由到正确的技能；超范围请求被拒绝 |
| Parameter Extraction | 参数抽对；**抽不到时如实标记缺失而不是瞎猜** |
| Scope Control | 城市不被自动扩成省；落地城市与玩乐范围分离 |
| Tool Failure | 超时 / 500 / 401 / 畸形数据下**不编造、不假称成功** |
| Human-in-the-loop | 副作用操作先预览后确认；拒绝后不执行 |
| Edge Cases | 空输入、残缺句、不存在的目的地、多意图不崩 |

**用例写在 `evals/cases.json`（回归闸门）与 `evals/hard_cases.json`（难例组），加用例只改数据不改代码。** 这一点是刻意的：将来把规则路由换成 LLM 路由时，同一套用例可以直接对比新旧实现，用数据证明「上 LLM 确实更好」，而不是凭感觉换技术。

`tests/` 与 `evals/` 的分工：前者是「代码有没有坏」的单元回归，后者是「Agent 表现好不好」的行为评测。

### 开发方式：Evaluation-driven

```
Build → Eval → 发现 Failure Mode → 修复 → Regression Eval → 迭代
```

评测在开发中确实抓到了真实缺陷，例如：

- 「月底上海去北京」里的时间词粘进了城市名，抽出「月底上海」；
- 「顺便看看四川有什么玩的」把连接词一起抽成了地名「顺便看看四川」；
- 「有什么地方适合玩」抽出通用词「地方」而不是「成都」。

三个都是评测跑出来的，修完后回归全绿。

---

## 接入真实数据源

默认 mock。接真实接口时**不改代码，只写配置**：

```bash
cp config/flight_api.example.json config/flight_api.json
cp .env.example .env
```

```bash
# .env
TRAVELWISE_FLIGHT_PROVIDER=http
TRAVELWISE_FLIGHT_TOKEN=你的凭证
```

```jsonc
// config/flight_api.json —— 描述"这个接口长什么样"
{
  "endpoint": "https://example.com/api/flights",
  "method": "GET",
  "supports_price": true,
  "date_format": "YYYY-MM-DD",
  "params": { "origin_key": "from", "destination_key": "to", "date_key": "date" },
  "auth": { "type": "bearer", "value_env": "TRAVELWISE_FLIGHT_TOKEN" },
  "response": { "list_path": "data.flights" }
}
```

支持 `header` / `bearer` / `query` 三种鉴权，GET / POST，字段名自动认领或显式映射。换供应商只改这份 JSON。

### 提醒落地方式

| Provider | 说明 |
|---|---|
| `console` | 打印到控制台。默认，永远可用 |
| `ics` | 生成标准 **.ics 日历文件（RFC 5545）**，Google / Apple / Outlook 均可导入 |
| `json` | 追加到本地 JSON，便于程序化核对 |
| `mcp` | 通用 MCP 适配器：注入一个 `tool_caller(name, args)`，工具名与字段映射均可配置 |

> 「把购票日写进日历」这个真实需求，用 `.ics` 这个开放标准就能满足，不需要绑定任何一家终端厂商的私有接口。

### 安全

- 凭证**只从环境变量读**，仓库里只有 `.env.example` 与 `*.example.json` 模板。
- `.gitignore` 已覆盖 `.env`、`config/flight_api.json`、`data/cache/`、`*.db`。
- 仓库中**不含任何真实密钥**。

---

## 项目结构

```
travelwise-agent/
├── README.md · LICENSE · pyproject.toml · requirements.txt
├── .env.example · .gitignore
│
├── docs/
│   ├── decision-policy.md       # ★ 业务规则唯一权威来源
│   ├── architecture.md          # 分层 / 请求流 / 状态机 / 设计取舍
│   ├── adr/                     # 架构决策记录
│   └── original-skill-design/   # V1 原始 Skill 文档（历史资料，不参与运行）
│
├── prompts/                     # 平台无关的 Agent 指令（由决策规范派生）
│   ├── orchestrator.md
│   ├── flight_analyst.md
│   └── destination_curator.md
│
├── src/travelwise/
│   ├── __init__.py · __main__.py · cli.py
│   ├── router.py                # 意图路由 / 参数抽取 / 范围判断（规则 baseline）
│   ├── orchestrator.py          # 调度 / 合并 / HITL 闸门 / 终态判定
│   ├── state.py                 # TravelState + TaskStatus 状态机 + 槽位合并
│   ├── session.py               # 多轮会话：分类回话 / 显式沿用 / 落盘续跑
│   ├── tracing.py               # Span / Tracer / JSONL 落盘，参数摘要与脱敏
│   ├── slots.py                 # 一句回话 → 槽位（含歧义识别与纠错）
│   ├── config.py                # 环境变量配置，零依赖 .env 读取
│   ├── paths.py                 # 统一路径解析
│   │
│   ├── skills/
│   │   ├── flight.py            # 机票技能：取数 + 分析 + 提醒请求构造
│   │   └── destination.py       # 目的地技能
│   │
│   ├── tools/                   # 确定性计算，无 IO、无平台依赖
│   │   ├── price_analysis.py    # 扫描 / 提前停止 / 双算法 / 警告
│   │   ├── destination_search.py
│   │   ├── spot_repository.py
│   │   ├── season.py
│   │   ├── search_links.py
│   │   └── city_codes.py
│   │
│   ├── providers/               # 与外部世界唯一的接缝
│   │   ├── base.py              # FlightProvider / ReminderProvider 抽象
│   │   ├── mock_flight.py       # Mock + 故意失败的 Provider（供评测）
│   │   ├── http_flight.py       # 通用 HTTP 数据源，配置驱动
│   │   └── reminders.py         # console / ics / json / mcp
│   │
│   └── analytics/
│       └── price_history.py     # 领域历史价格积累（非 Memory）
│
├── data/source/                 # A 级景区名录 · 城市三字码
├── config/flight_api.example.json
├── evals/                       # 评测集 + 执行器 + 结果留档
│   ├── cases.json               # 回归闸门：全绿是常态，红了即退化
│   ├── hard_cases.json          # 难例组：不全绿是常态，不参与退出码
│   └── compare_routers.py       # 规则 vs LLM 对照，判据取难例组
└── tests/                       # 351 项单元测试
```

---

## 已实现 / 未实现

诚实地讲清楚边界，比堆名词更重要。

**已实现**

- Orchestrator + Multi-Skill 架构，意图路由与多技能编排
- 参数抽取、Scope Control、部分执行（缺参数不连坐）
- 显式 TravelState
- Provider / Adapter 平台隔离；任意 HTTP 数据源配置化接入
- 结构化知识检索（CSV 名录）+ 季节判断 + 搜索入口生成
- Human-in-the-loop、工具失败降级、禁止假成功
- **多轮状态续跑**：追问 → 接住回答 → 接着跑；状态落盘，可跨进程 `--session <id>` 继续
- **调用链追踪**：`--trace` 记录每一次模型调用 / 工具调用 / 闸门事件，落 JSONL，可渲染成一页 HTML
- MCP Tool 调用的通用适配器设计
- Router 评测（回归闸门 6 类 32 用例 + 难例组 6 类 19 用例）+ Agent 评测（**9 项指标**，质量 / 安全 / 代价分开报）+ 单元测试（351 项）
- 全流程 Mock，无 API Key 可运行

**尚未实现（不宣称）**

- ❌ 完整 RAG —— 目前是 Structured Retrieval，**没有** chunking / embedding / 向量库 / retriever / reranker
- 🟡 LLM Function Calling —— 链路已实现（`LLMRouter` / `ToolRegistry` / `ToolCallingAgent`），
  真机跑过（见上方[真机验证记录](#接真实模型)），但**尚未做成系统的对照实验**：
  离线 badge 的数字全部来自合成响应回放，只能证明管道通畅，不代表模型守规矩
- ❌ 跨 Session 持久化 **Memory** —— 注意和上面的多轮续跑不是一回事：
  续跑管的是「同一段对话内的状态延续」，Memory 管的是「他一般从上海出发」这类长期偏好，
  后者需要自己的一套持久化与评测，现在没有，也不假装有
- ❌ 自建 MCP Server —— 目前是 MCP Tool 调用的适配器设计
- ❌ LangGraph / Multi-Agent
- 🟡 可观测性 —— 调用链追踪已接进两条执行路径并可渲染（见[调用链追踪](#调用链追踪)），
  但**不是生产级**：没有采样、没有上报后端、没有跨请求聚合，trace 只落在本机磁盘上。
  它解决的是「这次为什么跑错」，不解决「这周错误率多少」

---

## Roadmap

- [x] **Tool Calling**：已定义 `search_flights` / `search_destination` / `create_reminder`，
      `LLMRouter` 与 `RuleRouter` 可跑同一套评测（`evals/compare_routers.py`）
- [ ] **用真实模型跑对照实验** —— 难用例已就位（`evals/hard_cases.json`，19 条，
      规则路由 4/19），`compare_routers.py` 的判据已换成难例组；
      冒烟已通、记录机制已就位。**缺的只剩一次带 Key 的运行**
- [ ] **Persistent Memory**：SQLite 存跨 Session 偏好（常用出发地、避免红眼航班等）
- [ ] **Destination RAG**：为非结构化旅游知识加入语义检索，与结构化 CSV 检索共存
- [ ] **MCP Server**：自建一个简单的 TravelWise MCP Server
- [ ] **LangGraph**：把 Workflow 显式建模为图（在 Tool Calling / State / Memory 都独立实现之后再做）
- [x] **Tracing / Observability** —— 已接进 `agent_loop` 与 `orchestrator`，
      `scripts/view_trace.py` 渲染成单文件 HTML；缺的是采样与上报（见上方边界说明）

### 技术选型原则

每引入一项技术前先回答四个问题：**它解决什么问题？没有它会怎样？有没有更简单的做法？怎么测出它确实改善了系统？**

确定性任务（价格计算、日期判断、参数校验、格式转换）一律用代码；LLM 只负责自然语言理解、意图识别、复杂判断与结果组织。**不为了用新名词而增加技术。**

---

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/decision-policy.md`](docs/decision-policy.md) | **业务规则唯一权威来源**（Prompt 与代码均由它派生） |
| [`docs/architecture.md`](docs/architecture.md) | 分层、请求流、任务状态机、关键设计取舍 |
| [`docs/adr/`](docs/adr/) | 架构决策记录 |
| [`docs/original-skill-design/`](docs/original-skill-design/) | V1 原始 Agent Skill 设计与演进对照 |

## 数据来源与免责

- `data/source/scenic_spots.csv`：公开 A 级景区名录整理，字段含级别、所在地区、
  适宜季节、门票参考、开放时间。**门票与开放时间会随时间变化，仅供参考**，
  出行前请以景区官方公告为准。
- `data/source/city_codes.csv`：城市中文名与 IATA 三字码对照。
- 名录当前**未附来源 URL 与核验时间**，这是已知不足，已列入 Roadmap
  （计划为每条数据补 `source` / `updated_at` / `verified_at`）。
- 购票时机分析是**启发式**，不是价格预测模型，其核心假设尚未用真实历史数据验证。
  实际票价以购票平台为准。

## 安全

- 凭证只从环境变量读取，仓库内只有 `.env.example` 与 `*.example.json` 模板。
- `.gitignore` 已覆盖 `.env`、`config/flight_api.json`、`data/cache/`、`*.db`。
- 仓库中不含任何真实密钥。

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) —— **禁止任何商业用途**。

允许：个人学习、研究、教学、非营利与公共机构使用。
禁止：作为商业产品/服务的一部分、企业内部商业运营、付费服务、转售或再授权。

商业授权请见 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)。

> ⚠️ 本项目不构成购买建议，价格分析为近似启发式方法。
> 使用前请阅读[原理、稳定性与已知缺陷](docs/principles-and-limits.md)。
