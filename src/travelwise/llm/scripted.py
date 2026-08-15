# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""ScriptedLLMClient —— 离线回放的 LLM 客户端。

**用途明确：验证管道，而不是衡量模型质量。**

它的存在是为了让整条 Tool Calling 链路（Router → LLM → ToolRegistry → Tool →
LLM → Answer）在没有 API Key 的情况下也能跑通、能进 CI、能写断言。

⚠️ 必须始终清楚的一点：
    回放响应是**人工录制/编写**的，不是真实模型输出。
    因此用它跑出来的"LLM 路由准确率"**不构成模型能力的证据**，
    只能证明"解析与调度这套管道是通的"。
    真实对照必须配置 TRAVELWISE_LLM_PROVIDER=anthropic|openai 并提供 Key。

`is_synthetic = True` 就是这个标记，对照脚本据此在报告里打上警示。

找不到对应记录时抛 LLMUnavailable —— **绝不即兴编一个响应**。
这与项目「禁止假成功」的原则一致：宁可明确失败，也不给一个看似合理的假结果。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .base import LLMClient, LLMResponse, LLMUnavailable, ToolCall, Usage


def _normalize(text: str) -> str:
    """归一化用户输入，作为回放查找键。

    去掉空白与常见中英文标点，避免「月底上海去北京。」与「月底上海去北京」
    被当成两条不同的记录。
    """
    return re.sub(r"[\s，。、！？,.!?；;：:]+", "", (text or "").strip())


class ScriptedLLMClient(LLMClient):
    """按用户输入回放预先录制的响应。"""

    name = "scripted"
    #: 标记本客户端产出的是合成数据，不是真实模型输出
    is_synthetic = True

    def __init__(self, script: dict[str, Any] | None = None,
                 fixtures_path: str | Path | None = None,
                 fallback: Callable[[str], LLMResponse] | None = None,
                 model: str = "scripted-fixtures"):
        self.model = model
        self.fallback = fallback
        self.call_count = 0
        self.followup: dict[str, Any] | None = None
        self.script: dict[str, Any] = {}
        if fixtures_path:
            self.load_fixtures(fixtures_path)
        if script:
            self.script.update({_normalize(k): v for k, v in script.items()})

    # ------------------------------------------------------------------
    def load_fixtures(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            raise LLMUnavailable("回放文件不存在：%s" % p)
        data = json.loads(p.read_text(encoding="utf-8"))
        for section in ("responses", "agent_loop_responses"):
            for key, value in (data.get(section) or {}).items():
                self.script[_normalize(key)] = value
        # 工具结果回合的统一收尾响应：Agent Loop 第二轮拿到工具结果后用它作答
        followup = data.get("agent_loop_followup")
        if followup:
            self.followup = followup

    @staticmethod
    def _last_user_text(messages: list[dict[str, Any]]) -> str:
        for m in reversed(messages or []):
            if m.get("role") != "user":
                continue
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text", "")
        return ""

    # ------------------------------------------------------------------
    def complete(self, messages, *, system="", tools=None, tool_choice=None,
                 max_tokens=1024, temperature=0.0) -> LLMResponse:
        self.call_count += 1
        # 走与真实 client 相同的入口：基类默认实现会把 ToolResultMessage
        # 折成「工具执行结果：…」的 user 消息，下面的 followup 匹配因此不变。
        user_text = self._last_user_text(self.serialize_messages(messages))
        entry = self.script.get(_normalize(user_text))

        # Agent Loop 的第二轮：输入是工具执行结果，用统一的收尾响应作答
        if entry is None and self.followup and user_text.startswith("工具执行结果"):
            entry = self.followup

        if entry is None:
            if self.fallback is not None:
                return self.fallback(user_text)
            raise LLMUnavailable(
                "回放库中没有这条输入的记录：%r。\n"
                "离线模式只能回放已录制的响应，不会即兴编造。\n"
                "请补充 evals/fixtures/llm_responses.json，"
                "或配置真实模型（TRAVELWISE_LLM_PROVIDER + TRAVELWISE_LLM_API_KEY）。"
                % user_text[:60])

        calls = [
            ToolCall(name=c.get("name", ""), arguments=c.get("arguments") or {},
                     call_id=c.get("id", "scripted-%d" % self.call_count))
            for c in (entry.get("tool_calls") or [])
        ]
        usage = entry.get("usage") or {}
        return LLMResponse(
            text=entry.get("text", ""), tool_calls=calls,
            usage=Usage(int(usage.get("input_tokens", 0)),
                        int(usage.get("output_tokens", 0))),
            model=self.model,
            stop_reason=entry.get("stop_reason", "tool_use" if calls else "end_turn"),
            raw=entry)
