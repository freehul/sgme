"""tests/test_skills_indexer.py：ST-36 M1 索引器/配置/BM25/向量缓存 测试（TDD）。"""
from __future__ import annotations

from pathlib import Path

import pytest

# ---------- config ----------

class TestSkillsConfig:
    def test_defaults(self):
        from sgme.skills.config import SkillsConfig, parse_skills_config

        c = parse_skills_config({})
        assert isinstance(c, SkillsConfig)
        assert c.enabled is True
        assert c.source_dirs == []
        assert c.budget == 40
        assert c.vector_cache_policy == "lazy"

    def test_parse_full(self):
        from sgme.skills.config import parse_skills_config

        c = parse_skills_config({"skills": {
            "enabled": False, "source_dirs": ["D:/x"], "budget": 10,
            "vector_cache_policy": "refresh",
        }})
        assert c.enabled is False
        assert c.source_dirs == ["D:/x"]
        assert c.budget == 10
        assert c.vector_cache_policy == "refresh"

    @pytest.mark.parametrize("bad", [
        {"skills": {"enabled": "yes"}},
        {"skills": {"source_dirs": "D:/x"}},
        {"skills": {"budget": -1}},
        {"skills": {"budget": True}},
        {"skills": {"vector_cache_policy": "nope"}},
    ])
    def test_invalid(self, bad):
        from sgme.skills.config import parse_skills_config

        with pytest.raises(ValueError):
            parse_skills_config(bad)


# ---------- indexer ----------

class TestIndexer:
    def _wiki_conn(self, tmp_path: Path):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE wiki_pages (page_id TEXT PRIMARY KEY, title TEXT,"
            " content TEXT, category TEXT, tags TEXT, status TEXT)"
        )
        return conn

    def test_validate_name_rejects_traversal(self):
        from sgme.skills.indexer import validate_name

        for bad in ("", "..", "../x", "a/b", "a\\b"):
            with pytest.raises(ValueError):
                validate_name(bad)
        assert validate_name(" my-skill ") == "my-skill"

    def test_collect_from_dir_two_levels(self, tmp_path: Path):
        from sgme.skills.indexer import collect_from_dir

        d = tmp_path / "tree"
        (d / "cat-a").mkdir(parents=True)
        (d / "cat-a" / "alpha").mkdir()
        (d / "cat-a" / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: 技能A简介\nversion: 1.0.0\ncategory: cat-a\nuses:\n  - git-basics\n---\n# Alpha\n正文", encoding="utf-8")
        (d / "beta").mkdir()
        (d / "beta" / "SKILL.md").write_text("# Beta 无frontmatter", encoding="utf-8")
        recs = {r.name: r for r in collect_from_dir(d)}
        assert set(recs) == {"alpha", "beta"}
        a = recs["alpha"]
        assert a.description == "技能A简介"
        assert a.version == "1.0.0"
        assert a.uses == ["git-basics"]
        assert a.tags[0] == "skill"
        assert a.sha256 and a.source == "git"
        assert recs["beta"].description == ""

    def test_collect_from_wiki_skill_pages_only(self, tmp_path: Path):
        from sgme.skills.indexer import collect_from_wiki

        conn = self._wiki_conn(tmp_path)
        rows = [
            ("w1", "skill:terminal-safety", "# 终端安全…", "skill/common",
             '["skill","common"]', "active"),
            ("w2", "普通知识页", "# 内容", "research/dsh", '["dsh"]', "active"),
            ("w3", "skill:old-one", "# 旧技能", "skill/x", '["skill"]', "superseded"),
        ]
        conn.executemany(
            "INSERT INTO wiki_pages VALUES (?,?,?,?,?,?)", rows)
        got = {r.name: r for r in collect_from_wiki(conn)}
        assert set(got) == {"terminal-safety"}
        assert got["terminal-safety"].category == "common"
        # None 连接容错
        assert collect_from_wiki(None) == []

    def test_merge_git_wins(self, tmp_path: Path):
        from sgme.skills.indexer import SkillRecord, merge_records

        g = [SkillRecord(name="dup", content="git版", sha256="g", source="git")]
        w = [SkillRecord(name="dup", content="wiki版", sha256="w", source="wiki"),
             SkillRecord(name="only-wiki", content="x", sha256="x", source="wiki")]
        merged = merge_records(g, w)
        names = [r.name for r in merged]
        assert names == sorted(names)  # 按名排序
        dup = next(r for r in merged if r.name == "dup")
        assert dup.content == "git版" and dup.source == "git"
        assert "only-wiki" in names

    def test_index_all_dedup(self, tmp_path: Path):
        from sgme.skills.indexer import index_all

        d = tmp_path / "s"
        (d / "shared").mkdir(parents=True)
        (d / "shared" / "SKILL.md").write_text("---\ndescription: 目录版\n---\n目录版", encoding="utf-8")
        conn = self._wiki_conn(tmp_path)
        conn.execute("INSERT INTO wiki_pages VALUES ('w1','skill:shared','# wiki版','skill/c','[\"skill\"]','active')")
        recs = index_all([str(d)], conn)
        assert len(recs) == 1 and recs[0].source == "git"


# ---------- bm25 ----------

class TestBm25:
    def _recs(self):
        from sgme.skills.indexer import SkillRecord

        return [
            SkillRecord(name="nas-deploy", description="飞牛NAS部署技能", tags=["skill"],
                        content="# NAS 部署指南 docker compose 用法"),
            SkillRecord(name="douyin-pipeline", description="抖音视频分析入口", tags=["skill"],
                        content="# 抖音采集 yt-dlp cookies 流水线"),
        ]

    def test_score_ranking(self):
        from sgme.skills.bm25 import SkillsBm25

        idx = SkillsBm25(self._recs())
        s = idx.score("NAS 部署")
        assert s and max(s, key=s.get) == "nas-deploy"
        s2 = idx.score("抖音 视频")
        assert s2 and max(s2, key=s2.get) == "douyin-pipeline"

    def test_zero_hit_empty(self):
        from sgme.skills.bm25 import SkillsBm25

        idx = SkillsBm25(self._recs())
        assert idx.score("zzzqqqxxx") == {}

    def test_stale_detects_change(self):
        from sgme.skills.bm25 import SkillsBm25, rebuild_if_stale
        from sgme.skills.indexer import SkillRecord

        recs = self._recs()
        idx = SkillsBm25(recs)
        # 同一记录集 → 不重建（对象身份不变）
        assert rebuild_if_stale(idx, list(recs)) is idx
        # 改内容 → 重建（新对象）
        changed = [SkillRecord(name=recs[0].name, content="新内容"), recs[1]]
        assert rebuild_if_stale(idx, changed) is not idx


# ---------- vectors ----------

class TestVectorsCache:
    def test_cache_roundtrip_and_corrupt_selfheal(self, tmp_path: Path):
        from sgme.skills.vectors import load_cache, save_cache

        c = {"format": 1, "items": {"abc": [0.1, 0.2]}}
        save_cache(tmp_path, c)
        assert load_cache(tmp_path)["items"]["abc"] == [0.1, 0.2]
        (tmp_path / "skill_vectors.json").write_text("{broken", encoding="utf-8")
        assert load_cache(tmp_path)["items"] == {}

    def test_build_uses_cache_hit_without_network(self, tmp_path: Path):
        """SHA 命中缓存时不发网络请求（embed 会因未配置抛错，命中路径必须零调用）。"""
        from sgme.skills.indexer import SkillRecord
        from sgme.skills.vectors import build_vectors, save_cache

        rec = SkillRecord(name="cached-skill", content="内容", sha256="sha-hit")
        vec = [0.5] * 4
        save_cache(tmp_path, {"format": 1, "items": {"sha-hit": vec}})
        out = build_vectors([rec], cfg={}, cache_dir=tmp_path)
        assert out["cached-skill"] == vec

    def test_cosine_topk_dim_mismatch_skipped(self):
        from sgme.skills.vectors import cosine_topk

        qv = [1.0, 0.0]
        got = cosine_topk(qv, {"ok": [1.0, 0.0], "stale-dim": [0.9] * 1024}, top_k=2)
        assert set(got) == {"ok"} and got["ok"] > 0.999

    def test_embed_config_reads_active_provider(self, monkeypatch, tmp_path: Path):
        """激活向量提供商解析：providers vector_capable + search.vector.provider。"""
        import sgme.config as config_mod
        from sgme.skills.vectors import _embed_config

        monkeypatch.setattr(config_mod, "load_providers_config", lambda: {
            "providers": {
                "siliconflow": {
                    "base_url": "https://api.siliconflow.cn/v1",
                    "default_model": "BAAI/bge-m3",
                    "api_key_env": "SILICONFLOW_API_KEY_TEST",
                    "vector_capable": True,
                },
            },
        }, raising=False)
        monkeypatch.setenv("SILICONFLOW_API_KEY_TEST", "k-test")
        prov, model, base_url, key = _embed_config({
            "search": {"vector": {"provider": "siliconflow"}},
        })
        assert (prov, model) == ("siliconflow", "BAAI/bge-m3")
        assert base_url.endswith("/v1") and key == "k-test"
