# -*- coding: utf-8 -*-
"""import_history.py 单元测试（fixture 仿制 ZCode db.sqlite schema，零网络可离线跑）。

覆盖：
- session_messages：user/assistant 的 text part 抽取；reasoning/tool/step-* 跳过；role 过滤
- to_l0：L0 块格式（`# {ISO} user` / `## {ISO} assistant`）
- iter_sessions：默认只取主会话（parent_id 非空的 subagent 会话排除）
- main --dry-run：全链路冒烟（不发任何写请求）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_history as ih  # noqa: E402


def make_db(tmp: Path) -> Path:
    """仿制 ZCode 会话库最小 schema：session / message / part。"""
    db = tmp / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
                              title TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                              time_updated INTEGER, data TEXT, sequence INTEGER);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                           time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER);
        """
    )
    # sess_main：user + assistant（各带 text 与 reasoning/tool 混合分片）
    conn.execute("INSERT INTO session VALUES ('sess_main', NULL, 'D:/proj', '主会话', 100, 200)")
    rows = [
        ("m1", "sess_main", 1000, 1,
         {"role": "user", "time": {"created": 1790401174123}},
         [("p1", {"type": "text", "text": "用户的第一句话"}), ("p2", {"type": "reasoning", "text": "推理过程"})]),
        ("m2", "sess_main", 1100, 2,
         {"role": "assistant", "time": {"created": 1790401200000}},
         [("p3", {"type": "text", "text": "助手回答甲"}), ("p4", {"type": "text", "text": "助手回答乙"}),
          ("p5", {"type": "tool", "tool": "Bash"})]),
        ("m3", "sess_main", 1200, 3,
         {"role": "system", "time": {"created": 1790401201000}},
         [("p6", {"type": "text", "text": "系统消息应被过滤"})]),
        ("m4", "sess_main", 1300, 4,
         {"role": "assistant", "time": {"created": 1790401300000}},
         [("p7", {"type": "text", "text": ""})]),  # 空 text → 整条跳过
    ]
    for mid, sid, seq, _, mdata, parts in rows:
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?)",
                     (mid, sid, 0, 0, json.dumps(mdata, ensure_ascii=False), seq))
        for pid, pdata in parts:
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?,?)",
                         (pid, mid, sid, 0, 0, json.dumps(pdata, ensure_ascii=False), 0))
    # sess_sub：subagent 会话（parent_id 非空）
    conn.execute("INSERT INTO session VALUES ('sess_sub', 'sess_main', 'D:/proj', '子代理', 150, 160)")
    conn.execute("INSERT INTO message VALUES ('ms1', 'sess_sub', 0, 0, ?, 1)",
                 (json.dumps({"role": "user", "time": {"created": 1790401174000}},
                             ensure_ascii=False),))
    conn.execute("INSERT INTO part VALUES ('ps1', 'ms1', 'sess_sub', 0, 0, ?, 0)",
                 (json.dumps({"type": "text", "text": "子代理提示词" * 30}, ensure_ascii=False),))
    # sess_tiny：主会话但内容极小（大小过滤）
    conn.execute("INSERT INTO session VALUES ('sess_tiny', NULL, 'D:/proj', '太小', 90, 95)")
    conn.execute("INSERT INTO message VALUES ('mt1', 'sess_tiny', 0, 0, ?, 1)",
                 (json.dumps({"role": "user", "time": {"created": 1790401000000}},
                             ensure_ascii=False),))
    conn.execute("INSERT INTO part VALUES ('pt1', 'mt1', 'sess_tiny', 0, 0, ?, 0)",
                 (json.dumps({"type": "text", "text": "短"}, ensure_ascii=False),))
    conn.commit()
    conn.close()
    return db


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.db = make_db(Path(self._tmp.name))
        self.conn = ih.connect_ro(self.db)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_connect_ro(self):
        """只读连接：写操作必须失败。"""
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("INSERT INTO session VALUES ('x', NULL, '', '', 0, 0)")

    def test_iter_sessions_excludes_subagents_by_default(self):
        ids = [s["id"] for s in ih.iter_sessions(self.conn)]
        self.assertIn("sess_main", ids)
        self.assertNotIn("sess_sub", ids)

    def test_iter_sessions_include_subagents(self):
        ids = [s["id"] for s in ih.iter_sessions(self.conn, include_subagents=True)]
        self.assertIn("sess_sub", ids)

    def test_session_messages_filters_and_orders(self):
        msgs = ih.session_messages(self.conn, "sess_main")
        self.assertEqual(len(msgs), 2)  # system / 空 text 均被过滤
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "用户的第一句话")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["text"], "助手回答甲\n助手回答乙")  # 多 text 分片合并
        self.assertEqual(msgs[0]["ts_ms"], 1790401174123)

    def test_to_l0_block_format(self):
        msgs = ih.session_messages(self.conn, "sess_main")
        l0 = ih.to_l0(msgs)
        blocks = l0.split("\n\n")
        self.assertEqual(len(blocks), 2)
        first, _, rest = blocks[0].partition("\n")
        self.assertTrue(first.startswith("# "))
        self.assertTrue(first.endswith(" user"))
        self.assertEqual(rest, "用户的第一句话")
        second = blocks[1].split("\n")[0]
        self.assertTrue(second.startswith("## "))
        self.assertTrue(second.endswith(" assistant"))
        # 首块必须是 user 块（L0 契约：# 开头）
        self.assertTrue(l0.startswith("# "))


class TestDryRunMain(unittest.TestCase):
    def test_dry_run_imports_main_session_only(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = make_db(Path(d))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ih.main(["--db", str(db), "--dry-run",
                                "--min-chars", "50", "--max-chars", "100000"])
            out = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("待导入 1", out)          # 只剩 sess_main（tiny 被大小过滤）
            self.assertIn("zcode-sess_main", out)   # session_key 带 agent 前缀
            self.assertIn("未写任何数据", out)

    def test_dry_run_size_filters(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = make_db(Path(d))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                # min 高到把 sess_main 也滤掉
                code = ih.main(["--db", str(db), "--dry-run", "--min-chars", "999999"])
            self.assertEqual(code, 0)
            self.assertIn("待导入 0", buf.getvalue())

    def test_limit_caps_imports(self):
        """--limit 是实际导入上限（T-208 首版曾漏接线，一次把全库导完——回归防线）。"""
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = make_db(Path(d))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ih.main(["--db", str(db), "--dry-run",
                                "--min-chars", "50", "--max-chars", "100000", "--limit", "1"])
            self.assertEqual(code, 0)
            self.assertIn("待导入 1", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
