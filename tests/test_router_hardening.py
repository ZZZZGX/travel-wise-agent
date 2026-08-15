# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""路由加固的回归测试。

这个文件盯的是两类**静默错误**——不报错、不抛异常，就是答得不对：

  1. 槽位里塞进了用户没说过的值（「帮我看看这两天」当成出发城市）
  2. 日期算错了整整两天（「下周五」解析成周三）

两者的共同点是**它们看起来一切正常**。系统照常往下跑、照常给出一份
像模像样的购票建议，只是那份建议对应的是另一个城市、另一个日期。
比起抛异常，这种错更难被发现，也更难被信任回来。
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("TRAVELWISE_LLM_PROVIDER", "scripted")

from travelwise import slots                                   # noqa: E402
from travelwise.router import (                                # noqa: E402
    extract_date, looks_like_place, narrow_to_city, route)
from travelwise.tools import city_codes                        # noqa: E402

WED = date(2026, 8, 5)          # 周三，与 evals/run_evals.py 的基准日一致


class TestNarrowToCity(unittest.TestCase):
    """正则抓到的边界经常带渣。先尽力救，救不回来才判缺失。"""

    def test_recognized_city_passes_through(self):
        self.assertEqual(narrow_to_city("上海"), "上海")
        self.assertEqual(narrow_to_city("上海市"), "上海市")

    def test_trailing_noise_is_trimmed(self):
        self.assertEqual(narrow_to_city("成都的机票"), "成都")
        self.assertEqual(narrow_to_city("成都的票价走势"), "成都")

    def test_leading_noise_is_trimmed(self):
        """最长可识别**子串**，不只是前缀——渣可能长在前面。"""
        self.assertEqual(narrow_to_city("趟成都"), "成都")

    def test_garbage_becomes_none(self):
        """判为没抽到，让上层去问。拿它去查航班就是编造。"""
        for junk in ("帮我看看这两天", "我打算", "想问下", "什么时候"):
            self.assertIsNone(narrow_to_city(junk), junk)

    def test_unknown_but_clean_short_name_is_kept(self):
        """159 行的名录本来就不全。生僻地名不该因为查不到就被毙掉。"""
        self.assertIsNone(city_codes.try_resolve("都江堰"),
                          "这条测试假设都江堰不在城市码表里；名录扩了就换一个名字")
        self.assertEqual(narrow_to_city("都江堰"), "都江堰")

    def test_long_unknown_string_is_rejected(self):
        """收得太松，「帮我看看这两天」就会变成出发地。长度是最后一道闸。"""
        self.assertIsNone(narrow_to_city("某个很长的不知所云的串"))

    def test_empty_input(self):
        self.assertIsNone(narrow_to_city(""))
        self.assertIsNone(narrow_to_city(None))


class TestRouteNeverFabricatesSlots(unittest.TestCase):
    """不变量：进了槽位的城市名，必须是**能当城市用**的东西。"""

    NOISY = [
        "帮我看看这两天飞成都的票价走势，我从上海出发，28 号走",
        "我打算月底去趟成都，从上海过去，票怎么买划算",
        "在吗？想问下 8月28号从上海飞成都的机票 谢谢！！！",
        "我朋友说八月底机票会跌，我8月28号从上海飞成都，是这样吗",
        "8月28号从上海飞成都的机票",
        "顺便看看，下周五从上海飞成都的票",
    ]

    def test_no_slot_is_a_non_place(self):
        for text in self.NOISY:
            state = route(text, today=WED)
            for field in ("origin", "destination"):
                value = getattr(state, field)
                if value is None:
                    continue
                self.assertTrue(
                    city_codes.try_resolve(value) or looks_like_place(value),
                    "%s 的 %s 抽成了 %r —— 这不是一个城市" % (text[:20], field, value))

    def test_garbage_input_asks_instead_of_guessing(self):
        """抽不出出发地时必须进 missing，而不是拿一坨渣往下跑。"""
        state = route("帮我看看这两天飞成都的票价走势，我从上海出发，28 号走", today=WED)
        self.assertIsNone(state.origin)
        self.assertIn("origin", state.missing)

    def test_predicate_is_shared_with_slots(self):
        """首轮路由和多轮补槽位必须用同一把尺子。

        它们曾经用两把：多轮会校验城市名，首轮不会，于是同一个字符串
        在第一句里能通过、在第二句里被拒——同一种输入，两条路两种结果。
        """
        self.assertIs(slots._looks_like_place, looks_like_place)


class TestWeekdayDates(unittest.TestCase):
    """「下周五」不是「今天 +7」。"""

    def _weekday(self, text: str) -> tuple[str, str]:
        got = extract_date(text, WED)
        return got, date.fromisoformat(got).strftime("%a") if got else ""

    def test_next_week_weekday(self):
        self.assertEqual(self._weekday("下周五"), ("2026-08-14", "Fri"))
        self.assertEqual(self._weekday("下周一"), ("2026-08-10", "Mon"))
        self.assertEqual(self._weekday("下周日"), ("2026-08-16", "Sun"))

    def test_week_after_next(self):
        """长键优先：不排序的话「下下周」会先被「下周」吃掉，差整整一周。"""
        self.assertEqual(self._weekday("下下周三"), ("2026-08-19", "Wed"))
        self.assertNotEqual(extract_date("下下周", WED), extract_date("下周", WED))

    def test_this_week(self):
        self.assertEqual(self._weekday("这周五"), ("2026-08-07", "Fri"))
        self.assertEqual(self._weekday("本周五"), ("2026-08-07", "Fri"))

    def test_bare_weekday_means_the_upcoming_one(self):
        """光说「周五」而本周五还没到 → 就是这个周五。"""
        self.assertEqual(self._weekday("周五"), ("2026-08-07", "Fri"))

    def test_bare_weekday_never_returns_the_past(self):
        """本周一已经过去了。返回一个过去的日期毫无用处，
        而且会一路错到出票期分析里（负天数）。"""
        got = extract_date("周一", WED)
        self.assertGreaterEqual(date.fromisoformat(got), WED)

    def test_all_weekday_forms_agree(self):
        """周 / 星期 / 礼拜 是同一件事，不该有一个说法解析不出来。"""
        for form in ("下周五", "下星期五", "下礼拜五"):
            self.assertEqual(extract_date(form, WED), "2026-08-14", form)

    def test_plain_relative_words_still_work(self):
        self.assertEqual(extract_date("明天", WED), "2026-08-06")
        self.assertEqual(extract_date("一周后", WED), "2026-08-12")
        self.assertEqual(extract_date("月底", WED), "2026-08-31")

    def test_absolute_date_still_wins(self):
        """句子里同时有绝对日期和相对说法时，绝对的优先。"""
        self.assertEqual(extract_date("下周五之前，8月28号从上海飞成都", WED),
                         "2026-08-28")

    def test_weekday_flows_through_route(self):
        state = route("下周五从上海飞成都", today=WED)
        self.assertEqual(state.travel_date, "2026-08-14")
        self.assertEqual(date.fromisoformat(state.travel_date).weekday(), 4)

    def test_no_weekday_is_ever_off_by_days(self):
        """扫一遍所有星期几，逐个验证落在正确的星期上。

        逐条写死日期只能证明那几条对；这条证明的是**规则本身**对。
        """
        for index, name in enumerate("一二三四五六日"):
            got = extract_date("下周%s" % name, WED)
            self.assertEqual(date.fromisoformat(got).weekday(), index, name)
            self.assertEqual(date.fromisoformat(got),
                             WED - timedelta(days=WED.weekday())
                             + timedelta(weeks=1, days=index))


if __name__ == "__main__":
    unittest.main()


# ======================================================================
# 难例组这台仪器本身
# ======================================================================
class TestHardTier(unittest.TestCase):
    """难例组是一台**故意不会满分**的仪器。这里测的是仪器，不是被测物。"""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
        import run_evals
        self.run_evals = run_evals

    def test_hard_failures_do_not_gate_the_build(self):
        """难例红着不许把构建拖红。

        闸门长期红着，最后一定会被人 disable 掉，
        连带那些真正该守的回归用例一起失效。
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.run_evals.main([])
        out = buf.getvalue()
        self.assertEqual(code, 0, out[-600:])
        self.assertIn("难例组", out)
        self.assertIn("不参与退出码", out)

    def test_hard_tier_still_discriminates(self):
        """如果难例全过了，这台仪器就该退休了 —— 换一批更难的。

        这条测试的意义是**防止评测集悄悄失去区分度**：
        Router 回归组停在 32/32 那么久都没人注意到，正是因为
        没有任何东西在盯着「这套用例还能不能区分东西」。
        """
        results = self.run_evals.eval_hard()
        failed = sum(1 for _g, r in results if not r.passed)
        self.assertGreater(
            failed, 0,
            "难例组已经全过了。这是好消息，但也意味着它不再能区分任何东西——"
            "该往 hard_cases.json 里加更难的用例了。")

    def test_every_case_says_why_it_is_hard(self):
        """没有 why 的难例，三个月后没人知道它在守什么，也就没人敢删。"""
        for group, case in self.run_evals.iter_hard_cases():
            self.assertTrue(case.get("why"), "%s/%s 缺 why" % (group, case["id"]))
            self.assertTrue(case.get("input"), case["id"])

    def test_case_ids_are_unique(self):
        ids = [c["id"] for _g, c in self.run_evals.iter_hard_cases()]
        self.assertEqual(len(ids), len(set(ids)), "难例 id 撞车了")

    def test_readme_block_is_not_treated_as_cases(self):
        groups = {g for g, _c in self.run_evals.iter_hard_cases()}
        self.assertNotIn("_readme", groups)

    def test_judge_catches_each_assertion_kind(self):
        """判定器每种断言都要能真的判红——写宽了的断言等于没写。"""
        from travelwise.state import TaskStatus, TravelState
        judge = self.run_evals.judge_hard

        ok_state = TravelState(intents=["flight"], origin="上海",
                               destination="成都", travel_date="2026-08-28")
        self.assertEqual(judge({"expect_intents": ["flight"]}, ok_state), [])

        self.assertTrue(judge({"expect_intents": ["destination"]}, ok_state))
        self.assertTrue(judge({"expect": {"origin": "北京"}}, ok_state))
        self.assertTrue(judge({"expect_missing": ["origin"]}, ok_state))
        self.assertTrue(judge({"expect_notice": ["酒店"]}, ok_state))
        self.assertTrue(judge({"expect_out_of_scope": True}, ok_state))

        decided = TravelState(intents=["destination"], place="朝阳")
        self.assertTrue(judge({"expect_clarify": True}, decided),
                        "默默替用户选了一个「朝阳」，应当判红")

        asked = TravelState(intents=["destination"], missing=["place"])
        self.assertEqual(judge({"expect_clarify": True}, asked), [])

        oos = TravelState(current_step=TaskStatus.OUT_OF_SCOPE)
        self.assertEqual(judge({"expect_out_of_scope": True}, oos), [])


# ======================================================================
# 方法声明
# ======================================================================
class TestMethodDisclosure(unittest.TestCase):
    """「提前 6 天最便宜」这句结论，必须和它的成立前提贴在一起。

    分析用的是**今天这一时点、不同出发日之间的横向对比**，不是用户那班航班
    的价格历史——接口只给当前报价，回溯不了。这个替代写在模块 docstring 里，
    但用户不读 docstring，用户读那句结论。不摆在旁边，等于没说。
    """

    def _analysis_text(self) -> str:
        from datetime import date as D
        from travelwise.providers.mock_flight import MockFlightProvider
        from travelwise.skills.flight import FlightSkill
        today = D(2026, 8, 14)
        res = FlightSkill(MockFlightProvider(today=today)).run(
            "上海", "成都", "2026-08-28", today=today, matrix_days=0)
        return res["text"]

    def test_disclosure_appears_next_to_the_conclusion(self):
        text = self._analysis_text()
        self.assertIn("建议购票日", text, "前提是这份输出里确实有结论")
        self.assertIn("横向对比", text)
        self.assertIn("不是你那班航班的历史价格曲线", text)

    def test_disclosure_names_the_condition_that_breaks_it(self):
        """只说「这是近似」没用，得说清什么时候会失效。"""
        from travelwise.tools.price_analysis import METHOD_DISCLOSURE
        self.assertIn("节假日", METHOD_DISCLOSURE)
        self.assertIn("相邻出发日之间大致稳定", METHOD_DISCLOSURE)
