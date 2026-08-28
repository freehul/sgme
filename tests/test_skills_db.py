"""T-112 skills.db 测试：schema/DAO/FTS/增量同步/依赖图/embed 分批/空库回退。

覆盖 2026-08-29 技能索引持久化改造的回归点，尤其是两个历史坑：
① embed 一次性 POST 全量必然超时（403 条 → 分批 ≤10 条）；
② 库刚建、后台未同步完时读侧不得返回空（_db_ready 空库回退内存索引）。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from sgme.data import db as db_mod
from sgme.data import skills_dao


# ---------- fixtures ----------


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect_skills(tmp_path)
    yield c
    c.close()


def _rec(name, content, description="", category=None, sha=None, uses=None):
    return {
        "name": name,
        "sha256": sha or ("sha-" + name),
        "description": description,
        "tags": ["skill"],
        "category": category,
        "version": "1.0.0",
        "pattern": "manual",
        "source": "git",
        "origin_path": f"/tmp/{name}/SKILL.md",
        "content": content,
        "uses": uses or [],
    }


# ---------- schema ----------


class TestSchema:
    def test_connect_creates_tables(self, conn):
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"skills", "skill_vectors", "skill_uses", "skill_sync_meta"} <= tables

    def test_connect_is_idempotent(self, tmp_path):
        c1 = db_mod.connect_skills(tmp_path)
        c1.close()
        c2 = db_mod.connect_skills(tmp_path)  # 二次连接不应报错（FTS 虚表/触发器幂等）
        c2.close()
        assert (tmp_path / "skills.db").exists()

    def test_fts_table_created(self, conn):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='skills_fts'"
        ).fetchone()
        assert row is not None


# ---------- DAO 基本读写 ----------


class TestDaoBasics:
    def test_upsert_and_get(self, conn):
        skills_dao.upsert_skill(conn, _rec("pdf", "# PDF\n处理 pdf 表格"))
        conn.commit()
        got = skills_dao.get_skill(conn, "pdf")
        assert got is not None
        assert got["name"] == "pdf"
        assert got["content"].startswith("# PDF")
        assert got["tags"] == ["skill"]

    def test_upsert_is_idempotent(self, conn):
        skills_dao.upsert_skill(conn, _rec("x", "v1", sha="s1"))
        skills_dao.upsert_skill(conn, _rec("x", "v2", sha="s2"))
        conn.commit()
        assert skills_dao.count_skills(conn) == 1
        assert skills_dao.get_skill(conn, "x")["content"] == "v2"
        assert skills_dao.get_skill(conn, "x")["sha256"] == "s2"

    def test_content_seg_not_exposed(self, conn):
        skills_dao.upsert_skill(conn, _rec("x", "中文内容测试"))
        conn.commit()
        got = skills_dao.get_skill(conn, "x")
        assert "content_seg" not in got
        assert "description_seg" not in got

    def test_list_and_count(self, conn):
        for i in range(5):
            skills_dao.upsert_skill(conn, _rec(f"s{i}", f"body{i}", category="devops"))
        skills_dao.upsert_skill(conn, _rec("other", "x", category="ai"))
        conn.commit()
        assert skills_dao.count_skills(conn) == 6
        assert skills_dao.count_skills(conn, category="devops") == 5
        page = skills_dao.list_skills(conn, offset=0, limit=3)
        assert len(page) == 3
        assert [r["name"] for r in page] == ["other", "s0", "s1"]  # 按名排序

    def test_delete_cascades(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "x", uses=["b"]))
        skills_dao.upsert_skill(conn, _rec("b", "y"))
        conn.commit()
        conn.execute(
            "INSERT INTO skill_vectors (name, embedding, model, dims, embedded_at)"
            " VALUES ('a', X'00', 'm', 1, 'now')"
        )
        conn.commit()
        skills_dao.delete_skill(conn, "a")
        conn.commit()
        assert skills_dao.get_skill(conn, "a") is None
        assert skills_dao.vector_covered(conn) == set()   # 向量随删
        assert skills_dao.find_incoming(conn, "b") == []  # uses 边随删

    def test_list_categories(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "x", category="devops"))
        skills_dao.upsert_skill(conn, _rec("b", "x", category="devops"))
        skills_dao.upsert_skill(conn, _rec("c", "x", category="ai"))
        conn.commit()
        cats = {c["category"]: c["count"] for c in skills_dao.list_categories(conn)}
        assert cats["devops"] == 2
        assert cats["ai"] == 1


# ---------- FTS 检索（停用词 + name 加权 + category 过滤） ----------


class TestFtsSearch:
    def test_stopwords_filtered(self):
        # 虚词与口语填料必须被剔除，实词保留
        terms = skills_dao.fts_query_terms("帮我读取一下这个文件里的 pdf 表格")
        assert "pdf" in terms
        assert "读取" in terms          # 实词保留
        assert "表格" in terms
        # 虚词被剔除；「文件」是实词，保留（不当停用词，避免误伤检索意图）
        for w in ("帮", "我", "一下", "这个", "里", "的"):
            assert w not in terms, w
        assert "文件" in terms

    def test_empty_after_stopwords_returns_empty(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "内容"))
        conn.commit()
        assert skills_dao.fts_search(conn, "帮我一下") == []

    def test_name_hit_beats_body_hit(self, conn):
        """name 加权 10×：名为 pdf 的技能必须压过正文提到 pdf 的其他技能。"""
        skills_dao.upsert_skill(conn, _rec("pdf", "# PDF 技能\n处理文档"))
        for i in range(3):
            skills_dao.upsert_skill(
                conn, _rec(f"other{i}", "正文里也提到 pdf 这个格式" * 3)
            )
        conn.commit()
        hits = skills_dao.fts_search(conn, "pdf", limit=10)
        assert hits, "FTS 应有命中"
        assert hits[0]["name"] == "pdf"

    def test_long_query_no_longer_diluted(self, conn):
        """回归：长句曾因虚词累加导致名为 pdf 的技能掉出 top-5。"""
        skills_dao.upsert_skill(conn, _rec("pdf", "# PDF\n读取 pdf 文件里的表格"))
        for i in range(5):
            skills_dao.upsert_skill(
                conn, _rec(f"noise{i}", "网页抓取与视频分析相关内容 " * 4)
            )
        conn.commit()
        hits = skills_dao.fts_search(conn, "读取 pdf 文件里的表格", limit=5)
        names = [h["name"] for h in hits]
        assert "pdf" in names, names

    def test_category_filter(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "docker 部署", category="devops"))
        skills_dao.upsert_skill(conn, _rec("b", "docker 部署", category="ai"))
        conn.commit()
        hits = skills_dao.fts_search(conn, "docker", limit=10, category="devops")
        assert [h["name"] for h in hits] == ["a"]

    def test_quote_in_query_not_crash(self, conn):
        """FTS5 语法字符必须被转义：不抛异常，且不被当成语法注入。"""
        skills_dao.upsert_skill(conn, _rec("zzz-unique", "内容"))
        conn.commit()
        # 引号/OR/注释符都是 FTS5 语法字符，若未转义会抛 OperationalError
        assert skills_dao.fts_search(conn, 'nonexistent" OR 1=1 --') == []
        # 正常查询不受影响
        assert [h["name"] for h in skills_dao.fts_search(conn, "zzz-unique")] == ["zzz-unique"]


# ---------- 增量判据 ----------


class TestDiff:
    def test_diff_categories(self, conn):
        skills_dao.upsert_skill(conn, _rec("keep", "x", sha="s1"))
        skills_dao.upsert_skill(conn, _rec("gone", "x", sha="s2"))
        conn.commit()
        src = [
            _rec("keep", "x", sha="s1"),      # unchanged
            _rec("changed", "x", sha="new"),  # 不在库 → insert
        ]
        d = skills_dao.diff_records(conn, src)
        assert d["unchanged"] == ["keep"]
        assert d["delete"] == ["gone"]
        assert set(d["insert"]) == {"changed"}
        assert d["update"] == []

    def test_diff_detects_sha_change(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "x", sha="old"))
        conn.commit()
        d = skills_dao.diff_records(conn, [_rec("a", "y", sha="new")])
        assert d["update"] == ["a"]

    def test_vector_covered(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "x"))
        conn.execute(
            "INSERT INTO skill_vectors (name, embedding, model, dims, embedded_at)"
            " VALUES ('a', X'00', 'm', 1, 'now')"
        )
        conn.commit()
        assert skills_dao.vector_covered(conn) == {"a"}


# ---------- 依赖图 ----------


class TestUsesGraph:
    def test_outgoing_and_incoming(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "x"))
        skills_dao.upsert_skill(conn, _rec("b", "x"))
        skills_dao.replace_uses(conn, "a", ["b"])
        conn.commit()
        assert skills_dao.find_outgoing(conn, "a") == ["b"]
        assert skills_dao.find_incoming(conn, "b") == ["a"]

    def test_replace_is_idempotent(self, conn):
        skills_dao.upsert_skill(conn, _rec("a", "x"))
        skills_dao.replace_uses(conn, "a", ["b", "c"])
        skills_dao.replace_uses(conn, "a", ["c"])
        conn.commit()
        assert skills_dao.find_outgoing(conn, "a") == ["c"]


# ---------- 同步水位 ----------


class TestMeta:
    def test_get_set(self, conn):
        assert skills_dao.get_meta(conn, "k") is None
        assert skills_dao.get_meta(conn, "k", "dft") == "dft"
        skills_dao.set_meta(conn, "k", "v1")
        conn.commit()
        assert skills_dao.get_meta(conn, "k") == "v1"
        skills_dao.set_meta(conn, "k", "v2")
        conn.commit()
        assert skills_dao.get_meta(conn, "k") == "v2"


# ---------- embed 分批（核心回归） ----------


class TestEmbedBatching:
    """分批回归：403 条一次性 POST 必然超时（实测 Ollama 50 条即 35s 超时）。

    直接 mock ``_embed_batch``（单批）而非 httpx.post——``_embed_config`` 依赖
    真实 providers.yaml，mock 到 HTTP 层会先倒在配置解析上。
    """

    def test_batch_size_respected(self, monkeypatch):
        calls = []

        from sgme.skills import vectors as vectors_mod

        def fake_batch(texts, cfg, timeout):
            calls.append(list(texts))
            return {str(i): [0.1, 0.2] for i in range(len(texts))}

        monkeypatch.setattr(vectors_mod, "_embed_batch", fake_batch)
        out = vectors_mod.embed_texts([f"text {i}" for i in range(403)], {})

        assert len(calls) == 41, f"403 条应分 41 批，实际 {len(calls)}"
        assert all(len(c) <= 10 for c in calls), "每批不得超过 10 条"
        assert sum(len(c) for c in calls) == 403
        assert len(out) == 403, "索引必须还原为原始下标"
        assert set(out.keys()) >= {"0", "402"}

    def test_partial_batch_failure_keeps_rest(self, monkeypatch):
        """单批失败不得拖垮其他批（部分成功即可用）。"""
        from sgme.skills import vectors as vectors_mod

        seq = {"i": 0}

        def fake_batch(texts, cfg, timeout):
            seq["i"] += 1
            if seq["i"] == 1:
                raise RuntimeError("boom")
            return {str(i): [0.1] for i in range(len(texts))}

        monkeypatch.setattr(vectors_mod, "_embed_batch", fake_batch)
        out = vectors_mod.embed_texts([f"t{i}" for i in range(20)], {})
        assert len(out) == 10, "首批失败后，第二批的 10 条仍应可用"
        assert set(out.keys()) == {str(i) for i in range(10, 20)}

    def test_all_batches_fail_raises(self, monkeypatch):
        from sgme.skills import vectors as vectors_mod

        def fake_batch(texts, cfg, timeout):
            raise RuntimeError("down")

        monkeypatch.setattr(vectors_mod, "_embed_batch", fake_batch)
        with pytest.raises(RuntimeError):
            vectors_mod.embed_texts(["a", "b"], {})

    def test_batch_params_from_config(self):
        from sgme.skills.vectors import _embed_batch_params

        size, timeout = _embed_batch_params(
            {"skills": {"embed_batch_size": 5, "embed_timeout": 30}}
        )
        assert (size, timeout) == (5, 30.0)
        size, timeout = _embed_batch_params({})
        assert (size, timeout) == (10, 60.0)  # 默认值来自容器实测

    def test_build_vectors_respects_max_new(self, tmp_path, monkeypatch):
        """请求路径必须限批：403 条待补时只处理 max_new 条，余下交后台预热。"""
        from sgme.skills import vectors as vectors_mod

        seen = {"n": 0}

        def fake_embed(texts, cfg):
            seen["n"] = len(texts)
            return {str(i): [0.1] for i in range(len(texts))}

        monkeypatch.setattr(vectors_mod, "embed_texts", fake_embed)

        class R:
            def __init__(self, name):
                self.name = name
                self.sha256 = "sha-" + name
                self.content = "body"

        records = [R(f"s{i}") for i in range(50)]
        vectors_mod.build_vectors(records, {}, tmp_path, policy="refresh", max_new=10)
        assert seen["n"] == 10, f"限批 10 条，实际请求 {seen['n']} 条"


# ---------- 空库回退（冷启动空窗期） ----------


class TestDbReadyFallback:
    def test_empty_db_not_ready(self, conn):
        from sgme.operations import skills as skills_ops

        assert skills_ops._db_ready(conn) is False

    def test_none_conn_not_ready(self):
        from sgme.operations import skills as skills_ops

        assert skills_ops._db_ready(None) is False

    def test_populated_db_ready(self, conn):
        from sgme.operations import skills as skills_ops

        skills_dao.upsert_skill(conn, _rec("a", "x"))
        conn.commit()
        assert skills_ops._db_ready(conn) is True

    def test_empty_db_falls_back_to_memory_index(self, tmp_path, monkeypatch):
        """空库时 L0 列表必须回退内存索引，不能返回空。"""
        from sgme.operations import skills as skills_ops

        c = db_mod.connect_skills(tmp_path)
        seen = {}

        def fake_load(cfg, wiki_conn):
            class R:
                name = "mem-skill"
                description = "来自内存索引"
                tags = ["skill"]
                category = "devops"
                version = "1"
                pattern = "manual"
                source = "git"
                origin_path = "/x"
                content = "body"
                sha256 = "s"
                uses = []

            return [R()]

        # list_skills 的回退路径走 index_all（不是 _load_records）——两个都 mock 保险
        monkeypatch.setattr(skills_ops, "_load_records", fake_load)
        monkeypatch.setattr(skills_ops, "index_all", lambda dirs, wc: fake_load(None, None))
        cfg = {"skills": {"enabled": True, "source_dirs": [], "budget": 40}}
        res = skills_ops.list_skills(cfg, None, skills_conn=c)
        data = res.data if hasattr(res, "data") else res
        assert data["total"] == 1
        assert data["skills"][0]["name"] == "mem-skill"
        seen["ok"] = True
        c.close()


# ---------- 同步（结构化部分，不触网） ----------


class TestSyncIndex:
    def test_sync_inserts_and_deletes(self, tmp_path, monkeypatch):
        from sgme.operations import skills as skills_ops

        c = db_mod.connect_skills(tmp_path)

        src = [_rec("a", "aaa"), _rec("b", "bbb")]
        monkeypatch.setattr(skills_ops, "_load_records", lambda cfg, wc: src)
        cfg = {"skills": {"enabled": True, "source_dirs": []}}

        res = skills_ops.sync_index(cfg, c, None, max_embed=0, embed=False)
        assert res.ok
        d = res.data
        assert d["inserted"] == 2 and d["deleted"] == 0
        assert skills_dao.count_skills(c) == 2

        # 二次同步：内容未变 → 全 unchanged
        res2 = skills_ops.sync_index(cfg, c, None, max_embed=0, embed=False)
        assert res2.data["unchanged"] == 2
        assert res2.data["inserted"] == 0

        # 源里删掉 b → 同步后库内也应删除
        monkeypatch.setattr(skills_ops, "_load_records", lambda cfg, wc: [_rec("a", "aaa")])
        res3 = skills_ops.sync_index(cfg, c, None, max_embed=0, embed=False)
        assert res3.data["deleted"] == 1
        assert skills_dao.count_skills(c) == 1
        c.close()

    def test_sync_updates_uses_graph(self, tmp_path, monkeypatch):
        from sgme.operations import skills as skills_ops

        c = db_mod.connect_skills(tmp_path)
        monkeypatch.setattr(
            skills_ops,
            "_load_records",
            lambda cfg, wc: [_rec("a", "x", uses=["b"]), _rec("b", "y")],
        )
        cfg = {"skills": {"enabled": True, "source_dirs": []}}
        skills_ops.sync_index(cfg, c, None, max_embed=0, embed=False)
        assert skills_dao.find_outgoing(c, "a") == ["b"]
        assert skills_dao.find_incoming(c, "b") == ["a"]
        c.close()


# ---------- 库内检索（FTS 单路，向量不可达时降级） ----------


class TestSearchSkillsDb:
    def test_fts_only_when_vector_unavailable(self, conn, monkeypatch):
        from sgme.operations import skills as skills_ops

        skills_dao.upsert_skill(conn, _rec("pdf", "# PDF", "处理 pdf 表格"))
        skills_dao.upsert_skill(conn, _rec("noise", "网页抓取视频分析 " * 5))
        conn.commit()

        # 向量不可达 → 只走 FTS，routes 标记 skills_bm25
        def boom(*a, **kw):
            raise RuntimeError("no embed")

        monkeypatch.setattr(skills_ops, "_embed_into_db", boom)
        import sgme.skills.vectors as v

        monkeypatch.setattr(v, "embed_texts", boom)

        hits = skills_ops.search_skills_db("pdf 表格", conn, {}, limit=5)
        assert hits, "FTS 应命中"
        assert hits[0]["name"] == "pdf"
        assert hits[0]["_routes"] == ["skills_bm25"]

    def test_fusion_marks_rrf_when_vector_hits(self, conn, monkeypatch):
        from sgme.operations import skills as skills_ops

        skills_dao.upsert_skill(conn, _rec("pdf", "# PDF", "处理 pdf"))
        conn.commit()

        import sgme.skills.vectors as v

        monkeypatch.setattr(v, "embed_texts", lambda texts, cfg: {"0": [1.0, 0.0]})

        class FakeVecMod:
            @staticmethod
            def skill_vector_search(c, qv, limit=10):
                return [{"name": "pdf", "description": "", "category": None, "score": 0.99}]

        import sgme.data.search.vector as real_vec

        monkeypatch.setattr(real_vec, "skill_vector_search", FakeVecMod.skill_vector_search)

        hits = skills_ops.search_skills_db("pdf", conn, {}, limit=5)
        assert hits and hits[0]["_routes"] == ["skills_rrf"]

    def test_empty_query_returns_empty(self, conn):
        from sgme.operations import skills as skills_ops

        assert skills_ops.search_skills_db("", conn, {}, limit=5) == []
        assert skills_ops.search_skills_db("   ", conn, {}, limit=5) == []
