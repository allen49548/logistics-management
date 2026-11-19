#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：复用 selftest 的脱敏样例，固化端到端行为。

运行： python3 -m unittest discover -s tests -v
"""
import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from selftest import run_selftest  # noqa: E402


class TestPipeline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.report = run_selftest()

    def test_end_to_end(self):
        r = self.report
        self.assertTrue(r.get("ok"),
                        "自测未全通过: passed=%s/%s failed=%s traceback=%s" %
                        (r.get("passed"), r.get("total"), r.get("failed"), r.get("traceback")))
        self.assertEqual(r.get("passed"), r.get("total"))

    def test_checks_have_names(self):
        for c in self.report.get("checks", []):
            self.assertIn("name", c)
            self.assertTrue(c.get("ok"), "失败项: %s" % c.get("name"))


if __name__ == "__main__":
    unittest.main()
