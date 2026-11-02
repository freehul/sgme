"""tests/test_skill_lint.py：技能格式 lint（T-403，设计 §5.2 Anthropic 基准）测试（TDD）。

覆盖三层：
1. 纯规则引擎 ``sgme.operations.skill_lint.lint_skill``：
   - 名称字符集/长度 1–64/连字符首尾（64 过、65 拒）
   - description 非空/≤1024（1024 过、1025 拒）+ 触发式写法 warn
   - content 行数 <500（499 过、500 拒）、YAML frontmatter 必备、正文非空
   - frontmatter name/description 必需且与入参一致
2. 写入门禁：store.write_skill error 阻断 / warn 放行回传；HTTP PUT 与 /lint 端点
3. 批量扫描脚本：目录模式 + skills.db 模式（只读）
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.operations.skill_lint import (
    MAX_CONTENT_LINES,
    MAX_DESCRIPTION_LEN,
    MAX_NAME_LEN,
    lint_skill,
)
from sgme.server.app import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

ADMIN_KEY = "test-admin-key"
AGENT_KEY = "test-agent-key"
ADMIN_HEADERS = {"X-API-Key": ADMIN_KEY}
AGENT_HEADERS = {"X-API-Key": AGENT_KEY}
BASE = "/v1/admin/skills"

GOOD_DESC = "当需要核对技能格式时使用。"


# ---------- 构造工具 ----------


def _skill_md(
    name: str = "demo-skill",
    description: str = GOOD_DESC,
    body: str = "# 标题\n\n正文",
    *,
    include_name: bool = True,
    include_description: bool = True,
    extra_meta: str = "",
) -> str:
    """构造 SKILL.md 全文（可缺字段，用于负例）。"""
    lines = ["---"]
    if include_name:
        lines.append(f"name: {name}")
    if include_description:
        lines.append(f"description: {description}")
    if extra_meta:
        lines.extend(extra_meta.rstrip("\n").splitlines())
    else:
        lines.append("category: testing")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def _skill_md_with_lines(total: int, **kw) -> str:
    """构造恰好 ``total`` 行（splitlines 口径）的 SKILL.md。"""
    head = _skill_md(body="", **kw)
    body_lines = total - len(head.splitlines())
    assert body_lines > 0
    body = "\n".join("正文行" for _ in range(body_lines))
    return _skill_md(body=body, **kw)


def _rules(results, level: str | None = None) -> set[str]:
    return {r["rule"] for r in results if level is None or r["level"] == level}


# ---------- 返回结构 ----------


class TestResultShape:
    def test_items_have_level_rule_message(self):
        results = lint_skill("Bad_Name", "", "# 无 frontmatter")
        assert results, "非法输入应产出违规项"
        for item in results:
            assert set(item) == {"level", "rule", "message"}
            assert item["level"] in ("error", "warn")
            assert isinstance(item["rule"], str) and item["rule"]
            assert isinstance(item["message"], str) and item["message"]


# ---------- 名称 ----------


class TestName:
    def test_simple_name_passes(self):
        assert lint_skill("demo-skill", GOOD_DESC, _skill_md()) == []

    @pytest.mark.parametrize("name", ["a", "a" * MAX_NAME_LEN, "with_underscore", "a-b-c"])
    def test_valid_names_pass(self, name):
        assert "name_length" not in _rules(lint_skill(name, GOOD_DESC, _skill_md(name=name)))

    def test_name_64_chars_passes(self):
        name = "a" * 64
        results = lint_skill(name, GOOD_DESC, _skill_md(name=name))
        assert "name_length" not in _rules(results)

    def test_name_65_chars_rejected(self):
        name = "a" * 65
        results = lint_skill(name, GOOD_DESC, _skill_md(name=name))
        assert "name_length" in _rules(results, "error")

    @pytest.mark.parametrize("name", ["Bad-Name", "dot.name", "has space", "中文名", "a/b"])
    def test_illegal_chars_rejected(self, name):
        results = lint_skill(name, GOOD_DESC, _skill_md(name=name))
        assert "name_charset" in _rules(results, "error")

    @pytest.mark.parametrize("name", ["-lead", "trail-"])
    def test_hyphen_edges_rejected(self, name):
        results = lint_skill(name, GOOD_DESC, _skill_md(name=name))
        assert "name_hyphen_edge" in _rules(results, "error")

    def test_empty_name_rejected(self):
        results = lint_skill("", GOOD_DESC, _skill_md(name="demo-skill"))
        assert "name_length" in _rules(results, "error")


# ---------- description ----------


class TestDescription:
    def test_1024_chars_passes(self):
        desc = "当" + ("字" * (MAX_DESCRIPTION_LEN - 2)) + "时"  # 长度恰好 1024
        assert len(desc) == MAX_DESCRIPTION_LEN
        results = lint_skill("demo-skill", desc, _skill_md(description=desc))
        assert "description_length" not in _rules(results)

    def test_1025_chars_rejected(self):
        desc = "当" + ("字" * (MAX_DESCRIPTION_LEN - 1)) + "时"  # 长度 1025
        assert len(desc) == MAX_DESCRIPTION_LEN + 1
        results = lint_skill("demo-skill", desc, _skill_md(description=desc))
        assert "description_length" in _rules(results, "error")

    def test_empty_description_rejected(self):
        results = lint_skill("demo-skill", "", _skill_md(description=""))
        assert "description_empty" in _rules(results, "error")

    @pytest.mark.parametrize(
        "desc",
        ["当用户需要排查告警时使用。", "Use when the user reports an outage.", "触发场景：发版失败"],
    )
    def test_trigger_style_no_warning(self, desc):
        results = lint_skill("demo-skill", desc, _skill_md(description=desc))
        assert "description_trigger" not in _rules(results)

    def test_non_trigger_style_warns_not_blocks(self):
        desc = "一套普通的技能说明文字"
        results = lint_skill("demo-skill", desc, _skill_md(description=desc))
        assert "description_trigger" in _rules(results, "warn")
        assert "description_trigger" not in _rules(results, "error")


# ---------- 正文 / frontmatter ----------


class TestContent:
    def test_499_lines_passes(self):
        text = _skill_md_with_lines(499)
        assert len(text.splitlines()) == 499
        assert "content_line_limit" not in _rules(lint_skill("demo-skill", GOOD_DESC, text))

    def test_500_lines_rejected(self):
        text = _skill_md_with_lines(500)
        assert len(text.splitlines()) == 500
        results = lint_skill("demo-skill", GOOD_DESC, text)
        assert "content_line_limit" in _rules(results, "error")
        assert MAX_CONTENT_LINES == 500

    def test_missing_frontmatter_rejected(self):
        results = lint_skill("demo-skill", GOOD_DESC, "# 标题\n正文")
        assert "content_frontmatter_missing" in _rules(results, "error")

    def test_empty_body_rejected(self):
        results = lint_skill("demo-skill", GOOD_DESC, _skill_md(body=""))
        assert "content_empty" in _rules(results, "error")

    def test_missing_name_field_rejected(self):
        results = lint_skill("demo-skill", GOOD_DESC, _skill_md(include_name=False))
        assert "frontmatter_name_missing" in _rules(results, "error")

    def test_missing_description_field_rejected(self):
        results = lint_skill("demo-skill", GOOD_DESC, _skill_md(include_description=False))
        assert "frontmatter_description_missing" in _rules(results, "error")

    def test_name_mismatch_rejected(self):
        results = lint_skill("demo-skill", GOOD_DESC, _skill_md(name="other-skill"))
        assert "frontmatter_name_mismatch" in _rules(results, "error")

    def test_description_mismatch_rejected(self):
        results = lint_skill(
            "demo-skill", "入参描述", _skill_md(description="frontmatter 里的另一段描述")
        )
        assert "frontmatter_description_mismatch" in _rules(results, "error")


# ---------- 写入门禁（store） ----------


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


META_TEMPLATE = {
    "description": GOOD_DESC,
    "category": "testing",
    "pattern": "manual",
    "version": "1.0.0",
}


def _meta(name: str, **over) -> dict:
    return {"name": name, **META_TEMPLATE, **over}


class TestStoreGate:
    def test_format_error_blocks_write(self, skill_repo):
        from sgme.skills.store import write_skill

        name = "a" * 65
        r = write_skill(name, _meta(name), "# 正文", [str(skill_repo)])
        assert r["ok"] is False and r["code"] == "lint_failed"
        assert any("64" in v or "长度" in v for v in r["violations"])
        assert not (skill_repo / name).exists()

    def test_warn_passes_and_is_returned(self, skill_repo):
        from sgme.skills.store import write_skill

        r = write_skill(
            "warn-skill", _meta("warn-skill", description="一段普通说明"), "# 正文", [str(skill_repo)]
        )
        assert r["ok"] is True, r
        assert any("触发" in w for w in r["warnings"])
        assert (skill_repo / "warn-skill" / "SKILL.md").exists()

    def test_line_limit_blocks_write(self, skill_repo):
        from sgme.skills.store import write_skill

        body = "\n".join("正文行" for _ in range(600))
        r = write_skill("long-skill", _meta("long-skill"), body, [str(skill_repo)])
        assert r["ok"] is False and r["code"] == "lint_failed"
        assert any("行" in v for v in r["violations"])


# ---------- HTTP 端点 ----------


@pytest.fixture
def cfg(tmp_path, skill_repo):
    c = sgme_config.load_config()
    c["skills"] = {"enabled": True, "source_dirs": [str(skill_repo)]}
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


class TestLintEndpoint:
    def test_valid_content_ok_and_not_written(self, client, skill_repo):
        r = client.post(
            f"{BASE}/lint-demo/lint",
            json={"content": _skill_md(name="lint-demo")},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["errors"] == []
        assert not (skill_repo / "lint-demo").exists()  # 只校验不入库

    def test_invalid_content_reports_errors_without_writing(self, client, skill_repo):
        name = "a" * 65
        r = client.post(
            f"{BASE}/{name}/lint", json={"content": _skill_md(name=name)}, headers=ADMIN_HEADERS
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is False
        assert any(e["rule"] == "name_length" for e in data["errors"])
        assert not (skill_repo / name).exists()

    def test_missing_name_in_frontmatter_reports_error(self, client):
        r = client.post(
            f"{BASE}/no-name/lint",
            json={"content": _skill_md(include_name=False)},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert any(e["rule"] == "frontmatter_name_missing" for e in r.json()["errors"])

    def test_requires_admin_key(self, client):
        r = client.post(f"{BASE}/x/lint", json={"content": _skill_md(name="x")})
        assert r.status_code == 403
        r2 = client.post(
            f"{BASE}/x/lint", json={"content": _skill_md(name="x")}, headers=AGENT_HEADERS
        )
        assert r2.status_code == 403


class TestPutGate:
    def test_put_missing_name_rejected(self, client):
        r = client.put(
            f"{BASE}/put-no-name",
            json={"content": _skill_md(include_name=False)},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400, r.text
        violations = r.json()["error"]["details"]["violations"]
        assert any("name" in v for v in violations)

    def test_put_warn_passes_with_warnings(self, client):
        content = _skill_md(
            name="put-warn", description="一段普通说明"
        )
        r = client.put(f"{BASE}/put-warn", json={"content": content}, headers=ADMIN_HEADERS)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert any("触发" in w for w in data["warnings"])


# ---------- 批量扫描脚本 ----------


def _write_skill_file(root: Path, name: str, description: str = GOOD_DESC, body: str = "# 正文\n内容"):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_skill_md(name=name, description=description, body=body), encoding="utf-8")


class TestLintAllScript:
    def test_scan_source_dirs_flags_only_bad(self, tmp_path):
        import lint_all_skills

        lib = tmp_path / "lib"
        _write_skill_file(lib, "good-skill")
        _write_skill_file(lib, "a" * 65)  # 名称超长
        rows = lint_all_skills.scan_source_dirs([str(lib)])
        by_name = {r["name"]: r for r in rows}
        assert by_name["good-skill"]["errors"] == []
        assert any(e["rule"] == "name_length" for e in by_name["a" * 65]["errors"])

    def test_scan_db_reads_rows(self, tmp_path):
        import lint_all_skills

        db = tmp_path / "skills.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE skills (name TEXT, description TEXT, content TEXT)")
        conn.executemany(
            "INSERT INTO skills VALUES (?, ?, ?)",
            [
                ("db-good", GOOD_DESC, "# 正文\n内容"),
                ("a" * 65, GOOD_DESC, "# 正文\n内容"),
            ],
        )
        conn.commit()
        conn.close()
        rows = lint_all_skills.scan_db(str(db))
        assert len(rows) == 2
        assert {r["name"] for r in rows if r["errors"]} == {"a" * 65}
