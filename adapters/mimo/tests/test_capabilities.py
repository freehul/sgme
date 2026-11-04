# -*- coding: utf-8 -*-
"""D-58（2026-11-04）：能力面自检（GET /v1/capabilities）单元测试。

口径：api_compat='1.x' 视为匹配；不匹配/端点缺失只返回结构化结果，不抛异常。
测试全部假值（回环地址 + 低熵占位 key）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sgme_client as sc  # noqa: E402

KEY = "agt_" + "placeholder"


class CapabilitiesTest(unittest.TestCase):
    def _client(self) -> "sc.SGME":
        return sc.SGME(base_url="http://127.0.0.1:9910", key=KEY)

    def test_capabilities_match(self):
        c = self._client()
        with mock.patch.object(c, "_http", return_value={
            "api_compat": "1.x", "schema_version": 6,
        }):
            r = c.api_compat_check()
        self.assertTrue(r["ok"])
        self.assertIsNone(r["reason"])
        self.assertEqual(r["schema_version"], 6)

    def test_mismatch_is_structured(self):
        c = self._client()
        with mock.patch.object(c, "_http", return_value={
            "api_compat": "2.x", "schema_version": 7,
        }):
            r = c.api_compat_check()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "mismatch")
        self.assertEqual(r["api_compat"], "2.x")

    def test_unreachable_does_not_raise(self):
        c = self._client()
        with mock.patch.object(c, "_http", side_effect=sc.SGMEError("404")):
            r = c.api_compat_check()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "unreachable")


if __name__ == "__main__":
    unittest.main()
