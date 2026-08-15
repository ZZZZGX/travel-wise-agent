# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""destination_search.py —— 目的地策展：名录检索 + 搜索入口 + 季节标注。

检索范围（scope）由调用方【显式传入】，本模块不做任何自动升级：
  scope="city"     只整理该城市的景区；名录查不到就如实说明，仍给关键词轨。
  scope="province" 整理整个省 / 大区，按州 / 地区分组；关键词轨用省名。

「用户想在一个城市玩，还是在整个省玩」是业务意图判断，由 router 依用户
明说的诉求决定后传进来。本模块只忠实执行，不替用户猜范围、不替用户选帖子。
"""

from __future__ import annotations

from collections import OrderedDict

from . import scene_discovery
from .search_links import build_keyword_links, build_spot_links, build_subregion_links
from .season import check_match
from .spot_repository import is_province_name, load_city, load_province


def _annotate(slim_list, travel_month):
    """给每个景区加搜索链接与季节标注。"""
    linked = build_spot_links(slim_list)
    by_name = {s["名称"]: s for s in slim_list}
    out = []
    for item in linked:
        s = by_name.get(item["名称"], {})
        note, ok, unknown = "", True, False
        if travel_month:
            m = check_match(s.get("适宜季节", ""), travel_month)
            note, ok, unknown = m.get("note", ""), m.get("match", True), m.get("unknown", False)
        out.append({
            "名称": item["名称"],
            "级别": item["级别"],
            "所在地区": s.get("所在地区", "") or "",
            "适宜季节": s.get("适宜季节", "") or "未标注",
            "大门票参考": s.get("大门票参考", "") or "",
            "开放时间": s.get("开放时间", "") or "",
            "season_note": note,
            "season_ok": ok,
            "season_unknown": unknown,
            "links": {"web": item["web"], "app": item["app"], "fallback": item["fallback"]},
        })
    return out


def _discover(place: str, official: list, provider, scenes, min_mentions: int,
              limit: int, today) -> dict:
    """跑二层发现。名录里的景区名当作补充词表传下去——
    它们是**已确认存在**的地名，全文匹配即可命中，能补上规则抽取的召回缺口。
    """
    catalog = {s["名称"]: s.get("级别") or "" for s in official}
    return scene_discovery.discover(
        place, provider, scenes=scenes, min_mentions=min_mentions,
        catalog=catalog, max_spots=limit, today=today)


def curate(query: str, scope: str = "city", travel_month: int | None = None,
           rebuild: bool = False, max_official: int | None = None,
           search_provider=None, scenes=None, min_mentions: int = 2,
           discover_limit: int = 15, today=None) -> dict:
    """整理某地的目的地候选清单。scope 必须显式指定，不会自动扩大。

    `search_provider` 给了就跑二层发现（先搜场景、再抽地点、按地点给入口）；
    没给就只有一层的场景关键词入口，并在输出里**明说二层未启用**。
    """
    scope = "province" if scope == "province" else "city"

    if scope == "province":
        data = load_province(query, rebuild=rebuild)
        province = data["province"] or query
        flat = data["flat"][:max_official] if max_official else data["flat"]
        official = _annotate(flat, travel_month)
        if travel_month:
            official.sort(key=lambda s: 0 if s["season_ok"] else 1)

        groups: OrderedDict = OrderedDict()
        for s in official:
            pref = (s["所在地区"] or "其他").replace("等", "").strip() or "其他"
            groups.setdefault(pref, []).append(s)

        notice = ""
        if not official:
            if is_province_name(query):
                notice = ("「%s」是有效的省 / 自治区名，但名录中暂无该省景区数据"
                          "（数据源未收录，非输入错误）。" % province)
            else:
                notice = ("名录中未检索到「%s」——它看起来不是省 / 自治区名，"
                          "请确认省名，或改用城市名按城市范围查询。" % query)
        return {
            "query": query, "scope": "province", "title": province,
            "grouped": True, "not_found": not official, "notice": notice,
            "groups": groups, "official": official,
            "keyword_tracks": build_keyword_links(province),
            "subregion_tracks": build_subregion_links(list(groups.keys())),
            "discovery": _discover(province, official, search_provider, scenes,
                                   min_mentions, discover_limit, today),
        }

    # -------- 城市级 --------
    spots = load_city(query, rebuild=rebuild)
    if max_official:
        spots = spots[:max_official]
    official = _annotate(spots, travel_month)
    if travel_month:
        official.sort(key=lambda s: 0 if s["season_ok"] else 1)

    notice = ""
    if not official:
        # 名录未收录该城市：如实说明。是否扩到全省由上层征询用户后决定，这里绝不自动扩。
        notice = "名录中未收录「%s」的官方 A 级景区（仅提供关键词泛检索）。" % query
        if is_province_name(query):
            notice += "（提示：「%s」看起来是省 / 大区名，如需全省推荐请按省级范围查询。）" % query

    return {
        "query": query, "scope": "city", "title": query,
        "grouped": False, "not_found": not official, "notice": notice,
        "groups": OrderedDict(), "official": official,
        "keyword_tracks": build_keyword_links(query),
        "subregion_tracks": [],
        "discovery": _discover(query, official, search_provider, scenes,
                               min_mentions, discover_limit, today),
    }


def _extra_line(s: dict) -> str:
    bits = []
    if s.get("大门票参考"):
        bits.append("门票 %s" % s["大门票参考"])
    if s.get("开放时间"):
        bits.append("开放 %s" % s["开放时间"])
    return "｜".join(bits)


def _render_spot(s: dict, indent: str, prefix: str = "") -> list[str]:
    L = ["%s%s%s（%s）" % (indent, prefix, s["名称"], s["级别"])]
    pad = indent + "  "
    extra = _extra_line(s)
    if extra:
        L.append(pad + extra)
    L.append("%s🔗 %s" % (pad, s["links"]["web"]))
    L.append("%s📱 %s" % (pad, s["links"]["app"]))
    if s.get("season_note"):
        L.append(pad + s["season_note"])
    return L


def render(result: dict) -> str:
    """渲染成目的地推荐文本。"""
    if result["scope"] == "province":
        L = ["【%s · 全省 / 大区目的地推荐】" % result["title"]]
    else:
        L = ["【%s · 目的地推荐】" % result["title"]]
    L.append("")
    if result["notice"]:
        L.append("⚠️ %s" % result["notice"])
        L.append("")

    L.append("=== 官方 A 级景区（名录检索） ===")
    if result["grouped"] and result["official"]:
        for pref, items in result["groups"].items():
            L.append("")
            L.append("〔%s〕" % pref)
            for s in items:
                L.extend(_render_spot(s, "  "))
    elif result["official"]:
        L.append("")
        for i, s in enumerate(result["official"], 1):
            L.extend(_render_spot(s, "", "%d. " % i))
    else:
        L.append("（名录无收录）")

    L.append("")
    L.append("=== 按场景检索（一层：关键词入口，结果页里的地方要你自己挑） ===")
    for k in result["keyword_tracks"]:
        L.append("🔗 %s → %s" % (k["keyword"], k["web"]))

    disc = result.get("discovery") or {}
    if disc:
        L.append("")
        L.extend(scene_discovery.render(disc))

    subs = result.get("subregion_tracks") or []
    if subs:
        L.append("")
        L.append("=== 按州 / 地区分级的热点入口 ===")
        for blk in subs:
            L.append("")
            L.append("〔%s〕" % blk["区域"])
            for t in blk["tracks"]:
                L.append("  🔗 %s → %s" % (t["keyword"], t["web"]))

    L.append("")
    L.append("说明：以上均为搜索结果页链接，由你自行浏览决定，AI 不替你选帖子。")
    return "\n".join(L)
