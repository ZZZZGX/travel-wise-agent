# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""链接引用记号的单元测试。

重点不是"函数能跑"，而是**这套机制没有把红线变成必绿**：
模型漏写记号仍然会被检出，编造记号也会被检出。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from travelwise.tools import link_refs  # noqa: E402
from travelwise.tools.registry import ToolResult  # noqa: E402


class TestMask(unittest.TestCase):
    def test_replaces_urls_with_refs(self):
        text = "🔗 沈阳打卡 → https://www.xiaohongshu.com/search_result?keyword=abc"
        masked, mapping = link_refs.mask(text)
        self.assertIn("[L1]", masked)
        self.assertNotIn("http", masked)
        self.assertEqual(len(mapping), 1)

    def test_same_url_reuses_ref(self):
        url = "https://example.com/a"
        masked, mapping = link_refs.mask("%s 和 %s" % (url, url))
        self.assertEqual(len(mapping), 1)
        self.assertEqual(masked.count("[L1]"), 2)

    def test_app_scheme_also_masked(self):
        masked, mapping = link_refs.mask("📱 xhsdiscover://search/result?keyword=abc")
        self.assertEqual(len(mapping), 1)
        self.assertFalse(link_refs.is_primary(list(mapping.values())[0]))

    def test_masking_saves_a_lot_of_characters(self):
        """存在的理由就是省 token，那就把这件事测出来。"""
        long_url = "https://www.xiaohongshu.com/search_result?keyword=" + "%E6%B2%88" * 6
        masked, _ = link_refs.mask(long_url)
        self.assertLess(len(masked) * 5, len(long_url))


class TestRestore(unittest.TestCase):
    def setUp(self):
        self.mapping = {"L1": "https://a.example/1",
                        "L2": "https://a.example/2",
                        "L3": "xhsdiscover://x"}

    def test_full_answer_restores_all(self):
        out, stats = link_refs.restore("看 [L1] 和 [L2] 还有 [L3]", self.mapping)
        self.assertIn("https://a.example/1", out)
        self.assertEqual(stats.missing, [])
        self.assertEqual(stats.unknown, [])

    def test_missing_ref_is_detected(self):
        """核心断言：改成记号并没有让红线变成必绿。"""
        _, stats = link_refs.restore("只给 [L1]", self.mapping)
        self.assertEqual(stats.missing_primary, ["L2"])

    def test_claiming_without_writing_any_ref(self):
        _, stats = link_refs.restore("链接已准备好，点开即可浏览", self.mapping)
        self.assertEqual(len(stats.missing_primary), 2)
        self.assertEqual(stats.present, [])

    def test_invented_ref_is_flagged(self):
        _, stats = link_refs.restore("[L1] [L2] [L3] [L99]", self.mapping)
        self.assertEqual(stats.unknown, ["L99"])

    def test_invented_ref_is_not_silently_expanded(self):
        out, _ = link_refs.restore("[L99]", self.mapping)
        self.assertIn("[L99]", out)


class TestPrivatePayloadKeys(unittest.TestCase):
    def test_underscore_keys_never_reach_the_model(self):
        r = ToolResult(name="t", ok=True,
                       content={"report": "x", "_link_map": {"L1": "https://a"}})
        payload = r.to_model_payload()
        self.assertIn("report", payload)
        self.assertNotIn("_link_map", payload)
        self.assertNotIn("https://a", str(payload))


if __name__ == "__main__":
    unittest.main()
