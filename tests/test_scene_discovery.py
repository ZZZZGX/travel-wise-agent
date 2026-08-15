# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""景区二层发现的单元测试。

测的不是「函数能跑」，而是**这个功能的失败模式有没有被挡住**。
二层发现只有三种糟糕的失败方式，每一种都对应下面一组测试：

  1. **抽出不存在的地方**（"宝藏公园"、"一条街"、"尽头是翠湖公园"）——
     用户拿去搜什么都搜不到，比不给还差。→ TestExtractRules
  2. **假装做了发现**（没搜索源却输出一份清单）——
     那份清单只能是编的。→ TestDegradation
  3. **重复付费**（同一天同一城市反复调接口）。→ TestCache

外加两组保证「结果可用」的测试：合并同一地点的不同写法（TestMerge），
以及整条链路接得上（TestEndToEnd）。

全部离线运行：搜索源用 FixtureWebSearchProvider 回放本地 JSON，
0 次网络调用、0 元、0 token。
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from travelwise.providers.base import ProviderError                    # noqa: E402
from travelwise.providers.web_search import (FixtureWebSearchProvider,  # noqa: E402
                                             HttpWebSearchProvider,
                                             NullWebSearchProvider,
                                             SearchHit)
from travelwise.skills.destination import DestinationSkill             # noqa: E402
from travelwise.tools import destination_search, scene_discovery, spot_extract  # noqa: E402

FIXTURE = ROOT / "data" / "fixtures" / "scene_search.json"
TODAY = date(2026, 8, 14)


def hit(title, snippet="", url="", source="昆明 打卡"):
    return SearchHit(title=title, snippet=snippet, url=url, source=source)


# ---------------------------------------------------------------- 抽取规则
class TestExtractRules(unittest.TestCase):
    """一条一条钉死「什么算地名、什么不算」。"""

    def test_picks_place_names_out_of_a_real_title(self):
        names = spot_extract.extract_from_text(
            "昆明这5个出片机位，第3个绝了！翠湖公园的红嘴鸥太治愈", city="昆明")
        self.assertIn("翠湖公园", names)

    def test_marketing_words_are_not_places(self):
        """"宝藏公园""出片机位"是标题的形容词，不是能搜到的地方。"""
        names = spot_extract.extract_from_text("昆明宝藏公园合集｜必去打卡机位", city="昆明")
        self.assertEqual([n for n in names if "宝藏" in n or "机位" in n], [])

    def test_bare_suffix_is_not_a_place(self):
        """"公园"本身不是地名。"""
        self.assertNotIn("公园", spot_extract.extract_from_text("公园很多，随便逛逛"))

    def test_verbs_do_not_bleed_into_the_name(self):
        """"尽头是翠湖公园"→ 只要"翠湖公园"。粘着半句话的名字搜不到。"""
        names = spot_extract.extract_from_text(
            "从文林街出发，经过钱局街，尽头是翠湖公园", city="昆明")
        self.assertIn("翠湖公园", names)
        self.assertIn("钱局街", names)
        self.assertFalse(any(n.startswith("是") or n.startswith("过") for n in names))

    def test_measure_phrases_are_not_places(self):
        """"一条街"是数量短语。"""
        names = spot_extract.extract_from_text("文林街到钱局街，一条街喝三家咖啡", city="昆明")
        self.assertNotIn("一条街", names)
        self.assertIn("钱局街", names)

    def test_city_prefix_is_stripped(self):
        """"昆明翠湖公园"和"翠湖公园"是同一个地方，不该占两行。"""
        names = spot_extract.extract_from_text("推荐昆明翠湖公园", city="昆明")
        self.assertIn("翠湖公园", names)
        self.assertNotIn("昆明翠湖公园", names)

    def test_two_adjacent_places_do_not_merge_into_one(self):
        """"西山森林公园可以俯瞰滇池"→ 两个地名，不是一个叫"林公园可以俯瞰滇池"的地方。"""
        names = spot_extract.extract_from_text("西山森林公园可以俯瞰滇池", city="昆明")
        self.assertIn("西山森林公园", names)
        self.assertIn("滇池", names)
        self.assertFalse(any(len(n) > 6 and "俯瞰" in n for n in names))

    def test_catalog_names_are_matched_whole_even_without_a_suffix(self):
        """名录里的名字整词命中，补规则抽取的召回缺口。"""
        names = spot_extract.extract_from_text(
            "九乡值得去", city="昆明", extra_names=("九乡",))
        self.assertIn("九乡", names)


class TestMentionThreshold(unittest.TestCase):
    def test_single_mention_is_dropped_by_default(self):
        """只有一篇提过 = 可能是那位作者的私人叫法，也可能是抽错了。"""
        hits = [hit("翠湖公园很好", url="u1"), hit("某某小院不错", url="u2")]
        names = [c.name for c in spot_extract.extract_candidates(hits, city="昆明")]
        self.assertEqual(names, [])

    def test_two_mentions_pass(self):
        hits = [hit("翠湖公园很好", url="u1"), hit("翠湖公园的红嘴鸥", url="u2")]
        cands = spot_extract.extract_candidates(hits, city="昆明")
        self.assertEqual([c.name for c in cands], ["翠湖公园"])
        self.assertEqual(cands[0].mentions, 2)

    def test_same_result_mentioning_twice_is_still_one_piece_of_evidence(self):
        """同一篇里出现十次仍然只是一条证据。"""
        hits = [hit("翠湖公园", "翠湖公园真的好，翠湖公园必去", url="u1")]
        cands = spot_extract.extract_candidates(hits, city="昆明", min_mentions=1)
        self.assertEqual(cands[0].mentions, 1)


class TestMerge(unittest.TestCase):
    def test_short_prefix_merges_into_the_longer_name(self):
        """翠湖 ⊂ 翠湖公园 → 保留更具体的长名。"""
        hits = [hit("翠湖公园", url="u1"), hit("翠湖公园好逛", url="u2"),
                hit("翠湖边散步", "翠湖", url="u3")]
        cands = spot_extract.extract_candidates(hits, city="昆明", min_mentions=2)
        self.assertEqual([c.name for c in cands], ["翠湖公园"])
        self.assertIn("翠湖", cands[0].aliases)

    def test_context_prefix_is_dropped_in_favour_of_the_short_name(self):
        """滇池海埂大坝 ⊃ 海埂大坝 → "滇池"是上下文，不是名字的一部分。"""
        hits = [hit("海埂大坝看日落", url="u1"), hit("海埂大坝地铁直达", url="u2"),
                hit("滇池海埂大坝", url="u3")]
        names = [c.name for c in
                 spot_extract.extract_candidates(hits, city="昆明", min_mentions=2)]
        self.assertIn("海埂大坝", names)
        self.assertNotIn("滇池海埂大坝", names)


class TestOrdering(unittest.TestCase):
    def test_sorted_by_mentions_then_stable(self):
        hits = [hit("翠湖公园 官渡古镇", url="u%d" % i) for i in range(3)]
        hits.append(hit("翠湖公园", url="u9"))
        cands = spot_extract.extract_candidates(hits, city="昆明", min_mentions=2)
        self.assertEqual([c.name for c in cands], ["翠湖公园", "官渡古镇"])


# ---------------------------------------------------------- 降级与「不假装」
class TestDegradation(unittest.TestCase):
    """没搜索源时**必须如实说未启用**，绝不编一份清单顶上。"""

    def test_null_provider_reports_disabled_with_a_reason(self):
        res = scene_discovery.discover("昆明", NullWebSearchProvider(), today=TODAY)
        self.assertFalse(res["enabled"])
        self.assertEqual(res["spots"], [])
        self.assertIn("未启用", res["reason"])
        self.assertEqual(res["api_calls"], 0)

    def test_none_provider_behaves_like_null(self):
        res = scene_discovery.discover("昆明", None, today=TODAY)
        self.assertFalse(res["enabled"])
        self.assertEqual(res["spots"], [])

    def test_render_says_disabled_out_loud(self):
        text = "\n".join(scene_discovery.render(
            scene_discovery.discover("昆明", None, today=TODAY)))
        self.assertIn("未启用", text)

    def test_one_failing_scene_does_not_kill_the_rest_but_is_reported(self):
        """少搜了一轮 = 少了一批候选，用户有权知道结果不完整。"""
        class Flaky(FixtureWebSearchProvider):
            def search(self, query, limit=10):
                if "出片" in query:
                    raise ProviderError("429 限流")
                return super().search(query, limit)

        res = scene_discovery.discover("昆明", Flaky(str(FIXTURE)), today=TODAY,
                                       use_cache=False)
        self.assertTrue(res["enabled"])
        self.assertEqual(res["api_calls"], 3)          # 4 个场景词，成功 3 个
        self.assertTrue(any("出片" in e for e in res["errors"]))
        self.assertIn("结果不完整", "\n".join(scene_discovery.render(res)))

    def test_missing_fixture_file_degrades_instead_of_crashing(self):
        from travelwise.config import Settings, build_web_search_provider
        settings = Settings(search_provider="fixture",
                            search_fixtures="/no/such/file.json")
        provider = build_web_search_provider(settings)
        self.assertFalse(provider.enabled)
        self.assertIn("不存在", provider.reason)


# ------------------------------------------------------------------- 缓存
class TestCache(unittest.TestCase):
    def test_second_run_costs_nothing(self):
        """同一天同一城市重复跑不该重复付费。"""
        with tempfile.TemporaryDirectory() as tmp:
            original = scene_discovery.CACHE_DIR
            scene_discovery.CACHE_DIR = Path(tmp)
            try:
                provider = FixtureWebSearchProvider(str(FIXTURE))
                first = scene_discovery.discover("昆明", provider, today=TODAY)
                second = scene_discovery.discover("昆明", provider, today=TODAY)
            finally:
                scene_discovery.CACHE_DIR = original
        self.assertEqual(first["api_calls"], 4)
        self.assertFalse(first["cached"])
        self.assertEqual(second["api_calls"], 0)
        self.assertTrue(second["cached"])
        self.assertEqual([s["名称"] for s in first["spots"]],
                         [s["名称"] for s in second["spots"]])


# ------------------------------------------------------------ HTTP 源解析
class TestHttpProviderShape(unittest.TestCase):
    """不联网，只测**配置怎么变成请求、响应怎么变成 SearchHit**。"""

    CFG = {"endpoint": "https://api.example.com/search", "method": "POST",
           "body_format": "json",
           "params": {"query_key": "query", "count_key": "count"},
           "extra_params": {"summary": True},
           "auth": {"type": "header", "key": "Authorization", "prefix": "Bearer",
                    "value_env": "TRAVELWISE_SEARCH_TOKEN"},
           "response": {"list_path": "data.webPages.value",
                        "field_map": {"title": "name", "snippet": "snippet",
                                      "url": "url"}}}

    def test_payload_and_auth_header(self):
        p = HttpWebSearchProvider(self.CFG, token="k123")
        self.assertEqual(p._payload("昆明 打卡", 8),
                         {"summary": True, "query": "昆明 打卡", "count": 8})
        self.assertEqual(p._headers()["Authorization"], "Bearer k123")

    def test_endpoint_without_config_is_a_hard_error(self):
        with self.assertRaises(ProviderError):
            HttpWebSearchProvider({})

    def test_nested_response_path_is_dug_out(self):
        from travelwise.providers.web_search import _dig
        body = {"data": {"webPages": {"value": [
            {"name": "标题", "snippet": "摘要", "url": "https://x"}]}}}
        items = _dig(body, "data.webPages.value", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "标题")


# --------------------------------------------------------------- 端到端
class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.provider = FixtureWebSearchProvider(str(FIXTURE))

    def test_curate_attaches_a_discovery_block(self):
        data = destination_search.curate(
            "昆明", scope="city", search_provider=self.provider, today=TODAY)
        disc = data["discovery"]
        self.assertTrue(disc["enabled"])
        self.assertGreaterEqual(len(disc["spots"]), 5)
        names = [s["名称"] for s in disc["spots"]]
        self.assertIn("翠湖公园", names)
        self.assertIn("钱局街", names)

    def test_every_discovered_spot_carries_a_working_keyword_link(self):
        """二层的产物必须是**可点的地点入口**，且关键词带上城市名。"""
        data = destination_search.curate(
            "昆明", scope="city", search_provider=self.provider, today=TODAY)
        for s in data["discovery"]["spots"]:
            self.assertTrue(s["links"]["web"].startswith("https://"))
            self.assertIn("keyword=", s["links"]["web"])
            self.assertTrue(s["keyword"].startswith("昆明"))

    def test_catalog_names_are_flagged_despite_administrative_prefixes(self):
        """名录写「昆明市西山森林公园」，帖子写「西山森林公园」，必须对得上。"""
        data = destination_search.curate(
            "昆明", scope="city", search_provider=self.provider, today=TODAY)
        by_name = {s["名称"]: s for s in data["discovery"]["spots"]}
        self.assertTrue(by_name["西山森林公园"]["在名录内"])
        self.assertFalse(by_name["钱局街"]["在名录内"])      # 街不是 A 级景区

    def test_discovery_appears_after_the_keyword_track_in_the_report(self):
        """排序即定位：一层是入口，二层是结论，结论在后、且明确标注来源。"""
        data = destination_search.curate(
            "昆明", scope="city", search_provider=self.provider, today=TODAY)
        text = destination_search.render(data)
        self.assertIn("场景发现（二层", text)
        self.assertLess(text.index("按场景检索（一层"), text.index("场景发现（二层"))
        self.assertIn("归纳", text)                       # 说明来源的免责句

    def test_skill_reports_discovery_status(self):
        skill = DestinationSkill(self.provider)
        res = skill.run("昆明", scope="city", today=TODAY)
        self.assertTrue(res["ok"])
        self.assertTrue(res["discovery_enabled"])
        self.assertGreater(res["discovered_count"], 0)

    def test_skill_without_provider_keeps_old_behaviour(self):
        """不给搜索源 = 与改造前完全一致的一层行为，且明说二层没跑。"""
        res = DestinationSkill().run("昆明", scope="city", today=TODAY)
        self.assertTrue(res["ok"])
        self.assertFalse(res["discovery_enabled"])
        self.assertEqual(res["discovered_count"], 0)
        self.assertGreater(res["official_count"], 0)      # 名录轨照常
        self.assertIn("未启用", res["text"])

    def test_registry_tool_exposes_discovery_flags(self):
        from travelwise.skills.flight import FlightSkill
        from travelwise.providers.mock_flight import MockFlightProvider
        from travelwise.tools.registry import build_registry
        registry = build_registry(FlightSkill(MockFlightProvider()),
                                  DestinationSkill(self.provider), today=TODAY)
        out = registry.call("search_destination", {"place": "昆明", "scope": "city"})
        self.assertTrue(out.ok)
        self.assertTrue(out.content["discovery_enabled"])
        self.assertGreater(out.content["discovered_count"], 0)
        # 链接仍然走 [L1] 记号，二层没有绕过省 token 的机制
        self.assertNotIn("http", out.content["report"])
        self.assertIn("[L1]", out.content["report"])

    def test_another_city_works_too(self):
        data = destination_search.curate(
            "大理", scope="city", search_provider=self.provider, today=TODAY)
        names = [s["名称"] for s in data["discovery"]["spots"]]
        self.assertIn("喜洲古镇", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
