# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""价格矩阵的回归测试。

测的不是"能不能画出表格"，而是**这张表在什么条件下会撒谎**：
  - 查询失败的那天有没有被当成"没有航班"；
  - 航班号不稳时会不会假装矩阵仍然有效；
  - 矩阵和提前量分析是不是共用同一次扫描（额度只花一份）；
  - 600 个价格数字有没有被塞进模型上下文。
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travelwise.providers.base import Flight, ProviderError          # noqa: E402
from travelwise.providers.mock_flight import MockFlightProvider      # noqa: E402
from travelwise.skills.flight import FlightSkill                     # noqa: E402
from travelwise.tools import price_matrix, table_refs                # noqa: E402
from travelwise.tools.registry import build_registry                 # noqa: E402
from travelwise.skills.destination import DestinationSkill           # noqa: E402

TODAY = date(2026, 8, 13)


def _flight(no, price, day, airline="东方航空", dep="08:00"):
    return Flight(flight_no=no, airline=airline, departure_date=day,
                  departure_time=dep, arrival_time="11:00", transfer_num=1, price=price)


class TestMatrixShape(unittest.TestCase):
    def test_rows_are_flights_columns_are_dates(self):
        def fetcher(o, d, day):
            return [_flight("MU5101", 800, day),
                    _flight("CA1501", 950, day, "国航", dep="14:00")]

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=5)
        self.assertEqual(len(m.columns), 5)
        self.assertEqual(len(m.rows), 2)
        for row in m.rows:
            self.assertEqual(row.observed, 5)      # 每个航班每天都有格子

    def test_first_column_is_tomorrow_not_today(self):
        m = price_matrix.build_matrix("上海", "成都", TODAY,
                                      lambda o, d, day: [_flight("MU1", 700, day)], days=3)
        self.assertEqual(m.columns[0].day, TODAY + timedelta(days=1))


class TestHonestGaps(unittest.TestCase):
    """空格有两种含义，绝不能混。"""

    def test_failed_day_is_not_reported_as_no_flight(self):
        def fetcher(o, d, day):
            if day.endswith("16"):
                raise ProviderError("HTTP 429 频率超限")
            return [_flight("MU5101", 800, day)]

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=5)
        failed = [c for c in m.columns if c.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("429", failed[0].error)
        # 失败列不计入"有效天数"，否则覆盖率会被稀释成假象
        self.assertEqual(len(m.valid_columns), 4)
        text = price_matrix.render_matrix(m)
        self.assertIn("×", text)
        self.assertIn("数据缺口", text)

    def test_empty_day_is_not_an_error(self):
        def fetcher(o, d, day):
            return [] if day.endswith("16") else [_flight("MU5101", 800, day)]

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=5)
        self.assertEqual(m.failed_days, [])
        self.assertTrue(any(c.status == "no_flight" for c in m.columns))


class TestUnstableFlightNumbers(unittest.TestCase):
    def test_diagonal_matrix_is_flagged_not_silently_rendered(self):
        """数据源每天给不同航班号 → 矩阵退化成对角线，必须明说而不是照画。"""
        def fetcher(o, d, day):
            return [_flight("MU" + day.replace("-", ""), 800, day)]

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=6)
        joined = " ".join(m.warnings)
        self.assertIn("对角线", joined)

    def test_missing_flight_no_falls_back_but_still_groups(self):
        def fetcher(o, d, day):
            return [_flight("", 800, day, dep="08:00"), _flight("", 900, day, dep="14:00")]

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=4)
        self.assertEqual(len(m.rows), 2)           # 按 航司@起飞时刻 退化归并
        self.assertEqual(m.rows[0].observed, 4)


class TestCodeshareMerge(unittest.TestCase):
    """真实数据里 17 个"航班"其实只有 4 班飞机——同一分钟起降 = 代码共享。"""

    def _fetcher(self, groups):
        def fetcher(o, d, day):
            out = []
            for dep, arr, members in groups:
                for no, airline, price in members:
                    out.append(Flight(flight_no=no, airline=airline,
                                      departure_time=dep, arrival_time=arr,
                                      departure_date=day, transfer_num=1,
                                      price=float(price)))
            return out
        return fetcher

    def test_same_departure_and_arrival_merges(self):
        groups = [("07:00", "10:00", [("3U1450", "四川航空", 300),
                                      ("CZ5170", "南方航空", 320),
                                      ("GJ3108", "长龙航空", 400)]),
                  ("20:40", "22:20", [("CZ3784", "南方航空", 500)])]
        m = price_matrix.build_matrix("杭州", "武汉", TODAY, self._fetcher(groups), days=5)
        self.assertEqual(len(m.rows), 2)          # 4 个号 -> 2 班飞机
        self.assertEqual(m.raw_flight_count, 4)
        merged = [r for r in m.rows if r.is_merged][0]
        self.assertEqual(len(merged.codeshares), 3)

    def test_merged_price_is_the_cheapest_number(self):
        groups = [("07:00", "10:00", [("A1", "甲", 400), ("B2", "乙", 300)])]
        m = price_matrix.build_matrix("杭州", "武汉", TODAY, self._fetcher(groups), days=3)
        row = m.rows[0]
        self.assertEqual(row.min_price, 300)
        self.assertEqual(set(row.cheapest_no.values()), {"B2"})   # 记住便宜的是哪个号

    def test_different_arrival_time_does_not_merge(self):
        """同点起飞、不同时间到达 = 两架飞机，不能合。"""
        groups = [("07:00", "10:00", [("A1", "甲", 400)]),
                  ("07:00", "11:30", [("B2", "乙", 300)])]
        m = price_matrix.build_matrix("杭州", "武汉", TODAY, self._fetcher(groups), days=3)
        self.assertEqual(len(m.rows), 2)

    def test_unstable_flight_numbers_are_not_glued_together(self):
        """每天换一个航班号的坏数据源，不能靠"按时刻合并"把问题盖掉。"""
        def fetcher(o, d, day):
            return [Flight(flight_no="MU" + day.replace("-", ""), airline="东航",
                           departure_time="07:00", arrival_time="10:00",
                           departure_date=day, transfer_num=1, price=500.0)]

        m = price_matrix.build_matrix("杭州", "武汉", TODAY, fetcher, days=6)
        self.assertEqual(len(m.rows), 6)                       # 没被合并
        self.assertTrue(any("对角线" in w for w in m.warnings))  # 仍然被报出来

    def test_merge_can_be_turned_off(self):
        groups = [("07:00", "10:00", [("A1", "甲", 400), ("B2", "乙", 300)])]
        m = price_matrix.build_matrix("杭州", "武汉", TODAY, self._fetcher(groups),
                                      days=3, merge_codeshare=False)
        self.assertEqual(len(m.rows), 2)

    def test_render_shows_codeshare_detail(self):
        groups = [("07:00", "10:00", [("A1", "甲", 400), ("B2", "乙", 300)])]
        m = price_matrix.build_matrix("杭州", "武汉", TODAY, self._fetcher(groups), days=3)
        text = price_matrix.render_matrix(m)
        self.assertIn("共享代码明细", text)
        self.assertIn("B2 +1", text)               # 主号 + 还有几个号
        self.assertIn("实际航班 1 班", text)


class TestPerFlightAdvice(unittest.TestCase):
    """逐航班算，聚合 min() 会抹掉的信号。"""

    def _fetcher(self, flights_by_day):
        def fetcher(o, d, day):
            i = (date.fromisoformat(day) - TODAY).days - 1
            return flights_by_day(i, day)
        return fetcher

    def test_consensus_day_survives_a_flat_aggregate_curve(self):
        """实测场景：每班都在第 6 天见底，但每天都有一班特价把聚合曲线拉平。"""
        curve = [900, 850, 800, 750, 700, 600, 780]

        def by_day(i, day):
            out = [_flight("A%d" % k, curve[i] + k * 30, day, dep="%02d:00" % (7 + k))
                   for k in range(4)]
            out.append(_flight("SPOT%d" % i, 590, day, dep="23:%02d" % i))  # 每天不同的特价
            return out

        m = price_matrix.build_matrix("上海", "昆明", TODAY, self._fetcher(by_day), days=7)
        advice = price_matrix.per_flight_advice(m)
        self.assertEqual(advice["agree"], 4)
        self.assertEqual(advice["consensus_day"], (TODAY + timedelta(days=6)).isoformat())
        # 聚合曲线因为那班特价而完全看不出低点
        self.assertGreaterEqual(price_matrix.volatility(m)["trough_days"], 1)

    def test_ties_are_all_counted(self):
        def by_day(i, day):
            flat = [600, 600, 700, 700, 700, 700, 700]
            return [_flight("A%d" % k, flat[i], day, dep="%02d:00" % (7 + k))
                    for k in range(3)]

        m = price_matrix.build_matrix("A", "B", TODAY, self._fetcher(by_day), days=7)
        advice = price_matrix.per_flight_advice(m)
        self.assertEqual(sum(advice["distribution"].values()), 6)   # 3 班 × 2 个并列日

    def test_partial_coverage_flights_are_excluded(self):
        def by_day(i, day):
            out = [_flight("A%d" % k, 700, day, dep="%02d:00" % (7 + k)) for k in range(3)]
            if i == 2:
                out.append(_flight("RARE", 300, day, dep="23:00"))
            return out

        m = price_matrix.build_matrix("A", "B", TODAY, self._fetcher(by_day), days=7)
        advice = price_matrix.per_flight_advice(m)
        self.assertEqual(advice["total"], 3)
        self.assertNotIn("RARE", [f["flight_no"] for f in advice["flights"]])


class TestScanBasis(unittest.TestCase):
    """交给提前量分析的那条曲线，必须是可比的中位价，不是被特价定住的 min。"""

    def _fetcher(self):
        base = [900, 850, 800, 780, 700, 690, 800]

        def fetcher(o, d, day):
            i = (date.fromisoformat(day) - TODAY).days - 1
            out = [_flight("A%d" % k, base[i] + k * 50, day, dep="%02d:00" % (7 + k))
                   for k in range(4)]
            # 每天换一班的中转特价：它会把「当日最低价」焊死在 ¥520
            out.append(Flight(flight_no="LCC%d+X" % i, airline="某航", price=520.0,
                              departure_date=day, departure_time="23:%02d" % i,
                              arrival_time="06:00", transfer_num=2))
            return out
        return fetcher

    def test_scan_carries_panel_median(self):
        m = price_matrix.build_matrix("上海", "昆明", TODAY, self._fetcher(), days=7)
        scan = price_matrix.to_scan(m)
        self.assertEqual(len(scan), 7)
        self.assertTrue(all(r["panel_median"] is not None for r in scan))
        self.assertEqual(scan[0]["panel_size"], 4)
        # 全量最低价被中转特价定住，中位价没有
        self.assertTrue(all(r["min_price"] == 520 for r in scan))
        self.assertGreater(len({r["panel_median"] for r in scan}), 3)

    def test_median_curve_bottoms_where_the_flights_do(self):
        m = price_matrix.build_matrix("上海", "昆明", TODAY, self._fetcher(), days=7)
        scan = price_matrix.to_scan(m)
        low = min(scan, key=lambda r: r["panel_median"])
        self.assertEqual(low["advance"], 6)
        self.assertEqual(price_matrix.per_flight_advice(m)["consensus_day"],
                         (TODAY + timedelta(days=6)).isoformat())

    def test_panel_median_absent_when_no_panel(self):
        def fetcher(o, d, day):
            return [_flight("ONLY", 600, day)]

        m = price_matrix.build_matrix("A", "B", TODAY, fetcher, days=5)
        self.assertTrue(all(r["panel_median"] is None
                            for r in price_matrix.to_scan(m)))


class TestAirlineFilter(unittest.TestCase):
    def _fetcher(self):
        def fetcher(o, d, day):
            return [_flight("9C1", 500, day, "春秋航空", dep="08:00"),
                    _flight("MU1", 700, day, "东方航空", dep="10:00"),
                    _flight("HO1", 650, day, "吉祥航空", dep="12:00")]
        return fetcher

    def test_exclude_matches_by_substring(self):
        m = price_matrix.build_matrix("A", "B", TODAY, self._fetcher(), days=4,
                                      exclude_airlines=["春秋"])
        self.assertEqual({r.flight_no for r in m.rows}, {"MU1", "HO1"})

    def test_whitelist(self):
        m = price_matrix.build_matrix("A", "B", TODAY, self._fetcher(), days=4,
                                      airlines=["东方", "吉祥"])
        self.assertEqual({r.flight_no for r in m.rows}, {"MU1", "HO1"})

    def test_excluded_airline_does_not_set_the_daily_minimum(self):
        """不坐的航司留在表里只会干扰结论——最便宜那格点不了，等于没有。"""
        m = price_matrix.build_matrix("A", "B", TODAY, self._fetcher(), days=4,
                                      exclude_airlines=["春秋"])
        self.assertEqual(m.valid_columns[0].min_price, 650)


class TestVolatility(unittest.TestCase):
    """"这条航线值不值得挑日子"必须由数据回答，不能靠感觉。"""

    def _curve(self, prices):
        def fetcher(o, d, day):
            i = (date.fromisoformat(day) - TODAY).days - 1
            return [_flight("X1", prices[i], day)]
        return fetcher

    def test_wide_trough_is_called_out_even_when_cv_looks_ok(self):
        """实测的杭州→武汉：cv=0.17 看着"中等"，但 7 天里 4 天都在谷底。"""
        m = price_matrix.build_matrix(
            "杭州", "武汉", TODAY, self._curve([430, 430, 300, 300, 300, 320, 300]),
            days=7)
        vol = price_matrix.volatility(m)
        self.assertGreaterEqual(vol["trough_days"], 4)
        self.assertIn("低价窗口太宽", vol["verdict"])

    def test_scarce_trough_is_recommended(self):
        m = price_matrix.build_matrix(
            "上海", "三亚", TODAY,
            self._curve([2180, 2050, 1980, 1120, 980, 940, 1010]), days=7)
        vol = price_matrix.volatility(m)
        self.assertGreater(vol["cv"], 0.25)
        self.assertIn("能体现提前量分析的价值", vol["verdict"])

    def test_panel_ignores_flights_that_do_not_fly_every_day(self):
        """实测踩到的坑：稀疏航班压低了「当日最低价」，让低价窗口虚胖。

        天天飞的那班谷底只有 2 天，但另有两班各自只在一天出现、且报价很低，
        按全量口径算就成了 4 天。跨日期比较必须用同一批航班。
        """
        daily = [730, 700, 588, 595, 700, 710, 720]
        spot = {2: ("SPOT1", 580), 5: ("SPOT2", 580)}

        def fetcher(o, d, day):
            i = (date.fromisoformat(day) - TODAY).days - 1
            # 面板至少要 3 班天天飞的航班才成立
            out = [_flight("EVERYDAY1", daily[i], day, dep="08:00"),
                   _flight("EVERYDAY2", daily[i] + 40, day, dep="12:00"),
                   _flight("EVERYDAY3", daily[i] + 80, day, dep="16:00")]
            if i in spot:
                no, price = spot[i]
                out.append(_flight(no, price, day, dep="21:%02d" % i))
            return out

        m = price_matrix.build_matrix("上海", "昆明", TODAY, fetcher, days=7)
        vol = price_matrix.volatility(m)
        self.assertIn("可比面板", vol["basis"])
        self.assertLess(vol["trough_days"], vol["all_trough_days"])
        self.assertIn("跨日期比较不成立", vol["divergence"])

    def test_falls_back_to_all_flights_when_panel_too_small(self):
        def fetcher(o, d, day):
            return [_flight("ONLY", 600, day)]

        m = price_matrix.build_matrix("A", "B", TODAY, fetcher, days=5)
        self.assertIn("全部航班", price_matrix.volatility(m)["basis"])

    def test_too_few_samples_declines_to_judge(self):
        m = price_matrix.build_matrix("A", "B", TODAY, self._curve([500, 500]), days=2)
        self.assertFalse(price_matrix.volatility(m)["ok"])

    def test_price_basis_disclaimer_mentions_coupons(self):
        """票面价 ≠ 成交价。平台券不在这个数据源里，必须说清楚边界。"""
        m = price_matrix.build_matrix("A", "B", TODAY, self._curve([500] * 7), days=7)
        text = price_matrix.render_matrix(m)
        self.assertIn("不含", text)
        self.assertIn("优惠券", text)


class TestFlatPrices(unittest.TestCase):
    def test_identical_columns_are_flagged(self):
        """接口忽略了 depDate 的话，表看起来完全正常，只是每列都是同一天。"""
        def fetcher(o, d, day):
            return [_flight("MU5401", 780, day), _flight("CA4502", 915, day)]

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=7)
        self.assertTrue(any("完全相同" in w for w in m.warnings))

    def test_normal_variation_is_not_flagged(self):
        m = price_matrix.build_matrix(
            "上海", "成都", TODAY,
            MockFlightProvider(today=TODAY).search_flights, days=7)
        self.assertFalse(any("完全相同" in w for w in m.warnings))


class TestSharedScan(unittest.TestCase):
    def test_matrix_and_advance_analysis_share_one_scan(self):
        """两种视图 = 一次扫描。额度不能因为多了张表就翻倍。"""
        calls = []

        class Counting(MockFlightProvider):
            def search_flights(self, o, d, day):
                calls.append(day)
                return super().search_flights(o, d, day)

        skill = FlightSkill(Counting(today=TODAY))
        res = skill.run("上海", "成都", "2026-09-05", today=TODAY, matrix_days=10)
        self.assertEqual(res["mode"], "price_matrix")
        self.assertTrue(res["ok"])
        self.assertIsNotNone(res["analysis"])
        # 只扫 10 天 = 10 次。矩阵模式下不再单独预查出行日那一天——
        # 它多半就落在窗口里，白花一次钱。
        self.assertEqual(len(calls), 10)


class TestTokenBudget(unittest.TestCase):
    def test_model_gets_a_ref_not_600_numbers(self):
        registry = build_registry(FlightSkill(MockFlightProvider(today=TODAY)),
                                  DestinationSkill(), today=TODAY)
        result = registry.call("search_flights", {
            "origin": "上海", "destination": "成都",
            "travel_date": "2026-09-05", "days": 14})
        self.assertTrue(result.ok)
        payload = result.to_model_payload()
        self.assertIn("table_ref", payload)
        # 完整表格走边带通道，不进模型上下文
        self.assertNotIn("_table_map", payload)
        self.assertIn("_table_map", result.content)
        self.assertNotIn("| **当日最低**", payload["report"])
        self.assertLess(len(str(payload)), len(result.content["_table_map"]["T1"]))

    def test_restore_puts_the_table_back(self):
        mapping = {"T1": "| a | b |\n|---|---|\n| 1 | 2 |"}
        out, stats = table_refs.restore("这是结果：[T1] 请参考。", mapping)
        self.assertIn("| a | b |", out)
        self.assertEqual(stats.missing, [])
        self.assertEqual(stats.unknown, [])

    def test_missing_ref_is_recorded_even_though_user_still_gets_table(self):
        mapping = {"T1": "| a |\n|---|\n| 1 |"}
        out, stats = table_refs.restore("表格已经准备好了。", mapping)
        self.assertEqual(stats.missing, ["T1"])    # 评测照样判失败
        self.assertIn("| a |", out)                # 但用户不会白等

        out2, stats2 = table_refs.restore("表格已经准备好了。", mapping,
                                          append_missing=False)
        self.assertNotIn("| a |", out2)
        self.assertEqual(stats2.missing, ["T1"])

    def test_fabricated_ref_is_detected(self):
        out, stats = table_refs.restore("见 [T1] 和 [T9]。", {"T1": "x"})
        self.assertEqual(stats.unknown, ["T9"])
        self.assertIn("[T9]", out)                 # 原样留着，让它显眼


class TestNoFabrication(unittest.TestCase):
    def test_prices_are_never_interpolated(self):
        """缺格就是缺格，不许用相邻日期补出一个数字。"""
        def fetcher(o, d, day):
            return [] if day.endswith(("15", "16")) else [_flight("MU5101", 800, day)]

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=5)
        row = m.rows[0]
        self.assertEqual(row.observed, 3)
        self.assertNotIn((TODAY + timedelta(days=2)).isoformat(), row.prices)

    def test_no_priced_flights_is_reported_as_failure(self):
        m = price_matrix.build_matrix("上海", "成都", TODAY,
                                      lambda o, d, day: [], days=4)
        self.assertFalse(m.ok)
        skill = FlightSkill(MockFlightProvider(today=TODAY, supports_price=False))
        res = skill.run("上海", "成都", "2026-09-05", today=TODAY, matrix_days=5)
        self.assertEqual(res["mode"], "schedule_only")   # 没票价就不装作有矩阵


if __name__ == "__main__":
    unittest.main(verbosity=2)
