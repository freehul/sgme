# -*- coding: utf-8 -*-
"""技能包序列化模块测试（2.0 技能全存库：目录 -> 可入库结构 -> 原子落盘 -> 校验）。

覆盖三条主链路 + 安全边界：
- serialize_skill_dir: 目录遍历 / 隐藏目录剔除 / posix 相对路径 / package_sha256 公式；
- write_skill_package: 原子写（临时目录 + rename）、失败不留残件、嵌套目录自动创建；
- verify_manifest: 逐文件 sha256 内容一致 / 篡改检出 / 多文件不一致列表。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sgme.data.skill_package import (
    serialize_skill_dir,
    verify_manifest,
    write_skill_package,
)


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _expected_package_sha256(files: list[dict]) -> str:
    """预期 package_sha256：排序后的 (path, sha256) 拼接串取 sha256（契约钉死）。

    拼接格式与 operations/skills.py 的 canonical manifest 一致：
    每行 `<sha256>  <relpath>\n`（两个空格），按 relpath 字节序排序，UTF-8 编码。
    """
    rows = sorted(((f["path"], f["sha256"]) for f in files), key=lambda r: r[0].encode("utf-8"))
    concat = "".join(f"{sha}  {path}\n" for path, sha in rows)
    return _sha256(concat.encode("utf-8"))


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """标准技能目录：SKILL.md + references/ + scripts/（含嵌套）+ 隐藏目录。"""
    root = tmp_path / "my-skill"
    (root / "references").mkdir(parents=True)
    (root / "scripts" / "sub").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.txt").write_text("hidden", encoding="utf-8", newline="\n")
    (root / "SKILL.md").write_text("# My Skill\n\n正文。\n", encoding="utf-8", newline="\n")
    (root / "references" / "api.md").write_text("ref content\n", encoding="utf-8", newline="\n")
    (root / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    (root / "scripts" / "sub" / "deep.py").write_text("print('deep')\n", encoding="utf-8", newline="\n")
    return root


class TestSerializeSkillDir:
    def test_returns_name_and_files(self, skill_dir: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        assert pkg["name"] == "my-skill"
        # SKILL.md + references/api.md + scripts/run.sh + scripts/sub/deep.py = 4 个
        paths = {f["path"] for f in pkg["files"]}
        assert paths == {
            "SKILL.md",
            "references/api.md",
            "scripts/run.sh",
            "scripts/sub/deep.py",
        }

    def test_hidden_dirs_excluded(self, skill_dir: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        paths = [f["path"] for f in pkg["files"]]
        assert not any(p.startswith(".hidden") for p in paths)
        assert ".hidden/secret.txt" not in paths

    def test_file_fields(self, skill_dir: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        assert len(pkg["files"]) == 4
        for f in pkg["files"]:
            assert set(f.keys()) == {"path", "sha256", "content", "size"}
            assert f["size"] == len(f["content"])
            assert f["sha256"] == _sha256(f["content"])

    def test_package_sha256_formula(self, skill_dir: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        assert pkg["package_sha256"] == _expected_package_sha256(pkg["files"])

    def test_empty_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "empty-skill"
        root.mkdir()
        # 空目录：name 正常、files 空、package_sha256 = sha256(b"")
        pkg = serialize_skill_dir(root)
        assert pkg["name"] == "empty-skill"
        assert pkg["files"] == []
        assert pkg["package_sha256"] == _sha256(b"")

    def test_content_bytes_exact(self, skill_dir: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        by_path = {f["path"]: f for f in pkg["files"]}
        assert by_path["SKILL.md"]["content"] == "# My Skill\n\n正文。\n".encode("utf-8")
        assert by_path["scripts/sub/deep.py"]["content"] == b"print('deep')\n"

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(Exception):
            serialize_skill_dir(tmp_path / "no" / "such")


class TestWriteSkillPackage:
    def test_round_trip(self, skill_dir: Path, tmp_path: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        dest = tmp_path / "dest"
        n = write_skill_package(pkg, dest)
        assert n == 4
        # round-trip 一致性：内容逐文件还原，重序列化得到同一 package_sha256
        for f in pkg["files"]:
            assert (dest / f["path"]).read_bytes() == f["content"]
        assert serialize_skill_dir(dest)["package_sha256"] == pkg["package_sha256"]

    def test_creates_nested_dirs(self, tmp_path: Path) -> None:
        pkg = {
            "name": "x",
            "package_sha256": "0" * 64,
            "files": [
                {"path": "a/b/c.txt", "sha256": _sha256(b"c"), "content": b"c", "size": 1},
            ],
        }
        dest = tmp_path / "dest"
        assert write_skill_package(pkg, dest) == 1
        assert (dest / "a" / "b" / "c.txt").read_bytes() == b"c"

    def test_atomic_replace_existing(self, skill_dir: Path, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        # 预置旧文件：换包后必须消失（整目录原子替换，不残留旧成员）
        (dest / "stale.txt").write_text("old", encoding="utf-8")
        pkg = serialize_skill_dir(skill_dir)
        write_skill_package(pkg, dest)
        assert not (dest / "stale.txt").exists()
        assert (dest / "SKILL.md").exists()

    def test_no_leftover_on_failure(self, tmp_path: Path) -> None:
        # 制造写失败：包内 path 带 '..' 被安全校验拒绝 -> 整体失败
        pkg = {
            "name": "bad",
            "package_sha256": "0" * 64,
            "files": [
                {"path": "SKILL.md", "sha256": _sha256(b"ok"), "content": b"ok", "size": 2},
                {"path": "../escape.txt", "sha256": _sha256(b"x"), "content": b"x", "size": 1},
            ],
        }
        dest = tmp_path / "dest"
        with pytest.raises(ValueError):
            write_skill_package(pkg, dest)
        # 失败不留残件：dest 目录不得存在
        assert not dest.exists()


class TestVerifyManifest:
    def test_all_match(self, skill_dir: Path, tmp_path: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        dest = tmp_path / "dest"
        write_skill_package(pkg, dest)
        ok, bad = verify_manifest(dest, pkg)
        assert ok is True
        assert bad == []

    def test_detects_tampered_file(self, skill_dir: Path, tmp_path: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        dest = tmp_path / "dest"
        write_skill_package(pkg, dest)
        target = dest / "references" / "api.md"
        target.write_text("tampered\n", encoding="utf-8")
        ok, bad = verify_manifest(dest, pkg)
        assert ok is False
        assert bad == ["references/api.md"]

    def test_detects_multiple_mismatches(self, skill_dir: Path, tmp_path: Path) -> None:
        pkg = serialize_skill_dir(skill_dir)
        dest = tmp_path / "dest"
        write_skill_package(pkg, dest)
        (dest / "SKILL.md").write_text("x", encoding="utf-8")
        (dest / "scripts" / "run.sh").write_text("y", encoding="utf-8")
        ok, bad = verify_manifest(dest, pkg)
        assert ok is False
        assert set(bad) == {"SKILL.md", "scripts/run.sh"}

    def test_missing_file_reported(self, tmp_path: Path) -> None:
        pkg = {
            "name": "x",
            "package_sha256": "0" * 64,
            "files": [
                {"path": "a.txt", "sha256": _sha256(b"a"), "content": b"a", "size": 1},
                {"path": "b.txt", "sha256": _sha256(b"b"), "content": b"b", "size": 1},
            ],
        }
        dest = tmp_path / "dest"
        write_skill_package(pkg, dest)
        (dest / "b.txt").unlink()
        ok, bad = verify_manifest(dest, pkg)
        assert ok is False
        assert "b.txt" in bad


class TestSecurity:
    def test_write_rejects_dotdot_path(self, tmp_path: Path) -> None:
        pkg = {
            "name": "x",
            "package_sha256": "0" * 64,
            "files": [
                {"path": "a/../../evil.txt", "sha256": _sha256(b"e"), "content": b"e", "size": 1},
            ],
        }
        with pytest.raises(ValueError):
            write_skill_package(pkg, tmp_path / "dest")

    def test_write_rejects_absolute_path(self, tmp_path: Path) -> None:
        pkg = {
            "name": "x",
            "package_sha256": "0" * 64,
            "files": [
                {"path": "C:/evil.txt", "sha256": _sha256(b"e"), "content": b"e", "size": 1},
            ],
        }
        with pytest.raises(ValueError):
            write_skill_package(pkg, tmp_path / "dest")

    def test_write_rejects_symlink_member(self, tmp_path: Path) -> None:
        # dest 内预置一个指向外部的符号链接文件，包里同名成员必须拒写（不得跟随链接）
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        pkg = {
            "name": "x",
            "package_sha256": "0" * 64,
            "files": [
                {"path": "link.txt", "sha256": _sha256(b"l"), "content": b"l", "size": 1},
            ],
        }
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "link.txt").symlink_to(outside)
        with pytest.raises(ValueError):
            write_skill_package(pkg, dest)
        # 符号链接未被覆盖、未被跟随
        assert (dest / "link.txt").is_symlink()
        assert outside.read_text(encoding="utf-8") == "outside"
