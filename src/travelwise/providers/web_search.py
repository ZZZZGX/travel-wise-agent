# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""web_search.py —— 网页搜索数据源（二层发现的第一层）。

## 这一层为什么必须存在

原来的「场景检索」只是把 `城市 + 打卡` 拼成一个搜索链接丢给用户，
省下的仅仅是敲字的时间——判断「哪个地方值得去」的活还是用户自己干。

要真正省掉做攻略的时间，就得先**替用户把搜索结果读一遍**，从里面把
地名抽出来，再按地名给出各自的入口。第一层（本文件）负责拿到搜索结果的
标题与摘要；第二层（tools/spot_extract.py）负责从文字里抽地名。

## 三种实现，用途不同

  - `NullWebSearchProvider`：没配搜索源时的**如实降级**。返回空 + 原因，
    上层照旧给关键词链接，并明确告诉用户「二层发现未启用」，不假装做了。
  - `FixtureWebSearchProvider`：读本地 JSON 回放。**零成本、零凭证**，
    用来跑通整条链路和写测试——抽取逻辑的对错和搜索源无关，
    不该为了调一个正则去烧接口调用。
  - `HttpWebSearchProvider`：厂商无关的配置驱动实现。换搜索源
    （博查 / Bing / Serper / 阿里云市场 / 自建）只改一份 JSON，不改代码。

只用标准库 urllib，与 http_flight.py 保持一致，不引入 requests。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .base import ProviderError


@dataclass
class SearchHit:
    """一条搜索结果。只要标题和摘要，**不要正文**。

    正文要么拿不到（平台反爬），要么很长（烧 token 且没必要）。
    地名在标题和摘要里出现的密度本来就最高。
    """

    title: str = ""
    snippet: str = ""
    url: str = ""
    source: str = ""          # 来自哪个查询词，用于事后核对
    raw: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "%s %s" % (self.title or "", self.snippet or "")


class WebSearchProvider:
    """搜索源接口。`search()` 返回 SearchHit 列表；查不到就返回空列表。

    **失败与空结果必须分清**：网络 / 凭证错误抛 ProviderError，
    「确实没搜到」返回 []。上层要据此区分「没启用 / 坏了」和「这城市真没内容」。
    """

    name = "base"
    enabled = False

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        raise NotImplementedError

    def stats(self) -> str:
        return ""


class NullWebSearchProvider(WebSearchProvider):
    """没配搜索源。不抛异常——「未启用」是正常状态，不是故障。"""

    name = "none"
    enabled = False

    def __init__(self, reason: str = ""):
        self.reason = reason or (
            "未配置网页搜索源（TRAVELWISE_SEARCH_PROVIDER=none），"
            "二层发现未启用：只给场景关键词入口，不代为提取地点。")

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        return []


class FixtureWebSearchProvider(WebSearchProvider):
    """从本地 JSON 回放搜索结果。零成本、可离线、可进版本库。

    文件格式（键是查询词，支持子串匹配，便于一份文件覆盖多个场景词）::

        {
          "昆明 出片": [
            {"title": "...", "snippet": "...", "url": "https://..."},
            ...
          ]
        }
    """

    name = "fixture"
    enabled = True

    def __init__(self, path: str):
        self.path = str(path)
        self.calls = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f) or {}
        except FileNotFoundError as e:
            raise ProviderError("回放文件不存在：%s" % self.path) from e
        except (json.JSONDecodeError, OSError) as e:
            raise ProviderError("回放文件读不出来：%s（%s）" % (self.path, e)) from e

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        self.calls += 1
        items = self.data.get(query)
        if items is None:
            # 子串匹配：查询词里通常带城市名，回放文件不必写全
            for key, value in self.data.items():
                if key in query or query in key:
                    items = value
                    break
        hits = []
        for it in (items or [])[:limit]:
            hits.append(SearchHit(title=it.get("title", ""),
                                  snippet=it.get("snippet", "") or it.get("summary", ""),
                                  url=it.get("url", "") or it.get("link", ""),
                                  source=query, raw=it))
        return hits

    def stats(self) -> str:
        return "[回放] 搜索 %d 次（0 元）" % self.calls


def _dig(obj: Any, path: str, default=None):
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return default
    return default if cur is None else cur


class HttpWebSearchProvider(WebSearchProvider):
    """配置驱动的 HTTP 搜索源。

    配置字段（见 config/web_search_api.example.json）::

        endpoint / method / body_format(form|json|query)
        params.query_key / params.count_key / extra_params
        auth.type(header|query) / auth.key / auth.prefix / auth.value_env
        response.list_path / response.field_map{title,snippet,url}

    凭证只从环境变量读，JSON 里永远只出现变量名。
    """

    name = "http"
    enabled = True

    def __init__(self, config: dict, token: str = "", timeout: float = 12.0,
                 verify_ssl: bool = True):
        self.config = config or {}
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.calls = 0
        self.failures = 0
        if not self.config.get("endpoint"):
            raise ProviderError("搜索源配置缺少 endpoint，无法发起请求")

    # -- 请求构造 ---------------------------------------------------------
    def _headers(self) -> dict:
        auth = self.config.get("auth") or {}
        headers = {"Accept": "application/json",
                   "User-Agent": "travelwise-agent/0.7"}
        headers.update(self.config.get("extra_headers") or {})
        if auth.get("type") == "header" and self.token:
            prefix = auth.get("prefix") or ""
            headers[auth.get("key") or "Authorization"] = (
                ("%s %s" % (prefix, self.token)).strip())
        return headers

    def _payload(self, query: str, limit: int) -> dict:
        params = self.config.get("params") or {}
        payload = dict(self.config.get("extra_params") or {})
        payload[params.get("query_key") or "q"] = query
        count_key = params.get("count_key")
        if count_key:
            payload[count_key] = limit
        auth = self.config.get("auth") or {}
        if auth.get("type") == "query" and self.token:
            payload[auth.get("key") or "key"] = self.token
        return payload

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        method = (self.config.get("method") or "GET").upper()
        body_format = (self.config.get("body_format") or "query").lower()
        payload = self._payload(query, limit)
        endpoint = self.config["endpoint"]
        data = None
        headers = self._headers()

        if method == "GET" or body_format == "query":
            endpoint = endpoint + ("&" if "?" in endpoint else "?") + \
                urllib.parse.urlencode(payload, doseq=True)
        elif body_format == "json":
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = urllib.parse.urlencode(payload, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(endpoint, data=data, headers=headers,
                                     method=method)
        ctx = None if self.verify_ssl else ssl._create_unverified_context()
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                body = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                TimeoutError) as e:
            self.failures += 1
            raise ProviderError("搜索请求失败：%s: %s" % (type(e).__name__, e)) from e

        try:
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            self.failures += 1
            raise ProviderError("搜索源返回的不是 JSON（前 120 字：%s）"
                                % body[:120]) from e

        resp_cfg = self.config.get("response") or {}
        items = _dig(obj, resp_cfg.get("list_path") or "", []) or []
        if isinstance(items, dict):
            items = items.get("value") or items.get("list") or []
        fmap = resp_cfg.get("field_map") or {}
        hits = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            hits.append(SearchHit(
                title=str(_dig(it, fmap.get("title") or "title", "") or ""),
                snippet=str(_dig(it, fmap.get("snippet") or "snippet", "") or ""),
                url=str(_dig(it, fmap.get("url") or "url", "") or ""),
                source=query, raw=it))
        return hits

    def stats(self) -> str:
        return "[搜索] 调用 %d 次（失败 %d 次）" % (self.calls, self.failures)
