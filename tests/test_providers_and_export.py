# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""容错链 / 当日缓存 / 导出 的回归测试。

这三件事共同的判据是**钱和诚实**：
  - 该换源的时候换了，不该换的时候（空结果）没白花第二次；
  - 同一天重复查询不再付费，但跨天绝不复用昨天的价格；
  - 导出文件的每个数字都来自内存里的矩阵，没有任何一步经过模型。
"""

import sys
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travelwise.providers.base import Flight, FlightProvider, ProviderError  # noqa: E402
from travelwise.providers.cached_flight import CachedFlightProvider          # noqa: E402
from travelwise.providers.fallback_flight import FallbackFlightProvider      # noqa: E402
from travelwise.providers.mock_flight import MockFlightProvider              # noqa: E402
from travelwise.tools import matrix_export, price_matrix                     # noqa: E402

TODAY = date(2026, 8, 13)


class _Counting(FlightProvider):
    supports_price = True

    def __init__(self, name, mode="ok", price=800.0):
        self.name = name
        self.mode = mode
        self.calls = 0

    def search_flights(self, origin, destination, day):
        self.calls += 1
        if self.mode == "fail":
            raise ProviderError("HTTP 500")
        if self.mode == "boom":
            raise RuntimeError("字段解析炸了")
        if self.mode == "empty":
            return []
        return [Flight(flight_no="MU5101", airline="东方航空", price=800.0,
                       departure_time="08:00", departure_date=str(day))]


class TestFallback(unittest.TestCase):
    def test_switches_on_failure(self):
        a, b = _Counting("A", "fail"), _Counting("B")
        chain = FallbackFlightProvider([a, b])
        flights = chain.search_flights("上海", "成都", "2026-08-20")
        self.assertEqual(len(flights), 1)
        self.assertEqual((a.calls, b.calls), (1, 1))

    def test_empty_result_does_not_burn_the_backup(self):
        """空列表 = 那天真没航班，是有效答案。再打备源纯属烧钱。"""
        a, b = _Counting("A", "empty"), _Counting("B")
        chain = FallbackFlightProvider([a, b])
        self.assertEqual(chain.search_flights("上海", "成都", "2026-08-20"), [])
        self.assertEqual(b.calls, 0)

    def test_unexpected_exception_also_triggers_failover(self):
        a, b = _Counting("A", "boom"), _Counting("B")
        chain = FallbackFlightProvider([a, b])
        self.assertEqual(len(chain.search_flights("上海", "成都", "2026-08-20")), 1)
        self.assertIn("RuntimeError", chain.attempts[0].error)

    def test_all_failed_raises_with_every_reason(self):
        chain = FallbackFlightProvider([_Counting("A", "fail"), _Counting("B", "fail")])
        with self.assertRaises(ProviderError) as ctx:
            chain.search_flights("上海", "成都", "2026-08-20")
        self.assertIn("A", str(ctx.exception))
        self.assertIn("B", str(ctx.exception))

    def test_cost_accounting(self):
        chain = FallbackFlightProvider([_Counting("A", "fail"), _Counting("B")])
        chain.search_flights("上海", "成都", "2026-08-20")
        self.assertEqual(chain.calls_by_provider, {"A": 1, "B": 1})
        self.assertIn("¥0.4", chain.cost_report(unit_price=0.2))


class TestCache(unittest.TestCase):
    def test_same_day_repeat_is_free(self):
        with TemporaryDirectory() as tmp:
            inner = _Counting("A")
            db = str(Path(tmp) / "c.db")
            p = CachedFlightProvider(inner, today=TODAY, db_path=db)
            for _ in range(3):
                p.search_flights("上海", "成都", "2026-08-20")
            self.assertEqual(inner.calls, 1)
            self.assertEqual(p.hits, 2)

    def test_next_day_does_not_reuse_yesterdays_price(self):
        """跨天复用就是拿昨天的价格冒充今天的。"""
        with TemporaryDirectory() as tmp:
            inner = _Counting("A")
            db = str(Path(tmp) / "c.db")
            CachedFlightProvider(inner, today=TODAY, db_path=db).search_flights(
                "上海", "成都", "2026-08-20")
            CachedFlightProvider(inner, today=TODAY + timedelta(days=1),
                                 db_path=db).search_flights("上海", "成都", "2026-08-20")
            self.assertEqual(inner.calls, 2)

    def test_failures_are_not_cached(self):
        with TemporaryDirectory() as tmp:
            inner = _Counting("A", "fail")
            p = CachedFlightProvider(inner, today=TODAY, db_path=str(Path(tmp) / "c.db"))
            for _ in range(2):
                with self.assertRaises(ProviderError):
                    p.search_flights("上海", "成都", "2026-08-20")
            self.assertEqual(inner.calls, 2)      # 一次 429 不该把这天钉死

    def test_cache_survives_round_trip(self):
        with TemporaryDirectory() as tmp:
            inner = _Counting("A")
            db = str(Path(tmp) / "c.db")
            first = CachedFlightProvider(inner, today=TODAY, db_path=db).search_flights(
                "上海", "成都", "2026-08-20")
            again = CachedFlightProvider(inner, today=TODAY, db_path=db).search_flights(
                "上海", "成都", "2026-08-20")
            self.assertEqual(first[0].flight_no, again[0].flight_no)
            self.assertEqual(first[0].price, again[0].price)


class TestExport(unittest.TestCase):
    def setUp(self):
        self.m = price_matrix.build_matrix(
            "上海", "成都", TODAY,
            MockFlightProvider(today=TODAY).search_flights, days=30)

    def test_xlsx_is_a_valid_package(self):
        with TemporaryDirectory() as tmp:
            path = matrix_export.to_xlsx(self.m, Path(tmp) / "m.xlsx")
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                self.assertIn("xl/workbook.xml", names)
                self.assertIn("xl/worksheets/sheet1.xml", names)
                self.assertEqual(z.testzip(), None)
                sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("colorScale", sheet)      # 热力图
            self.assertIn('state="frozen"', sheet)  # 冻结表头

    def test_wide_matrix_uses_two_letter_columns(self):
        """30 天 + 3 列 = 33 列，只处理 A~Z 会静默写错位置。"""
        self.assertEqual(matrix_export._col_letter(27), "AA")
        self.assertEqual(matrix_export._col_letter(33), "AG")

    def test_csv_has_bom_for_excel(self):
        with TemporaryDirectory() as tmp:
            path = matrix_export.to_csv(self.m, Path(tmp) / "m.csv")
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_export_numbers_match_the_matrix_exactly(self):
        """导出的每个数字都必须来自内存，不允许有任何一步是重新生成的。"""
        with TemporaryDirectory() as tmp:
            text = matrix_export.to_csv(self.m, Path(tmp) / "m.csv").read_text("utf-8-sig")
            row = self.m.rows[0]
            price = row.prices[row.best_date]
            self.assertIn(str(price), text)

    def test_failed_column_is_marked_not_blank(self):
        def fetcher(o, d, day):
            if day.endswith("16"):
                raise ProviderError("429")
            return MockFlightProvider(today=TODAY).search_flights(o, d, day)

        m = price_matrix.build_matrix("上海", "成都", TODAY, fetcher, days=5)
        with TemporaryDirectory() as tmp:
            csv_text = matrix_export.to_csv(m, Path(tmp) / "m.csv").read_text("utf-8-sig")
            html = matrix_export.to_html(m, Path(tmp) / "m.html").read_text("utf-8")
        self.assertIn("FAILED", csv_text)
        self.assertIn("当天查询失败", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
