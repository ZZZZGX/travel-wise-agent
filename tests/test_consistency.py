# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""版本号漂移的回归测试。

`_version.py` 的文档里写着「漂移变成一个会失败的测试，而不是一次疏忽」。
这个文件就是兑现那句话的地方。

**这里只测便宜的那部分**（版本号三处一致 + 没有硬编码复辟）。
测试数量 / 评测通过数需要实跑整套测试才能得到，放在测试里会递归自己调自己，
所以那部分交给 `scripts/check_consistency.py`，由 CI 单独跑一步。
分工是：单元测试守「结构」，CI 守「数字」。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from travelwise import _version  # noqa: E402


class TestVersionSingleSource(unittest.TestCase):

    def test_package_version_comes_from_version_module(self):
        """`travelwise.__version__` 必须等于 `_version.VERSION`。"""
        import travelwise
        self.assertEqual(travelwise.__version__, _version.VERSION)

    def test_init_does_not_hardcode_version(self):
        """`__init__.py` 里不许再出现 `__version__ = "x.y.z"` 字面量。

        断言的是**写法**而不是值：值相等可能只是巧合，
        上一版三处数字就曾经短暂地都对过，然后各自漂走。
        """
        text = (ROOT / "src" / "travelwise" / "__init__.py").read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r'^__version__\s*=\s*[\'"]', text, re.M),
            "__init__.py 又开始手写版本号了 —— 应当 from ._version import VERSION")

    def test_pyproject_reads_version_dynamically(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r'^version\s*=\s*"', text, re.M),
            "pyproject.toml 又硬编码 version 了 —— 应当用 dynamic + tool.setuptools.dynamic")
        self.assertIn("travelwise._version.VERSION", text)

    def test_readme_status_badge_matches_version(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r"badge/status-v([0-9][^-)]*?)-", text)
        self.assertIsNotNone(m, "README 里找不到 status badge")
        expected = "%s%%20%s" % (_version.VERSION, _version.STAGE)
        self.assertEqual(
            m.group(1), expected,
            "README badge 与 _version.py 不一致 —— "
            "跑 `python scripts/check_consistency.py --fix`")

    def test_consistency_script_exists(self):
        """`_version.py` 的文档承诺了这个脚本存在。

        上一版它只存在于文档里 —— 文档描述了一个不存在的机制，
        比没有机制更糟：读的人会以为有人在守着。
        """
        self.assertTrue((ROOT / "scripts" / "check_consistency.py").is_file())


if __name__ == "__main__":
    unittest.main()
