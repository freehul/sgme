"""tests/test_routes_skills_admin.py：ST-36 M3 技能写侧管理端点 测试（TDD）。

覆盖：
1. 鉴权：无 Key → 403；Agent Key → 403（Admin 专用）
2. PUT /v1/admin/skills/{name}：{content} 自动解析 frontmatter；lint 违规 →
   400 且 error.details.violations 带完整违规清单；成功 → 200 ok=true；
   body 为空/非法名 → 400
3. DELETE ?hard=&force=：入向引用未 force → 409 + 引用清单；软删/硬删成功 → 200
4. POST rename：成功 200；新名冲突 → 409；旧名不存在 → 404

fixture 范式参照 tests/test_demands.py（隔离三库 + create_app 注入）。
skills.source_dirs 指向 tmp 下真实 git 仓库。
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

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}

VALID_CONTENT = (
    "---\n"
    "description: 合法技能描述\n"
    "version: 1.0.0\n"
    "pattern: manual\n"
    "category: testing\n"
    "triggers:\n"
    "  - 合法\n"
    "---\n"
    "# 标题\n\n正文\n"
)


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def skill_repo(tmp_path: Path) -> Path:
    src = tmp_path / "skills_src"
    src.mkdir()
    _run_git(src, "init")
    _run_git(src, "config", "user.email", "test@sgme.local")
    _run_git(src, "config", "user.name", "SGME Test")
    (src / ".gitignore").write_text("*\n!*/\n!*/SKILL.md\n", encoding="utf-8")
    _run_git(src, "add", "-f", ".gitignore")
    _run_git(src, "commit", "-m", "chore: 初始化测试技能仓")
    return src


@pytest.fixture
def cfg(tmp_path, skill_repo):
    c = sgme_config.load_config()
    c["skills"] = {"enabled": True, "source_dirs": [str(skill_repo)]}
    # 墓碑登记目录指向测试临时区
    c["skills"]["tombstone_registry"] = str(tmp_path / "data" / "skills" / "tombstones.json")
    return c


@pytest.fixture
def conns(tmp_path, cfg):
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def no_bearer(monkeypatch):
    """清除 SGME_BEARER_TOKEN（create_app 的 setdefault 是进程级副作用）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)


@pytest.fixture
def app(cfg, conns, no_bearer, tmp_path):
    mem, session, wiki = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem,
        session_conn=session,
        wiki_conn=wiki,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        agent_store_path=tmp_path / "agent_keys.json",
        start_background_tasks=False,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


BASE = "/v1/admin/skills"


# ---------- 鉴权 ----------


class TestAuth:
    def test_no_key_403(self, client):
        assert client.put(f"{BASE}/some-skill", json={"content": VALID_CONTENT}).status_code == 403
        assert client.delete(f"{BASE}/some-skill").status_code == 403
        assert client.post(f"{BASE}/some-skill/rename", json={"new_name": "x"}).status_code == 403

    def test_agent_key_403(self, client):
        assert client.put(
            f"{BASE}/some-skill", json={"content": VALID_CONTENT}, headers=AGENT_HEADERS
        ).status_code == 403


# ---------- PUT 写入 ----------


class TestPutSkill:
    def test_put_content_ok(self, client, skill_repo):
        r = client.put(f"{BASE}/put-ok", json={"content": VALID_CONTENT}, headers=ADMIN_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert (skill_repo / "put-ok" / "SKILL.md").exists()

    def test_put_meta_body_ok(self, client, skill_repo):
        body = {
            "meta": {
                "description": "字段式写入",
                "version": "1.0.0",
                "pattern": "manual",
                "category": "testing",
            },
            "body": "# 字段式\n正文",
        }
        r = client.put(f"{BASE}/meta-style", json=body, headers=ADMIN_HEADERS)
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_put_lint_violation_400_with_list(self, client, skill_repo):
        bad = VALID_CONTENT.replace("description: 合法技能描述\n", "")  # 缺 description
        r = client.put(f"{BASE}/bad-lint", json={"content": bad}, headers=ADMIN_HEADERS)
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"].startswith("ERR_")
        violations = err["details"]["violations"]
        assert isinstance(violations, list) and any("description" in v for v in violations)

    def test_put_empty_body_400(self, client):
        r = client.put(f"{BASE}/empty", json={}, headers=ADMIN_HEADERS)
        assert r.status_code == 400

    def test_put_invalid_name_400(self, client):
        # httpx 会把路径中的 .. 归一化掉（../escape → /escape）→ 路由不匹配；
        # 穿越形态经 URL 编码后到达路由层：无 SPA catch-all 时=白名单 400，
        # 有 catch-all（ui/dist 存在）时路径通配命中但方法不符 → 405。
        r = client.put(f"{BASE}/%2e%2e/escape", json={"content": VALID_CONTENT},
                       headers=ADMIN_HEADERS)
        assert r.status_code in (400, 404, 405)
        r2 = client.put(f"{BASE}/Bad%5FName", json={"content": VALID_CONTENT},
                        headers=ADMIN_HEADERS)
        assert r2.status_code == 400
        assert any("kebab" in v for v in r2.json()["error"]["details"]["violations"])

    def test_put_duplicate_rejected_409(self, client):
        assert client.put(
            f"{BASE}/dup-x", json={"content": VALID_CONTENT}, headers=ADMIN_HEADERS
        ).status_code == 200
        r = client.put(f"{BASE}/dup-x", json={"content": VALID_CONTENT}, headers=ADMIN_HEADERS)
        # 同名重复提交按查重拒绝（409 冲突）
        assert r.status_code == 409, r.text


# ---------- DELETE 删除 ----------


class TestDeleteSkill:
    def _seed_pair(self, client):
        """alpha 被 beta 的 frontmatter uses 引用。"""
        r1 = client.put(f"{BASE}/alpha-del", json={"content": VALID_CONTENT}, headers=ADMIN_HEADERS)
        assert r1.status_code == 200, r1.text
        uses_beta = (
            "---\n"
            "description: 依赖方技能\n"
            "version: 1.0.0\n"
            "pattern: manual\n"
            "category: testing\n"
            "uses:\n"
            "  - alpha-del\n"
            "---\n正文提及 alpha-del\n"
        )
        r2 = client.put(f"{BASE}/beta-user", json={"content": uses_beta}, headers=ADMIN_HEADERS)
        assert r2.status_code == 200, r2.text

    def test_delete_blocked_by_inbound_uses_409(self, client):
        self._seed_pair(client)
        r = client.delete(f"{BASE}/alpha-del", headers=ADMIN_HEADERS)
        assert r.status_code == 409
        refs = r.json()["error"]["details"]["referenced_by"]
        assert any("beta-user" in x for x in refs)

    def test_soft_delete_ok(self, client, skill_repo):
        self._seed_pair(client)
        r = client.delete(f"{BASE}/beta-user", headers=ADMIN_HEADERS)
        assert r.status_code == 200 and r.json()["ok"] is True
        text = (skill_repo / "beta-user" / "SKILL.md").read_text(encoding="utf-8")
        assert "deprecated: true" in text

    def test_hard_delete_ok(self, client, skill_repo):
        self._seed_pair(client)
        r = client.delete(f"{BASE}/alpha-del?hard=true&force=true", headers=ADMIN_HEADERS)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert not (skill_repo / "alpha-del").exists()

    def test_delete_missing_404(self, client):
        r = client.delete(f"{BASE}/ghost-x", headers=ADMIN_HEADERS)
        assert r.status_code == 404


# ---------- POST rename ----------


class TestRenameSkill:
    def test_rename_ok_tombstone_written(self, client, skill_repo, tmp_path):
        assert client.put(
            f"{BASE}/old-nm", json={"content": VALID_CONTENT}, headers=ADMIN_HEADERS
        ).status_code == 200
        r = client.post(
            f"{BASE}/old-nm/rename", json={"new_name": "new-nm"}, headers=ADMIN_HEADERS
        )
        assert r.status_code == 200, r.text
        assert "superseded_by: new-nm" in (
            skill_repo / "old-nm" / "SKILL.md"
        ).read_text(encoding="utf-8")
        # 墓碑登记文件（fixture 写入 cfg.skills.tombstone_registry 指定的测试路径）
        reg_path = tmp_path / "data" / "skills" / "tombstones.json"
        assert reg_path.exists()

    def test_rename_to_existing_409(self, client):
        # 两个技能内容必须不同（同内容异名会被 SHA 查重拒绝）
        for n, body in (("r-a", "# 甲\n内容甲"), ("r-b", "# 乙\n内容乙")):
            payload = {
                "meta": {"description": "改名冲突用", "version": "1.0.0",
                         "pattern": "manual", "category": "testing"},
                "body": body,
            }
            assert client.put(
                f"{BASE}/{n}", json=payload, headers=ADMIN_HEADERS
            ).status_code == 200
        r = client.post(f"{BASE}/r-a/rename", json={"new_name": "r-b"}, headers=ADMIN_HEADERS)
        assert r.status_code == 409

    def test_rename_missing_old_404(self, client):
        r = client.post(f"{BASE}/ghost-y/rename", json={"new_name": "zz"}, headers=ADMIN_HEADERS)
        assert r.status_code == 404
