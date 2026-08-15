# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""自动化测试。用标准库 unittest，零第三方依赖：

    python -m unittest discover -s tests -v

覆盖的是本项目最容易出错、也最该被回归保护的几件事：
路由准确性、范围不被擅自扩大、工具失败不得伪造成功、副作用必须经确认。
"""

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from travelwise.orchestrator import TravelWiseAgent            # noqa: E402
from travelwise.providers.base import ProviderError            # noqa: E402
from travelwise.providers.mock_flight import (                 # noqa: E402
    FailingFlightProvider, MockFlightProvider)
from travelwise.providers.reminders import (                   # noqa: E402
    ConsoleReminderProvider, ICSReminderProvider)
from travelwise.router import decide_scope, extract_date, route  # noqa: E402
from travelwise.state import TaskStatus  # noqa: E402
from travelwise.tools import price_analysis                    # noqa: E402
from travelwise.tools.season import check_match, normalize_season  # noqa: E402

TODAY = date(2026, 8, 5)


class TestIntentRouting(unittest.TestCase):
    """Intent Routing Eval —— 路由到正确的技能。"""

    def test_flight_only(self):
        self.assertEqual(route("8月28号从上海去北京的机票", TODAY).intents, ["flight"])

    def test_destination_only(self):
        self.assertEqual(route("成都秋天有什么地方适合玩", TODAY).intents, ["destination"])

    def test_both_intents(self):
        s = route("8月28号去成都，顺便看看四川有什么玩的", TODAY)
        self.assertIn("flight", s.intents)
        self.assertIn("destination", s.intents)

    def test_strong_travel_signal_implies_both(self):
        """「去 + 地名」即使没说机票/景点，也应兼顾两件事。"""
        s = route("我下周要去新疆", TODAY)
        self.assertEqual(sorted(s.intents), ["destination", "flight"])

    def test_out_of_scope(self):
        self.assertEqual(route("帮我订个酒店", TODAY).intents, [])


class TestParameterExtraction(unittest.TestCase):
    """Parameter Extraction Eval。"""

    def test_route_extraction(self):
        s = route("8月28号从上海飞成都", TODAY)
        self.assertEqual(s.origin, "上海")
        self.assertEqual(s.destination, "成都")
        self.assertEqual(s.travel_date, "2026-08-28")

    def test_relative_date(self):
        self.assertEqual(extract_date("明天出发", TODAY), "2026-08-06")
        self.assertEqual(extract_date("月底走", TODAY), "2026-08-31")

    def test_no_date_is_not_guessed(self):
        """抽不到日期就该是 None，不许瞎猜一个。"""
        self.assertIsNone(extract_date("我想买张机票", TODAY))

    def test_missing_params_are_reported(self):
        s = route("我想买张机票", TODAY)
        self.assertEqual(sorted(s.missing), ["destination", "origin", "travel_date"])


class TestScopeControl(unittest.TestCase):
    """Scope Control Eval —— 最关键的边界：绝不擅自扩大范围。"""

    def test_city_stays_city(self):
        self.assertEqual(route("沈阳有什么玩的", TODAY).scope, "city")

    def test_province_detected(self):
        self.assertEqual(route("新疆有什么玩的", TODAY).scope, "province")

    def test_landing_city_differs_from_play_scope(self):
        """飞乌鲁木齐、玩新疆：落地城市 ≠ 玩乐范围。"""
        s = route("8月28号飞乌鲁木齐，新疆有什么玩的", TODAY)
        self.assertEqual(s.destination, "乌鲁木齐")
        self.assertEqual(s.place, "新疆")
        self.assertEqual(s.scope, "province")

    def test_sparse_city_never_auto_expands(self):
        """沈阳结果少，也不许自动变成辽宁省。"""
        agent = TravelWiseAgent(MockFlightProvider(today=TODAY), today=TODAY)
        state = agent.handle("沈阳有什么好玩的")
        self.assertEqual(state.destination_result["scope"], "city")
        self.assertEqual(state.destination_result["place"], "沈阳")
        self.assertNotIn("辽宁", state.destination_result["text"])

    def test_decide_scope_direct(self):
        self.assertEqual(decide_scope("大连", "大连有什么玩的"), "city")
        self.assertEqual(decide_scope("云南", "云南有什么玩的"), "province")


class TestToolFailure(unittest.TestCase):
    """Tool Failure Eval —— 失败必须如实报告，不得编造、不得假称成功。"""

    def _run_failure(self, mode):
        agent = TravelWiseAgent(FailingFlightProvider(mode), today=TODAY)
        return agent.handle("8月28号从上海飞成都的机票")

    def test_timeout_reports_failure(self):
        state = self._run_failure("timeout")
        self.assertFalse(state.flight_result["ok"])
        self.assertIn("超时", state.flight_result["error"])
        self.assertTrue(state.errors)

    def test_http_500_reports_failure(self):
        state = self._run_failure("http_500")
        self.assertFalse(state.flight_result["ok"])
        self.assertIn("500", state.flight_result["error"])

    def test_malformed_reports_failure(self):
        state = self._run_failure("malformed")
        self.assertFalse(state.flight_result["ok"])

    def test_failure_fabricates_no_flights(self):
        """失败时绝不能凭空造出航班。"""
        state = self._run_failure("timeout")
        self.assertEqual(state.flight_result["flights"], [])
        self.assertIsNone(state.flight_result["analysis"])

    def test_empty_result_is_not_an_error(self):
        """连上了但当天没航班 —— 这是空结果，不是失败。"""
        state = self._run_failure("empty")
        self.assertTrue(state.flight_result["ok"])
        self.assertEqual(state.flight_result["flights"], [])

    def test_one_skill_failure_does_not_block_other(self):
        """机票挂了，目的地清单照样交付。"""
        agent = TravelWiseAgent(FailingFlightProvider("timeout"), today=TODAY)
        state = agent.handle("8月28号从上海飞成都，成都有什么好玩的")
        self.assertFalse(state.flight_result["ok"])
        self.assertTrue(state.destination_result["ok"])
        self.assertIn("成都", state.destination_result["text"])


class TestHumanInTheLoop(unittest.TestCase):
    """HITL Eval —— 副作用操作必须先预览再确认。"""

    def test_no_callback_means_no_execution(self):
        """没有审批回调 = 绝不自动执行。"""
        agent = TravelWiseAgent(MockFlightProvider(today=TODAY), today=TODAY)
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        self.assertIsNotNone(state.pending_action)
        self.assertNotIn("result", state.pending_action)

    def test_explicit_request_still_requires_confirmation(self):
        """即使用户说"直接帮我设"，仍要先出预览。"""
        seen = {}

        def approve(preview):
            seen["preview"] = preview
            return True

        agent = TravelWiseAgent(MockFlightProvider(today=TODAY),
                                approval_callback=approve, today=TODAY)
        state = agent.handle("8月28号从北京飞广州，看到合适的直接帮我设置提醒",
                             want_reminder=True)
        self.assertIn("preview", seen)
        self.assertIn("确认", seen["preview"])
        self.assertTrue(state.pending_action["approved"])

    def test_rejection_prevents_execution(self):
        agent = TravelWiseAgent(MockFlightProvider(today=TODAY),
                                approval_callback=lambda _p: False, today=TODAY)
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        self.assertFalse(state.pending_action["approved"])
        self.assertNotIn("result", state.pending_action)

    def test_ics_reminder_written(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            agent = TravelWiseAgent(MockFlightProvider(today=TODAY),
                                    reminder_provider=ICSReminderProvider(tmp),
                                    approval_callback=lambda _p: True, today=TODAY)
            state = agent.handle("8月28号从北京飞广州", want_reminder=True)
            result = state.pending_action["result"]
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["location"]).exists())
            content = Path(result["location"]).read_text(encoding="utf-8")
            self.assertIn("BEGIN:VCALENDAR", content)


class TestPriceAnalysis(unittest.TestCase):
    """价格分析算法的确定性行为。"""

    def _fetcher(self, trough, weekend_premium=0):
        def f(_o, _d, day):
            d = date.fromisoformat(day)
            adv = (d - TODAY).days
            price = 1000 + abs(adv - trough) * 60
            if weekend_premium and d.weekday() >= 5:
                price += weekend_premium
            return [{"price": price, "transfer_num": 1}]
        return f

    def test_finds_trough_and_recommends_buy_date(self):
        """干净曲线（无星期几噪声）下应精确命中谷底。

        这里的 fetcher 只给单班航班、无面板数据，所以口径退回「当日最低价」，
        raw 的含义与改造前一致。
        """
        res = price_analysis.analyze("A", "B", "2026-08-28", self._fetcher(4), today=TODAY)
        self.assertTrue(res["ok"])
        self.assertEqual(res["raw"]["advance_days"], 4)
        self.assertEqual(res["raw"]["recommended_buy_date"], "2026-08-24")

    def test_weekday_alignment_needs_two_samples(self):
        """7 天窗口里同星期几只有 1 个样本——「在 1 个样本里取最小值」不是对齐。

        以前这里会输出一个标着「更稳」的结论，而它根本没有第二个点可选；
        上海→昆明那次两个方法给出同一个答案，不是互相印证，是同义反复。
        """
        res = price_analysis.analyze("A", "B", "2026-08-28",
                                     self._fetcher(4, weekend_premium=200), today=TODAY)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["weekday_aligned"])
        self.assertTrue(any("同星期几对齐本次不可用" in w for w in res["warnings"]))

    def test_weekday_alignment_works_with_a_long_enough_window(self):
        """窗口拉到 14 天就有 2 个同星期几样本，这时对齐法才成立。"""
        res = price_analysis.analyze("A", "B", "2026-09-11",
                                     self._fetcher(4, weekend_premium=200), today=TODAY,
                                     min_window=14, max_window=14, validate_after=99)
        self.assertIsNotNone(res["weekday_aligned"])
        self.assertGreaterEqual(res["weekday_aligned"]["sample_size"], 2)
        aligned = date.fromisoformat(res["weekday_aligned"]["cheapest_scan_date"])
        self.assertEqual(aligned.weekday(), date(2026, 9, 11).weekday())

    def test_ties_are_reported_and_earliest_is_not_taken_silently(self):
        """并列最低不能静默取最早那天——实测的 ¥580 在 4 天里并列。"""
        def fetcher(o, d, day):
            adv = (date.fromisoformat(day) - TODAY).days
            price = 580 if adv in (3, 5, 6, 7) else 900
            return [{"price": price, "transfer_num": 1}]

        res = price_analysis.analyze("A", "B", "2026-08-31", fetcher, today=TODAY,
                                     min_window=7, max_window=7, validate_after=99)
        self.assertEqual(res["raw"]["tie_advances"], [3, 5, 6, 7])
        self.assertEqual(res["raw"]["advance_days"], 7)      # 并列取提前量最大的
        self.assertTrue(any("并列最低" in w for w in res["warnings"]))

    def test_consensus_overrides_the_curve_minimum(self):
        """逐航班共识优先于曲线最低点：后者可以被一班特价定住。"""
        # 复刻上海→昆明实测：曲线最低点在提前 3 天（¥580，来自中转特价），
        # 而 25/34 班各自的最低点落在提前 6 天。
        scan = [{"date": TODAY + timedelta(days=a), "advance": a,
                 "weekday": (TODAY + timedelta(days=a)).weekday(),
                 "min_price": 580 if a == 3 else 800} for a in range(1, 8)]
        consensus_day = (TODAY + timedelta(days=6)).isoformat()
        advice = {"ok": True, "consensus_day": consensus_day, "agree": 25, "total": 34,
                  "median_saving": 22.0, "median_price_on_consensus": 788}
        res = price_analysis.analyze_from_scan(
            scan, TODAY, "2026-08-31",
            consensus=price_analysis.consensus_from_advice(advice, TODAY, "2026-08-31"))
        self.assertEqual(res["primary_method"], "逐航班共识")
        self.assertEqual(res["primary"]["advance_days"], 6)
        self.assertEqual(res["primary"]["recommended_buy_date"], "2026-08-25")   # 8/31 − 6
        self.assertEqual(res["raw"]["advance_days"], 3)       # 曲线最低点仍如实保留
        report = price_analysis.render_report({**res, "route": "上海→昆明", "_scan": scan})
        self.assertIn("建议购票日：**2026-08-25**", report)
        self.assertLess(report.index("建议购票日"), report.index("当日最低价法"))

    def test_weak_consensus_is_not_used(self):
        advice = {"ok": True, "consensus_day": "2026-08-20", "agree": 5, "total": 34,
                  "median_saving": 3.0}
        self.assertIsNone(price_analysis.consensus_from_advice(
            advice, TODAY, "2026-08-31"))

    def test_early_stop_saves_quota(self):
        """谷底确认后应提前停止，不扫满上限。"""
        res = price_analysis.analyze("A", "B", "2026-08-28", self._fetcher(4), today=TODAY)
        self.assertTrue(res["_meta"]["stopped_early"])
        self.assertLess(res["_meta"]["api_calls"], 14)

    def test_passed_window_warns(self):
        """最佳提前量 > 距出行天数 → 应警告窗口已过。"""
        res = price_analysis.analyze("A", "B", "2026-08-08", self._fetcher(5), today=TODAY)
        self.assertTrue(any("窗口已过" in w for w in res["warnings"]))

    def test_empty_scan_is_honest(self):
        res = price_analysis.analyze_from_scan([], TODAY, "2026-08-28")
        self.assertFalse(res["ok"])

    def test_schedule_only_when_no_price(self):
        """数据源不含票价 → 降级列时刻表，且明确说明为什么没有价格建议。"""
        agent = TravelWiseAgent(
            MockFlightProvider(today=TODAY, supports_price=False), today=TODAY)
        state = agent.handle("8月28号从上海飞成都的航班")
        self.assertEqual(state.flight_result["mode"], "schedule_only")
        self.assertIn("不含票价", state.flight_result["text"])


class TestSeason(unittest.TestCase):
    """季节判断 —— 空字段不得冒充「全年」。"""

    def test_empty_season_is_unknown_not_all_year(self):
        self.assertEqual(normalize_season(""), set())
        r = check_match("", 12)
        self.assertTrue(r["unknown"])

    def test_mismatch_warns(self):
        r = check_match("夏季", 12)
        self.assertFalse(r["match"])
        self.assertIn("⚠️", r["note"])

    def test_month_range(self):
        self.assertEqual(normalize_season("4月~10月"), {"春", "夏", "秋"})


class TestProviderSwap(unittest.TestCase):
    """平台无关性：换 Provider 不改业务代码。"""

    def test_mock_is_deterministic(self):
        p1 = MockFlightProvider(today=TODAY)
        p2 = MockFlightProvider(today=TODAY)
        self.assertEqual([f.to_dict() for f in p1.search_flights("上海", "成都", "2026-08-20")],
                         [f.to_dict() for f in p2.search_flights("上海", "成都", "2026-08-20")])

    def test_http_provider_without_endpoint_errors_clearly(self):
        from travelwise.providers.http_flight import HttpFlightProvider
        with self.assertRaises(ProviderError):
            HttpFlightProvider({}).search_flights("上海", "成都", "2026-08-20")

    def test_console_reminder_always_available(self):
        self.assertTrue(ConsoleReminderProvider().available())


class TestNoFakeSuccess(unittest.TestCase):
    """MCP 返回无法判定时必须判失败 —— 对应「禁止假成功」原则。

    这是交接文档点名的缺陷：原实现 ok 默认 True，遇到没见过的返回结构
    就会向用户宣称提醒已创建，而实际可能什么都没发生。
    """

    def _make(self, response):
        from travelwise.providers.reminders import McpReminderProvider
        return McpReminderProvider(lambda _n, _a: response)

    def _req(self):
        from travelwise.providers.base import ReminderRequest
        return ReminderRequest(title="t", remind_at=datetime(2026, 8, 19, 9, 0))

    def test_unknown_structure_is_failure(self):
        self.assertFalse(self._make({"something": "unknown"}).create(self._req()).ok)

    def test_none_response_is_failure(self):
        self.assertFalse(self._make(None).create(self._req()).ok)

    def test_non_dict_response_is_failure(self):
        self.assertFalse(self._make("done").create(self._req()).ok)

    def test_explicit_success_is_success(self):
        self.assertTrue(self._make({"ok": True}).create(self._req()).ok)

    def test_is_error_flag_is_failure(self):
        self.assertFalse(self._make({"isError": True}).create(self._req()).ok)

    def test_mcp_content_without_error_is_success(self):
        self.assertTrue(self._make({"content": []}).create(self._req()).ok)

    def test_tool_exception_is_failure(self):
        from travelwise.providers.reminders import McpReminderProvider

        def boom(_n, _a):
            raise RuntimeError("connection refused")

        self.assertFalse(McpReminderProvider(boom).create(self._req()).ok)

    def test_custom_predicate_respected(self):
        from travelwise.providers.reminders import McpReminderProvider
        p = McpReminderProvider(lambda _n, _a: {"code": 0},
                                success_predicate=lambda r: r.get("code") == 0)
        self.assertTrue(p.create(self._req()).ok)


class TestTaskStatus(unittest.TestCase):
    """任务终态语义 —— done 不能同时表示"办完了"和"还等着用户"。"""

    def test_completed(self):
        agent = TravelWiseAgent(MockFlightProvider(today=TODAY), today=TODAY)
        state = agent.handle("沈阳有什么好玩的")
        self.assertEqual(state.current_step, TaskStatus.COMPLETED)
        self.assertTrue(state.is_terminal())

    def test_awaiting_approval_is_not_terminal(self):
        agent = TravelWiseAgent(MockFlightProvider(today=TODAY), today=TODAY)
        state = agent.handle("8月28号从北京飞广州", want_reminder=True)
        self.assertEqual(state.current_step, TaskStatus.AWAITING_APPROVAL)
        self.assertFalse(state.is_terminal())
        self.assertTrue(state.awaits_user())

    def test_partial_complete_when_params_missing(self):
        """缺出发地但目的地清单已交付 → 部分完成，不是 completed。"""
        agent = TravelWiseAgent(MockFlightProvider(today=TODAY), today=TODAY)
        state = agent.handle("8月28号飞乌鲁木齐，新疆有什么玩的")
        self.assertEqual(state.current_step, TaskStatus.PARTIAL_COMPLETE)

    def test_failed_when_nothing_produced(self):
        agent = TravelWiseAgent(FailingFlightProvider("timeout"), today=TODAY)
        state = agent.handle("8月28号从上海飞成都的机票")
        self.assertEqual(state.current_step, TaskStatus.FAILED)

    def test_out_of_scope(self):
        agent = TravelWiseAgent(MockFlightProvider(today=TODAY), today=TODAY)
        state = agent.handle("帮我订个酒店")
        self.assertEqual(state.current_step, TaskStatus.OUT_OF_SCOPE)


class TestCacheDir(unittest.TestCase):
    """缓存目录不可用时必须返回 None，而不是一个写不进去的 Path。"""

    def test_unavailable_returns_none(self):
        """跨平台构造一个必然写不进去的路径。

        原实现硬编码 "/proc/..."，那是 Linux 特有的只读虚拟文件系统；
        Windows 上它会被解析成 C:\\proc\\... 并且 mkdir **成功**，
        于是这条用例在 Windows 上必挂 —— 挂的是测试本身，不是被测代码。

        改用「拿一个文件当父目录」：在任何操作系统上，
        往文件底下建子目录都会抛 NotADirectoryError（OSError 的子类）。
        """
        import tempfile

        import travelwise.paths as paths
        original = paths.CACHE_DIR
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "not-a-directory.txt"
            blocker.write_text("x", encoding="utf-8")
            try:
                paths.CACHE_DIR = blocker / "cache"
                self.assertIsNone(paths.ensure_cache_dir("x"))
            finally:
                paths.CACHE_DIR = original

    def test_available_returns_path(self):
        import tempfile
        import travelwise.paths as paths
        original = paths.CACHE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths.CACHE_DIR = Path(tmp)
                got = paths.ensure_cache_dir("sub")
                self.assertIsNotNone(got)
                self.assertTrue(got.exists())
        finally:
            paths.CACHE_DIR = original


class TestICS(unittest.TestCase):
    """ICS 必须带时区，且 UID 唯一。"""

    def _create(self, tmp):
        from travelwise.providers.base import ReminderRequest
        from travelwise.providers.reminders import ICSReminderProvider
        req = ReminderRequest(title="购票", remind_at=datetime(2026, 8, 19, 9, 0))
        return ICSReminderProvider(tmp).create(req)

    def test_has_timezone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(self._create(tmp).location).read_text(encoding="utf-8")
            self.assertIn("DTSTART;TZID=", content)
            self.assertIn("DTSTAMP:", content)

    def test_uid_unique_across_same_timestamp(self):
        """同一时刻创建两条，UID 必须不同，否则日历会覆盖。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(self._create(tmp).location).read_text(encoding="utf-8")
            b = Path(self._create(tmp).location).read_text(encoding="utf-8")
            uid_a = [x for x in a.splitlines() if x.startswith("UID:")][0]
            uid_b = [x for x in b.splitlines() if x.startswith("UID:")][0]
            self.assertNotEqual(uid_a, uid_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
