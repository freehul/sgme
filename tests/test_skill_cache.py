"""技能落盘缓存服务端接口测试（2.0 D-59：两级缓存的服务端三函数）。

契约（主 agent 接线为 HTTP 端点；本文件即契约的 executable spec）：
- catalog_version(conn) -> str：skills 表全部 (name, sha256) 按 name 排序拼接后取
  sha256 前 16 位十六进制；空表返回 ``empty``。
- skill_manifest(conn, name) -> dict | None：{"name", "version", "package_sha256",
  "content_len", "updated_at"}；其中 package_sha256 = 确定性包字节的 sha256
  （客户端两级缓存第 2 级：比对它决定是否重新拉包，下载后还能完整性校验）。
- skill_package(conn, name) -> bytes | None：DB 行打确定性 tar.gz（内存中），
  成员排序固定、mtime 固定 0、gzip 头 mtime 也为 0——同内容产出同字节。

三函数必须**只读**：用 ``mode=ro`` 只读连接硬性强制（任何写入直接抛错）。
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from sgme.data import db as db_mod
from sgme.data import skills_dao
from sgme.operations import skill_cache


def _seed(conn: sqlite3.Connection, name: str, content: str, sha: str | None = None,
          version: str = "1.0.0", description: str = "", category: str = "general",
          updated_at: str = "2026-01-01T00:00:00Z") -> None:
    """经 DAO 写一条技能（用与生产一致的 upsert 路径造数据）。"""
    skills_dao.upsert_skill(conn, {
        "name": name,
        "sha256": sha or ("sha-" + name),
        "description": description,
        "tags": ["skill"],
        "category": category,
        "version": version,
        "pattern": "manual",
        "source": "git",
        "origin_path": f"/tmp/{name}/SKILL.md",
        "content": content,
        "uses": [],
    })
    # upsert_skill 会以 _now() 覆盖 updated_at（与生产索引路径一致），测试显式盖回固定值
    conn.execute("UPDATE skills SET updated_at = ? WHERE name = ?", (updated_at, name))
    conn.commit()


@pytest.fixture()
def conn(tmp_path: Path):
    """可写 skills.db（造数用）。"""
    c = db_mod.connect_skills(tmp_path)
    yield c
    c.close()


@pytest.fixture()
def ro_conn(tmp_path: Path):
    """已造 2 条技能数据的**只读**连接——实现若写库，sqlite 立即抛错。"""
    w = db_mod.connect_skills(tmp_path)
    try:
        _seed(w, "pdf", "# PDF\n处理 pdf 表格", sha="sha-pdf", version="1.2.0",
              description="PDF 技能", updated_at="2026-05-05T01:02:03Z")
        _seed(w, "wiki", "# Wiki\n查 wiki", sha="sha-wiki", version="2.0.0",
              description="Wiki 技能", updated_at="2026-06-06T00:00:00Z")
    finally:
        w.close()
    c = sqlite3.connect(f"file:{(tmp_path / 'skills.db').as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ---------- catalog_version ----------


class TestCatalogVersion:
    def test_empty_table_returns_empty_literal(self, conn):
        """空表 → 字面量 'empty'（不是空串/哈希）。"""
        assert skill_cache.catalog_version(conn) == "empty"

    def test_returns_16_lowercase_hex_chars(self, ro_conn):
        v = skill_cache.catalog_version(ro_conn)
        assert len(v) == 16
        int(v, 16)  # 必须是十六进制
        assert v == v.lower()

    def test_changes_when_sha256_changes(self, conn):
        """技能 sha256 变化 → 版本变化（目录级短路的信号源）。"""
        _seed(conn, "pdf", "# PDF v1", sha="aaa")
        v1 = skill_cache.catalog_version(conn)
        _seed(conn, "pdf", "# PDF v2", sha="bbb")
        v2 = skill_cache.catalog_version(conn)
        assert v1 != "empty" and v2 != "empty"
        assert v1 != v2

    def test_content_change_without_sha_change_keeps_version(self, conn):
        """版本锚定 (name, sha256)：sha256 不变则版本不变（索引器保证 content↔sha 一致）。"""
        _seed(conn, "pdf", "# PDF v1", sha="same")
        v1 = skill_cache.catalog_version(conn)
        _seed(conn, "pdf", "# PDF v2", sha="same")
        assert skill_cache.catalog_version(conn) == v1

    def test_adding_or_removing_skill_changes_version(self, conn):
        _seed(conn, "pdf", "# PDF", sha="aaa")
        v1 = skill_cache.catalog_version(conn)
        _seed(conn, "wiki", "# Wiki", sha="bbb")
        v2 = skill_cache.catalog_version(conn)
        assert v1 != v2

    def test_insertion_order_insensitive_across_databases(self, tmp_path):
        """同一批技能以相反顺序灌进两个库 → 版本一致（按 name 排序归一化）。"""
        versions = []
        for order in (["pdf", "wiki"], ["wiki", "pdf"]):
            c = db_mod.connect_skills(tmp_path / f"db-{order[0]}")
            for name in order:
                _seed(c, name, f"# {name}", sha="sha-" + name)
            versions.append(skill_cache.catalog_version(c))
            c.close()
        assert versions[0] == versions[1] != "empty"

    def test_stable_across_repeated_calls(self, ro_conn):
        assert (skill_cache.catalog_version(ro_conn)
                == skill_cache.catalog_version(ro_conn))

    def test_only_skills_table_counts(self, conn):
        """依赖表（skill_uses 等）变化不影响版本指纹。"""
        _seed(conn, "pdf", "# PDF", sha="aaa")
        v1 = skill_cache.catalog_version(conn)
        conn.execute("INSERT INTO skill_uses (src, dst) VALUES ('pdf', 'wiki')")
        conn.commit()
        assert skill_cache.catalog_version(conn) == v1


# ---------- skill_manifest ----------


class TestSkillManifest:
    def test_missing_skill_returns_none(self, conn):
        assert skill_cache.skill_manifest(conn, "nope") is None

    def test_returns_contract_fields(self, ro_conn):
        m = skill_cache.skill_manifest(ro_conn, "pdf")
        # files 为 T-404 新增（附属文件清单，不含 SKILL.md）；新增键向后兼容，
        # 老客户端只读自己认识的键，不因多一个键而失败。
        assert m is not None
        assert set(m.keys()) == {"name", "version", "package_sha256",
                                 "content_len", "updated_at", "files"}
        assert m["files"] == []  # 该夹具未回填附属文件
        assert m["name"] == "pdf"
        assert m["version"] == "1.2.0"
        assert m["content_len"] == len("# PDF\n处理 pdf 表格")
        assert m["updated_at"] == "2026-05-05T01:02:03Z"

    def test_package_sha256_is_64_hex_digest(self, ro_conn):
        m = skill_cache.skill_manifest(ro_conn, "pdf")
        assert len(m["package_sha256"]) == 64
        int(m["package_sha256"], 16)
        assert m["package_sha256"] == m["package_sha256"].lower()

    def test_manifest_is_stable(self, ro_conn):
        assert (skill_cache.skill_manifest(ro_conn, "pdf")
                == skill_cache.skill_manifest(ro_conn, "pdf"))

    def test_manifest_reflects_row_changes(self, conn):
        _seed(conn, "pdf", "# v1", sha="s1", version="1.0.0")
        m1 = skill_cache.skill_manifest(conn, "pdf")
        _seed(conn, "pdf", "# v2 longer content", sha="s2", version="1.1.0")
        m2 = skill_cache.skill_manifest(conn, "pdf")
        assert (m1["package_sha256"] != m2["package_sha256"]
                or m1["content_len"] != m2["content_len"]
                or m1["version"] != m2["version"])


# ---------- skill_package ----------


def _untar(blob: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


def _members(blob: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        return tar.getmembers()


class TestSkillPackage:
    def test_missing_skill_returns_none(self, conn):
        assert skill_cache.skill_package(conn, "nope") is None

    def test_returns_gzip_tar_bytes(self, ro_conn):
        blob = skill_cache.skill_package(ro_conn, "pdf")
        assert isinstance(blob, (bytes, bytearray))
        assert bytes(blob[:2]) == b"\x1f\x8b"  # gzip 魔数
        assert _untar(bytes(blob))  # 可解包且非空

    def test_contains_manifest_and_skill_content(self, ro_conn):
        files = _untar(skill_cache.skill_package(ro_conn, "pdf"))
        joined = b"\n".join(files.values())
        assert "# PDF\n处理 pdf 表格".encode("utf-8") in joined  # content 在包里
        assert "PDF 技能".encode("utf-8") in joined  # description 等元数据在包里

    def test_members_sorted(self, ro_conn):
        names = [m.name for m in _members(skill_cache.skill_package(ro_conn, "pdf"))]
        assert names == sorted(names)
        assert len(names) >= 2  # 至少含元数据与内容两类成员

    def test_mtime_fixed_to_zero(self, ro_conn):
        mtimes = [m.mtime for m in _members(skill_cache.skill_package(ro_conn, "pdf"))]
        assert mtimes == [0] * len(mtimes)

    def test_deterministic_bytes_across_calls(self, ro_conn):
        """核心回归点：两次调用产出完全相同字节（tar + gzip 头都无时间戳）。"""
        b1 = skill_cache.skill_package(ro_conn, "pdf")
        b2 = skill_cache.skill_package(ro_conn, "pdf")
        assert b1 == b2

    def test_deterministic_bytes_across_connections(self, tmp_path):
        """跨进程/跨连接同样确定（重灌同数据 → 同字节）。"""
        blobs = []
        for i in range(2):
            c = db_mod.connect_skills(tmp_path / f"db{i}")
            _seed(c, "pdf", "# PDF\n你好", sha="sha-pdf", version="1.2.0",
                  description="PDF 技能", updated_at="2026-05-05T01:02:03Z")
            blobs.append(skill_cache.skill_package(c, "pdf"))
            c.close()
        assert blobs[0] == blobs[1]

    def test_package_sha256_closed_loop_with_manifest(self, ro_conn):
        """两级缓存闭环：manifest.package_sha256 == sha256(包字节)。

        客户端拉包后可校验完整性；缓存比对可判定是否需要重拉。
        """
        m = skill_cache.skill_manifest(ro_conn, "pdf")
        blob = skill_cache.skill_package(ro_conn, "pdf")
        assert hashlib.sha256(blob).hexdigest() == m["package_sha256"]


# ---------- 只读纪律 ----------


class TestReadOnly:
    def test_all_three_functions_are_readonly(self, ro_conn):
        """mode=ro 连接上三函数全部可用 = 未写库；任何写尝试都会抛 OperationalError。"""
        v = skill_cache.catalog_version(ro_conn)
        m = skill_cache.skill_manifest(ro_conn, "pdf")
        p = skill_cache.skill_package(ro_conn, "pdf")
        assert v != "empty"
        assert m is not None
        assert isinstance(p, (bytes, bytearray))


# ---------- T-404：附属文件进包 ----------


class TestAttachmentFiles:
    """skill_files 表里的附件必须进包、进 manifest、能单独读。"""

    def test_package_includes_attachment_bytes(self, conn):
        from sgme.data import skill_package_dao

        _seed(conn, "pdf", "# PDF 表格处理", sha="sha-pdf")
        skill_package_dao.ensure_tables(conn)
        skill_package_dao.put_files(
            conn, "pdf", [{"path": "references/table.md", "content": "表结构说明".encode()}, {"path": "scripts/a.py", "content": b"print(1)"}]
        )

        m = skill_cache.skill_manifest(conn, "pdf")
        assert m is not None
        paths = {f["path"] for f in m["files"]}
        assert paths == {"references/table.md", "scripts/a.py"}

        pkg = skill_cache.skill_package(conn, "pdf")
        assert pkg is not None
        with tarfile.open(fileobj=io.BytesIO(pkg), mode="r:gz") as tar:
            names = set(tar.getnames())
        assert "references/table.md" in names
        assert "scripts/a.py" in names
        assert "SKILL.md" in names
        assert "SKILL.json" in names

    def test_package_sha_changes_when_attachment_changes(self, conn):
        from sgme.data import skill_package_dao

        _seed(conn, "pdf", "# PDF", sha="sha-pdf")
        skill_package_dao.ensure_tables(conn)
        before = skill_cache.skill_manifest(conn, "pdf")["package_sha256"]
        skill_package_dao.put_files(conn, "pdf", [{"path": "references/x.md", "content": b"v1"}])
        after = skill_cache.skill_manifest(conn, "pdf")["package_sha256"]
        assert before != after

    def test_skill_file_reads_from_db(self, conn):
        from sgme.data import skill_package_dao

        _seed(conn, "pdf", "# PDF", sha="sha-pdf")
        skill_package_dao.ensure_tables(conn)
        skill_package_dao.put_files(conn, "pdf", [{"path": "references/table.md", "content": "表结构说明".encode()}])
        assert skill_cache.skill_file(conn, "pdf", "references/table.md") == "表结构说明".encode()

    def test_skill_file_rejects_traversal(self, conn):
        _seed(conn, "pdf", "# PDF", sha="sha-pdf")
        assert skill_cache.skill_file(conn, "pdf", "../../etc/passwd") is None
        assert skill_cache.skill_file(conn, "pdf", "/etc/passwd") is None
