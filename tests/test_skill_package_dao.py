# -*- coding: utf-8 -*-
"""技能包附属文件与修订史 DAO 测试（2.0 设计 5.2：技能真源 git → 数据库）。

覆盖面：
- ensure_tables 幂等（连跑两次不报错，表与索引就位）；
- put_files 新建/覆盖、返回统计、package_sha256 与 skill_package.serialize_skill_dir
  对同一组文件跨模块一致（canonical 公式锚点）、输入顺序无关；
- list_files 不含 content 大字段、按 path 排序；
- get_file 原字节保真（含二进制）；delete_files 删除计数；
- record/list/latest_revision 修订史（created_at DESC）；
- 非法路径（.. / 绝对路径 / 反斜杠 / 盘符 / 空段）全批拒绝且库内零写入。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from sgme.data import skill_package_dao as dao
from sgme.data.skill_package import serialize_skill_dir


@pytest.fixture
def conn() -> sqlite3.Connection:
    """内存库夹具：每测一个独立连接，ensure_tables 就绪。"""
    c = sqlite3.connect(":memory:")
    dao.ensure_tables(c)
    yield c
    c.close()


def _files(*pairs: tuple[str, bytes]) -> list[dict]:
    """构造 put_files 入参：[{"path", "content"}, ...]。"""
    return [{"path": p, "content": c} for p, c in pairs]


class TestEnsureTables:
    def test_idempotent(self) -> None:
        c = sqlite3.connect(":memory:")
        try:
            dao.ensure_tables(c)
            dao.ensure_tables(c)  # 第二次不报错即幂等
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"skill_files", "skill_revisions"} <= names
            idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert "idx_skill_rev_name" in idx
        finally:
            c.close()


class TestPutFiles:
    def test_create_then_overwrite(self, conn: sqlite3.Connection) -> None:
        r1 = dao.put_files(conn, "my-skill", _files(("references/api.md", b"v1\n")))
        assert r1["written"] == 1
        assert r1["total_bytes"] == 3
        assert dao.get_file(conn, "my-skill", "references/api.md") == b"v1\n"

        r2 = dao.put_files(conn, "my-skill", _files(("references/api.md", b"v2-longer\n")))
        assert r2["written"] == 1
        assert r2["total_bytes"] == 10
        assert dao.get_file(conn, "my-skill", "references/api.md") == b"v2-longer\n"
        rows = dao.list_files(conn, "my-skill")
        assert len(rows) == 1  # 覆盖不新增行
        assert rows[0]["size"] == 10
        assert rows[0]["sha256"] == hashlib.sha256(b"v2-longer\n").hexdigest()
        assert rows[0]["updated_at"]  # 时间戳已记录

    def test_package_sha256_matches_serialize_skill_dir(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """跨模块一致性锚点：DAO 聚合哈希 == serialize_skill_dir 对同一组文件的值。"""
        root = tmp_path / "demo-skill"
        (root / "references").mkdir(parents=True)
        (root / "scripts" / "sub").mkdir(parents=True)
        (root / "references" / "api.md").write_bytes(b"ref content\n")
        (root / "scripts" / "run.sh").write_bytes(b"#!/bin/sh\n")
        (root / "scripts" / "sub" / "deep.py").write_bytes(b"print('deep')\n")
        pkg = serialize_skill_dir(root)
        res = dao.put_files(
            conn,
            "demo-skill",
            [{"path": f["path"], "content": f["content"]} for f in pkg["files"]],
        )
        assert res["written"] == len(pkg["files"])
        assert res["total_bytes"] == sum(f["size"] for f in pkg["files"])
        assert res["package_sha256"] == pkg["package_sha256"]

    def test_batch_hash_order_independent(self, conn: sqlite3.Connection) -> None:
        """同一组文件换输入顺序，package_sha256 不变（canonical 公式内部按 path 字节序重排）。"""
        h1 = dao.put_files(conn, "s1", _files(("b.txt", b"2"), ("a.txt", b"1")))["package_sha256"]
        h2 = dao.put_files(conn, "s2", _files(("a.txt", b"1"), ("b.txt", b"2")))["package_sha256"]
        assert h1 == h2

    def test_rejects_non_bytes_content(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            dao.put_files(conn, "s", [{"path": "a.txt", "content": "文本不是字节"}])
        assert dao.list_files(conn, "s") == []


class TestListAndGet:
    def test_list_files_excludes_content_and_sorted(self, conn: sqlite3.Connection) -> None:
        dao.put_files(
            conn,
            "s",
            _files(
                ("scripts/run.sh", b"#!/bin/sh\n"),
                ("references/api.md", b"ref\n"),
                ("SKILL.md", b"# t\n"),
            ),
        )
        rows = dao.list_files(conn, "s")
        assert [r["path"] for r in rows] == ["SKILL.md", "references/api.md", "scripts/run.sh"]
        for r in rows:
            assert set(r.keys()) == {"path", "size", "sha256", "updated_at"}  # 不含 content
        api = rows[1]
        assert api["size"] == 4
        assert api["sha256"] == hashlib.sha256(b"ref\n").hexdigest()

    def test_get_file_bytes_fidelity(self, conn: sqlite3.Connection) -> None:
        payload = bytes(range(256)) + "中文内容".encode("utf-8")
        dao.put_files(conn, "s", _files(("assets/blob.bin", payload)))
        assert dao.get_file(conn, "s", "assets/blob.bin") == payload

    def test_get_file_missing_returns_none(self, conn: sqlite3.Connection) -> None:
        dao.put_files(conn, "s", _files(("a.txt", b"1")))
        assert dao.get_file(conn, "s", "missing.txt") is None
        assert dao.get_file(conn, "ghost-skill", "a.txt") is None


class TestDeleteFiles:
    def test_delete_returns_count(self, conn: sqlite3.Connection) -> None:
        dao.put_files(conn, "s", _files(("a.txt", b"1"), ("b.txt", b"2"), ("c.txt", b"3")))
        assert dao.delete_files(conn, "s", ["a.txt", "b.txt"]) == 2
        assert [r["path"] for r in dao.list_files(conn, "s")] == ["c.txt"]
        assert dao.delete_files(conn, "s", ["a.txt"]) == 0  # 已删过的再删计 0


class TestRevisions:
    def test_record_list_latest(self, conn: sqlite3.Connection) -> None:
        id1 = dao.record_revision(conn, "s", "1.0.0", "aa" * 32, 3, 100, note="初版入库")
        id2 = dao.record_revision(conn, "s", "1.1.0", "bb" * 32, 4, 200)
        id3 = dao.record_revision(conn, "s", "1.2.0", "cc" * 32, 5, 300)
        assert id1 < id2 < id3  # revision_id 单调递增

        revs = dao.list_revisions(conn, "s")
        assert [r["version"] for r in revs] == ["1.2.0", "1.1.0", "1.0.0"]  # created_at DESC
        assert set(revs[0].keys()) == {
            "revision_id",
            "skill_name",
            "version",
            "package_sha256",
            "file_count",
            "total_bytes",
            "note",
            "created_at",
        }
        assert revs[0]["note"] is None  # 未传 note 存 NULL
        assert revs[2]["note"] == "初版入库"

        latest = dao.latest_revision(conn, "s")
        assert latest is not None
        assert latest["revision_id"] == id3
        assert latest["version"] == "1.2.0"

    def test_list_revisions_limit(self, conn: sqlite3.Connection) -> None:
        for i in range(5):
            dao.record_revision(conn, "s", f"1.0.{i}", "aa" * 32, i, i * 10)
        top2 = dao.list_revisions(conn, "s", limit=2)
        assert [r["version"] for r in top2] == ["1.0.4", "1.0.3"]

    def test_empty_history(self, conn: sqlite3.Connection) -> None:
        assert dao.list_revisions(conn, "ghost") == []
        assert dao.latest_revision(conn, "ghost") is None


class TestPathValidation:
    @pytest.mark.parametrize(
        "bad",
        ["../evil.txt", "/abs.txt", "a\\b.txt", "..", "C:/x.txt", "", "a//b.txt"],
        ids=["parent", "abs", "backslash", "dotdot", "drive", "empty", "empty-seg"],
    )
    def test_put_rejects_illegal_and_writes_nothing(
        self, conn: sqlite3.Connection, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            dao.put_files(conn, "s", _files(("good.txt", b"ok"), (bad, b"evil")))
        # 先全量校验后落库：合法成员也不得部分写入
        assert dao.list_files(conn, "s") == []
        assert conn.execute("SELECT COUNT(*) FROM skill_files").fetchone()[0] == 0

    @pytest.mark.parametrize(
        "bad", ["../evil.txt", "/abs.txt", "a\\b.txt"], ids=["parent", "abs", "backslash"]
    )
    def test_delete_and_get_reject_illegal(self, conn: sqlite3.Connection, bad: str) -> None:
        dao.put_files(conn, "s", _files(("good.txt", b"ok")))
        with pytest.raises(ValueError):
            dao.delete_files(conn, "s", [bad])
        with pytest.raises(ValueError):
            dao.get_file(conn, "s", bad)
        assert dao.get_file(conn, "s", "good.txt") == b"ok"  # 未误伤
