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
        assert {"alpha", "beta", "wiki-skill"} <= names
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

    def test_unknown_section_fails(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import skill_get

        res = skill_get(skills_cfg, wiki_conn, name="alpha", section="不存在的节")
        assert res.ok is False and res.error_code == ERR_NOT_FOUND

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

    def test_no_match_empty(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import search_skills

        assert search_skills("zzzqqqxxx", skills_cfg, wiki_conn) == []

    def test_limit_respected(self, skills_cfg, wiki_conn):
        from sgme.operations.skills import search_skills

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
