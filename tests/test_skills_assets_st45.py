"""tests/test_skills_assets_st45.py：技能资产通道测试（ST-45 / B195）。

覆盖：
1. 资产相对路径校验（白名单三类 / 路径穿越 / 绝对路径 / 盘符 / 空路径）
2. 工作区 .gitignore 白名单版与**存量旧版幂等升级**
   （原实现 ``if not exists()`` 对既有部署永不生效——这是本任务的关键坑）
3. ``store.write_skill_file``：落盘 + commit / 技能不存在 / 配额三档 / 覆盖写不重复计容
4. 引用完整性提示（**只提示不拦**，占位示例同样提示）
5. 端点 ``PUT /v1/admin/skills/{name}/files/{relpath}``：成功 / 403 / 400 / 404

零真实网络；``skills.source_dirs`` 指向 tmp 下真实 git 仓库。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.server.app import create_app
from sgme.skills import store as skills_store

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}

# 旧版「仅 SKILL.md 单文件」内容——用于构造存量工作区
OLD_GITIGNORE = "*\n!*/\n!*/SKILL.md\n"
SKILL_MD = ("---\nname: demo-skill\ndescription: 测试技能\ncategory: testing\n---\n"
            "\n# 标题\n\n正文\n")


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _commit_count(repo: Path) -> int:
    return int(_run_git(repo, "rev-list", "--count", "HEAD").stdout.strip() or 0)


def _gitignore_text(repo: Path) -> str:
    return (repo / ".gitignore").read_text(encoding="utf-8")


def _mk_skill(repo: Path, name: str = "demo-skill") -> Path:
    d = repo / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """tmp 下真实 git 仓；**故意**预置旧版「单文件」.gitignore 以覆盖存量升级场景。"""
    src = tmp_path / "skills_src"
    src.mkdir()
    _run_git(src, "init")
    _run_git(src, "config", "user.email", "test@sgme.local")
    _run_git(src, "config", "user.name", "SGME Test")
    (src / ".gitignore").write_text(OLD_GITIGNORE, encoding="utf-8")
    _run_git(src, "add", "-f", ".gitignore")
    _run_git(src, "commit", "-m", "chore: 初始化测试技能仓（旧版 .gitignore）")
    return src


# ---------- 1. 路径校验 ----------


class TestAssetRelpath:
    @pytest.mark.parametrize("raw,expect", [
        ("references/a.md", "references/a.md"),
        ("scripts/run.py", "scripts/run.py"),
        ("assets/logo.png", "assets/logo.png"),
        ("references/sub/deep.md", "references/sub/deep.md"),
        ("references\\a.md", "references/a.md"),
        ("references/./a.md", "references/a.md"),
    ])
    def test_valid_paths(self, raw, expect):
        assert skills_store._validate_asset_relpath(raw) == expect

    @pytest.mark.parametrize("raw", [
        "", ".", "..", "../etc/passwd", "references/../escape.md",
        "/abs/x.md", "C:/x.md", "secret/x.md", "notes/a.md", "tmp/a.md",
    ])
    def test_rejected_paths(self, raw):
        with pytest.raises(ValueError):
            skills_store._validate_asset_relpath(raw)


# ---------- 2. .gitignore 白名单与存量升级 ----------


class TestWorkspaceGitignore:
    def test_upgrades_legacy_single_file_version(self, repo):
        """存量旧版必须被幂等升级（原 if not exists() 对既有部署永不生效）。"""
        assert _gitignore_text(repo) == OLD_GITIGNORE
        before = _commit_count(repo)

        assert skills_store.ensure_workspace_gitignore(repo) is True

        now = _gitignore_text(repo)
        assert now == skills_store.WORKSPACE_GITIGNORE
        assert "!*/references/**" in now
        assert "!*/scripts/**" in now
        assert "!*/assets/**" in now
        assert _commit_count(repo) == before + 1

    def test_idempotent_when_already_current(self, repo):
        skills_store.ensure_workspace_gitignore(repo)
        before = _commit_count(repo)
        assert skills_store.ensure_workspace_gitignore(repo) is False
        assert _commit_count(repo) == before

    def test_bootstrap_when_missing(self, tmp_path):
        fresh = tmp_path / "fresh_repo"
        fresh.mkdir()
        _run_git(fresh, "init")
        _run_git(fresh, "config", "user.email", "t@e.com")
        _run_git(fresh, "config", "user.name", "T")
        assert skills_store.ensure_workspace_gitignore(fresh) is True
        assert _gitignore_text(fresh) == skills_store.WORKSPACE_GITIGNORE

    def test_asset_dirs_constant_matches_whitelist(self):
        for d in skills_store.ASSET_DIRS:
            assert f"!*/{d}/**" in skills_store.WORKSPACE_GITIGNORE


# ---------- 3. write_skill_file ----------


class TestWriteSkillFile:
    def test_success_writes_and_commits(self, repo):
        _mk_skill(repo)
        skills_store.ensure_workspace_gitignore(repo)
        before = _commit_count(repo)

        r = skills_store.write_skill_file("demo-skill", "references/notes.md", "内容",
                                          [str(repo)])

        assert r["ok"] is True
        assert r["bytes"] == len("内容".encode("utf-8"))
        dest = repo / "demo-skill" / "references" / "notes.md"
        assert dest.read_text(encoding="utf-8") == "内容"
        assert _commit_count(repo) == before + 1

    def test_asset_is_actually_tracked_by_git(self, repo):
        """核心断言：放开白名单后资产要**真的**进版本控制（否则等于没解冻）。"""
        _mk_skill(repo)
        skills_store.ensure_workspace_gitignore(repo)

        skills_store.write_skill_file("demo-skill", "references/a.md", "x", [str(repo)])
        skills_store.write_skill_file("demo-skill", "scripts/run.py", "print(1)", [str(repo)])

        tracked = _run_git(repo, "ls-files").stdout
        assert "demo-skill/references/a.md" in tracked
        assert "demo-skill/scripts/run.py" in tracked

    def test_skill_not_found(self, repo):
        skills_store.ensure_workspace_gitignore(repo)
        r = skills_store.write_skill_file("ghost", "references/a.md", "x", [str(repo)])
        assert r["ok"] is False and r["code"] == "not_found"

    def test_path_whitelist_enforced(self, repo):
        _mk_skill(repo)
        r = skills_store.write_skill_file("demo-skill", "secret/a.md", "x", [str(repo)])
        assert r["ok"] is False and r["code"] == "invalid_path"

    def test_quota_single_file(self, repo):
        _mk_skill(repo)
        r = skills_store.write_skill_file("demo-skill", "references/big.md", "x" * 100,
                                          [str(repo)], max_file_bytes=10)
        assert r["ok"] is False and r["code"] == "quota_exceeded"

    def test_quota_total_bytes(self, repo):
        _mk_skill(repo)
        skills_store.write_skill_file("demo-skill", "references/a.md", "x" * 50, [str(repo)])
        r = skills_store.write_skill_file("demo-skill", "references/b.md", "y" * 50,
                                          [str(repo)], max_total_bytes=60)
        assert r["ok"] is False and r["code"] == "quota_exceeded"

    def test_quota_max_files(self, repo):
        _mk_skill(repo)
        skills_store.write_skill_file("demo-skill", "references/a.md", "x", [str(repo)])
        r = skills_store.write_skill_file("demo-skill", "references/b.md", "y",
                                          [str(repo)], max_files=1)
        assert r["ok"] is False and r["code"] == "quota_exceeded"

    def test_overwrite_does_not_double_count(self, repo):
        """覆盖同一文件时旧大小要扣掉，否则重复写入会自我膨胀。"""
        _mk_skill(repo)
        skills_store.write_skill_file("demo-skill", "references/a.md", "x" * 100, [str(repo)])
        r = skills_store.write_skill_file("demo-skill", "references/a.md", "y" * 100,
                                          [str(repo)], max_total_bytes=150, max_files=1)
        assert r["ok"] is True

    def test_rename_via_overwrite(self, repo):
        """同一路径重复写是幂等覆盖，不产生重复文件。"""
        _mk_skill(repo)
        skills_store.write_skill_file("demo-skill", "references/a.md", "v1", [str(repo)])
        skills_store.write_skill_file("demo-skill", "references/a.md", "v2", [str(repo)])
        assert (repo / "demo-skill" / "references" / "a.md").read_text(encoding="utf-8") == "v2"
        used, count = skills_store._asset_usage(repo / "demo-skill")
        assert count == 1


# ---------- 4. 引用完整性提示（只提示不拦） ----------


class TestDanglingWarnings:
    def test_warns_when_referenced_asset_missing(self, repo):
        d = _mk_skill(repo)
        w = skills_store._dangling_asset_warnings(d, "见 [踩坑](references/07-section-07.md)")
        assert len(w) == 1
        assert "references/07-section-07.md" in w[0]

    def test_silent_when_asset_exists(self, repo):
        d = _mk_skill(repo)
        (d / "references").mkdir()
        (d / "references" / "ok.md").write_text("x", encoding="utf-8")
        assert skills_store._dangling_asset_warnings(d, "见 references/ok.md") == []

    def test_placeholder_also_warns_but_never_blocks(self, repo):
        """占位示例同样提示——但它是 warning 不是 violation。

        全库 438 技能有 245 个正文出现 assets 字样、其中大量是占位与文档性提及，
        硬拦会大面积误伤（同 gates.py 规则 5 的「按目录实体判据」）。
        """
        d = _mk_skill(repo)
        w = skills_store._dangling_asset_warnings(d, "参考 references/xxx.md 写法")
        assert len(w) == 1
        assert skills_store._dangling_asset_warnings(d, "") == []

    def test_dedupes_repeated_references(self, repo):
        d = _mk_skill(repo)
        w = skills_store._dangling_asset_warnings(
            d, "references/a.md 与 references/a.md 重复出现")
        assert len(w) == 1


# ---------- 5. 端点 ----------


@pytest.fixture
def cfg(tmp_path, repo):
    c = sgme_config.load_config()
    c["skills"] = {"enabled": True, "source_dirs": [str(repo)]}
    c["skills"]["tombstone_registry"] = str(tmp_path / "data" / "skills" / "tombstones.json")
    return c


@pytest.fixture
def conns(tmp_path, cfg):
    mem, session, wiki = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem, cfg["dimensions"], cfg["aliases"])
    yield mem, session, wiki
    db_mod.close(mem)
    db_mod.close(session)
    db_mod.close(wiki)


@pytest.fixture
def client(cfg, conns, monkeypatch, tmp_path):
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem, session, wiki = conns
    app = create_app(
        cfg=cfg,
        mem_conn=mem,
        session_conn=session,
        wiki_conn=wiki,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )
    return TestClient(app)


FILES = "/v1/admin/skills/demo-skill/files"


class TestAssetEndpoint:
    def test_agent_key_403(self, client):
        r = client.put(f"{FILES}/references/a.md", json={"content": "x"},
                       headers=AGENT_HEADERS)
        assert r.status_code == 403

    def test_no_key_403(self, client):
        r = client.put(f"{FILES}/references/a.md", json={"content": "x"})
        assert r.status_code == 403

    def test_success(self, client, repo):
        _mk_skill(repo)
        r = client.put(f"{FILES}/references/a.md", json={"content": "hello"},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert (repo / "demo-skill" / "references" / "a.md").read_text(encoding="utf-8") == "hello"

    def test_nested_path(self, client, repo):
        _mk_skill(repo)
        r = client.put(f"{FILES}/references/sub/deep.md", json={"content": "d"},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert (repo / "demo-skill" / "references" / "sub" / "deep.md").is_file()

    def test_skill_missing_404(self, client):
        r = client.put(f"{FILES}/references/a.md", json={"content": "x"},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 404

    def test_whitelist_violation_400(self, client, repo):
        _mk_skill(repo)
        r = client.put(f"{FILES}/secret/a.md", json={"content": "x"}, headers=ADMIN_HEADERS)
        assert r.status_code == 400
        assert "error" in r.json()

    def test_missing_content_400(self, client, repo):
        _mk_skill(repo)
        r = client.put(f"{FILES}/references/a.md", json={}, headers=ADMIN_HEADERS)
        assert r.status_code == 400
