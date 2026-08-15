# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""search_links.py —— 构造社交平台「搜索结果页」链接（不返回具体帖子链接）。

设计决策：只给搜索结果页 URL，让用户自己浏览平台排序后的全部结果。
  - AI 不做筛选 → 没有选错帖子的风险；
  - 决策权留在用户手里；
  - Agent 的价值是"省下输入搜索词的时间"，不是"替你挑"。

URL 结构集中在本文件顶部常量，平台改版只改这一处。
"""

from __future__ import annotations

import urllib.parse

# --- URL 模板（平台改版只改这里） ---------------------------------------
# 网页版搜索结果页：任何浏览器可打开（部分内容需登录才完整显示）
WEB_SEARCH_TMPL = "https://www.xiaohongshu.com/search_result?keyword={q}"
# App 深链：装了 App 的移动端可直接唤起；部分客户端不渲染自定义 scheme 属正常现象
APP_SEARCH_TMPL = "xhsdiscover://search/result?keyword={q}"
# 通用搜索引擎兜底：免登录、任何环境可点开
FALLBACK_TMPL = "https://www.bing.com/search?q={q}"

# 场景关键词。"出片"靠前——旅行人群中该需求高频。平台热词变化时直接增删。
SCENE_KEYWORDS = [
    "打卡", "必去", "出片", "拍照圣地", "机位",
    "小众景点", "小众徒步", "Citywalk", "特种兵旅游",
    "周末去哪儿", "一日游攻略", "美食",
]

# 省域分级热点用的精简场景词
SUBREGION_SCENES = ["出片", "打卡", "游玩攻略"]


def _q(keyword: str) -> str:
    return urllib.parse.quote(keyword)


def web_search_url(keyword: str) -> str:
    return WEB_SEARCH_TMPL.format(q=_q(keyword))


def app_search_url(keyword: str) -> str:
    return APP_SEARCH_TMPL.format(q=_q(keyword))


def fallback_search_url(keyword: str) -> str:
    """通用搜索引擎兜底入口，免登录可点。"""
    return FALLBACK_TMPL.format(q=_q(keyword + " 小红书"))


def links_for(keyword: str) -> dict:
    """一个关键词的全部入口。"""
    return {
        "keyword": keyword,
        "web": web_search_url(keyword),
        "app": app_search_url(keyword),
        "fallback": fallback_search_url(keyword),
    }


def build_spot_links(spots) -> list:
    """轨道一：名录里每个景区各构造搜索入口。"""
    out = []
    for s in spots:
        name = s.get("名称") if isinstance(s, dict) else str(s)
        item = links_for(name)
        item["名称"] = name
        item["级别"] = s.get("级别") if isinstance(s, dict) else None
        out.append(item)
    return out


def build_keyword_links(place: str, scenes=None) -> list:
    """轨道二：地名 + 场景词泛检索，覆盖非官方网红打卡地。

    place 由调用方按 scope 传入（城市名或省名），使热点检索也随范围分级。
    """
    return [links_for(place + kw) for kw in (scenes or SCENE_KEYWORDS)]


def build_subregion_links(sub_regions, scenes=None) -> list:
    """省域分级热点：给每个州 / 地区各构造精简场景入口。"""
    scenes = scenes or SUBREGION_SCENES
    out, seen = [], set()
    for region in sub_regions:
        region = (region or "").strip()
        if not region or region == "其他" or region in seen:
            continue
        seen.add(region)
        out.append({"区域": region, "tracks": [links_for(region + kw) for kw in scenes]})
    return out
