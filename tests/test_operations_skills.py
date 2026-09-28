"""tests/test_operations_skills.py：ST-36 M2 读侧披露操作测试（TDD）。

覆盖 sgme.operations.skills 五操作：
1. list_skills      L0 索引列表（budget 截断）
2. skill_digest     L1 摘要（frontmatter + 骨架 + uses；不存在 → fail NOT_FOUND）
3. skill_get        L2 全文（section 截取；未知 section → fail）
4. materialize      L3 字节保真落盘 + 遥测日志一条
5. search_skills    BM25 + 向量余弦融合（0.6/0.4），向量不可达降级纯 BM25

数据源：tmp git 目录双技能 + wiki skill 标记页（镜像 test_skills_indexer 口径）。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from sgme.operations.errors import ERR_NOT_FOUND, OperationResult
from sgme.skills.indexer import SkillRecord

# ---------- fixtures ----------

ALPHA_MD = (
    "---\n"
    "name: alpha\n"
    "description: 技能A简介——NAS 部署流水线\n"
    "version: 1.2.0\n"
    "category: deploy\n"
    "tags: [skill, deploy]\n"
    "uses:\n"
    "  - beta\n"
    "---\n"
    "# Alpha 总纲\n"
    "正文第一段。\n"
    "\n"
    "## 步骤\n"
    "docker compose up -d\n"
    "\n"
    "## 踩坑\n"
    "端口冲突先查 netstat。\n"
)

BETA_MD = (
    "---\n"
    "name: beta\n"
    "description: 技能B简介——抖音视频分析入口\n"
    "version: 2.0.0\n"
    "---\n"
    "# Beta\n"
    "yt-dlp cookies 流水线。\n"
)


def _make_skill_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    (d / "alpha").mkdir(parents=True)
    (d / "alpha" / "SKILL.md").write_text(ALPHA_MD, encoding="utf-8")
    (d / "beta").mkdir()
    (d / "beta" / "SKILL.md").write_text(BETA_MD, encoding="utf-8")
    return d


def _make_wiki_conn(with_skill_page: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT,"
        " content TEXT, category TEXT, tags TEXT, status TEXT)"
    )
    if with_skill_page:
        conn.execute(
            "INSERT INTO wiki_pages VALUES ('w1','skill:wiki-skill','# Wiki 技能 NAS',"
            "'skill/common','[\"skill\"]','active')"
        )
    return conn


@pytest.fixture
def skills_cfg(tmp_path):
    """skills 配置段（指向 tmp 目录；budget=1 供截断测试用小预算）。"""
    return {
        "skills": {
            "enabled": True,
            "source_dirs": [str(_make_skill_dir(tmp_path))],
            "budget": 40,
            "vector_cache_policy": "lazy",
        }
    }


@pytest.fixture
def wiki_conn():
    return _make_wiki_conn()


# ---------- list_skills（L0） ----------


class TestListSkills:
    def test_returns_l0_entries_with_meta(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import list_skills

        res = list_skills(skills_cfg, wiki_conn)
        assert isinstance(res, OperationResult) and res.ok is True
        items = res.data["skills"]
        names = {s["name"] for s in items}
        assert {"alpha", "beta"} <= names
        # 2026-08-28：wiki 桥接已移除，wiki 页不再被视为技能
        assert "wiki-skill" not in names
        alpha = next(s for s in items if s["name"] == "alpha")
        assert alpha["description"] == "技能A简介——NAS 部署流水线"
        assert alpha["category"] == "deploy"
        assert "skill" in alpha["tags"]

    def test_budget_truncates(self, tmp_path, wiki_conn):
        from sgme.operations.skills import list_skills

        cfg = {"skills": {"enabled": True, "source_dirs": [], "budget": 2}}
        # 无 git 目录 → 仅 wiki 1 条也 < budget；补一个多记录目录验证截断
        d = _make_skill_dir(tmp_path)
        cfg["skills"]["source_dirs"] = [str(d)]
        res = list_skills(cfg, wiki_conn)
        assert res.ok is True
        assert len(res.data["skills"]) <= 2

    def test_disabled_module_raises_invalid(self, wiki_conn):
        from sgme.operations.errors import InvalidArgs
        from sgme.operations.skills import list_skills

        with pytest.raises(InvalidArgs):
            list_skills({"skills": {"enabled": False}}, wiki_conn)


# ---------- skill_digest（L1） ----------


class TestSkillDigest:
    def test_digest_has_frontmatter_skeleton_uses(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import skill_digest

        data = skill_digest(skills_cfg, wiki_conn, name="alpha").data
        assert data["name"] == "alpha"
        assert data["version"] == "1.2.0"
        assert data["category"] == "deploy"
        assert data["description"].startswith("技能A简介")
        assert data["uses"] == ["beta"]
        assert data["sha256"]
        # 骨架 = 各标题行
        skeleton = data["sections"]
        assert any("Alpha 总纲" in s for s in skeleton)
        assert any("踩坑" in s for s in skeleton)
        assert data["source"] in ("git", "wiki")

    def test_digest_missing_not_found(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import skill_digest

        res = skill_digest(skills_cfg, wiki_conn, name="no-such")
        assert res.ok is False and res.error_code == ERR_NOT_FOUND

    def test_digest_invalid_name_rejected(self, skills_cfg, wiki_conn):
        from sgme.operations.errors import InvalidArgs
        from sgme.operations.skills import skill_digest

        with pytest.raises(InvalidArgs):
            skill_digest(skills_cfg, wiki_conn, name="../evil")


# ---------- skill_get（L2） ----------


class TestSkillGet:
    def test_full_content(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import skill_get

        data = skill_get(skills_cfg, wiki_conn, name="alpha").data
        assert "docker compose up -d" in data["content"]
        assert data["name"] == "alpha"

    def test_section_extract(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import skill_get

        res = skill_get(skills_cfg, wiki_conn, name="alpha", section="踩坑")
        assert res.ok is True
        assert "netstat" in res.data["content"]
        assert "docker compose" not in res.data["content"]

    def test_section_extract_accepts_digest_skeleton_form(self, skills_cfg, wiki_conn):
        """digest.sections 带 # 前缀（`## 踩坑`）原样回传必须命中（契约对齐）。"""
        from sgme.operations.skills import skill_digest, skill_get

        skeleton = skill_digest(skills_cfg, wiki_conn, name="alpha").data["sections"]
        assert any(s.strip().endswith("踩坑") and s.strip().startswith("#") for s in skeleton)

        for section in ("## 踩坑", "  ## 踩坑  ", "踩坑"):
            res = skill_get(skills_cfg, wiki_conn, name="alpha", section=section)
            assert res.ok is True, f"section={section!r} 应命中: {res}"
            assert "netstat" in res.data["content"]
            assert "docker compose" not in res.data["content"]

    def test_unknown_section_fails(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import skill_get

        res = skill_get(skills_cfg, wiki_conn, name="alpha", section="不存在的节")
        assert res.ok is False and res.error_code == ERR_NOT_FOUND
        # 节缺失 ≠ 技能缺失：文案不得误导为「技能不存在」
        assert "小节不存在" in (res.message or "")
        assert "技能不存在" not in (res.message or "")

    def test_missing_skill_not_found(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import skill_get

        res = skill_get(skills_cfg, wiki_conn, name="ghost")
        assert res.ok is False and res.error_code == ERR_NOT_FOUND


# ---------- materialize（L3） ----------


class TestMaterialize:
    def test_writes_bytes_and_returns_sha(self, skills_cfg, wiki_conn, tmp_path):
        import hashlib

        from sgme.operations.skills import materialize

        dest = tmp_path / "workspace"
        src_text = (Path(skills_cfg["skills"]["source_dirs"][0]) / "alpha" / "SKILL.md").read_bytes()
        res = materialize(skills_cfg, wiki_conn, name="alpha", dest_dir=str(dest))
        assert res.ok is True
        out = Path(res.data["path"])
        assert out.name == "SKILL.md" and out.parent.name == "alpha"
        # 字节保真
        assert out.read_bytes() == src_text
        expect_sha = hashlib.sha256(out.read_bytes()).hexdigest()
        assert res.data["sha256"] == expect_sha

    def test_telemetry_log_one_line(self, skills_cfg, wiki_conn, tmp_path, caplog):
        from sgme.operations.skills import materialize

        with caplog.at_level(logging.INFO, logger="sgme.operations.skills"):
            res = materialize(
                skills_cfg, wiki_conn, name="alpha", dest_dir=str(tmp_path / "ws2")
            )
        assert res.ok is True
        recs = [r for r in caplog.records if "materialize" in r.getMessage()]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "alpha" in msg and res.data["sha256"][:12] in msg

    def test_missing_skill_not_found(self, skills_cfg, wiki_conn, tmp_path):
        from sgme.operations.skills import materialize

        res = materialize(
            skills_cfg, wiki_conn, name="ghost", dest_dir=str(tmp_path / "ws3")
        )
        assert res.ok is False and res.error_code == ERR_NOT_FOUND

    def test_copies_companions_scripts_install(self, skills_cfg, wiki_conn, tmp_path):
        """A2：随附 scripts/、install.py、locales 一并落盘；junk 跳过。"""
        from sgme.operations.skills import materialize

        root = Path(skills_cfg["skills"]["source_dirs"][0]) / "alpha"
        (root / "scripts").mkdir()
        (root / "scripts" / "sgme_client.py").write_text("print(1)\n", encoding="utf-8")
        (root / "install.py").write_text("# install\n", encoding="utf-8")
        (root / "locales").mkdir()
        (root / "locales" / "zh-CN.json").write_text("{}", encoding="utf-8")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.pyc").write_bytes(b"junk")
        (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("pass\n", encoding="utf-8")

        dest = tmp_path / "ws_comp"
        res = materialize(skills_cfg, wiki_conn, name="alpha", dest_dir=str(dest))
        assert res.ok is True
        base = dest / "alpha"
        assert (base / "scripts" / "sgme_client.py").is_file()
        assert (base / "install.py").is_file()
        assert (base / "locales" / "zh-CN.json").is_file()
        assert not (base / "__pycache__").exists()
        assert not (base / ".env").exists()
        assert not (base / "tests").exists()
        assert "scripts/" in res.data.get("companions", [])
        assert "install.py" in res.data.get("companions", [])

    def test_companions_absent_is_ok(self, skills_cfg, wiki_conn, tmp_path):
        """纯 SKILL.md 技能（无随附文件）物化仍成功。"""
        from sgme.operations.skills import materialize

        res = materialize(skills_cfg, wiki_conn, name="beta", dest_dir=str(tmp_path / "ws_b"))
        assert res.ok is True
        assert res.data.get("companions") == []


# ---------- name 直达 + 写后同步（T-215 收口） ----------


class TestNamePinAndReindex:
    def test_pin_exact_name(self):
        from sgme.operations.skills import _pin_exact_name

        fused = {"other": 0.2}
        out = _pin_exact_name("adapter-workbuddy", fused, ["adapter-workbuddy", "other"])
        assert out["adapter-workbuddy"] == 1.0
        assert out["adapter-workbuddy"] > out["other"]

    def test_pin_normalizes_spaces_underscores(self):
        from sgme.operations.skills import _pin_exact_name

        out = _pin_exact_name("Adapter_WorkBuddy", {}, ["adapter-workbuddy"])
        assert out.get("adapter-workbuddy") == 1.0

    def test_search_memory_path_exact_name(self, skills_cfg, wiki_conn):
        """内存检索路径：精确技能名必须出现在首位。"""
        from sgme.operations.skills import search_skills

        hits = search_skills("alpha", skills_cfg, wiki_conn, limit=5)
        assert hits and hits[0]["name"] == "alpha"
        assert hits[0]["score"] >= 1.0

    def test_reindex_after_write_none_conn_ok(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import reindex_after_write

        r = reindex_after_write(skills_cfg, None, wiki_conn)
        assert r.ok is True and r.data.get("skipped") is True

    def test_reindex_after_write_upserts(self, skills_cfg, wiki_conn, tmp_path):
        from sgme.data import db as db_mod
        from sgme.operations.skills import reindex_after_write

        conn = db_mod.connect_skills(tmp_path)
        r = reindex_after_write(skills_cfg, conn, wiki_conn)
        assert r.ok is True
        names = {row["name"] for row in conn.execute("SELECT name FROM skills")}
        assert "alpha" in names and "beta" in names
        # 二次调用幂等
        r2 = reindex_after_write(skills_cfg, conn, wiki_conn)
        assert r2.ok is True and r2.data.get("inserted", 0) == 0


# ---------- search_skills ----------


class TestSearchSkills:
    def test_bm25_hit(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import search_skills

        hits = search_skills("NAS 部署", skills_cfg, wiki_conn)
        assert hits, "BM25 主路必须有命中"
        top = hits[0]
        assert set(top.keys()) >= {"name", "score", "source"}
        # ⚠️ 排序语义（2026-08-27 校准）：BM25 短文档词频密度高（wiki-skill 内容仅
        #    「# Wiki 技能 NAS」7 字得分反超完整技能 alpha 属正常行为）——断言「被召回」
        #    而非「必第一」，检索有效性判据 = 相关技能出现在命中列表，不锁死顺序
        names = [h["name"] for h in hits]
        assert "alpha" in names, f"alpha 必须被召回，实际: {names}"
        assert any(h["source"] == "git" for h in hits), "git 源技能必须被召回"

    def test_vector_unreachable_degrades_to_bm25(self, skills_cfg, wiki_conn, monkeypatch):
        """embed 失败（未配置/网络不可达）→ 自动降级纯 BM25，仍出结果。"""
        from sgme.operations import skills as ops_skills

        def _boom(*a, **k):
            raise RuntimeError("向量引擎离线")

        monkeypatch.setattr(ops_skills, "_query_embedding_safe", _boom)
        hits = ops_skills.search_skills("NAS 部署", skills_cfg, wiki_conn)
        # 降级语义：有结果 + alpha 被召回（同 test_bm25_hit 排序校准）
        assert hits, "降级后仍须有命中"
        names = [h["name"] for h in hits]
        assert "alpha" in names, f"降级后 alpha 必须被召回，实际: {names}"

    def test_no_match_empty(self, skills_cfg, wiki_conn, monkeypatch):
        """纯垃圾串不命中（向量路打桩降级：真实 embed 会让弱相似虚命中 + 触外网，B123）。"""
        from sgme.operations.skills import search_skills
        import sgme.skills.vectors as sv

        def boom(texts, cfg, timeout=None):
            raise RuntimeError("test: vector unavailable")

        monkeypatch.setattr(sv, "embed_texts", boom)
        assert search_skills("zzzqqqxxx", skills_cfg, wiki_conn) == []

    def test_limit_respected(self, skills_cfg, wiki_conn, monkeypatch):
        from sgme.operations.skills import search_skills
        import sgme.skills.vectors as sv

        def boom(texts, cfg, timeout=None):
            raise RuntimeError("test: vector unavailable")

        monkeypatch.setattr(sv, "embed_texts", boom)
        hits = search_skills("技能", skills_cfg, wiki_conn, limit=1)
        assert len(hits) <= 1


# ---------- 纯函数：融合与骨架 ----------


class TestPureHelpers:
    def test_fuse_weights(self):
        from sgme.operations.skills import _fuse_scores

        # 两路各归一到 [0,1] 再加权：单元素归一为 1.0 → 0.6*1 + 0.4*1 = 1.0
        fused = _fuse_scores({"a": 1.0}, {"a": 0.5}, w_bm25=0.6, w_vec=0.4)
        assert abs(fused["a"] - 1.0) < 1e-9
        # 双元素验证权重区分度：两路各自归一后均为 a=1/b=0 → b 权重和为 0
        fused2 = _fuse_scores({"a": 2.0, "b": 1.0}, {"a": 0.9, "b": 0.8})
        assert fused2["a"] > fused2["b"]
        assert abs(fused2["a"] - (0.6 + 0.4)) < 1e-9
        assert abs(fused2["b"]) < 1e-9

    def test_section_slice(self):
        from sgme.operations.skills import _extract_section

        body = "# A\nx\n\n## B\ny1\ny2\n\n## C\nz"
        seg = _extract_section(body, "B")
        assert seg.startswith("## B") and "y1" in seg and "y2" in seg
        assert "## C" not in seg


# ---------- FTS 脱节自检与修复（T-217） ----------


class TestFtsDriftSelfHeal:
    """skills_fts（外部内容表）与 skills 主表脱节 → 「列表有、检索无」。

    复现 NAS 实况形态：主表行存在且 sha 未变（diff 全 unchanged，reindex
    永不重写），但 FTS 索引缺行。模拟手法 = 摘除 INSERT 触发器后裸插一行
    （绕过触发器的写入是脱节的现实成因族）。
    修复 = sync_index 逐行短语探测（name_seg 整段 MATCH 必须命中本行 rowid），
    检出即全量重建外部内容索引（connect_skills 的 B156 迁移同款手法）。

    ⚠️ 三个不能用的检测姿势（TDD 探针实测，2026-09-29）：
    - FTS5 外部内容表的非 MATCH 查询（count(*)/SELECT rowid）直接读 content
      表（主表），恒等于主表行数——天生假检测器；
    - integrity-check 不带 rank 参数不核对 content；带 rank=1 需 SQLite>=3.43
      （NAS Debian bookworm 为 3.40.1），跨部署环境不可依赖；
    - FTS 'delete' 命令模拟脱节：墓碑在 MATCH 侧立即生效但 count 滞后，
      且「行从未入索引」（NAS 实况）才是缺行形态。
    """

    GAMMA_MD = (
        "---\n"
        "name: gamma\n"
        "description: 技能G简介——NAS 备份恢复手册\n"
        "version: 1.0.0\n"
        "---\n"
        "# Gamma\n"
        "rsync 快照恢复流水线。\n"
    )

    def _bypass_trigger_insert(self, conn: sqlite3.Connection, skills_cfg, wiki_conn) -> None:
        """摘掉 INSERT 触发器 → 按 upsert 同款字段值裸插 gamma → 还原触发器。

        字段值必须与 upsert_skill 将写入的完全一致（含分词列与 sha），否则
        diff 判 update 重写主表、FTS 由触发器补上，测试会因错误原因通过。
        """
        import json

        from sgme.data.db import SKILLS_FTS_TRIGGERS
        from sgme.operations.skills import _load_records, _record_to_dict
        from sgme.segment import segment

        d = Path(skills_cfg["skills"]["source_dirs"][0])
        (d / "gamma").mkdir(parents=True, exist_ok=True)
        (d / "gamma" / "SKILL.md").write_text(self.GAMMA_MD, encoding="utf-8")
        rec = next(
            _record_to_dict(r) for r in _load_records(skills_cfg, wiki_conn)
            if r.name == "gamma"
        )
        conn.execute("DROP TRIGGER skills_ai")
        conn.execute(
            "INSERT INTO skills (name, sha256, name_seg, description, description_seg,"
            " category, tags, version, pattern, source, origin_path, content, content_seg,"
            " content_len, updated_at, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec["name"], rec["sha256"], segment(rec["name"]),
                rec["description"], segment(rec["description"]),
                rec["category"], json.dumps(rec["tags"], ensure_ascii=False),
                rec["version"], rec["pattern"], rec["source"], rec["origin_path"],
                rec["content"], segment(rec["content"]), len(rec["content"]),
                "2026-09-29T00:00:00Z", "2026-09-29T00:00:00Z",
            ),
        )
        conn.executescript(SKILLS_FTS_TRIGGERS)  # 幂等 DDL：还原三触发器
        conn.commit()

    @staticmethod
    def _fts_counts(conn: sqlite3.Connection) -> tuple[int, int]:
        n_main = conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"]
        n_fts = conn.execute("SELECT COUNT(*) AS n FROM skills_fts").fetchone()["n"]
        return n_main, n_fts

    def test_sync_index_self_heals_fts_drift(self, skills_cfg, wiki_conn, tmp_path):
        from sgme.data import db as db_mod
        from sgme.data.skills_dao import fts_search
        from sgme.operations.skills import sync_index

        conn = db_mod.connect_skills(tmp_path)
        r1 = sync_index(skills_cfg, conn, wiki_conn, max_embed=0, embed=False)
        assert r1.ok is True
        # 健全基线：FTS 能按描述词搜到 beta
        assert any(h["name"] == "beta" for h in fts_search(conn, "视频分析", limit=10))

        self._bypass_trigger_insert(conn, skills_cfg, wiki_conn)
        # 主表 3 行；FTS 缺 gamma 行（count(*) 读 content 表恒为 3，
        # 缺行只能从 MATCH 侧看出——这正是短语探测存在的理由）
        assert self._fts_counts(conn) == (3, 3)
        assert not any(h["name"] == "gamma" for h in fts_search(conn, "备份恢复", limit=10))

        # sha 未变 → diff 全 unchanged；短语探测检出缺行 → 自动重建并如实上报
        r2 = sync_index(skills_cfg, conn, wiki_conn, max_embed=0, embed=False, fts_check=True)
        assert r2.ok is True
        assert r2.data.get("unchanged") == 3
        assert r2.data.get("fts_drift_detected") is True
        assert r2.data.get("fts_rebuilt") is True
        assert any(h["name"] == "gamma" for h in fts_search(conn, "备份恢复", limit=10))

        # 三次调用：脱节已修复，不再误报
        r3 = sync_index(skills_cfg, conn, wiki_conn, max_embed=0, embed=False, fts_check=True)
        assert r3.data.get("fts_drift_detected") is False
        assert r3.data.get("fts_rebuilt") is False

    def test_rebuild_fts_flag_forces_rebuild_without_drift(self, skills_cfg, wiki_conn, tmp_path):
        """rebuild_fts=True 为运维兜底：无脱节也无条件重建。"""
        from sgme.data import db as db_mod
        from sgme.data.skills_dao import fts_search
        from sgme.operations.skills import sync_index

        conn = db_mod.connect_skills(tmp_path)
        sync_index(skills_cfg, conn, wiki_conn, max_embed=0, embed=False)
        r = sync_index(skills_cfg, conn, wiki_conn, max_embed=0, embed=False, rebuild_fts=True)
        assert r.ok is True
        assert r.data.get("fts_drift_detected") is False
        assert r.data.get("fts_rebuilt") is True
        assert any(h["name"] == "alpha" for h in fts_search(conn, "部署流水线", limit=10))

    def test_pending_embed_reported_without_embed(self, skills_cfg, wiki_conn, tmp_path):
        """embed=False 时 pending_embed 也必须如实上报（修复 reindex 假 0）。"""
        from sgme.data import db as db_mod
        from sgme.operations.skills import sync_index

        conn = db_mod.connect_skills(tmp_path)
        r = sync_index(skills_cfg, conn, wiki_conn, max_embed=0, embed=False)
        assert r.ok is True
        assert r.data.get("embedded") == 0
        assert r.data.get("pending_embed") == 2  # 向量空表 → 待补 2 条
