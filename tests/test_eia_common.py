# -*- coding: utf-8 -*-
"""Unit tests for tools/eia_common.py."""

import unittest
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import eia_common as ec


class TestEiaCommon(unittest.TestCase):

    def test_sanitize(self):
        self.assertEqual(ec.sanitize("  abc/def:ghi  "), "abc_def_ghi")
        self.assertEqual(ec.sanitize(""), "未命名")
        self.assertEqual(ec.sanitize("a" * 100, maxlen=10), "a" * 10)

    def test_parse_size(self):
        self.assertEqual(ec.parse_size("1024B"), 1024)
        self.assertEqual(ec.parse_size("1KB"), 1024)
        self.assertEqual(ec.parse_size("1.5MB"), int(1.5 * 1024 * 1024))
        self.assertEqual(ec.parse_size("1GB"), 1024**3)
        self.assertEqual(ec.parse_size("invalid"), 0)
        self.assertEqual(ec.parse_size(""), 0)

    def test_human(self):
        self.assertEqual(ec.human(500), "500B")
        self.assertEqual(ec.human(1024), "1.0KB")
        self.assertEqual(ec.human(1024 * 1024), "1.0MB")

    def test_is_real_oss(self):
        self.assertTrue(ec.is_real_oss("https://oss-cn-hangzhou.aliyuncs.com/doc.pdf"))
        self.assertFalse(ec.is_real_oss("https://hbj.huzhou.gov.cn/download?fileUrl=abc"))
        self.assertFalse(ec.is_real_oss("https://hbj.huzhou.gov.cn/api-gateway/download"))
        self.assertFalse(ec.is_real_oss(""))

    def test_clip_bytes(self):
        self.assertEqual(ec.clip_bytes("test.pdf", max_bytes=100), "test.pdf")
        # Long Chinese filename clipped by byte limit
        long_name = "这是一个非常非常长的环评公告项目名称全本报告表文件.pdf"
        clipped = ec.clip_bytes(long_name, max_bytes=30)
        self.assertLessEqual(len(clipped.encode("utf-8")), 30)
        self.assertTrue(clipped.endswith(".pdf"))

    def test_dedupe(self):
        used = {"test.pdf"}
        name2 = ec.dedupe("test.pdf", used)
        self.assertEqual(name2, "test_2.pdf")

        used.add("test_2.pdf")
        name3 = ec.dedupe("test.pdf", used)
        self.assertEqual(name3, "test_3.pdf")

    def test_sniff_archive_kind(self):
        import tempfile
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"PK\x03\x04testdata")
            tmp_path = f.name
        try:
            self.assertEqual(ec.sniff_archive_kind(tmp_path), "zip")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"Rar!\x1a\x07\x00")
            tmp_path2 = f.name
        try:
            self.assertEqual(ec.sniff_archive_kind(tmp_path2), "other")
        finally:
            if os.path.exists(tmp_path2):
                os.remove(tmp_path2)


if __name__ == "__main__":
    unittest.main()
