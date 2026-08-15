# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""HttpFlightProvider —— 厂商无关的 HTTP 航班数据源。

原实现把某一家云市场商品的地址、鉴权头、字段名写死在代码里，换供应商就得改代码。
这里把「一个航班查询接口长什么样」抽象成配置：

    endpoint / method / 参数名 / 日期格式 / 鉴权方式 / 响应路径 / 字段映射

于是接入任何一家（阿里云云市场、聚合数据、飞猪、Skyscanner、甚至你自己的服务）
都只是写一份配置，不改一行代码。配置可来自环境变量或 JSON 文件。

只用标准库 urllib，不引入 requests——保证零依赖可运行。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from .base import Flight, FlightProvider, ProviderError

# 常见字段别名：不同厂商叫法不一，先按别名自动认领，认不出再让用户在配置里显式映射。
_ALIASES: dict[str, tuple[str, ...]] = {
    "flight_no": ("FLIGHT_ID", "flightNo", "flight_no", "fltNo", "flightNumber"),
    "airline": ("FLIGHT_AIRWAYS_CH", "airlineName", "airline", "carrier"),
    "departure_city": ("START_CITY", "depCity", "departure", "fromCity"),
    "arrival_city": ("END_CITY", "arrCity", "arrival", "toCity"),
    "departure_airport": ("START_AIRPORT_CH", "depAirport", "fromAirport"),
    "arrival_airport": ("END_AIRPORT_CH", "arrAirport", "toAirport"),
    "departure_date": ("START_DATE", "departureDate", "depDate"),
    "departure_time": ("START_TIME", "departureTime", "depTime"),
    "arrival_time": ("END_TIME", "arrivalTime", "arrTime"),
}

_PRICE_HINTS = ("PRICE", "FARE", "价", "票价")
_PRICE_PREFER = ("LOW", "MIN", "AGIO", "最低", "现价", "SALE")

_LIST_KEYS = ("data", "result", "list", "flights", "rows", "items", "flightInfo")


def _coerce_price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").lstrip("¥$￥ ").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


_MISSING = object()


def _dig(raw: dict, path: str):
    """按点号路径取值，如 "price.adultPrice"。

    真实接口的价格常常挂在嵌套对象里（成人/儿童/婴儿三档）。只支持平铺键名
    就只能靠别名表瞎猜，或者被迫在业务层写一个厂商专属的解析分支——
    那正是这个 Provider 存在的意义要避免的事。
    """
    node = raw
    for seg in str(path).split("."):
        if not isinstance(node, dict) or seg not in node:
            return _MISSING
        node = node[seg]
    return node


def _pick(raw: dict, field: str, explicit: dict[str, str]) -> str:
    """按「显式映射 > 别名表」取值。显式映射支持 "a.b" 形式的嵌套路径。"""
    key = explicit.get(field)
    if key:
        value = _dig(raw, key)
        if value is not _MISSING:
            return str(value or "")
    for alias in _ALIASES.get(field, ()):
        if alias in raw:
            return str(raw.get(alias) or "")
    return ""


def _extract_price(raw: dict, explicit_field: str = "") -> float | None:
    if explicit_field:
        value = _dig(raw, explicit_field)
        if value is not _MISSING:
            return _coerce_price(value)
    best: list[tuple[int, float]] = []
    for k, v in raw.items():
        ks, ku = str(k), str(k).upper()
        if any(h in ku or h in ks for h in _PRICE_HINTS):
            p = _coerce_price(v)
            if p is not None and p > 0:
                pref = 0 if any(x in ku or x in ks for x in _PRICE_PREFER) else 1
                best.append((pref, p))
    return min(best)[1] if best else None


def _fmt_time(s: str) -> str:
    """兼容 "YYYY-MM-DD HH:MM:SS" / YYYYMMDDHHMMSS / HHMM / HH:MM 四种写法。

    很多接口只给一个完整的 departureDateTime，日期和时刻都得从这一个字段里切；
    不处理的话，矩阵表格的「起飞」列会直接显示一整串时间戳。
    """
    s = (s or "").strip()
    if " " in s and ":" in s:                     # 2026-06-30 20:40:00
        s = s.split(" ", 1)[1]
    if len(s) >= 12 and s[:12].isdigit():         # 20260630204000
        return "%s:%s" % (s[8:10], s[10:12])
    if len(s) == 4 and s.isdigit():               # 2040
        return "%s:%s" % (s[:2], s[2:])
    return s[:5] if len(s) >= 5 and s[2] == ":" else s


def _fmt_date(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":   # 2026-06-30[ 20:40:00]
        return s[:10]
    if len(s) >= 8 and s[:8].isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return s


def _transfer_num(raw: dict) -> int:
    """航段数。direct = 1 段直飞；有 segments 就按段数算。

    这个字段决定 --direct-only 过滤是否成立。取不到时返回 1（当直飞）会让
    中转航班混进"直飞"结果里，所以宁可按 segments 长度算。
    """
    explicit = raw.get("transfer_num") or raw.get("transferNum")
    if isinstance(explicit, int):
        return max(1, explicit)
    ftype = str(raw.get("flightType") or "").lower()
    if ftype in ("direct", "nonstop", "直飞"):
        return 1
    segments = raw.get("segments")
    if isinstance(segments, list) and segments:
        return len(segments)
    return 1


class HttpFlightProvider(FlightProvider):
    """按配置调用任意 HTTP 航班接口。

    config 结构见 config/flight_api.example.json。核心字段：
      endpoint      接口地址
      method        GET / POST
      params        {origin_key, destination_key, date_key} 三个参数的真实字段名
      date_format   YYYYMMDD / YYYY-MM-DD
      extra_params  该接口需要的其它固定参数
      auth          {type: header|bearer|query, key, value_env, prefix}
      response      {list_path, price_field, field_map}
      supports_price 该数据源是否含票价
    """

    def __init__(self, config: dict[str, Any], token: str = "", timeout: int = 15,
                 verify_ssl: bool = True):
        self.config = config or {}
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.name = self.config.get("name") or "http"
        self.supports_price = bool(self.config.get("supports_price", False))

    # -- 请求构造 ---------------------------------------------------------
    def _format_date(self, day: date) -> str:
        fmt = str(self.config.get("date_format", "YYYY-MM-DD")).upper()
        return day.strftime("%Y%m%d") if fmt == "YYYYMMDD" else day.isoformat()

    def _resolve_cities(self, origin: str, destination: str) -> tuple[str, str]:
        """中文城市名 → 三字码。

        接口要的是 SHA / CTU，用户说的是「上海」「成都」。这一步必须在
        **发请求之前**完成并且**查不到就直接失败**：拿一个猜出来的码去查，
        轻则查错城市，重则白烧一次额度还拿到一份看起来正常的错数据。

        配置里 resolve_city_code=false 可关掉（接口本身收中文名时）。
        """
        if not self.config.get("resolve_city_code", False):
            return origin, destination

        from ..tools.city_codes import CityCodeError, resolve
        out = []
        for name in (origin, destination):
            try:
                out.append(resolve(name))
            except CityCodeError as e:
                raise ProviderError("城市码解析失败：%s" % e) from e
            except (OSError, UnicodeError) as e:
                raise ProviderError(
                    "读取城市三字码表失败（data/source/city_codes.csv）：%s" % e) from e
        return out[0], out[1]

    def _build_request(self, origin: str, destination: str, day: date):
        cfg = self.config
        endpoint = cfg.get("endpoint")
        if not endpoint:
            raise ProviderError(
                "未配置航班接口地址（endpoint）。请填写 config/flight_api.json，"
                "或用 --provider mock 以离线模式运行。")

        origin, destination = self._resolve_cities(origin, destination)

        p = cfg.get("params") or {}
        params: dict[str, str] = {
            p.get("origin_key", "origin"): origin,
            p.get("destination_key", "destination"): destination,
            p.get("date_key", "date"): self._format_date(day),
        }
        params.update({k: str(v) for k, v in (cfg.get("extra_params") or {}).items()})

        headers = {"Accept": "application/json"}
        auth = cfg.get("auth") or {}
        atype = str(auth.get("type", "none")).lower()
        if atype != "none":
            if not self.token:
                raise ProviderError(
                    "该接口需要凭证，但未提供。请设置环境变量 %s，或改用 mock 模式。"
                    % auth.get("value_env", "TRAVELWISE_FLIGHT_TOKEN"))
            if atype == "header":
                prefix = auth.get("prefix", "")
                headers[auth.get("key", "Authorization")] = (
                    ("%s %s" % (prefix, self.token)).strip() if prefix else self.token)
            elif atype == "bearer":
                headers["Authorization"] = "Bearer " + self.token
            elif atype == "query":
                params[auth.get("key", "key")] = self.token

        method = str(cfg.get("method", "GET")).upper()
        body = None
        if method == "POST":
            if str(cfg.get("body_format", "form")).lower() == "json":
                body = json.dumps(params, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json; charset=UTF-8"
            else:
                body = urllib.parse.urlencode(params).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            url = endpoint
        else:
            url = endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(params)
        return method, url, headers, body

    # -- 响应解析 ---------------------------------------------------------
    def _locate_list(self, payload: Any) -> list:
        path = self.config.get("response", {}).get("list_path")
        if path:                                  # 显式路径，如 "data.flights"
            node = payload
            for seg in str(path).split("."):
                if not isinstance(node, dict) or seg not in node:
                    raise ProviderError("响应里找不到配置的 list_path「%s」，接口可能已变更。" % path)
                node = node[seg]
            if not isinstance(node, list):
                raise ProviderError("list_path「%s」指向的不是数组。" % path)
            return node

        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):             # 自动探测常见容器
            for k in _LIST_KEYS:
                v = payload.get(k)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    for k2 in _LIST_KEYS:
                        if isinstance(v.get(k2), list):
                            return v[k2]
            msg = (payload.get("msg") or payload.get("message") or payload.get("error")
                   or str(payload)[:200])
            raise ProviderError(
                "接口未返回航班数组（常见原因：参数不符 / 额度用尽 / 未订购）。接口原文：%s" % msg)
        raise ProviderError("响应结构无法识别，接口可能已变更。")

    def _to_flight(self, raw: Any) -> Flight:
        if not isinstance(raw, dict):
            raise ProviderError("航班数组的元素不是对象，接口可能已变更。")
        resp = self.config.get("response") or {}
        fmap = resp.get("field_map") or {}
        return Flight(
            flight_no=_pick(raw, "flight_no", fmap),
            airline=_pick(raw, "airline", fmap),
            departure_city=_pick(raw, "departure_city", fmap),
            arrival_city=_pick(raw, "arrival_city", fmap),
            departure_airport=_pick(raw, "departure_airport", fmap),
            arrival_airport=_pick(raw, "arrival_airport", fmap),
            departure_date=_fmt_date(_pick(raw, "departure_date", fmap)),
            departure_time=_fmt_time(_pick(raw, "departure_time", fmap)),
            arrival_time=_fmt_time(_pick(raw, "arrival_time", fmap)),
            transfer_num=_transfer_num(raw),
            price=_extract_price(raw, resp.get("price_field", "")),
        )

    # -- 接口实现 ---------------------------------------------------------
    def fetch_raw(self, origin: str, destination: str, day: str | date):
        """只发请求、只解 JSON，**不做字段映射**。

        接入一家新接口时，你需要先看见它到底返回了什么才能填 field_map。
        把这一步单独暴露出来，probe 脚本就能用 1 次额度把原文打给你看，
        而不是让你对着「找不到航班数组」这句话瞎猜。
        """
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        return self._request_json(origin, destination, d)

    def search_flights(self, origin: str, destination: str, day: str | date) -> list[Flight]:
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        payload = self._request_json(origin, destination, d)
        return [self._to_flight(x) for x in self._locate_list(payload)]

    def _request_json(self, origin: str, destination: str, d: date):
        method, url, headers, body = self._build_request(origin, destination, d)

        req = urllib.request.Request(url, data=body, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        ctx = None
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            hint = ""
            if e.code in (401, 403):
                hint = "（凭证无效 / 额度用尽 / 未订购该接口）"
            elif e.code == 400:
                hint = "（参数名或日期格式不符，检查配置里的 params 与 date_format）"
            raise ProviderError("HTTP %s 调用失败%s：%s" % (e.code, hint, e.reason)) from e
        except urllib.error.URLError as e:
            raise ProviderError("网络不可达（运行环境可能禁止外网访问）：%s" % e.reason) from e
        except TimeoutError as e:
            raise ProviderError("请求超时（%ss）" % self.timeout) from e

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(
                "返回内容不是合法 JSON（前 120 字：%s）" % (text[:120] or "(空)")) from e
