# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""destination.py —— 目的地技能：按显式范围整理景区清单。"""

from __future__ import annotations

from ..tools import destination_search


class DestinationSkill:
    """目的地技能。

    两条轨道：
      - 名录轨：官方 A 级景区清单（内置数据，0 次外部调用）；
      - 发现轨：二层场景发现（先搜后抽），需要一个网页搜索源；
        没有搜索源就只出一层关键词入口，并**如实说明二层未启用**。
    """

    def __init__(self, search_provider=None, scenes=None, min_mentions: int = 2,
                 discover_limit: int = 15):
        self.search_provider = search_provider
        self.scenes = scenes
        self.min_mentions = min_mentions
        self.discover_limit = discover_limit

    def run(self, place: str, scope: str = "city", travel_month: int | None = None,
            rebuild: bool = False, today=None) -> dict:
        """scope 必须由调用方显式给出。

        本技能不猜范围、不因结果少而自动扩大——那是产品意图判断，
        只能由用户明说，由 router 传进来。
        """
        try:
            data = destination_search.curate(
                place, scope=scope, travel_month=travel_month, rebuild=rebuild,
                search_provider=self.search_provider, scenes=self.scenes,
                min_mentions=self.min_mentions,
                discover_limit=self.discover_limit, today=today)
        except (OSError, UnicodeError, ValueError) as e:
            # 名录读不出来要明确报错，不能静默返回空清单假装"这地方没景点"
            return {"ok": False, "place": place, "scope": scope, "text": "",
                    "error": "读取景区名录失败：%s" % e, "data": None}

        disc = data.get("discovery") or {}
        return {
            "ok": True, "place": place, "scope": scope,
            "not_found": data["not_found"], "notice": data["notice"],
            "official_count": len(data["official"]),
            "discovered_count": len(disc.get("spots") or []),
            "discovery_enabled": bool(disc.get("enabled")),
            "discovery_reason": disc.get("reason", ""),
            "text": destination_search.render(data),
            "error": None, "data": data,
        }
