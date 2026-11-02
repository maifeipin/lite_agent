#!/usr/bin/env python3
"""
静态资源指纹构建工具自动化测试 (test_build_web_assets.py)。
验证 build_web_assets.py 能够正确计算内容 SHA-256 并注入 ?v=，且不破坏外部 CDN 链接。
"""

import unittest
import tempfile
import re
from pathlib import Path
from scripts.build_web_assets import hash_file, build_assets

class TestBuildWebAssets(unittest.TestCase):
    def test_hash_file(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("console.log('hello world');")
            p = Path(f.name)
        try:
            h = hash_file(p)
            self.assertEqual(len(h), 8)
            self.assertTrue(all(c in "0123456789abcdef" for c in h))
        finally:
            p.unlink()

    def test_build_assets_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            dist = Path(td) / "dist"
            src.mkdir()
            (src / "app.js").write_text("var a = 1;")
            (src / "theme.css").write_text("body { color: red; }")
            (src / "index.html").write_text(
                '<!DOCTYPE html><html><head>'
                '<link rel="stylesheet" href="theme.css">'
                '<link rel="stylesheet" href="https://cdn.example.com/ext.css">'
                '</head><body>'
                '<script src="app.js"></script>'
                '<script src="https://cdn.example.com/ext.js"></script>'
                '</body></html>'
            )

            build_assets(src, dist)
            out_html = (dist / "index.html").read_text()
            self.assertIn('href="theme.css?v=', out_html)
            self.assertIn('src="app.js?v=', out_html)
            # CDN 不受影响
            self.assertIn('href="https://cdn.example.com/ext.css"', out_html)
            self.assertIn('src="https://cdn.example.com/ext.js"', out_html)

if __name__ == "__main__":
    unittest.main()
