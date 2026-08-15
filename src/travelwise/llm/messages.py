# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""内部消息类型 —— Agent Loop 与厂商协议之间的接缝。

## 为什么需要这一层

Agent Loop 关心的是「谁说了什么、调了哪个工具、工具返回了什么」，
而不是「Anthropic 把 tool_use 放在 content 数组里、OpenAI 用独立的 tool 角色」。

在引入这层之前，`agent_loop.py` 直接手拼厂商格式，结果是：
  - 拼出来的形状两家都不认（顶层 tool_calls + role=user 的工具结果）；
  - `ScriptedLLMClient` 不校验历史形状，所以离线全绿、真机必挂；
  - 想换 provider 就得改循环。

现在的分工：
  agent_loop.py   只产出 UserMessage / AssistantMessage / ToolResultMessage
  各 LLMClient    负责 serialize_messages() 翻译成自家 wire 格式
  tests/test_message_contract.py  在**无网络**下钉住两家的翻译结果

## call_id 的约定

`AssistantMessage.tool_calls[i].call_id` 与对应 `ToolResultMessage.call_id`
**必须相等**，否则两家都会拒绝请求。id 一律沿用模型返回的原始值；
模型没给（例如离线回放）时由 Agent Loop 补一个确定性的占位 id，
但补的 id 同样要在两侧一致 —— 这条由契约测试守着。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Union

from .base import ToolCall


@dataclass
class UserMessage:
    """用户说的话。"""

    text: str = ""


@dataclass
class AssistantMessage:
    """模型说的话，可能同时请求调用工具。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolResultMessage:
    """一个工具的执行结果，回填给模型。

    `ok=False` 时两家都支持标成错误块，让模型明确知道这是失败而不是数据。
    """

    call_id: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    ok: bool = True

    def as_text(self) -> str:
        """两家的工具结果最终都以字符串承载，这里统一序列化。"""
        return json.dumps(self.payload, ensure_ascii=False)


#: Agent Loop 内部流通的消息类型；dict 表示调用方自行拼好的原始格式（向后兼容）
Message = Union[UserMessage, AssistantMessage, ToolResultMessage, dict]

__all__ = ["UserMessage", "AssistantMessage", "ToolResultMessage", "Message"]
