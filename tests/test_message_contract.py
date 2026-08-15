# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""消息契约测试 —— 不需要 API Key，也不需要网络。

## 这个文件为什么存在

`ScriptedLLMClient` 只读最后一条 user 文本，**完全不校验历史消息的形状**。
所以 Agent Loop 把工具结果塞回历史时用什么格式，离线测试一律看不出来。
结果就是：90 项测试全绿，但真机第一次多轮调用就会被 400 打回来。

本文件的做法是拦截 HTTP 出口（monkeypatch `_post_json`），把**真正会发出去的
payload** 抓下来，按两家厂商的协议逐字段断言：

  Anthropic  assistant.content 必须是数组，含 {"type":"tool_use","id","name","input"}
             工具结果回合仍是 role="user"，content 数组里放
             {"type":"tool_result","tool_use_id","content"}
             顶层**不允许**出现 tool_calls 字段

  OpenAI     assistant.tool_calls[] 每项须有 id / type="function" /
             function.arguments（**JSON 字符串**）
             工具结果是独立的 role="tool" 消息，必须带 tool_call_id

把「只有真机才能发现的错」变成 CI 能抓的错 —— 这是本文件唯一的目的。
"""

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travelwise import llm  # noqa: E402,F401  (确保包已导入)
from travelwise.agent_loop import ToolCallingAgent                    # noqa: E402
from travelwise.llm import http_clients                               # noqa: E402
from travelwise.llm.http_clients import AnthropicClient, OpenAIClient  # noqa: E402
from travelwise.providers.mock_flight import MockFlightProvider       # noqa: E402
from travelwise.skills.destination import DestinationSkill            # noqa: E402
from travelwise.skills.flight import FlightSkill                      # noqa: E402
from travelwise.tools.registry import build_registry                  # noqa: E402

TODAY = date(2026, 8, 5)


def make_registry():
    return build_registry(FlightSkill(MockFlightProvider(today=TODAY)),
                          DestinationSkill(), today=TODAY)


class _CapturingTransport:
    """替换 `_post_json`：记录发出的 payload，按顺序返回预置响应。

    这样就能在**零网络**的前提下检查真实 client 究竟会把什么发上去。
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def __call__(self, url, headers, payload, timeout):
        self.payloads.append(json.loads(json.dumps(payload)))   # 深拷贝，防后续改动
        if not self.responses:
            raise AssertionError("client 发起的请求次数超过预置响应数")
        return self.responses.pop(0)


class _ContractCase(unittest.TestCase):
    """公共装配：跑一次「调工具 → 回结果 → 收尾」的两轮闭环。"""

    def run_two_turns(self, client, responses):
        transport = _CapturingTransport(responses)
        original = http_clients._post_json
        http_clients._post_json = transport
        try:
            result = ToolCallingAgent(client, make_registry(),
                                      today=TODAY).run("沈阳有什么好玩的")
        finally:
            http_clients._post_json = original
        self.assertEqual(len(transport.payloads), 2,
                         "应当发生两轮调用：选工具 + 基于工具结果收尾")
        return transport.payloads, result


# ==========================================================================
class TestAnthropicToolHistoryContract(_ContractCase):
    """Anthropic Messages API 的 tool_use / tool_result 形状。"""

    RESPONSES = [
        {"model": "claude-x", "stop_reason": "tool_use",
         "usage": {"input_tokens": 600, "output_tokens": 50},
         "content": [{"type": "tool_use", "id": "toolu_01ABC",
                      "name": "search_destination",
                      "input": {"place": "沈阳", "scope": "city"}}]},
        {"model": "claude-x", "stop_reason": "end_turn",
         "usage": {"input_tokens": 1500, "output_tokens": 180},
         "content": [{"type": "text", "text": "已根据工具结果整理如下。"}]},
    ]

    def setUp(self):
        self.payloads, self.result = self.run_two_turns(
            AnthropicClient(api_key="test-key"), self.RESPONSES)
        self.messages = self.payloads[1]["messages"]

    def test_history_has_three_messages(self):
        """user → assistant(tool_use) → user(tool_result)。"""
        self.assertEqual([m["role"] for m in self.messages],
                         ["user", "assistant", "user"])

    def test_assistant_content_is_a_block_list(self):
        assistant = self.messages[1]
        self.assertIsInstance(assistant["content"], list,
                              "Anthropic 的 assistant.content 必须是内容块数组")

    def test_assistant_has_no_toplevel_tool_calls_field(self):
        """顶层 tool_calls 是 OpenAI 的形状，Anthropic 会拒绝未知字段。"""
        self.assertNotIn("tool_calls", self.messages[1])

    def test_tool_use_block_is_wellformed(self):
        block = next(b for b in self.messages[1]["content"]
                     if isinstance(b, dict) and b.get("type") == "tool_use")
        self.assertEqual(block["name"], "search_destination")
        self.assertEqual(block["input"], {"place": "沈阳", "scope": "city"})
        self.assertTrue(block.get("id"), "tool_use 必须带 id，否则结果无法配对")

    def test_tool_result_pairs_with_tool_use_id(self):
        """id 对不上是真机最常见的 400，必须断言。"""
        use_block = next(b for b in self.messages[1]["content"]
                         if isinstance(b, dict) and b.get("type") == "tool_use")
        result_blocks = [b for b in self.messages[2]["content"]
                         if isinstance(b, dict) and b.get("type") == "tool_result"]
        self.assertEqual(len(result_blocks), 1)
        self.assertEqual(result_blocks[0]["tool_use_id"], use_block["id"])
        self.assertEqual(result_blocks[0]["tool_use_id"], "toolu_01ABC",
                         "必须回传模型给的原始 id，不能自己另编一个")

    def test_tool_result_content_is_string_or_blocks(self):
        block = self.messages[2]["content"][0]
        self.assertIn(type(block["content"]), (str, list))


# ==========================================================================
class TestOpenAIToolHistoryContract(_ContractCase):
    """OpenAI Chat Completions 的 tool_calls / role=tool 形状。"""

    RESPONSES = [
        {"model": "gpt-x", "usage": {"prompt_tokens": 600, "completion_tokens": 50},
         "choices": [{"finish_reason": "tool_calls", "message": {
             "content": None,
             "tool_calls": [{"id": "call_abc123", "type": "function",
                             "function": {"name": "search_destination",
                                          "arguments": '{"place": "沈阳", "scope": "city"}'}}]}}]},
        {"model": "gpt-x", "usage": {"prompt_tokens": 1500, "completion_tokens": 180},
         "choices": [{"finish_reason": "stop",
                      "message": {"content": "已根据工具结果整理如下。"}}]},
    ]

    def setUp(self):
        self.payloads, self.result = self.run_two_turns(
            OpenAIClient(api_key="test-key"), self.RESPONSES)
        self.messages = self.payloads[1]["messages"]

    def test_tool_result_uses_dedicated_tool_role(self):
        """OpenAI 的工具结果不是 user 消息，而是 role="tool"。"""
        self.assertIn("tool", [m["role"] for m in self.messages])

    def test_assistant_tool_calls_are_wellformed(self):
        assistant = next(m for m in self.messages if m["role"] == "assistant")
        self.assertIn("tool_calls", assistant)
        call = assistant["tool_calls"][0]
        self.assertTrue(call.get("id"))
        self.assertEqual(call.get("type"), "function")
        self.assertEqual(call["function"]["name"], "search_destination")

    def test_tool_call_arguments_are_json_string(self):
        """OpenAI 要求 arguments 是字符串，传 dict 会 400。"""
        assistant = next(m for m in self.messages if m["role"] == "assistant")
        args = assistant["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(args, str)
        self.assertEqual(json.loads(args), {"place": "沈阳", "scope": "city"})

    def test_tool_message_carries_matching_call_id(self):
        assistant = next(m for m in self.messages if m["role"] == "assistant")
        tool_msg = next(m for m in self.messages if m["role"] == "tool")
        self.assertEqual(tool_msg["tool_call_id"],
                         assistant["tool_calls"][0]["id"])
        self.assertEqual(tool_msg["tool_call_id"], "call_abc123")

    def test_tool_message_content_is_string(self):
        tool_msg = next(m for m in self.messages if m["role"] == "tool")
        self.assertIsInstance(tool_msg["content"], str)


# ==========================================================================
class TestSystemPromptNotDuplicated(_ContractCase):
    """system 的位置也是两家的差异点，顺手钉住。"""

    def test_anthropic_uses_toplevel_system(self):
        payloads, _ = self.run_two_turns(
            AnthropicClient(api_key="k"),
            TestAnthropicToolHistoryContract.RESPONSES)
        self.assertIn("system", payloads[0])
        self.assertNotIn("system", [m["role"] for m in payloads[0]["messages"]])

    def test_openai_uses_system_message(self):
        payloads, _ = self.run_two_turns(
            OpenAIClient(api_key="k"),
            TestOpenAIToolHistoryContract.RESPONSES)
        self.assertEqual(payloads[0]["messages"][0]["role"], "system")
        self.assertNotIn("system", payloads[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
