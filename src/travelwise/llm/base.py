# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""LLM 客户端抽象层 —— 与模型厂商之间的接缝。

和 providers/ 是同一个思路：Core 不认识任何具体厂商。
无论背后是 Anthropic、OpenAI、本地模型，还是离线回放的固定响应，
上层只面对 `LLMClient.complete()` 编程。

这样做的直接收益与 providers 一致：
  - 换模型 = 换一个实现，路由与 Agent 逻辑一行不动；
  - 没有 API Key 时用 ScriptedLLMClient，整条 Tool Calling 链路照样跑通（CI / 测试）；
  - 同一套 Eval 可以对不同模型、以及规则基线做横向对照。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class ToolCall:
    """模型请求调用某个工具。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass
class Usage:
    """Token 消耗。用于 Rule vs LLM 的成本对照——规则路由这一项恒为 0。"""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens)


@dataclass
class LLMResponse:
    """一次模型调用的结果。

    text 与 tool_calls 可以同时存在（模型先说话再调工具），
    也可以都为空（模型什么都没给——这属于异常，调用方需按失败处理）。
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    stop_reason: str = ""
    raw: Any = None

    def first_tool_call(self, name: str | None = None) -> ToolCall | None:
        for call in self.tool_calls:
            if name is None or call.name == name:
                return call
        return None


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class LLMError(Exception):
    """LLM 调用层的可预期错误。上层必须如实上报，不得吞掉后编造答案。"""


class LLMUnavailable(LLMError):
    """未配置模型、缺少凭证，或离线回放里没有对应记录。"""


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------

class LLMClient(ABC):
    """模型客户端接口。"""

    name: str = "llm"
    model: str = ""

    @abstractmethod
    def complete(self, messages: list[dict[str, Any]], *,
                 system: str = "", tools: list[dict[str, Any]] | None = None,
                 tool_choice: str | dict | None = None,
                 max_tokens: int = 1024, temperature: float = 0.0) -> LLMResponse:
        """发起一次补全。

        messages 用通用格式：[{"role": "user"|"assistant", "content": ...}]，
        由各实现翻译成自家 API 的形状。

        tools 用 JSON Schema 描述（见 tools/registry.py 的 to_schema），
        各实现负责翻译成自家的 function calling 格式。

        失败一律抛 LLMError —— 绝不返回一个"看起来像样"的假响应。
        """
        raise NotImplementedError

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # 消息序列化（Phase 2 · MessageAdapter）
    #
    # Agent Loop 只产出 messages.py 里的内部类型，各实现在这里翻译成自家形状。
    # 传入 dict 时原样透传 —— 保证既有调用方（以及全部旧测试）行为不变。
    # ------------------------------------------------------------------
    def serialize_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        """把内部消息翻译成本厂商的 wire 格式。

        相邻的多个 ToolResultMessage 会被打成一组交给 `_serialize_tool_results`：
        Anthropic 要求同一轮的所有工具结果**合并进一条** user 消息，
        而 OpenAI 要求**每个结果一条**独立的 tool 消息。差异收在那个方法里。
        """
        from .messages import AssistantMessage, ToolResultMessage, UserMessage

        items = list(messages or [])
        out: list[dict[str, Any]] = []
        i = 0
        while i < len(items):
            item = items[i]
            if isinstance(item, dict):
                out.append(item)
                i += 1
            elif isinstance(item, ToolResultMessage):
                group = []
                while i < len(items) and isinstance(items[i], ToolResultMessage):
                    group.append(items[i])
                    i += 1
                out.extend(self._serialize_tool_results(group))
            elif isinstance(item, AssistantMessage):
                out.append(self._serialize_assistant(item))
                i += 1
            elif isinstance(item, UserMessage):
                out.append(self._serialize_user(item))
                i += 1
            else:
                raise TypeError("无法序列化的消息类型：%s" % type(item).__name__)
        return out

    # -- 默认实现：通用 chat 形状，供离线客户端使用 --------------------
    def _serialize_user(self, message) -> dict[str, Any]:
        return {"role": "user", "content": message.text}

    def _serialize_assistant(self, message) -> dict[str, Any]:
        return {"role": "assistant", "content": message.text}

    def _serialize_tool_results(self, group: list) -> list[dict[str, Any]]:
        return [{"role": "user",
                 "content": "工具执行结果：\n" + "\n".join(m.as_text() for m in group)}]
