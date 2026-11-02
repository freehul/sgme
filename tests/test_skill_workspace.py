# -*- coding: utf-8 -*-
"""技能编写工作流测试（T-407，设计 §5.2：本地工作副本 + 提交入库命令）。

覆盖：
- pull → 改 SKILL.md/附属文件 → status 差异分类 → push 全链路：
  库内 sha 变更、附属文件新增/覆盖/删除落库、修订史追加、manifest 刷新；
- 附属文件表缺失时 pull 自动 ensure_tables（迁移期老库兜底）；
- 格式 lint 拒绝（error 时零写入，并给出违规清单）；
- 工作副本有未入库改动时拒绝覆盖，--force 才重拉；
- 库在 pull 后变化 → status 报「库已更新」，push 默认冲突、--force 覆盖；
- list 汇总多个工作副本状态；
- CLI 四个子命令的退出码与中文输出（pull/status/push/list）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sgme.data import skill_package_dao, skills_dao
from sgme.data.db import connect_skills
from sgme.operations import skill_workspace as ws
from sgme.skills.indexer import parse_skill_md
from sgme.skills.store import render_skill_md

DESCRIPTION = "演示技能：验证本地工作副本的拉取、比对与入库链路。"
BODY_V1 = "# 演示技能\n\n第一版正文。\n"


def _meta(name: str = "demo-skill") -> dict:
    return {
        "name": name,
        "description": DESCRIPTION,
        "category": "demo",
        "tags": ["skill", "demo"],
        "version": "1.0.0",
        "pattern": "manual",
    }


def _body_sha(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def seed_skill(
    conn,
    name: str = "demo-skill",
    *,
    with_files: bool = True,
    body: str = BODY_V1,
) -> dict:
    """直接写库造种子技能（模拟迁移后的库内既有数据）。

    with_files=False 时**不**调用 ensure_tables，用于验证 pull 的兜底建表。
    """
    meta = _meta(name)
    md = render_skill_md(meta, body)
    parsed = parse_skill_md(md)
    stored_body = parsed["body"].strip()
    skills_dao.upsert_skill(
        conn,
        {
            "name": name,
            "sha256": _body_sha(stored_body),
            "description": meta["description"],
            "category": meta["category"],
            "tags": meta["tags"],
            "version": meta["version"],
            "pattern": meta["pattern"],
            "source": "test",
            "origin_path": f"/seed/{name}/SKILL.md",
            "content": stored_body,
        },
    )
    conn.commit()
    if with_files:
        skill_package_dao.ensure_tables(conn)
        skill_package_dao.put_files(
            conn,
            name,
            [
                {"path": "references/api.md", "content": b"api v1\n"},
                {"path": "assets/logo.bin", "content": b"\x00\x01\x02"},
            ],
        )
    return {"meta": meta, "md": md, "body": stored_body}


@pytest.fixture
def library(tmp_path: Path):
    """tmp_path 下的独立 skills.db（文件库，CLI 可另开连接）。"""
    conn = connect_skills(tmp_path / "data")
    yield conn, tmp_path / "data"
    conn.close()


def _work_dir(tmp_path: Path, name: str = "demo-skill") -> Path:
    return tmp_path / "work" / name


def _set_pattern(skill_md: Path, value: str) -> None:
    text = skill_md.read_text(encoding="utf-8")
    assert "pattern: manual" in text
    skill_md.write_text(text.replace("pattern: manual", f"pattern: {value}"), encoding="utf-8")


# ---------- 全链路 ----------


def test_pull_edit_status_push_roundtrip(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn)
    old_sha = skills_dao.get_skill(conn, "demo-skill")["sha256"]
    work = _work_dir(tmp_path)

    pulled = ws.pull_skill(conn, "demo-skill", work)
    assert pulled["ok"] is True
    # SKILL.md + 2 个附属文件 + .sgme-manifest.json
    assert pulled["files"] == 4
    assert (work / "SKILL.md").is_file()
    assert (work / "references" / "api.md").read_bytes() == b"api v1\n"
    assert (work / "assets" / "logo.bin").read_bytes() == b"\x00\x01\x02"
    manifest = json.loads((work / ws.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["name"] == "demo-skill"
    assert {f["path"] for f in manifest["files"]} == {
        "references/api.md",
        "assets/logo.bin",
    }

    # 刚拉下来必须判干净（SKILL.md 语义比较，不受 YAML 空白差异影响）
    clean = ws.status_skill(conn, work)
    assert clean["dirty"] is False
    assert clean["skill"]["state"] == "same"

    # 改正文、改附属、加脚本、删资源
    text = (work / "SKILL.md").read_text(encoding="utf-8")
    (work / "SKILL.md").write_text(
        text.replace("第一版正文。", "第二版正文。\n新增内容。"), encoding="utf-8"
    )
    (work / "references" / "api.md").write_bytes(b"api v2\n")
    (work / "scripts").mkdir()
    (work / "scripts" / "run.py").write_bytes(b"print('hi')\n")
    (work / "assets" / "logo.bin").unlink()

    st = ws.status_skill(conn, work)
    assert st["dirty"] is True
    assert st["skill"]["state"] == "modified"
    assert st["skill"]["changed_by"] == "local"
    assert st["files"]["modified"] == ["references/api.md"]
    assert st["files"]["added"] == ["scripts/run.py"]
    assert st["files"]["deleted"] == ["assets/logo.bin"]

    pushed = ws.push_skill(conn, work)
    assert pushed["ok"] is True and pushed["changed"] is True
    assert pushed["attachments_written"] == 2
    assert pushed["attachments_deleted"] == 1

    row = skills_dao.get_skill(conn, "demo-skill")
    expected_body = parse_skill_md(
        (work / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    )["body"].strip()
    assert row["sha256"] != old_sha
    assert row["sha256"] == _body_sha(expected_body)
    assert "第二版正文。" in row["content"]

    # 附属文件：修改覆盖、新增入库、删除清掉
    assert skill_package_dao.get_file(conn, "demo-skill", "references/api.md") == b"api v2\n"
    assert skill_package_dao.get_file(conn, "demo-skill", "scripts/run.py") == b"print('hi')\n"
    assert skill_package_dao.get_file(conn, "demo-skill", "assets/logo.bin") is None
    assert [r["path"] for r in skill_package_dao.list_files(conn, "demo-skill")] == [
        "references/api.md",
        "scripts/run.py",
    ]

    # 修订史 + manifest 与库内包指纹一致
    revisions = skill_package_dao.list_revisions(conn, "demo-skill")
    assert len(revisions) == 1
    assert revisions[0]["file_count"] == 3  # SKILL.md + 2 个附属
    assert revisions[0]["note"] == "workspace push"
    manifest = json.loads((work / ws.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["package_sha256"] == ws.library_state(conn, "demo-skill")["package_sha256"]
    assert manifest["skill_sha256"] == row["sha256"]

    # push 后立即再 push：幂等，无变更不再写修订史
    again = ws.push_skill(conn, work)
    assert again["ok"] is True and again["changed"] is False
    assert len(skill_package_dao.list_revisions(conn, "demo-skill")) == 1

    # push 后状态回到干净
    assert ws.status_skill(conn, work)["dirty"] is False


def test_pull_into_arbitrary_dir_name_then_push(library, tmp_path: Path) -> None:
    """--dir 只是本地路径，不必与技能名同名；push 以清单里的 name 为准。"""
    conn, _ = library
    seed_skill(conn)
    work = tmp_path / "editing" / "my-pdf-copy"
    assert ws.pull_skill(conn, "demo-skill", work)["ok"] is True
    text = (work / "SKILL.md").read_text(encoding="utf-8")
    (work / "SKILL.md").write_text(
        text.replace("第一版正文。", "任意目录名版本。"), encoding="utf-8"
    )
    pushed = ws.push_skill(conn, work)
    assert pushed["ok"] is True, pushed
    assert pushed["name"] == "demo-skill"
    assert ws.status_skill(conn, work)["dirty"] is False


def test_pull_refreshes_when_only_library_changed(library, tmp_path: Path) -> None:
    """本地未动、库被别人更新：pull 直接刷新，不该误报「有未入库改动」。"""
    conn, _ = library
    seed_skill(conn)
    work = _work_dir(tmp_path)
    assert ws.pull_skill(conn, "demo-skill", work)["ok"] is True

    seed_skill(conn, body="# 演示技能\n\n库内新版本。\n")
    refreshed = ws.pull_skill(conn, "demo-skill", work)
    assert refreshed["ok"] is True, refreshed
    assert "库内新版本。" in (work / "SKILL.md").read_text(encoding="utf-8")
    assert ws.status_skill(conn, work)["dirty"] is False


# ---------- 建表兜底 ----------


def test_pull_creates_attachment_tables_when_absent(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn, with_files=False)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "skill_files" not in tables
    assert "skill_revisions" not in tables

    pulled = ws.pull_skill(conn, "demo-skill", _work_dir(tmp_path))
    assert pulled["ok"] is True
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"skill_files", "skill_revisions"} <= tables
    assert (work_md := (_work_dir(tmp_path) / "SKILL.md")).is_file()
    assert "第一版正文。" in work_md.read_text(encoding="utf-8")


# ---------- lint 门禁 ----------


def test_push_lint_error_rejects_with_violations(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn)
    old_sha = skills_dao.get_skill(conn, "demo-skill")["sha256"]
    work = _work_dir(tmp_path)
    assert ws.pull_skill(conn, "demo-skill", work)["ok"] is True

    _set_pattern(work / "SKILL.md", "乱写")
    result = ws.push_skill(conn, work)

    assert result["ok"] is False
    assert result["code"] == "lint_failed"
    assert any("pattern" in v for v in result["violations"])
    # 零写入：sha 不变、无修订史、库内附属文件保持原状
    assert skills_dao.get_skill(conn, "demo-skill")["sha256"] == old_sha
    assert skill_package_dao.list_revisions(conn, "demo-skill") == []
    assert skill_package_dao.get_file(conn, "demo-skill", "references/api.md") == b"api v1\n"


def test_push_rejects_name_mismatch(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn)
    work = _work_dir(tmp_path)
    assert ws.pull_skill(conn, "demo-skill", work)["ok"] is True
    text = (work / "SKILL.md").read_text(encoding="utf-8")
    (work / "SKILL.md").write_text(
        text.replace("name: demo-skill", "name: other-skill"), encoding="utf-8"
    )
    result = ws.push_skill(conn, work)
    assert result["ok"] is False
    assert result["code"] == "lint_failed"
    assert any("不一致" in v for v in result["violations"])


# ---------- pull 覆盖保护 ----------


def test_pull_refuses_dirty_copy_then_force(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn)
    work = _work_dir(tmp_path)
    assert ws.pull_skill(conn, "demo-skill", work)["ok"] is True
    (work / "SKILL.md").write_text("# 本地未入库的改动\n", encoding="utf-8")

    refused = ws.pull_skill(conn, "demo-skill", work)
    assert refused["ok"] is False
    assert refused["code"] == "dirty"
    assert "本地未入库的改动" in (work / "SKILL.md").read_text(encoding="utf-8")

    forced = ws.pull_skill(conn, "demo-skill", work, force=True)
    assert forced["ok"] is True
    assert "第一版正文。" in (work / "SKILL.md").read_text(encoding="utf-8")
    assert ws.status_skill(conn, work)["dirty"] is False


def test_pull_refuses_foreign_directory(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn)
    work = _work_dir(tmp_path)
    work.mkdir(parents=True)
    (work / "random.txt").write_text("x", encoding="utf-8")

    result = ws.pull_skill(conn, "demo-skill", work)
    assert result["ok"] is False
    assert result["code"] == "occupied"
    assert (work / "random.txt").is_file()  # 未动原目录

    assert ws.pull_skill(conn, "demo-skill", work, force=True)["ok"] is True
    assert not (work / "random.txt").exists()


# ---------- 冲突检测 ----------


def test_status_reports_library_drift_and_push_conflict(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn)
    work = _work_dir(tmp_path)
    assert ws.pull_skill(conn, "demo-skill", work)["ok"] is True

    # 模拟别处 push：库内正文变化
    seed_skill(conn, body="# 演示技能\n\n库内被别人改过。\n")

    st = ws.status_skill(conn, work)
    assert st["dirty"] is True
    assert st["skill"]["state"] == "modified"
    assert st["skill"]["changed_by"] == "library"
    assert st["library_changed"] is True

    conflicted = ws.push_skill(conn, work)
    assert conflicted["ok"] is False
    assert conflicted["code"] == "conflict"

    forced = ws.push_skill(conn, work, force=True)
    assert forced["ok"] is True and forced["changed"] is True
    assert "第一版正文。" in skills_dao.get_skill(conn, "demo-skill")["content"]


def test_push_new_skill_without_manifest(library, tmp_path: Path) -> None:
    conn, _ = library
    work = tmp_path / "work" / "brand-new"
    work.mkdir(parents=True)
    meta = _meta("brand-new")
    (work / "SKILL.md").write_text(render_skill_md(meta, BODY_V1), encoding="utf-8")
    (work / "references").mkdir()
    (work / "references" / "note.md").write_bytes(b"note\n")

    result = ws.push_skill(conn, work)
    assert result["ok"] is True and result["changed"] is True
    assert skills_dao.get_skill(conn, "brand-new") is not None
    assert skill_package_dao.get_file(conn, "brand-new", "references/note.md") == b"note\n"
    assert (work / ws.MANIFEST_NAME).is_file()  # push 后补清单


# ---------- list ----------


def test_list_workspaces_aggregates(library, tmp_path: Path) -> None:
    conn, _ = library
    seed_skill(conn, "demo-skill")
    seed_skill(conn, "other-skill")
    root = tmp_path / "work"
    assert ws.pull_skill(conn, "demo-skill", root / "demo-skill")["ok"] is True
    assert ws.pull_skill(conn, "other-skill", root / "other-skill")["ok"] is True
    (root / "demo-skill" / "references" / "api.md").write_bytes(b"changed\n")

    entries = ws.list_workspaces(conn, root)
    by_name = {e["name"]: e for e in entries}
    assert set(by_name) == {"demo-skill", "other-skill"}
    assert by_name["demo-skill"]["status"]["dirty"] is True
    assert by_name["other-skill"]["status"]["dirty"] is False


# ---------- CLI ----------


def test_cli_pull_status_push_list(tmp_path: Path, capsys) -> None:
    from scripts import skill_workspace as cli

    data_dir = tmp_path / "cli-data"
    conn = connect_skills(data_dir)
    seed_skill(conn)
    old_sha = skills_dao.get_skill(conn, "demo-skill")["sha256"]
    conn.close()

    work = tmp_path / "cli-work" / "demo-skill"
    base = ["--data-dir", str(data_dir)]

    assert cli.main(["pull", "demo-skill", "--dir", str(work), *base]) == 0
    assert "已拉取 demo-skill" in capsys.readouterr().out

    text = (work / "SKILL.md").read_text(encoding="utf-8")
    (work / "SKILL.md").write_text(
        text.replace("第一版正文。", "CLI 版本正文。"), encoding="utf-8"
    )
    (work / "scripts").mkdir()
    (work / "scripts" / "run.py").write_bytes(b"print(1)\n")

    assert cli.main(["status", "--dir", str(work), *base]) == 1
    out = capsys.readouterr().out
    assert "已修改" in out and "scripts/run.py" in out

    assert cli.main(["push", "--dir", str(work), *base]) == 0
    out = capsys.readouterr().out
    assert "已入库 demo-skill" in out

    conn = connect_skills(data_dir)
    assert skills_dao.get_skill(conn, "demo-skill")["sha256"] != old_sha
    assert skill_package_dao.get_file(conn, "demo-skill", "scripts/run.py") == b"print(1)\n"
    conn.close()

    assert cli.main(["status", "--dir", str(work), *base]) == 0
    assert "无差异" in capsys.readouterr().out

    assert cli.main(["list", "--dir", str(tmp_path / "cli-work"), *base]) == 0
    out = capsys.readouterr().out
    assert "demo-skill: 无变更" in out


def test_cli_db_option_points_at_existing_library(tmp_path: Path, capsys) -> None:
    """--db 直接指向 skills.db（容器/NAS 上常见的用法）应可用。"""
    from scripts import skill_workspace as cli

    data_dir = tmp_path / "db-data"
    conn = connect_skills(data_dir)
    seed_skill(conn)
    conn.close()
    db_path = data_dir / "skills.db"
    work = tmp_path / "db-work" / "demo-skill"

    assert cli.main(["pull", "demo-skill", "--dir", str(work), "--db", str(db_path)]) == 0
    assert (work / "SKILL.md").is_file()
    assert cli.main(["status", "--dir", str(work), "--db", str(db_path)]) == 0
    assert "无差异" in capsys.readouterr().out

    # 参数互斥：--db 与 --data-dir 同时给 → 环境/用法错误退出码 2
    assert (
        cli.main(
            [
                "status",
                "--dir",
                str(work),
                "--db",
                str(db_path),
                "--data-dir",
                str(data_dir),
            ]
        )
        == 2
    )


def test_cli_push_lint_failure_outputs_violations(tmp_path: Path, capsys) -> None:
    from scripts import skill_workspace as cli

    data_dir = tmp_path / "cli-data"
    conn = connect_skills(data_dir)
    seed_skill(conn)
    conn.close()
    work = tmp_path / "cli-work" / "demo-skill"
    base = ["--data-dir", str(data_dir)]
    assert cli.main(["pull", "demo-skill", "--dir", str(work), *base]) == 0
    _set_pattern(work / "SKILL.md", "bad")

    assert cli.main(["push", "--dir", str(work), *base]) == 1
    captured = capsys.readouterr()
    assert "格式 lint 未通过" in captured.err
    assert "pattern" in captured.err


def test_cli_missing_skill_exit_code(library, tmp_path: Path, capsys) -> None:
    conn, data_dir = library
    from scripts import skill_workspace as cli

    code = cli.main(["pull", "no-such", "--dir", str(tmp_path / "x"), "--data-dir", str(data_dir)])
    assert code == 1
    assert "不存在技能" in capsys.readouterr().err
