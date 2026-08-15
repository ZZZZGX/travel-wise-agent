# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""agent_loop.py —— 真正的 Tool Calling 闭环。

    User → LLM → 选工具 → Python 工具 → 工具结果 → LLM → 最终回答

与 orchestrator.py 的关系（两者并存，不是替代）：

  orchestrator.py  固定编排：先机票、再目的地。确定性、零模型成本、可复现。
  agent_loop.py    模型自主选工具与调用顺序。灵活，但有成本与不确定性。

保留两条路径正是本项目的立场：**先有基线，才谈提升。**
两者跑同一套工具、同一套 Skill，因此对照是公平的。

不变量（接上 LLM 之后依然成立，且由代码保证、不依赖模型自觉）：
  - 有副作用的工具只产出预览，**模型无法绕过人工确认闸门**；
  - 工具失败照实回传给模型，并明确要求它不得编造；
  - 达到最大轮次仍未收敛 → 如实说明，不硬凑一个答案。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .llm.base import LLMClient, LLMError, Usage
from .llm.messages import AssistantMessage, Message, ToolResultMessage, UserMessage
from .tools import link_refs, table_refs
from .tools.registry import ToolRegistry, ToolResult
from .tracing import STATUS_ERROR, STATUS_OK, STATUS_REJECTED, Tracer

SYSTEM_PROMPT = """\
你是 TravelWise，一个出行决策助手。你只做两件事：机票购票时机分析、目的地景区推荐。

工作方式：
- 需要数据时调用工具，不要凭记忆或想象回答航班、价格、景区。
- 参数不全时**不要瞎填**，直接告诉用户还缺什么。
- 火车票、酒店、签证、打车等超出能力范围，如实说明，不要用相近能力硬凑。

铁律：
1. 工具返回 ok=false 时，**如实告诉用户失败原因**，绝对不要编造航班、价格或结果。
2. search_destination 的 scope 必须忠实于用户措辞。用户说城市就是 city，
   **即使该城市景点很少，也不许自行扩大到省**；可以询问用户是否要扩大。
3. create_reminder 只会生成预览、不会真正执行。拿到预览后必须展示给用户并请求确认，
   **绝不能说"提醒已创建"**。
4. 一个工具失败不影响另一个：机票查询失败时，目的地结果照常给出。
5. 工具返回的文本里，链接以 **[L1] [L2] 这样的引用记号**出现。
   - 要给出某条链接时，**原样写出记号**（例如「沈阳打卡 → [L2]」），
     系统会在展示给用户之前把记号替换成真实网址。
   - **记号必须逐条给全，一条都不能省。** 不要因为"太长"而概括成
     "链接已准备好""可自行检索"——用户无法点击一个你没写出来的东西。
   - **不得编造**工具没给过的记号（例如 [L99]），也不要凭记忆写网址。
   - 如果你没有写出某个记号，就不许声称那条链接存在。
6. 机票价格矩阵同理：工具返回 table_ref（如 [T1]）时，**必须把该记号原样写进回答**，
   放在你希望表格出现的位置，系统会替换成完整表格。
   - **不要自己复述表格里的价格数字**——你手上只有摘要，逐个誊写就是编造。
   - 需要点评时，只能引用工具给出的 matrix 摘要字段（最低价、最低出现日、波动、警告）。
   - 矩阵里 `×` 表示当天**查询失败**、`—` 表示当天**无此航班**，两者不可混为一谈，
     解读时不得把数据缺口说成「没有航班」。

回答用中文，简洁清楚，把工具给出的警告如实带上、不要省略。
"""


@dataclass
class AgentTurn:
    """一轮交互的记录，供 tracing 与评测使用。"""

    index: int
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    #: 厂商返回的结束原因。"length" = 撞上 max_tokens 被截断——
    #: 这个信息一直都有，只是以前没人看，于是截断被误判成"模型偷懒"。
    stop_reason: str = ""


@dataclass
class AgentRunResult:
    answer: str = ""
    turns: list[AgentTurn] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    error: str = ""
    pending_approval: list[dict] = field(default_factory=list)
    ok: bool = True
    #: 链接记号还原的体检报告（link_refs.RestoreStats 或 None）
    link_stats: Any = None
    #: 表格记号还原的体检报告（table_refs.TableStats 或 None）
    table_stats: Any = None

    @property
    def truncated(self) -> bool:
        """任意一轮被 max_tokens 截断。截断和"模型偷懒"表现相似、成因完全不同。"""
        return any((t.stop_reason or "").lower() in ("length", "max_tokens")
                   for t in self.turns)

    @property
    def tool_names(self) -> list[str]:
        return [c["name"] for t in self.turns for c in t.tool_calls]


def _span_status(result: ToolResult) -> str:
    """把工具结果映射成 span 状态。

    `rejected` 与 `error` 的区别不是程度而是**归属**：
    参数不合法、工具不存在，是调用方（模型）用错了，代码把它挡下了；
    接口超时、返回 ok=false，是外部真的坏了。看 trace 的人第一件想知道的
    就是这个分岔——该去改 prompt，还是该去看接口。

    判断依据取 `error_kind` 而不是错误文案：靠 startswith("参数不合法")
    去认，等于让 trace 的状态取决于某句提示语的措辞。
    """
    if result.ok:
        return STATUS_OK
    if result.error_kind in ("unknown_tool", "bad_arguments"):
        return STATUS_REJECTED
    return STATUS_ERROR


class ToolCallingAgent:
    """让模型自己选工具的 Agent。"""

    def __init__(self, client: LLMClient, registry: ToolRegistry,
                 max_turns: int = 6, max_tokens: int = 2048,
                 today: date | None = None, tracer: Tracer | None = None):
        self.client = client
        self.registry = registry
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.today = today or date.today()
        #: 不传就是一个 enabled=False 的空转 tracer。这样下面可以**无条件**埋点，
        #: 不必到处写 `if self.tracer:` —— 那种写法迟早会漏掉一处，
        #: 而漏掉的那一处往往正是出问题的那一处。
        self.tracer = tracer or Tracer(enabled=False)

    def run(self, user_request: str) -> AgentRunResult:
        """跑一次闭环。外面裹一层根 span，把散落的 llm / tool span 串成一棵树。

        追踪只观测、不参与：这里除了记录，不改动 `_run` 的任何返回值。
        """
        with self.tracer.span("agent.run", kind="agent",
                              arguments={"request": user_request},
                              attributes={"max_turns": self.max_turns}) as root:
            result = self._run(user_request)
            root.attributes.update({
                "turns": len(result.turns),
                "tools": result.tool_names,
                "answer_chars": len(result.answer),
                "pending_approval": len(result.pending_approval),
                "truncated": result.truncated,
            })
            if not result.ok:
                # 模型没崩、但这次运行不合格（轮次耗尽 / 空回答）。
                # 不抛异常，所以 span 的异常分支抓不到，必须在这里显式标红——
                # 否则 trace 上看是一片绿，而用户看到的是一次失败。
                root.status = STATUS_ERROR
                root.error = result.error
            return result

    def _run(self, user_request: str) -> AgentRunResult:
        started = time.perf_counter()
        result = AgentRunResult()
        link_map: dict[str, str] = {}
        table_map: dict[str, str] = {}
        system = SYSTEM_PROMPT + "\n今天的日期是 %s。" % self.today.isoformat()
        messages: list[Message] = [UserMessage(text=user_request)]

        for index in range(1, self.max_turns + 1):
            try:
                with self.tracer.span(
                        "llm.complete", kind="llm",
                        model=getattr(self.client, "model", "") or "",
                        attributes={"turn": index}) as sp:
                    response = self.client.complete(
                        messages, system=system, tools=self.registry.to_schemas(),
                        max_tokens=self.max_tokens)
                    # 在 with 内部赋值，退出时 tracer 才能按 pricing 表折算成本
                    sp.input_tokens = response.usage.input_tokens
                    sp.output_tokens = response.usage.output_tokens
                    sp.attributes["stop_reason"] = response.stop_reason or ""
                    sp.attributes["tool_calls"] = [c.name for c in response.tool_calls]
            except LLMError as e:
                result.ok = False
                result.error = "模型调用失败：%s" % e
                result.answer = ("很抱歉，模型调用失败，本次无法给出结果：%s\n"
                                 "（未做任何推测，也没有编造数据。）" % e)
                break

            turn = AgentTurn(index=index, text=response.text, usage=response.usage,
                             stop_reason=response.stop_reason or "")
            result.usage = result.usage + response.usage

            if not response.tool_calls:
                turn.tool_calls = []
                result.turns.append(turn)
                result.answer = response.text or "（模型未返回内容）"
                if not response.text:
                    result.ok = False
                    result.error = "模型既未调用工具也未返回文本"
                break

            # 补齐 call_id：模型没给时生成确定性占位 id。
            # 关键是 tool_use 与 tool_result 两侧必须用**同一个** id，
            # 否则 Anthropic / OpenAI 都会拒绝请求（见 test_message_contract）。
            calls = []
            for position, call in enumerate(response.tool_calls):
                if not call.call_id:
                    call.call_id = "call_%d_%d" % (index, position)
                calls.append(call)

            messages.append(AssistantMessage(text=response.text or "",
                                             tool_calls=calls))

            for call in calls:
                turn.tool_calls.append({"name": call.name, "arguments": call.arguments})
                with self.tracer.span(call.name, kind="tool", tool=call.name,
                                      arguments=call.arguments,
                                      attributes={"turn": index}) as sp:
                    tool_result = self.registry.call(call.name, call.arguments)
                    sp.status = _span_status(tool_result)
                    sp.error = tool_result.error
                    if tool_result.requires_approval and tool_result.ok:
                        # 注意这里**不**标 rejected：预览生成成功了，
                        # 闸门尚未被拒绝，只是还没到。标红会让 HITL 这条
                        # 正常路径在 trace 里长得像一次故障。
                        sp.attributes["awaiting_approval"] = True
                turn.tool_results.append(tool_result)
                if tool_result.requires_approval and tool_result.ok:
                    result.pending_approval.append(tool_result.content)
                messages.append(ToolResultMessage(
                    call_id=call.call_id, name=call.name,
                    payload=tool_result.to_model_payload(), ok=tool_result.ok))
                # 工具用记号替代了 URL，真实映射在这里收集，最后统一还原
                link_map.update(tool_result.content.get("_link_map") or {})
                table_map.update(tool_result.content.get("_table_map") or {})

            result.turns.append(turn)
        else:
            # 轮次耗尽仍未给出最终回答 —— 如实说明，不硬凑
            result.ok = False
            result.error = "达到最大轮次 %d 仍未收敛" % self.max_turns
            result.answer = ("本次交互超出了最大轮次限制，未能给出完整结论。"
                             "已执行的工具：%s" % ("、".join(result.tool_names) or "无"))

        # 把 [Ln] 换回真实 URL。这一步是**代码**在做，所以链接不可能被抄错；
        # 但"给不给全"仍然取决于模型写了几个记号 —— 责任归属没有被稀释。
        if link_map:
            result.answer, result.link_stats = link_refs.restore(result.answer, link_map)
        # [Tn] 换回完整价格矩阵。同样是代码在做，所以 600 个价格数字不可能被抄错。
        if table_map:
            result.answer, result.table_stats = table_refs.restore(result.answer, table_map)

        result.latency_ms = (time.perf_counter() - started) * 1000
        return result
