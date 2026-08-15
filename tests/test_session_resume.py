# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""多轮状态续跑的回归测试。

这个文件盯的是一类特定的失败：**「问了但接不住」**。

原实现能识别缺什么、能把追问打印出来，但每轮都从零 `route()` 一遍，
于是用户回答之后收到的是「这条请求不属于机票或目的地范围」。
问了却接不住，比压根不问更糟——它把一次可完成的任务变成了死循环。

所以这里的用例大多长成「两句话」的样子：第一句触发追问，第二句回答。
断言的重点不在措辞，而在**第二句之后任务是否真的往前走了**。
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travelwise import cli, paths                              # noqa: E402
from travelwise import session as session_mod                  # noqa: E402
from travelwise.orchestrator import TravelWiseAgent            # noqa: E402
from travelwise.providers.base import ReminderRequest, ReminderResult  # noqa: E402
from travelwise.providers.mock_flight import MockFlightProvider  # noqa: E402
from travelwise.session import Session, SessionStore, replay   # noqa: E402
from travelwise.state import TaskStatus, TravelState           # noqa: E402

TODAY = date(2026, 8, 14)


def make_agent(**kwargs) -> TravelWiseAgent:
    return TravelWiseAgent(MockFlightProvider(), today=TODAY, **kwargs)


class SilentReminder:
    """不打印任何东西的提醒 provider —— 测试输出要干净。"""

    name = "silent"

    def available(self) -> bool:
        return True

    def create(self, request: ReminderRequest) -> ReminderResult:
        return ReminderResult(ok=True, provider=self.name, message="ok", location="")


# ======================================================================
# TravelState 的槽位机制
# ======================================================================
class TestStateSlots(unittest.TestCase):

    def test_apply_slots_recomputes_missing(self):
        """补完槽位必须重算 missing —— 不重算就会「补了还在问」。"""
        s = TravelState(intents=["flight"])
        s.recompute_missing()
        self.assertEqual(set(s.missing), {"origin", "destination", "travel_date"})

        s.apply_slots({"origin": "上海", "destination": "成都"})
        self.assertEqual(s.missing, ["travel_date"])

        s.apply_slots({"travel_date": "2026-08-28"})
        self.assertEqual(s.missing, [])

    def test_apply_slots_does_not_overwrite(self):
        """已确定的值不会被顺手改掉。改值属于纠错，必须显式先清空。"""
        s = TravelState(intents=["flight"], origin="上海")
        applied = s.apply_slots({"origin": "北京"})
        self.assertEqual(applied, {})
        self.assertEqual(s.origin, "上海")

        s.clear_slots(["origin"])
        s.apply_slots({"origin": "北京"})
        self.assertEqual(s.origin, "北京")

    def test_apply_slots_ignores_empty_values(self):
        """一次没解析出东西的回话，不该把上一轮的成果清零。"""
        s = TravelState(intents=["flight"], origin="上海")
        s.apply_slots({"origin": None, "destination": ""})
        self.assertEqual(s.origin, "上海")
        self.assertIsNone(s.destination)

    def test_apply_slots_rejects_non_slot_fields(self):
        """槽位通道只能写槽位。它接的是用户输入，不能成为改写内部状态的口子。"""
        s = TravelState(intents=["flight"])
        s.apply_slots({"flight_result": {"ok": True}, "errors": ["伪造"]})
        self.assertIsNone(s.flight_result)
        self.assertEqual(s.errors, [])

    def test_carry_over_is_recorded(self):
        """沿用必须留痕：来源不是本轮用户输入的，都要记下来。"""
        s = TravelState(intents=["flight"])
        s.apply_slots({"origin": "上海"}, source="carry_over")
        self.assertEqual(s.carried_over, {"origin": "carry_over"})

        s2 = TravelState(intents=["flight"])
        s2.apply_slots({"origin": "上海"}, source="reply")
        self.assertEqual(s2.carried_over, {})


class TestStateRoundTrip(unittest.TestCase):

    def test_round_trip_preserves_everything(self):
        s = TravelState(
            user_request="8月28号从上海飞成都", intents=["flight"],
            origin="上海", destination="成都", travel_date="2026-08-28",
            missing=[], warnings=["w"], errors=["e"],
            carried_over={"origin": "carry_over"},
            current_step=TaskStatus.AWAITING_APPROVAL)
        back = TravelState.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(back.to_dict(), s.to_dict())
        self.assertIsInstance(back.current_step, TaskStatus)

    def test_unknown_fields_are_ignored(self):
        """旧会话文件里可能有本版已删掉的字段。为此崩掉毫无意义。"""
        back = TravelState.from_dict({"origin": "上海", "某个已废弃字段": 1})
        self.assertEqual(back.origin, "上海")

    def test_corrupt_status_falls_back(self):
        """状态值坏掉时「从头再来」远好过「这个会话再也打不开」。"""
        back = TravelState.from_dict({"current_step": "外星状态"})
        self.assertEqual(back.current_step, TaskStatus.CREATED)

    def test_none_containers_become_empty(self):
        back = TravelState.from_dict({"intents": None, "warnings": None,
                                      "carried_over": None})
        self.assertEqual(back.intents, [])
        self.assertEqual(back.warnings, [])
        self.assertEqual(back.carried_over, {})


# ======================================================================
# Orchestrator 的三个多轮入口
# ======================================================================
class TestOrchestratorResume(unittest.TestCase):

    def test_resume_clears_stale_question_but_keeps_errors(self):
        """上一轮的追问要清掉（用户已经答了），执行期的错误要留着。"""
        agent = make_agent()
        state = TravelState(intents=["flight"], origin="上海", destination="成都",
                            travel_date="2026-08-28",
                            warnings=["还需要你补充：出发城市"], errors=["机票模块：超时"])
        out = agent.resume(state)
        self.assertNotIn("还需要你补充：出发城市", out.warnings)
        self.assertIn("机票模块：超时", out.errors)

    def test_dispatch_does_not_rerun_successful_skill(self):
        """已经交付过的技能不重跑，否则用户会看到同一份清单打印两遍。"""
        agent = make_agent()
        state = TravelState(intents=["flight"], origin="上海", destination="成都",
                            travel_date="2026-08-28")
        first = agent.dispatch(state)
        marker = object()
        first.flight_result["_marker"] = marker
        second = agent.resume(first)
        self.assertIs(second.flight_result.get("_marker"), marker)

    def test_approve_executes_pending_action(self):
        agent = make_agent(reminder_provider=SilentReminder())
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        self.assertEqual(state.current_step, TaskStatus.AWAITING_APPROVAL)

        out = agent.approve(state, approved=True)
        self.assertTrue(out.pending_action["result"]["ok"])
        self.assertEqual(out.current_step, TaskStatus.COMPLETED)

    def test_approve_false_does_not_execute(self):
        agent = make_agent(reminder_provider=SilentReminder())
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        out = agent.approve(state, approved=False)
        self.assertNotIn("result", out.pending_action)
        self.assertIs(out.pending_action["approved"], False)

    def test_approve_is_idempotent(self):
        """副作用的幂等必须在这一层保证，不能指望 provider 去重。"""
        agent = make_agent(reminder_provider=SilentReminder())
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        agent.approve(state, approved=True)
        out = agent.approve(state, approved=True)
        self.assertTrue(any("已经创建过" in w for w in out.warnings))

    def test_approve_with_corrupt_pending_reports_failure(self):
        """还原不出来就如实报错，绝不静默跳过。"""
        agent = make_agent(reminder_provider=SilentReminder())
        state = TravelState(intents=["flight"],
                            pending_action={"type": "create_reminder",
                                            "preview": "x", "request": {}})
        out = agent.approve(state, approved=True)
        self.assertTrue(out.errors)
        self.assertNotIn("result", out.pending_action)


# ======================================================================
# Session：整段对话
# ======================================================================
class TestSessionMultiTurn(unittest.TestCase):

    def test_ask_then_answer_completes(self):
        """本文件存在的理由。第一句触发追问，第二句必须把任务推完。"""
        s = Session(make_agent(), today=TODAY)
        first = s.send("我想买张机票")
        self.assertEqual(first.state.current_step, TaskStatus.AWAITING_INPUT)

        second = s.send("上海到成都，8月28号")
        self.assertTrue(second.resumed)
        self.assertEqual(second.state.current_step, TaskStatus.COMPLETED)
        self.assertTrue(second.state.flight_result["ok"])

    def test_ambiguous_reply_asks_again(self):
        """出发地和目的地都缺、只回了一个地名 → 问，不猜。"""
        s = Session(make_agent(), today=TODAY)
        s.send("我想买张机票")
        r = s.send("上海")
        self.assertEqual(r.kind, "clarify")
        self.assertEqual(r.state.current_step, TaskStatus.AWAITING_INPUT)
        self.assertIsNone(r.state.origin)

    def test_explicit_mention_beats_carry_over(self):
        """试跑抓到的真实事故：用户说「不对，是从北京飞」，
        沿用逻辑把上一轮的「上海」填了回去，把北京静默丢掉。"""
        s = Session(make_agent(), today=TODAY)
        s.send("8月28号从上海飞成都，机票什么时候买划算")
        r = s.send("不对，是从北京飞")
        self.assertEqual(r.state.origin, "北京")
        self.assertEqual(r.state.destination, "成都")
        self.assertNotIn("origin", r.state.carried_over)

    def test_carry_over_is_visible_in_reply(self):
        """沿用了什么，必须在回复里说出来。"""
        s = Session(make_agent(), today=TODAY)
        s.send("8月28号从上海飞成都，机票什么时候买划算")
        r = s.send("不对，是从北京飞")
        self.assertIn("沿用上一轮", r.reply)
        self.assertIn("成都", r.reply)

    def test_approval_three_states(self):
        """确认 / 取消 / 听不懂 —— 听不懂不折叠成任何一边。"""
        s = Session(make_agent(reminder_provider=SilentReminder()),
                    today=TODAY, want_reminder=True)
        r = s.send("8月28号从北京飞广州，帮我设个购票提醒")
        self.assertEqual(r.state.current_step, TaskStatus.AWAITING_APPROVAL)

        r = s.send("啊这")
        self.assertEqual(r.kind, "clarify")
        self.assertEqual(r.state.current_step, TaskStatus.AWAITING_APPROVAL)
        self.assertNotIn("result", r.state.pending_action)

        r = s.send("确认")
        self.assertTrue(r.state.pending_action["result"]["ok"])

    def test_repeat_confirm_after_done_is_not_out_of_scope(self):
        """任务收尾后又说「确认」，回一句「不属于机票范围」比不回答更让人困惑。"""
        s = Session(make_agent(reminder_provider=SilentReminder()),
                    today=TODAY, want_reminder=True)
        s.send("8月28号从北京飞广州，帮我设个购票提醒")
        s.send("确认")
        r = s.send("确认")
        self.assertEqual(r.kind, "approval")
        self.assertTrue(any("已经创建过" in w for w in r.state.warnings))

    def test_partial_delivery_then_fill(self):
        """缺出发地时目的地那节照常交付；补上之后机票那节补跑，
        且已交付的那节不重复。"""
        s = Session(make_agent(), today=TODAY)
        first = s.send("飞乌鲁木齐，新疆有什么玩的")
        self.assertTrue(first.state.destination_result["ok"])
        self.assertIn("origin", first.state.missing)

        second = s.send("从上海出发，8月28号")
        self.assertTrue(second.state.flight_result["ok"])
        self.assertTrue(second.state.destination_result["ok"])


class TestSessionPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_slot_fill_across_processes(self):
        """落盘 → 换一个全新的 agent 读回来 → 接着补参数。

        这条模拟的是「上午问了一半，下午重开终端接着聊」。
        只存内存的多轮，在 CLI 场景里等于没有多轮。
        """
        s = Session(make_agent(), session_id="t1", today=TODAY)
        s.send("我想买张机票")
        self.assertIsNotNone(self.store.save(s))

        revived = self.store.load("t1", make_agent())
        self.assertIsNotNone(revived)
        r = revived.send("上海到成都，8月28号")
        self.assertEqual(r.state.current_step, TaskStatus.COMPLETED)

    def test_approve_across_processes(self):
        """确认往往发生在另一个进程里，内存里的 ReminderRequest 早没了，
        必须能从纯 JSON 还原。"""
        s = Session(make_agent(reminder_provider=SilentReminder()),
                    session_id="t2", today=TODAY, want_reminder=True)
        s.send("8月28号从北京飞广州，帮我设个购票提醒")
        self.store.save(s)

        revived = self.store.load("t2", make_agent(reminder_provider=SilentReminder()))
        self.assertEqual(revived.state.current_step, TaskStatus.AWAITING_APPROVAL)
        r = revived.send("确认")
        self.assertTrue(r.state.pending_action["result"]["ok"])

    def test_history_survives_round_trip(self):
        s = Session(make_agent(), session_id="t3", today=TODAY)
        s.send("我想买张机票")
        self.store.save(s)
        revived = self.store.load("t3", make_agent())
        self.assertEqual(revived.turn_count, 1)
        self.assertIn("我想买张机票", replay(revived))

    def test_unavailable_store_reports_itself(self):
        """目录建不出来时明确表示不可用，而不是假装存上了。

        用一个**已存在的文件**当目录来触发：mkdir 必然失败。
        这比 mock 掉 mkdir 更接近真实（磁盘满、只读挂载、路径被占）。
        """
        blocker = Path(self.tmp.name) / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")

        store = SessionStore(blocker)
        self.assertFalse(store.available())
        self.assertIsNone(store.path_for("any"))
        self.assertIsNone(store.save(Session(make_agent(), today=TODAY)))
        self.assertEqual(store.list_ids(), [])

    def test_corrupt_file_returns_none(self):
        path = self.store.path_for("bad")
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertIsNone(self.store.load("bad", make_agent()))

    def test_resume_or_new_creates_when_absent(self):
        s = self.store.resume_or_new("never-seen", make_agent(), today=TODAY)
        self.assertEqual(s.session_id, "never-seen")
        self.assertIsNone(s.state)


if __name__ == "__main__":
    unittest.main()


# ======================================================================
# CLI 接线
# ======================================================================
class TestSessionCLI(unittest.TestCase):
    """多轮如果只有测试能用，等于没做。这里验证它确实接到了命令行上。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("TRAVELWISE_DATA_DIR")
        os.environ["TRAVELWISE_DATA_DIR"] = self.tmp.name
        os.environ["TRAVELWISE_FLIGHT_PROVIDER"] = "mock"
        os.environ["TRAVELWISE_MATRIX_DAYS"] = "0"
        # paths 模块在 import 时就解析过目录了，必须重载才能吃到新的环境变量
        importlib.reload(paths)
        importlib.reload(session_mod)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TRAVELWISE_DATA_DIR", None)
        else:
            os.environ["TRAVELWISE_DATA_DIR"] = self._old
        importlib.reload(paths)
        importlib.reload(session_mod)
        self.tmp.cleanup()

    def _run(self, *argv) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()

    def test_two_invocations_continue_one_task(self):
        """两次独立的 main() 调用 —— 模拟两次敲命令。"""
        first = self._run("--session", "cli-t1", "--today", "2026-08-14",
                          "我想买张机票")
        self.assertIn("还需要你补充", first)

        second = self._run("--session", "cli-t1", "--today", "2026-08-14",
                           "上海到成都，8月28号")
        self.assertIn("购票时机", second)
        self.assertNotIn("不属于机票或目的地范围", second)

    def test_replay_shows_both_turns(self):
        self._run("--session", "cli-t2", "--today", "2026-08-14", "我想买张机票")
        self._run("--session", "cli-t2", "--today", "2026-08-14",
                  "上海到成都，8月28号")
        out = self._run("--replay", "cli-t2")
        self.assertIn("我想买张机票", out)
        self.assertIn("slot_fill", out)

    def test_replay_missing_session_reports_clearly(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--replay", "no-such-session"])
        self.assertEqual(code, 1)
        self.assertIn("找不到会话", buf.getvalue())
