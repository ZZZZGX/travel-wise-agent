# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""Phase 1 测试：LLM Function Calling / ToolRegistry / LLMRouter / Agent Loop。

原有 tests/test_agent.py 一行未改 —— 本文件全部是新增覆盖。

重点保护的不变量：
  - 接上 LLM 之后，HITL 依然成立（模型无法绕过人工确认）；
  - 模型不可用 / 返回不可解析时，如实失败或标记降级，绝不编造；
  - LLMRouter 与 RuleRouter 输出结构一致，因此能跑同一套 Eval。
"""

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travelwise.agent_loop import ToolCallingAgent                       # noqa: E402
from travelwise.llm.base import LLMError, LLMResponse, LLMUnavailable, ToolCall, Usage  # noqa: E402
from travelwise.llm.scripted import ScriptedLLMClient                    # noqa: E402
from travelwise.providers.mock_flight import (                           # noqa: E402
    FailingFlightProvider, MockFlightProvider)
from travelwise.routing.base import RuleRouter                           # noqa: E402
from travelwise.routing.llm_router import LLMRouter                      # noqa: E402
from travelwise.skills.destination import DestinationSkill               # noqa: E402
from travelwise.skills.flight import FlightSkill                         # noqa: E402
from travelwise.state import TaskStatus                                  # noqa: E402
from travelwise.tools.registry import (                                  # noqa: E402
    ToolArgumentError, build_registry, validate_arguments)

TODAY = date(2026, 8, 5)


def make_registry(provider=None):
    return build_registry(FlightSkill(provider or MockFlightProvider(today=TODAY)),
                          DestinationSkill(), today=TODAY)


def scripted(args: dict, **kw) -> ScriptedLLMClient:
    """构造一个只会回一次 plan_travel_request 的回放客户端。"""
    return ScriptedLLMClient(script={
        kw.pop("text", "任意"): {
            "tool_calls": [{"name": "plan_travel_request", "arguments": args}],
            "usage": {"input_tokens": 300, "output_tokens": 40},
        }}, **kw)


# ==========================================================================
class TestToolRegistry(unittest.TestCase):

    def setUp(self):
        self.registry = make_registry()

    def test_three_tools_registered(self):
        self.assertEqual(sorted(self.registry.names()),
                         ["create_reminder", "search_destination", "search_flights"])

    def test_schemas_are_wellformed(self):
        for schema in self.registry.to_schemas():
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            self.assertEqual(schema["parameters"]["type"], "object")
            self.assertIn("properties", schema["parameters"])

    def test_missing_required_argument_rejected(self):
        result = self.registry.call("search_destination", {"scope": "city"})
        self.assertFalse(result.ok)
        self.assertIn("place", result.error)

    def test_enum_violation_rejected(self):
        result = self.registry.call("search_destination",
                                    {"place": "沈阳", "scope": "国家"})
        self.assertFalse(result.ok)
        self.assertIn("scope", result.error)

    def test_numeric_string_coerced(self):
        """模型常把整数写成字符串，应归一化而不是报错。"""
        result = self.registry.call(
            "search_destination", {"place": "沈阳", "scope": "city", "travel_month": "8"})
        self.assertTrue(result.ok)

    def test_out_of_range_integer_rejected(self):
        result = self.registry.call(
            "search_destination", {"place": "沈阳", "scope": "city", "travel_month": 13})
        self.assertFalse(result.ok)

    def test_unknown_tool_reports_available_ones(self):
        result = self.registry.call("book_hotel", {})
        self.assertFalse(result.ok)
        self.assertIn("search_flights", result.error)

    def test_extra_arguments_ignored(self):
        result = self.registry.call(
            "search_destination", {"place": "沈阳", "scope": "city", "junk": 1})
        self.assertTrue(result.ok)

    def test_validate_arguments_direct(self):
        schema = {"type": "object",
                  "properties": {"n": {"type": "integer", "minimum": 1}},
                  "required": ["n"]}
        self.assertEqual(validate_arguments(schema, {"n": 3}), {"n": 3})
        with self.assertRaises(ToolArgumentError):
            validate_arguments(schema, {})

    def test_tool_failure_payload_forbids_fabrication(self):
        """工具失败时回给模型的内容必须明确禁止编造。"""
        registry = make_registry(FailingFlightProvider("timeout"))
        result = registry.call("search_flights",
                               {"origin": "上海", "destination": "成都",
                                "travel_date": "2026-08-28"})
        payload = result.to_model_payload()
        self.assertFalse(payload["ok"])
        self.assertIn("不要编造", payload["note"])


# ==========================================================================
class TestHitlSurvivesLLM(unittest.TestCase):
    """接上 LLM 之后，人工确认闸门必须依然成立。"""

    def test_create_reminder_never_executes_in_tool_layer(self):
        result = make_registry().call(
            "create_reminder", {"title": "购票", "remind_date": "2026-08-15"})
        self.assertTrue(result.ok)
        self.assertTrue(result.requires_approval)
        self.assertFalse(result.content["executed"])
        self.assertEqual(result.content["status"], "pending_approval")

    def test_payload_tells_model_not_to_claim_success(self):
        result = make_registry().call(
            "create_reminder", {"title": "购票", "remind_date": "2026-08-15"})
        self.assertIn("尚未执行", result.to_model_payload()["note"])

    def test_agent_loop_surfaces_pending_approval(self):
        registry = make_registry()
        client = ScriptedLLMClient(script={
            "帮我设个购票提醒": {"tool_calls": [{"name": "create_reminder",
                                        "arguments": {"title": "购票",
                                                      "remind_date": "2026-08-15"}}]}},
            fallback=lambda _t: LLMResponse(text="请确认以下提醒内容。"))
        result = ToolCallingAgent(client, registry, today=TODAY).run("帮我设个购票提醒")
        self.assertEqual(len(result.pending_approval), 1)
        self.assertFalse(result.pending_approval[0]["executed"])


# ==========================================================================
class TestLLMRouter(unittest.TestCase):

    def _route(self, args, text="任意"):
        client = scripted(args, text=text)
        return LLMRouter(client).route(text, TODAY)

    def test_produces_same_shape_as_rule_router(self):
        """两个路由器的 TravelState 字段必须一致，否则无法跑同一套 Eval。"""
        rule = RuleRouter().route("8月28号从上海飞成都", TODAY).state
        llm = self._route({"intents": ["flight"], "origin": "上海",
                           "destination": "成都", "travel_date": "2026-08-28"}).state
        self.assertEqual(set(rule.to_dict()), set(llm.to_dict()))
        self.assertEqual(rule.intents, llm.intents)
        self.assertEqual(rule.origin, llm.origin)
        self.assertEqual(rule.travel_date, llm.travel_date)

    def test_empty_intents_marks_out_of_scope(self):
        outcome = self._route({"intents": []})
        self.assertEqual(outcome.state.current_step, TaskStatus.OUT_OF_SCOPE)

    def test_missing_params_detected(self):
        outcome = self._route({"intents": ["flight"], "destination": "成都"})
        self.assertEqual(sorted(outcome.state.missing), ["origin", "travel_date"])

    def test_bad_date_is_discarded_not_guessed(self):
        """模型给了非法日期 → 丢弃并标记缺失，不猜一个。"""
        outcome = self._route({"intents": ["flight"], "origin": "上海",
                               "destination": "成都", "travel_date": "下周三"})
        self.assertIsNone(outcome.state.travel_date)
        self.assertIn("travel_date", outcome.state.missing)

    def test_impossible_date_discarded(self):
        outcome = self._route({"intents": ["flight"], "origin": "上海",
                               "destination": "成都", "travel_date": "2026-02-31"})
        self.assertIsNone(outcome.state.travel_date)

    def test_same_origin_and_destination_treated_as_error(self):
        outcome = self._route({"intents": ["flight"], "origin": "成都",
                               "destination": "成都", "travel_date": "2026-08-28"})
        self.assertIsNone(outcome.state.origin)
        self.assertIn("origin", outcome.state.missing)

    def test_invalid_scope_falls_back_to_city(self):
        """范围只能是 city/province；给了别的一律按 city，绝不擅自扩大。"""
        outcome = self._route({"intents": ["destination"], "place": "沈阳",
                               "scope": "country"})
        self.assertEqual(outcome.state.scope, "city")

    def test_intents_are_deduped_and_ordered(self):
        outcome = self._route({"intents": ["destination", "flight", "flight"],
                               "origin": "上海", "destination": "成都",
                               "travel_date": "2026-08-28", "place": "四川",
                               "scope": "province"})
        self.assertEqual(outcome.state.intents, ["flight", "destination"])

    def test_usage_recorded(self):
        outcome = self._route({"intents": ["flight"]})
        self.assertEqual(outcome.usage.total, 340)

    def test_rule_router_costs_no_tokens(self):
        outcome = RuleRouter().route("沈阳有什么好玩的", TODAY)
        self.assertEqual(outcome.usage.total, 0)


# ==========================================================================
class TestLLMFailureHandling(unittest.TestCase):
    """模型出问题时不得编造，且降级必须被标记。"""

    class _Boom:
        name = "boom"
        model = "boom"
        is_synthetic = False

        def complete(self, *a, **kw):
            raise LLMError("connection refused")

    class _NoToolCall:
        name = "chatty"
        model = "chatty"
        is_synthetic = False

        def complete(self, *a, **kw):
            return LLMResponse(text="我觉得你应该去成都玩", usage=Usage(100, 20))

    def test_llm_error_without_fallback_reports_failure(self):
        outcome = LLMRouter(self._Boom()).route("8月28号从上海飞成都", TODAY)
        self.assertIn("connection refused", outcome.error)
        self.assertEqual(outcome.state.intents, [])

    def test_llm_error_with_fallback_is_marked(self):
        """降级到规则路由必须打标记，成绩不能算在 LLM 头上。"""
        outcome = LLMRouter(self._Boom(), fallback=RuleRouter()).route(
            "8月28号从上海飞成都", TODAY)
        self.assertTrue(outcome.fell_back)
        self.assertEqual(outcome.state.origin, "上海")
        self.assertIn("降级", outcome.error)

    def test_model_ignoring_tool_is_a_failure(self):
        outcome = LLMRouter(self._NoToolCall()).route("8月28号从上海飞成都", TODAY)
        self.assertIn("未调用", outcome.error)

    def test_agent_loop_reports_llm_failure_honestly(self):
        result = ToolCallingAgent(self._Boom(), make_registry(), today=TODAY).run("查机票")
        self.assertFalse(result.ok)
        self.assertIn("模型调用失败", result.answer)
        self.assertIn("没有编造", result.answer)

    def test_empty_input_costs_no_call(self):
        client = scripted({"intents": ["flight"]})
        outcome = LLMRouter(client).route("", TODAY)
        self.assertEqual(client.call_count, 0)


# ==========================================================================
class TestScriptedClient(unittest.TestCase):
    """离线回放：找不到记录必须明确失败，绝不即兴编造。"""

    def test_unknown_input_raises(self):
        with self.assertRaises(LLMUnavailable):
            ScriptedLLMClient(script={"甲": {"text": "x"}}).complete(
                [{"role": "user", "content": "乙"}])

    def test_punctuation_insensitive_lookup(self):
        client = ScriptedLLMClient(script={"月底上海去北京": {"text": "ok"}})
        self.assertEqual(client.complete(
            [{"role": "user", "content": "月底上海去北京。"}]).text, "ok")

    def test_marked_as_synthetic(self):
        """必须自报是合成数据，否则对照报告会把它当成真实模型成绩。"""
        self.assertTrue(ScriptedLLMClient().is_synthetic)

    def test_fixtures_file_loads(self):
        path = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / "llm_responses.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("_README", data)
        self.assertTrue(data["responses"])

    def test_fixtures_declare_synthetic_nature(self):
        """回放文件必须自带"这不是真实模型输出"的说明。"""
        path = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / "llm_responses.json"
        readme = json.loads(path.read_text(encoding="utf-8"))["_README"]
        self.assertIn("合成", readme)


# ==========================================================================
class TestAgentLoop(unittest.TestCase):

    def _agent(self, script, provider=None, **kw):
        client = ScriptedLLMClient(
            script=script,
            fallback=lambda _t: LLMResponse(text="已根据工具结果整理完毕。",
                                            usage=Usage(400, 50)))
        return ToolCallingAgent(client, make_registry(provider), today=TODAY, **kw)

    def test_full_loop_calls_tool_then_answers(self):
        agent = self._agent({"沈阳有什么好玩的": {
            "tool_calls": [{"name": "search_destination",
                            "arguments": {"place": "沈阳", "scope": "city"}}]}})
        result = agent.run("沈阳有什么好玩的")
        self.assertTrue(result.ok)
        self.assertEqual(result.tool_names, ["search_destination"])
        self.assertEqual(len(result.turns), 2)
        self.assertTrue(result.answer)

    def test_usage_accumulates_across_turns(self):
        agent = self._agent({"沈阳有什么好玩的": {
            "tool_calls": [{"name": "search_destination",
                            "arguments": {"place": "沈阳", "scope": "city"}}],
            "usage": {"input_tokens": 300, "output_tokens": 40}}})
        self.assertEqual(agent.run("沈阳有什么好玩的").usage.total, 790)

    def test_tool_failure_passed_to_model_not_hidden(self):
        agent = self._agent({"查机票": {
            "tool_calls": [{"name": "search_flights",
                            "arguments": {"origin": "上海", "destination": "成都",
                                          "travel_date": "2026-08-28"}}]}},
            provider=FailingFlightProvider("timeout"))
        result = agent.run("查机票")
        self.assertFalse(result.turns[0].tool_results[0].ok)
        self.assertTrue(result.ok)          # 循环本身没崩，失败已交给模型解释

    def test_max_turns_exhausted_is_honest(self):
        """一直要调工具却不收敛 → 如实说明，不硬凑答案。"""
        client = ScriptedLLMClient(script={}, fallback=lambda _t: LLMResponse(
            tool_calls=[ToolCall(name="search_destination",
                                 arguments={"place": "沈阳", "scope": "city"})],
            usage=Usage(10, 5)))
        result = ToolCallingAgent(client, make_registry(), max_turns=2, today=TODAY).run("x")
        self.assertFalse(result.ok)
        self.assertIn("最大轮次", result.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
