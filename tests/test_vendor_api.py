# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""按两家真实接口返回样例写的解析测试。

样例直接来自接口文档，字段名一个都没改。这类测试的价值在于：
接口哪天改了字段名，这里会先红，而不是等到某张矩阵悄悄变成空表。
"""

import copy
import json
import sys
import unittest
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travelwise.providers.base import ProviderError                           # noqa: E402
from travelwise.providers.http_flight import HttpFlightProvider               # noqa: E402

# 接口文档给的返回样例，原样照抄
SAMPLE = {
    "code": 200, "msg": "成功", "taskNo": "192365493231988621589326",
    "data": {"list": [{
        "arrivalCityCode": "WUH", "duration": "1小时40分", "departureCityCode": "HGH",
        "price": {"discountRate": 0.74, "adultPrice": "670.00",
                  "childPrice": "460.00", "infantPrice": "无"},
        "airlineCode": "GJ", "departureDateTime": "2026-06-30 20:40:00",
        "arrivalDateTime": "2026-06-30 22:20:00", "state": "航班计划",
        "departureAirportCode": "HGH", "aircraftName": "波音737(中)",
        "departureTerminal": "T4", "arrivalAirportCode": "WUH",
        "flightType": "direct", "flightNo": "GJ3072",
        "departureCityName": "杭州", "arrivalCityName": "武汉",
        "aircraftCode": "73H", "departureAirportName": "萧山国际机场",
        "airlineName": "长龙航空", "arrivalAirportName": "天河国际机场",
        "segments": []}]}}

CONFIG = {
    "name": "vendor", "endpoint": "https://example.com/flight/detail",
    "method": "POST", "body_format": "form", "supports_price": True,
    "date_format": "YYYY-MM-DD", "resolve_city_code": True,
    "params": {"origin_key": "depCode", "destination_key": "arrCode",
               "date_key": "depDate"},
    "auth": {"type": "header", "key": "Authorization", "prefix": "APPCODE",
             "value_env": "X"},
    "response": {
        "list_path": "data.list",
        "price_field": "price.adultPrice",
        "field_map": {
            "flight_no": "flightNo", "airline": "airlineName",
            "departure_city": "departureCityName", "arrival_city": "arrivalCityName",
            "departure_airport": "departureAirportName",
            "arrival_airport": "arrivalAirportName",
            "departure_date": "departureDateTime",
            "departure_time": "departureDateTime",
            "arrival_time": "arrivalDateTime"}},
}


class _FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Patched:
    """临时替换 urlopen，捕获请求并返回给定响应。"""

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def __enter__(self):
        self._orig = urllib.request.urlopen

        def fake(req, timeout=None, context=None):
            self.requests.append(req)
            return _FakeResponse(self.payload)

        urllib.request.urlopen = fake
        return self

    def __exit__(self, *a):
        urllib.request.urlopen = self._orig
        return False


class TestVendorPayload(unittest.TestCase):
    def _fetch(self, payload=None):
        provider = HttpFlightProvider(CONFIG, token="FAKECODE")
        with _Patched(payload or SAMPLE) as patched:
            flights = provider.search_flights("杭州", "武汉", "2026-06-30")
        return flights, patched.requests[0]

    def test_request_shape_matches_the_docs(self):
        _, req = self._fetch()
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.data, b"depCode=HGH&arrCode=WUH&depDate=2026-06-30")
        self.assertEqual(req.get_header("Authorization"), "APPCODE FAKECODE")

    def test_nested_price_path(self):
        """票价挂在 price.adultPrice 里，平铺取值取不到。"""
        flights, _ = self._fetch()
        self.assertEqual(flights[0].price, 670.0)

    def test_datetime_is_split_into_date_and_time(self):
        flights, _ = self._fetch()
        f = flights[0]
        self.assertEqual(f.departure_date, "2026-06-30")
        self.assertEqual(f.departure_time, "20:40")
        self.assertEqual(f.arrival_time, "22:20")

    def test_all_business_fields_land(self):
        flights, _ = self._fetch()
        f = flights[0]
        self.assertEqual(f.flight_no, "GJ3072")
        self.assertEqual(f.airline, "长龙航空")
        self.assertEqual(f.departure_airport, "萧山国际机场")
        self.assertEqual(f.transfer_num, 1)          # flightType=direct

    def test_transfer_flight_is_not_counted_as_direct(self):
        payload = copy.deepcopy(SAMPLE)
        row = payload["data"]["list"][0]
        row["flightType"] = "transfer"
        row["segments"] = [{"a": 1}, {"b": 2}]
        flights, _ = self._fetch(payload)
        self.assertEqual(flights[0].transfer_num, 2)

    def test_city_name_is_converted_to_code_before_sending(self):
        provider = HttpFlightProvider(CONFIG, token="FAKECODE")
        with self.assertRaises(ProviderError) as ctx:
            provider.search_flights("火星", "武汉", "2026-06-30")
        self.assertIn("三字码", str(ctx.exception))

    def test_business_error_code_is_not_silently_empty(self):
        """接口返回业务错误时不能当成『当天没航班』。"""
        provider = HttpFlightProvider(CONFIG, token="FAKECODE")
        with _Patched({"code": 40001, "msg": "参数错误", "data": {}}):
            with self.assertRaises(ProviderError):
                provider.search_flights("杭州", "武汉", "2026-06-30")


if __name__ == "__main__":
    unittest.main(verbosity=2)
