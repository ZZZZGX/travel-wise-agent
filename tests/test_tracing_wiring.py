# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""埋点的回归测试。

这个文件盯的是一类特定的失败：**观测改变了被观测的东西**。

可观测性最容易出的事故不是「没记上」，而是「记的过程中把行为改了」——
span 里赋值时顺手改了一个变量、异常被 with 吞掉、开了 trace 之后
某条分支走法不一样了。这类 bug 尤其恶心，因为它只在开 trace 时出现，
而人们恰恰是在出问题、想看 trace 的时候才打开它。

所以下面每一组的第一条测试，测的都是「加了 tracer 之后结果一模一样」。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.setdefault("TRAVELWISE_LLM_PROVIDER", "scripted")

import view_trace                                              # noqa: E402
from travelwise.agent_loop import ToolCallingAgent             # noqa: E402
from travelwise.config import Settings, build_llm_client       # noqa: E402
from travelwise.orchestrator import TravelWiseAgent            # noqa: E402
from travelwise.providers.base import ReminderResult           # noqa: E402
from travelwise.providers.mock_flight import (                 # noqa: E402
    FailingFlightProvider, MockFlightProvider)
from travelwise.skills.destination import DestinationSkill     # noqa: E402
from travelwise.skills.flight import FlightSkill               # noqa: E402
from travelwise.tools.registry import build_registry           # noqa: E402
from travelwise.tracing import (                               # noqa: E402
    STATUS_ERROR, STATUS_OK, STATUS_REJECTED, MemoryTraceSink, Span,
    Tracer, build_tracer, load_trace)

TODAY = date(2026, 8, 14)
REQUEST = "8月28号从上海飞乌鲁木齐，新疆有什么玩的"


class SilentReminder:
    name = "silent"

    def available(self) -> bool:
        return True

    def create(self, request) -> ReminderResult:
        return ReminderResult(ok=True, provider=self.name, message="ok", location="")


def make_registry(provider=None):
    return build_registry(FlightSkill(provider or MockFlightProvider()),
                          DestinationSkill(), today=TODAY)


def make_client():
    return build_llm_client(Settings.from_env())


def tree(spans: list[Span]) -> dict[str, list[str]]:
    """{父 span 名: [子 span 名]}，用名字断言比用随机 id 断言可读得多。"""
    by_id = {s.span_id: s for s in spans}
    out: dict[str, list[str]] = {}
    for s in spans:
        parent = by_id.get(s.parent_id)
        out.setdefault(parent.name if parent else "", []).append(s.name)
    return out


# ======================================================================
# 一、观测不得改变行为
# ======================================================================
class TestTracingIsNonIntrusive(unittest.TestCase):

    def test_agent_loop_answer_is_identical(self):
        """开 trace 与不开 trace，回答必须逐字相同。"""
        client, reg = make_client(), make_registry()
        plain = ToolCallingAgent(client, reg, today=TODAY).run(REQUEST)
        traced = ToolCallingAgent(client, reg, today=TODAY,
                                  tracer=Tracer(enabled=True)).run(REQUEST)
        self.assertEqual(traced.answer, plain.answer)
        self.assertEqual(traced.tool_names, plain.tool_names)
        self.assertEqual(traced.ok, plain.ok)
        self.assertEqual(traced.usage.total, plain.usage.total)

    def test_orchestrator_state_is_identical(self):
        """state 除了 trace_id 之外必须完全一样。

        trace_id 是唯一允许多出来的东西——它是**指向这次观测的指针**，
        不是这次运行的结果。
        """
        plain = TravelWiseAgent(MockFlightProvider(), today=TODAY).handle(REQUEST)
        traced = TravelWiseAgent(MockFlightProvider(), today=TODAY,
                                 tracer=Tracer(enabled=True)).handle(REQUEST)
        a, b = plain.to_dict(), traced.to_dict()
        a.pop("trace_id", None)
        b.pop("trace_id", None)
        self.assertEqual(a, b)

    def test_default_tracer_is_disabled(self):
        """不传 tracer 时必须是空转的。默认打开可观测性，等于默认写磁盘。"""
        self.assertFalse(TravelWiseAgent(MockFlightProvider()).tracer.enabled)
        self.assertFalse(
            ToolCallingAgent(make_client(), make_registry()).tracer.enabled)

    def test_disabled_tracer_records_nothing(self):
        tracer = Tracer(enabled=False)
        TravelWiseAgent(MockFlightProvider(), today=TODAY,
                        tracer=tracer).handle(REQUEST)
        self.assertEqual(tracer.spans, [])

    def test_disabled_tracer_leaves_no_trace_id_in_state(self):
        """没有 trace 却留个 id，会让人拿着它去找一份不存在的文件。"""
        state = TravelWiseAgent(MockFlightProvider(), today=TODAY).handle(REQUEST)
        self.assertEqual(state.trace_id, "")

    def test_build_tracer_writes_nothing_when_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracer = build_tracer(False, out_dir=tmp)
            TravelWiseAgent(MockFlightProvider(), today=TODAY,
                            tracer=tracer).handle(REQUEST)
            tracer.close()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_exception_inside_span_still_propagates(self):
        """span 记下错误之后必须原样把异常抛出去，不许吞。"""
        tracer = Tracer(enabled=True)
        with self.assertRaises(ValueError):
            with tracer.span("boom", kind="tool"):
                raise ValueError("炸了")
        self.assertEqual(tracer.spans[0].status, STATUS_ERROR)
        self.assertIn("炸了", tracer.spans[0].error)


# ======================================================================
# 二、span 树的形状
# ======================================================================
class TestAgentLoopSpans(unittest.TestCase):

    def setUp(self):
        self.tracer = Tracer(enabled=True)
        self.result = ToolCallingAgent(make_client(), make_registry(), today=TODAY,
                                       tracer=self.tracer).run(REQUEST)

    def test_root_span_wraps_everything(self):
        roots = [s for s in self.tracer.spans if not s.parent_id]
        self.assertEqual([s.name for s in roots], ["agent.run"])
        root = roots[0]
        self.assertEqual(root.attributes["turns"], len(self.result.turns))
        self.assertEqual(root.attributes["tools"], self.result.tool_names)

    def test_llm_and_tool_spans_hang_off_the_root(self):
        kids = tree(self.tracer.spans)["agent.run"]
        self.assertIn("llm.complete", kids)
        for name in self.result.tool_names:
            self.assertIn(name, kids)

    def test_llm_span_carries_tokens(self):
        llm = [s for s in self.tracer.spans if s.kind == "llm"]
        self.assertTrue(llm)
        self.assertEqual(sum(s.total_tokens for s in llm),
                         self.result.usage.total)

    def test_every_span_knows_its_turn(self):
        """哪一轮出的问题，是看 trace 时第一个想知道的事。"""
        for s in self.tracer.spans:
            if s.name != "agent.run":
                self.assertIn("turn", s.attributes, s.name)

    def test_unconverged_run_marks_root_red(self):
        """轮次耗尽不抛异常，所以 span 的异常分支抓不到——
        必须显式标红，否则 trace 一片绿而用户看到的是失败。"""
        tracer = Tracer(enabled=True)
        result = ToolCallingAgent(make_client(), make_registry(), today=TODAY,
                                  max_turns=1, tracer=tracer).run(REQUEST)
        self.assertFalse(result.ok)
        root = [s for s in tracer.spans if not s.parent_id][0]
        self.assertEqual(root.status, STATUS_ERROR)
        self.assertIn("最大轮次", root.error)


class TestToolSpanStatus(unittest.TestCase):
    """`rejected` 与 `error` 的区别不是程度而是归属：
    该去改 prompt，还是该去看接口。"""

    def _status(self, tool: str, args: dict, provider=None) -> Span:
        from travelwise.agent_loop import _span_status
        result = make_registry(provider).call(tool, args)
        return _span_status(result), result

    def test_unknown_tool_is_rejected_not_error(self):
        status, _ = self._status("search_hotels", {})
        self.assertEqual(status, STATUS_REJECTED)

    def test_bad_arguments_is_rejected(self):
        status, _ = self._status("search_flights", {"origin": "上海"})
        self.assertEqual(status, STATUS_REJECTED)

    def test_provider_failure_is_error(self):
        status, result = self._status(
            "search_flights",
            {"origin": "上海", "destination": "成都", "travel_date": "2026-08-28"},
            provider=FailingFlightProvider("timeout"))
        self.assertFalse(result.ok)
        self.assertEqual(status, STATUS_ERROR)
        self.assertEqual(result.error_kind, "tool_failed")

    def test_pending_approval_is_not_an_error(self):
        """HITL 是正常路径。把它标红，等于让本项目最骄傲的那条行为
        在 trace 里长得像一次故障。"""
        from travelwise.agent_loop import _span_status
        result = make_registry().call(
            "create_reminder", {"title": "买 上海→成都 机票",
                                "remind_date": "2026-08-20"})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.requires_approval,
                        "create_reminder 必须是需要人工确认的工具")
        self.assertEqual(_span_status(result), STATUS_OK)
        self.assertEqual(result.error_kind, "")

    def test_error_kind_is_set_for_every_failure_path(self):
        self.assertEqual(make_registry().call("nope", {}).error_kind, "unknown_tool")
        self.assertEqual(
            make_registry().call("search_flights", {}).error_kind, "bad_arguments")


class TestOrchestratorSpans(unittest.TestCase):

    def test_tree_shape(self):
        tracer = Tracer(enabled=True)
        TravelWiseAgent(MockFlightProvider(), today=TODAY,
                        tracer=tracer).handle(REQUEST)
        t = tree(tracer.spans)
        self.assertEqual(t[""], ["agent.handle"])
        self.assertEqual(sorted(t["agent.handle"]), ["dispatch", "route"])
        self.assertEqual(sorted(t["dispatch"]),
                         ["skill.destination", "skill.flight"])

    def test_route_span_records_which_router_ran(self):
        tracer = Tracer(enabled=True)
        TravelWiseAgent(MockFlightProvider(), today=TODAY,
                        tracer=tracer).handle(REQUEST)
        route = [s for s in tracer.spans if s.name == "route"][0]
        self.assertEqual(route.attributes["router"], "rule")
        self.assertIn("flight", route.attributes["intents"])

    def test_failed_skill_is_red_but_dispatch_reports_partial(self):
        """部分交付是本项目最看重的行为。如果它在 trace 上和「整轮全崩」
        长得一模一样，那这条行为就等于没被观测到。"""
        tracer = Tracer(enabled=True)
        TravelWiseAgent(FailingFlightProvider("timeout"), today=TODAY,
                        tracer=tracer).handle(REQUEST)
        by_name = {s.name: s for s in tracer.spans}
        self.assertEqual(by_name["skill.flight"].status, STATUS_ERROR)
        self.assertEqual(by_name["skill.destination"].status, STATUS_OK)
        self.assertEqual(by_name["dispatch"].status, STATUS_OK)
        self.assertTrue(by_name["dispatch"].attributes["partial"])
        self.assertEqual(by_name["dispatch"].attributes["delivered"],
                         ["destination"])

    def test_hitl_gate_is_visible(self):
        """一次运行到底有没有真的写出去、是谁点的头，最终回答里看不出来。"""
        tracer = Tracer(enabled=True)
        agent = TravelWiseAgent(MockFlightProvider(), today=TODAY,
                                reminder_provider=SilentReminder(), tracer=tracer)
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        names = [s.name for s in tracer.spans]
        self.assertIn("hitl.awaiting_approval", names)
        self.assertNotIn("reminder.create", names)      # 还没人点头，不许写

        agent.approve(state, approved=True)
        names = [s.name for s in tracer.spans]
        self.assertIn("reminder.create", names)
        approve = [s for s in tracer.spans if s.name == "hitl.approve"][0]
        self.assertTrue(approve.attributes["executed"])
        self.assertIs(approve.attributes["ok"], True)

    def test_rejected_approval_does_not_emit_a_write_span(self):
        tracer = Tracer(enabled=True)
        agent = TravelWiseAgent(MockFlightProvider(), today=TODAY,
                                reminder_provider=SilentReminder(), tracer=tracer)
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        agent.approve(state, approved=False)
        self.assertNotIn("reminder.create", [s.name for s in tracer.spans])
        approve = [s for s in tracer.spans if s.name == "hitl.approve"][0]
        self.assertFalse(approve.attributes["executed"])


# ======================================================================
# 三、脱敏与落盘
# ======================================================================
class TestRedactionAndPersistence(unittest.TestCase):

    def test_arguments_are_digested_not_raw(self):
        """trace 是要贴进 issue、发给同事的东西。凭证不许进去。"""
        tracer = Tracer(enabled=True)
        with tracer.span("x", kind="tool",
                         arguments={"api_key": "sk-abcdefghijklmnop",
                                    "note": "Bearer abcdefghijklmnopqrst",
                                    "city": "上海"}):
            pass
        args = tracer.spans[0].arguments
        self.assertEqual(args["api_key"], "<redacted>")
        self.assertNotIn("abcdefghijklmnopqrst", json.dumps(args, ensure_ascii=False))
        self.assertEqual(args["city"], "上海")

    def test_jsonl_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracer = build_tracer(True, out_dir=tmp, metadata={"mode": "test"})
            TravelWiseAgent(MockFlightProvider(), today=TODAY,
                            tracer=tracer).handle(REQUEST)
            tracer.close()
            path = Path(tracer.metadata["path"])
            meta, spans, summary = load_trace(path)
            self.assertEqual(meta["mode"], "test")
            self.assertEqual(len(spans), len(tracer.spans))
            self.assertEqual(summary["trace_id"], tracer.trace_id)

    def test_truncated_file_still_loads(self):
        """被 Ctrl-C 掉的那次运行，恰恰是最需要看 trace 的那次。"""
        with tempfile.TemporaryDirectory() as tmp:
            tracer = build_tracer(True, out_dir=tmp)
            TravelWiseAgent(MockFlightProvider(), today=TODAY,
                            tracer=tracer).handle(REQUEST)
            path = Path(tracer.metadata["path"])
            tracer.sink._fh.close()        # 模拟进程被杀：没有走 close()，没有 summary
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"_type": "span", "name": "半行就断')
            meta, spans, summary = load_trace(path)
            self.assertTrue(spans)
            self.assertEqual(summary, {})

    def test_memory_sink_receives_every_span(self):
        sink = MemoryTraceSink()
        tracer = Tracer(enabled=True, sink=sink)
        TravelWiseAgent(MockFlightProvider(), today=TODAY,
                        tracer=tracer).handle(REQUEST)
        tracer.close()
        self.assertEqual(len(sink.spans), len(tracer.spans))
        self.assertEqual(sink.summary["trace_id"], tracer.trace_id)


# ======================================================================
# 四、Trace Viewer
# ======================================================================
class TestTraceViewer(unittest.TestCase):

    def _spans(self) -> list[Span]:
        tracer = Tracer(enabled=True)
        agent = TravelWiseAgent(FailingFlightProvider("timeout"), today=TODAY,
                                reminder_provider=SilentReminder(), tracer=tracer)
        agent.handle(REQUEST, want_reminder=True)
        return tracer.spans

    def test_parents_are_rendered_before_children(self):
        """JSONL 里 span 是子先父后的（父在 finally 才写）。
        照文件顺序画，会看到子节点浮在父节点上面。"""
        rows = view_trace.build_rows(self._spans())
        seen: set[str] = set()
        for row in rows:
            span = row["span"]
            if span.parent_id:
                self.assertIn(span.parent_id, seen,
                              "%s 出现在它父节点之前" % span.name)
            seen.add(span.span_id)

    def test_orphan_spans_are_kept_not_dropped(self):
        """父节点丢失通常意味着文件被截断——这时候更该显示，而不是丢掉。"""
        spans = self._spans()
        spans[-1].parent_id = "不存在的父节点"
        rows = view_trace.build_rows(spans)
        self.assertEqual(len(rows), len(spans))

    def test_layout_never_produces_invisible_bars(self):
        rows, total, _ = view_trace.layout(view_trace.build_rows(self._spans()))
        for row in rows:
            self.assertGreaterEqual(row["width_pct"], 0.6)
            self.assertLessEqual(row["offset_pct"] + row["width_pct"], 100.01)
        self.assertGreater(total, 0)

    def test_sub_millisecond_run_is_flagged_not_faked(self):
        """离线回放全程不到 1ms，条形的先后位置没有意义。
        这件事必须写在页面上，而不是让人对着一堆左对齐的条形猜自己看错了。"""
        html = view_trace.render({"trace_id": "t"}, self._spans(), {})
        self.assertIn("精度是 1 毫秒", html)

    def test_render_leaves_no_placeholder(self):
        html = view_trace.render({"trace_id": "t", "mode": "orchestrator"},
                                 self._spans(), {"wall_ms": 12.0, "spans": 6})
        self.assertNotIn("__", html.replace("__init__", ""))
        self.assertIn("orchestrator", html)

    def test_render_is_self_contained(self):
        """要能拖进浏览器、粘进 issue、发给同事。任何外链都会让它在别人那儿变样。"""
        html = view_trace.render({}, self._spans(), {})
        for token in ('src="http', 'href="http', "@import", "cdn."):
            self.assertNotIn(token, html)

    def test_failed_span_is_marked_red_in_markup(self):
        html = view_trace.render({}, self._spans(), {})
        self.assertIn("bar error", html)

    def test_hitl_row_is_flagged(self):
        html = view_trace.render({}, self._spans(), {})
        self.assertIn("gateflag", html)

    def test_empty_trace_tells_you_what_to_do(self):
        """空状态是一次邀请，不是一句道歉。"""
        html = view_trace.render({}, [], {})
        self.assertIn("--trace", html)

    def test_missing_summary_is_explained(self):
        html = view_trace.render({}, self._spans(), {})
        self.assertIn("中断", html)

    def test_html_escapes_user_text(self):
        """请求文本来自用户，直接拼进 HTML 就是一个注入点。"""
        spans = self._spans()
        spans[0].name = "<img src=x onerror=alert(1)>"
        html = view_trace.render({"request": "<script>alert(1)</script>"},
                                 spans, {})
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)

    def test_cli_reports_when_no_trace_exists(self):
        buf = io.StringIO()
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            original = view_trace.default_dir
            view_trace.default_dir = lambda: Path(tmp) / "traces"
            try:
                with contextlib.redirect_stdout(buf):
                    code = view_trace.main(["--latest"])
            finally:
                view_trace.default_dir = original
        self.assertEqual(code, 1)
        self.assertIn("--trace", buf.getvalue())

    def test_cli_renders_a_real_file(self):
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            tracer = build_tracer(True, out_dir=tmp, metadata={"mode": "orchestrator"})
            TravelWiseAgent(MockFlightProvider(), today=TODAY,
                            tracer=tracer).handle(REQUEST)
            tracer.close()
            path = tracer.metadata["path"]
            with contextlib.redirect_stdout(io.StringIO()):
                code = view_trace.main([path])
            self.assertEqual(code, 0)
            out = Path(path).with_suffix(".html")
            self.assertTrue(out.is_file())
            self.assertIn("TravelWise", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
