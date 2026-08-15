# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""flight.py —— 机票技能：取数 + 价格分析 + 构造购票提醒请求。

技能层只做编排：
  - 具体数据从哪来 → Provider 的事；
  - 提前量怎么算   → tools/price_analysis 的事；
  - 本层负责把两者接起来，并把失败如实地往上传。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..providers.base import FlightProvider, ProviderError, ReminderRequest
from ..tools import price_analysis, price_matrix


class FlightSkill:
    def __init__(self, provider: FlightProvider):
        self.provider = provider

    # -- 主入口 -----------------------------------------------------------
    def run(self, origin: str, destination: str, travel_date: str,
            today: date | None = None, min_window: int = 7,
            max_window: int = 14, matrix_days: int = 0,
            direct_only: bool = False, sleep_between: float = 0.0,
            airlines: list | None = None,
            exclude_airlines: list | None = None) -> dict:
        """返回结构化结果。

        matrix_days > 0 时启用【价格矩阵模式】：扫描未来 matrix_days 天，
        输出「每航班 × 每出发日」的价格表；提前量分析由**同一批数据**折叠得出，
        不额外消耗额度（见 price_matrix.to_scan）。

        失败时 ok=False 且 error 说明原因，flights/analysis 保持空——
        绝不编造航班或票价（见 tests 中的 forbid_fabrication 用例）。
        """
        today = today or date.today()
        result: dict = {
            "ok": False, "route": "%s→%s" % (origin, destination),
            "travel_date": travel_date, "provider": self.provider.name,
            "mode": None, "text": "", "error": None, "analysis": None, "flights": [],
            "matrix": None, "matrix_text": "",
        }

        # 矩阵模式下**跳过**这次单日预查：它查的出行日多半就落在扫描窗口里，
        # 白花 0.2 元。原来的报价"最多 7 次"实际是 8 次，差的就是这一次。
        if matrix_days and matrix_days > 0 and self.provider.supports_price:
            return self._run_matrix(origin, destination, travel_date, today,
                                    matrix_days, direct_only, sleep_between, result,
                                    airlines, exclude_airlines)

        # 1) 先取出行当天的航班
        try:
            flights = self.provider.search_flights(origin, destination, travel_date)
        except ProviderError as e:
            result["error"] = str(e)
            result["text"] = (
                "未能获取 %s→%s %s 的航班数据：%s\n"
                "（这是数据源返回的真实失败，未做任何推测；可稍后重试、"
                "换用其它数据源，或用 mock 模式查看完整流程。）"
                % (origin, destination, travel_date, e))
            return result

        result["flights"] = [f.to_dict() for f in flights]

        # 2) 数据源不含票价 → 降级为只列时刻表，并说清为什么没有价格建议
        if not self.provider.supports_price or all(f.price is None for f in flights):
            result["ok"] = True
            result["mode"] = "schedule_only"
            result["text"] = price_analysis.render_schedule(
                flights, origin, destination, travel_date)
            return result

        # 3) 有票价 → 做提前量分析
        def fetcher(o, d, day):
            try:
                return self.provider.search_flights(o, d, day)
            except ProviderError:
                # 单日取数失败不阻断整条曲线；样本缺失会体现在扫描点数与警告里
                return []

        analysis = price_analysis.analyze(
            origin, destination, travel_date, fetcher, today=today,
            min_window=min_window, max_window=max_window)
        result["analysis"] = analysis
        result["ok"] = bool(analysis.get("ok"))
        result["mode"] = "price_analysis"
        result["text"] = price_analysis.render_report(analysis)
        if not analysis.get("ok"):
            result["error"] = analysis.get("reason")
        return result

    # -- 价格矩阵模式 -----------------------------------------------------
    def _run_matrix(self, origin, destination, travel_date, today, matrix_days,
                    direct_only, sleep_between, result: dict,
                    airlines=None, exclude_airlines=None) -> dict:
        """一次扫描，两种视图：矩阵 + 提前量分析。额度只花一份。"""
        # 这里**不吞异常**：矩阵要区分「当天没航班」和「当天查询失败」，
        # 前者是事实，后者是缺口，吞掉就分不清了。
        def raw_fetcher(o, d, day):
            return self.provider.search_flights(o, d, day)

        matrix = price_matrix.build_matrix(
            origin, destination, today, raw_fetcher,
            days=matrix_days, direct_only=direct_only,
            sleep_between=sleep_between,
            airlines=airlines, exclude_airlines=exclude_airlines)
        result["matrix"] = matrix
        result["matrix_text"] = price_matrix.render_matrix(matrix)
        result["mode"] = "price_matrix"
        result["flights"] = [{"flight_no": r.flight_no, "airline": r.airline,
                              "departure_time": r.departure_time,
                              "arrival_time": r.arrival_time,
                              "min_price": r.min_price} for r in matrix.rows]

        if not matrix.rows:
            result["ok"] = False
            result["error"] = "扫描窗口内没有任何带票价的航班"
            result["text"] = result["matrix_text"]
            return result

        # 逐航班共识优先：每班各自算最低点，多数落在同一天才算信号。
        # 「哪天的最低价最低」可以被一班特价航班决定，共识需要几十班同时同意。
        advice = price_matrix.per_flight_advice(matrix)
        result["per_flight"] = advice
        scan = price_matrix.to_scan(matrix)
        analysis = price_analysis.analyze_from_scan(
            scan, today, travel_date,
            consensus=price_analysis.consensus_from_advice(advice, today, travel_date))
        analysis["route"] = result["route"]
        analysis["_scan"] = scan
        analysis["_meta"] = {
            "scanned_days": len(matrix.columns), "api_calls": matrix.api_calls,
            "stopped_early": False, "trough_confirmed": False,
            "min_window": matrix_days, "max_window": matrix_days,
            "validate_after": 0, "best_advance": None,
            "source": "price_matrix（与矩阵共用同一次扫描）",
        }
        result["analysis"] = analysis
        result["ok"] = bool(analysis.get("ok"))
        tail = ""
        if analysis.get("ok"):
            tail = price_analysis.render_report({**analysis, "_scan": []})
        result["text"] = result["matrix_text"] + ("\n\n" + tail if tail else "")
        if not analysis.get("ok"):
            result["error"] = analysis.get("reason")
        return result

    # -- 提醒 -------------------------------------------------------------
    @staticmethod
    def build_reminder(result: dict, hour: int = 9, minute: int = 0) -> ReminderRequest | None:
        """把分析结论变成一条提醒请求。没有可用购票日则返回 None。

        这里只【构造】不【执行】——是否执行由 Orchestrator 在拿到用户确认后决定。
        """
        analysis = (result or {}).get("analysis")
        if not analysis or not analysis.get("ok"):
            return None
        chosen = price_analysis.pick_recommendation(analysis)
        if not chosen:
            return None
        buy_date = chosen["recommended_buy_date"]
        method = analysis.get("primary_method") or "提前量分析"

        d = date.fromisoformat(buy_date)
        # 购票日若已过，把提醒挪到明天，避免创建一个永远不会触发的过去时间点
        if d <= date.today():
            d = date.today() + timedelta(days=1)

        return ReminderRequest(
            title="购买 %s 机票" % result["route"],
            remind_at=datetime(d.year, d.month, d.day, hour, minute),
            note=("建议购票日 %s（%s%s）；出行日 %s"
                  % (buy_date, method,
                     ("，参考价 ¥%s" % chosen["cheapest_scan_price"])
                     if chosen.get("cheapest_scan_price") is not None else "",
                     result["travel_date"])),
            important=True,
        )
