# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""`.env` 解析的回归测试。

这些用例全部来自 Windows 上真实会发生的存盘方式。它们的共同点是
**静默失效**：程序照跑，只是把「你配了」当成「你没配」，然后在几步之后
以一个看起来毫不相关的错误爆出来（鉴权失败 / 走了 mock / 用了默认模型）。
"""

import io
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travelwise.config import (_parse_dotenv_line, diagnose_dotenv,   # noqa: E402
                               load_dotenv)

KEY = "TRAVELWISE_TEST_KEY"


class TestParseLine(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_parse_dotenv_line("A=b"), ("A", "b"))

    def test_bom_prefix(self):
        """记事本存 UTF-8 默认带 BOM，第一个键会变成 "\ufeffA"。"""
        self.assertEqual(_parse_dotenv_line("\ufeffA=b"), ("A", "b"))

    def test_trailing_comment_is_stripped(self):
        self.assertEqual(_parse_dotenv_line("A=b   # 说明"), ("A", "b"))
        self.assertEqual(_parse_dotenv_line("A=b # 说明"), ("A", "b"))

    def test_hash_inside_quotes_survives(self):
        """密钥里真有 # 的时候，加引号必须能保住它。"""
        self.assertEqual(_parse_dotenv_line('A="b#c"'), ("A", "b#c"))

    def test_export_prefix(self):
        self.assertEqual(_parse_dotenv_line("export A=b"), ("A", "b"))

    def test_spaces_around_equals(self):
        self.assertEqual(_parse_dotenv_line("A = b"), ("A", "b"))

    def test_comment_and_blank_lines_ignored(self):
        self.assertIsNone(_parse_dotenv_line("# 注释"))
        self.assertIsNone(_parse_dotenv_line("   "))
        self.assertIsNone(_parse_dotenv_line("没有等号"))

    def test_empty_value(self):
        self.assertEqual(_parse_dotenv_line("A="), ("A", ""))


class TestLoadDotenv(unittest.TestCase):
    def setUp(self):
        os.environ.pop(KEY, None)

    tearDown = setUp

    def _write(self, tmp, text, encoding="utf-8"):
        path = Path(tmp) / ".env"
        io.open(path, "w", encoding=encoding, newline="").write(text)
        return str(path)

    def test_bom_file_still_loads(self):
        with TemporaryDirectory() as tmp:
            load_dotenv(self._write(tmp, "%s=sk-real\n" % KEY, "utf-8-sig"))
        self.assertEqual(os.environ.get(KEY), "sk-real")

    def test_inline_comment_not_part_of_value(self):
        with TemporaryDirectory() as tmp:
            load_dotenv(self._write(tmp, "%s=sk-real   # 我的key\n" % KEY))
        self.assertEqual(os.environ.get(KEY), "sk-real")

    def test_duplicate_key_empty_first(self):
        """.env 被追加过内容时最常见：上面旧块留空，下面新块填了值。"""
        with TemporaryDirectory() as tmp:
            load_dotenv(self._write(tmp, "%s=\n%s=sk-real\n" % (KEY, KEY)))
        self.assertEqual(os.environ.get(KEY), "sk-real")

    def test_duplicate_key_empty_last_does_not_wipe(self):
        """反过来也不能翻车：下面模板里那行空的不该抹掉上面填好的。"""
        with TemporaryDirectory() as tmp:
            load_dotenv(self._write(tmp, "%s=sk-real\n%s=\n" % (KEY, KEY)))
        self.assertEqual(os.environ.get(KEY), "sk-real")

    def test_duplicate_key_both_filled_last_wins(self):
        with TemporaryDirectory() as tmp:
            load_dotenv(self._write(tmp, "%s=old\n%s=new\n" % (KEY, KEY)))
        self.assertEqual(os.environ.get(KEY), "new")

    def test_existing_env_wins(self):
        os.environ[KEY] = "from-shell"
        with TemporaryDirectory() as tmp:
            load_dotenv(self._write(tmp, "%s=from-file\n" % KEY))
        self.assertEqual(os.environ[KEY], "from-shell")

    def test_missing_file_is_not_an_error(self):
        load_dotenv("/nonexistent/path/.env")       # 不该抛


class TestDiagnose(unittest.TestCase):
    def test_reports_keys_without_leaking_values(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            io.open(path, "w", encoding="utf-8", newline="").write(
                "%s=super-secret-value\n" % KEY)
            report = diagnose_dotenv(str(path))
        self.assertEqual([k["key"] for k in report["keys"]], [KEY])
        self.assertNotIn("super-secret-value", str(report))   # 只报长度，不报值
        self.assertEqual(report["keys"][0]["length"], len("super-secret-value"))

    def test_flags_bom(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            io.open(path, "w", encoding="utf-8-sig", newline="").write("%s=x\n" % KEY)
            report = diagnose_dotenv(str(path))
        self.assertTrue(any("BOM" in p for p in report["problems"]))

    def test_flags_utf16(self):
        """记事本的『Unicode』选项 = UTF-16，整个文件都读不出来。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            io.open(path, "w", encoding="utf-16", newline="").write("%s=x\n" % KEY)
            report = diagnose_dotenv(str(path))
        self.assertTrue(any("UTF-16" in p for p in report["problems"]))

    def test_flags_duplicate_keys(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            io.open(path, "w", encoding="utf-8", newline="").write(
                "%s=\n%s=sk-real\n" % (KEY, KEY))
            report = diagnose_dotenv(str(path))
        problems = " ".join(report["problems"])
        self.assertIn("出现了 2 次", problems)
        self.assertIn("第1行（空值）", problems)

    def test_flags_malformed_line(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            io.open(path, "w", encoding="utf-8", newline="").write("这行没有等号\n")
            report = diagnose_dotenv(str(path))
        self.assertTrue(any("不是合法" in p for p in report["problems"]))

    def test_missing_file_is_reported_not_crashed(self):
        report = diagnose_dotenv("/nonexistent/path/.env")
        self.assertFalse(report["exists"])
        self.assertTrue(report["problems"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
