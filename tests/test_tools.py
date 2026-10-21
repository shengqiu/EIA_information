# -*- coding: utf-8 -*-
"""Unit tests for tools/fetch_remote.py, monitor.py, and rendering logic."""

import unittest
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fetch_remote as fr
import monitor as mon


class TestTools(unittest.TestCase):

    def test_fetch_remote_real_oss(self):
        self.assertTrue(fr.real_oss("https://zjjcmspublicnew.oss-cn-hangzhou.com/a.pdf"))
        self.assertFalse(fr.real_oss("https://hbj.huzhou.gov.cn/api-gateway/download?fileUrl=xxx"))
        self.assertFalse(fr.real_oss(""))

    def test_upsert_links(self):
        rows = [
            {"公告日期": "2026-01-01", "发布单位": "市局", "项目名称": "项目A",
             "建设单位": "单位A", "文件名": "a.pdf", "大小": "1MB",
             "OSS直链": "https://oss.com/a.pdf", "网关地址": "https://gw.com/a"}
        ]
        links = [
            {"公告日期": "2026-01-01", "发布单位": "市局", "项目名称": "项目A",
             "建设单位": "单位A", "文件名": "a.pdf", "大小": "1.2MB",
             "OSS直链": "https://oss.com/a_v2.pdf", "网关地址": "https://gw.com/a"},
            {"公告日期": "2026-01-02", "发布单位": "吴兴分局", "项目名称": "项目B",
             "建设单位": "单位B", "文件名": "b.pdf", "大小": "2MB",
             "OSS直链": "https://oss.com/b.pdf", "网关地址": "https://gw.com/b"}
        ]

        added, updated = fr.upsert_links(rows, links)
        self.assertEqual(added, 1)
        self.assertEqual(updated, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["OSS直链"], "https://oss.com/a_v2.pdf")

    def test_build_html(self):
        sample_items = [
            {
                "art_id": "test_001",
                "title": "测试公示项目",
                "unit": "市局",
                "category": "非辐射环评审批公示",
                "category_code": "nrad",
                "类型": "拟审批",
                "区域": "湖州市",
                "发布日期": "2026-01-01",
                "url": "https://hbj.huzhou.gov.cn/art/1",
                "项目": [
                    {
                        "项目名称": "测试项目1",
                        "地点": "湖州市",
                        "建设单位": "测试单位",
                        "环评机构": "测试机构",
                        "附件链接": ["https://gw.com/1"]
                    }
                ]
            }
        ]
        html = mon.build_html(sample_items, {"test_001"}, {"total": 1})
        self.assertIn("测试公示项目", html)
        self.assertIn('"unit": "市局"', html)
        self.assertIn("const DATA =", html)


if __name__ == "__main__":
    unittest.main()
