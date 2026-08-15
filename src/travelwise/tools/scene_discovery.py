# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""scene_discovery.py —— 二层发现：先搜场景，再抽地点，最后按地点给入口。

## 一层和二层的区别

一层（原有行为）给的是**话题入口**：

    昆明打卡 → https://www.xiaohongshu.com/search_result?keyword=昆明打卡

用户点进去还要自己读几十篇帖子，判断哪个地方值得去——攻略的活没省掉，
只省了敲字。

二层给的是**地点入口**：

    翠湖公园（7 条结果提到）→ .../search_result?keyword=昆明翠湖公园
    海埂大坝（4 条结果提到）→ .../search_result?keyword=昆明海埂大坝

中间那步「读几十篇标题、把地名挑出来」由程序做掉。用户拿到的是一份
**已经收敛过的地点清单**，再点进去看的是某一个地方的帖子，
而不是这个城市的所有帖子。

## 三条硬约束

  1. **不替用户选帖子。** 二层输出的仍然是每个地点的搜索结果页，
     不是某一篇帖子。程序做的是「归纳出现了哪些地方」，不是「评价哪篇好」。
  2. **每个地点都带证据。** 提及次数 + 出处标题。用户可以自己判断
     「只有 2 篇提过」和「11 篇都提」的差别，程序不代替这个判断。
  3. **没搜索源就如实说未启用。** 不用模型编一份地点清单顶上——
     编出来的地名搜不到，比不给更糟。

## 花费

每个场景词 = 1 次搜索调用。默认 4 个场景词 = 4 次。
同一天同一城市的结果落盘缓存，重复跑不再付费。
抽取过程是纯 Python，**0 token**。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..paths import DATA_DIR
from . import search_links, spot_extract

#: 默认场景词。少而互补：覆盖「出片 / 小众 / 城市漫步 / 必去」四个不同人群，
#: 再多就是重复付费——它们的搜索结果重合度很高。
DEFAULT_SCENES: tuple[str, ...] = ("打卡", "出片", "小众景点", "citywalk")

CACHE_DIR = DATA_DIR / "cache" / "discover"


def _cache_path(place: str, day: str, scenes: tuple) -> Path:
    tag = "-".join(scenes)
    safe = "".join(ch for ch in "%s-%s" % (place, tag) if ch not in '\\/:*?"<>|')
    return CACHE_DIR / ("%s-%s.json" % (safe, day))


def _load_cache(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_cache(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except OSError:
        pass          # 缓存写不进去不影响主流程，不因此报错


#: 名录名常带行政前缀（"昆明市西山森林公园"），社交平台上没人这么写。
#: 不归一化的后果：抽出来的"西山森林公园"对不上名录，
#: 于是整份清单全被标成"名录外"，"哪些是名录里本来就有的"这个信息就废了。
_ADMIN_SUFFIX = ("市", "县", "区", "州")


def normalize_catalog(catalog: dict, place: str) -> dict:
    """把名录名的行政前缀剥掉，原名与短名都留在表里，两种写法都能命中。"""
    out = dict(catalog or {})
    for name, level in list((catalog or {}).items()):
        short = name
        for prefix in ([place + s for s in _ADMIN_SUFFIX] + [place] if place else []):
            if short.startswith(prefix) and len(short) - len(prefix) >= 2:
                short = short[len(prefix):]
                break
        if short != name and short not in out:
            out[short] = level
    return out


def _keyword_for(place: str, name: str) -> str:
    """地点搜索词。地名里没带城市就补上——「文林街」全国有好几条。"""
    return name if place and place in name else "%s%s" % (place, name)


class _Hit:
    """轻量壳：缓存回放时不必再造一个 provider。"""

    __slots__ = ("title", "snippet", "url", "source")

    def __init__(self, d: dict):
        self.title = d.get("title", "")
        self.snippet = d.get("snippet", "")
        self.url = d.get("url", "")
        self.source = d.get("source", "")

    @property
    def text(self) -> str:
        return "%s %s" % (self.title, self.snippet)


def discover(place: str, provider, scenes=None, per_query: int = 10,
             min_mentions: int = 2, catalog: dict | None = None,
             max_spots: int = 15, today=None, use_cache: bool = True) -> dict:
    """跑完整的二层：搜索 → 抽取 → 给每个地点配入口。

    provider 为 None 或未启用时返回 `{"enabled": False, "reason": ...}`，
    调用方据此如实告知用户，而不是假装做了这一步。
    """
    scenes = tuple(scenes or DEFAULT_SCENES)
    day = today if isinstance(today, str) else (today or date.today()).isoformat()

    if provider is None or not getattr(provider, "enabled", False):
        reason = getattr(provider, "reason", "") or "未配置网页搜索源，二层发现未启用。"
        return {"enabled": False, "reason": reason, "place": place,
                "scenes": list(scenes), "spots": [], "queries": [],
                "hits": 0, "api_calls": 0, "cached": False, "errors": [],
                "candidates_total": 0, "min_mentions": min_mentions,
                "provider": getattr(provider, "name", "none")}

    path = _cache_path(place, day, scenes)
    cached_payload = _load_cache(path) if use_cache else None
    queries = ["%s %s" % (place, s) for s in scenes]

    raw: list[dict] = []
    api_calls = 0
    errors: list[str] = []
    if cached_payload:
        raw = cached_payload.get("hits") or []
    else:
        for q in queries:
            try:
                hits = provider.search(q, limit=per_query)
            except Exception as e:                     # noqa: BLE001
                # 一个场景词失败不该让整件事失败，但**必须报出来**：
                # 少搜了一轮 = 少了一批候选，用户有权知道结果不完整。
                errors.append("%s：%s" % (q, str(e)[:80]))
                continue
            api_calls += 1
            for h in hits:
                raw.append({"title": h.title, "snippet": h.snippet,
                            "url": h.url, "source": h.source})
        if raw and use_cache:
            _save_cache(path, {"place": place, "day": day, "scenes": list(scenes),
                               "hits": raw})

    hits = [_Hit(d) for d in raw]
    catalog = normalize_catalog(catalog or {}, place)
    cands = spot_extract.extract_candidates(
        hits, city=place, min_mentions=min_mentions, catalog=catalog,
        extra_names=tuple(catalog.keys()))

    spots = []
    for c in cands[:max_spots]:
        keyword = _keyword_for(place, c.name)
        item = c.to_dict()
        item["keyword"] = keyword
        item["links"] = search_links.links_for(keyword)
        spots.append(item)

    return {"enabled": True, "reason": "", "place": place, "scenes": list(scenes),
            "queries": queries, "spots": spots, "hits": len(hits),
            "candidates_total": len(cands), "api_calls": api_calls,
            "cached": bool(cached_payload), "errors": errors,
            "min_mentions": min_mentions,
            "provider": getattr(provider, "name", "?")}


def render(result: dict) -> list[str]:
    """渲染二层区块。返回行列表，由 destination_search.render 拼进整体输出。"""
    if not result:
        return []

    L = ["=== 场景发现（二层：先搜后抽，直接给地点） ==="]
    if not result.get("enabled"):
        L.append("")
        L.append("⚠️ %s" % result.get("reason", "二层发现未启用。"))
        L.append("上面的场景关键词入口仍然可用，只是需要你自己在结果页里挑地方。")
        return L

    L.append("")
    src = "缓存（今天已搜过，未再付费）" if result.get("cached") else \
        "实时搜索 %d 次" % result.get("api_calls", 0)
    L.append("检索 %d 个场景词 · 读到 %d 条结果 · %s ｜ 抽取地点 %d 个"
             "（只保留被 ≥%d 条结果提到的）"
             % (len(result.get("scenes") or []), result.get("hits", 0), src,
                result.get("candidates_total", 0), result.get("min_mentions", 2)))
    for err in result.get("errors") or []:
        L.append("⚠️ 有场景词没搜成功，结果不完整：%s" % err)

    if not result["spots"]:
        L.append("")
        L.append("（没有地点被两条以上结果同时提到——可能是这个城市的内容太少，"
                 "或搜索源返回的标题不含地名。可以把 min_mentions 降到 1 再看。）")
        return L

    L.append("")
    for i, s in enumerate(result["spots"], 1):
        tags = []
        if s.get("在名录内"):
            tags.append("名录内 %s" % (s.get("级别") or ""))
        else:
            tags.append("名录外·社交平台热点")
        if s.get("别名"):
            tags.append("又写作 " + "/".join(s["别名"]))
        L.append("%d. %s（被 %d 条结果提到 ｜ %s）"
                 % (i, s["名称"], s["提及次数"], "｜".join(tags)))
        L.append("   🔗 %s" % s["links"]["web"])
        L.append("   📱 %s" % s["links"]["app"])

    L.append("")
    L.append("说明：地点是从搜索结果的标题 / 摘要里**归纳**出来的，"
             "提及次数只代表被提到的多少，不代表好坏；链接仍是该地点的搜索结果页，"
             "具体去不去、看哪篇，由你自己判断。")
    return L
