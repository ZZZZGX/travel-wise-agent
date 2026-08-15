# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""真实厂商的 LLM 客户端。只用标准库 urllib，保持零第三方依赖。

两家的 function calling 形状不同，差异全部吸收在这一层：

  Anthropic：tools=[{name, description, input_schema}]
             返回 content 数组里 type=="tool_use" 的块
  OpenAI：   tools=[{type:"function", function:{name, description, parameters}}]
             返回 choices[0].message.tool_calls，arguments 是 JSON 字符串

上层拿到的都是统一的 LLMResponse + ToolCall。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import LLMClient, LLMError, LLMResponse, LLMUnavailable, ToolCall, Usage


def _post_json(url: str, headers: dict[str, str], payload: dict, timeout: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:                                  # noqa: BLE001
            pass
        hint = ""
        if e.code in (401, 403):
            hint = "（API Key 无效或无权限）"
        elif e.code == 429:
            hint = "（触发限流或额度用尽）"
        raise LLMError("HTTP %s 调用模型失败%s：%s" % (e.code, hint, detail or e.reason)) from e
    except urllib.error.URLError as e:
        raise LLMError("网络不可达（运行环境可能禁止外网访问）：%s" % e.reason) from e
    except TimeoutError as e:
        raise LLMError("模型调用超时（%ss）" % timeout) from e

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("模型返回不是合法 JSON（前 120 字：%s）" % text[:120]) from e


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6",
                 base_url: str = "https://api.anthropic.com",
                 timeout: int = 60, api_version: str = "2023-06-01"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_version = api_version

    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # 消息序列化：Anthropic 用内容块数组承载 tool_use / tool_result
    # ------------------------------------------------------------------
    def _serialize_assistant(self, message) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
            # 空文本块会被 API 拒绝，所以只在真有内容时才加
        for call in message.tool_calls:
            blocks.append({"type": "tool_use", "id": call.call_id,
                           "name": call.name, "input": call.arguments or {}})
        return {"role": "assistant", "content": blocks}

    def _serialize_tool_results(self, group: list) -> list[dict[str, Any]]:
        """同一轮的所有工具结果必须合并进**一条** user 消息，这是 Anthropic 的硬要求。"""
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": m.call_id,
             "content": m.as_text(), "is_error": not m.ok}
            for m in group]}]

    def complete(self, messages, *, system="", tools=None, tool_choice=None,
                 max_tokens=1024, temperature=0.0) -> LLMResponse:
        if not self.available():
            raise LLMUnavailable("未提供 Anthropic API Key（设置 TRAVELWISE_LLM_API_KEY）")

        payload: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": self.serialize_messages(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t["parameters"]}
                for t in tools
            ]
            if tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            elif isinstance(tool_choice, str) and tool_choice not in ("auto", "none"):
                payload["tool_choice"] = {"type": "tool", "name": tool_choice}

        data = _post_json("%s/v1/messages" % self.base_url,
                          {"x-api-key": self.api_key,
                           "anthropic-version": self.api_version},
                          payload, self.timeout)

        text_parts, calls = [], []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(name=block.get("name", ""),
                                      arguments=block.get("input") or {},
                                      call_id=block.get("id", "")))
        u = data.get("usage") or {}
        return LLMResponse(
            text="\n".join(p for p in text_parts if p),
            tool_calls=calls,
            usage=Usage(int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))),
            model=data.get("model", self.model),
            stop_reason=data.get("stop_reason", ""), raw=data)


class OpenAIClient(LLMClient):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini",
                 base_url: str = "https://api.openai.com", timeout: int = 60):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # 消息序列化：OpenAI 用 assistant.tool_calls + 独立的 role="tool" 消息
    # ------------------------------------------------------------------
    def _serialize_assistant(self, message) -> dict[str, Any]:
        out: dict[str, Any] = {"role": "assistant",
                               "content": message.text or None}
        if message.tool_calls:
            out["tool_calls"] = [
                {"id": call.call_id, "type": "function",
                 "function": {"name": call.name,
                              # arguments 必须是 JSON **字符串**，传 dict 会 400
                              "arguments": json.dumps(call.arguments or {},
                                                      ensure_ascii=False)}}
                for call in message.tool_calls]
        return out

    def _serialize_tool_results(self, group: list) -> list[dict[str, Any]]:
        """每个工具结果一条独立消息，且必须带 tool_call_id 与上一轮配对。"""
        return [{"role": "tool", "tool_call_id": m.call_id,
                 "name": m.name, "content": m.as_text()} for m in group]

    def complete(self, messages, *, system="", tools=None, tool_choice=None,
                 max_tokens=1024, temperature=0.0) -> LLMResponse:
        if not self.available():
            raise LLMUnavailable("未提供 OpenAI API Key（设置 TRAVELWISE_LLM_API_KEY）")

        msgs = (([{"role": "system", "content": system}] if system else [])
                + self.serialize_messages(messages))
        payload: dict[str, Any] = {
            "model": self.model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t["parameters"]}}
                for t in tools
            ]
            if tool_choice == "required":
                payload["tool_choice"] = "required"
            elif isinstance(tool_choice, str) and tool_choice not in ("auto", "none"):
                payload["tool_choice"] = {"type": "function",
                                          "function": {"name": tool_choice}}

        data = _post_json("%s/v1/chat/completions" % self.base_url,
                          {"Authorization": "Bearer " + self.api_key},
                          payload, self.timeout)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError("模型未返回任何 choices")
        message = choices[0].get("message") or {}

        calls = []
        for c in message.get("tool_calls") or []:
            fn = c.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                # 参数解析不了就是解析不了，不猜——交给上层按失败处理
                raise LLMError("模型返回的工具参数不是合法 JSON：%s" % str(raw_args)[:120])
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args,
                                  call_id=c.get("id", "")))

        u = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or "", tool_calls=calls,
            usage=Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))),
            model=data.get("model", self.model),
            stop_reason=choices[0].get("finish_reason", ""), raw=data)
