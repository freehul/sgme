"""tests/test_prompt_registry.py：提示词版本登记与激活校验测试（T-315）。

覆盖契约（scripts/prompt_registry.py）：
- list_versions：目录识别版本文件（忽略非 vNNN）、registered 依据 manifest / .index.json
- compute_sha256：与标准值比对
- register：写 .index.json、同版本同内容重复登记幂等（registered_at 不变）
- verify_active：工作副本与版本文件 sha256 一致 / 不一致两种

路径约定见模块 docstring：prompt_root = 某 stage 的版本目录
（如 sgme/resources/prompts/versions/l1_extraction/）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

# 脚本 import（scripts/ 非包，直接加载，沿用 test_care_consumer 先例）
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import prompt_registry  # noqa: E402


def _make_stage(tmp_path: Path, files: dict[str, bytes], *, index: dict | None = None,
                manifest_versions: list[str] | None = None) -> Path:
    """造临时 stage 布局：<tmp>/prompts/versions/l1_extraction/{vNNN.txt, .index.json}。

    manifest_versions 非空时，在 <tmp>/prompts/manifest.yaml 写 versions 段
    （结构与项目 manifest.yaml 一致，仅含 l1_extraction 一项）。
    返回版本目录 prompt_root。
    """
    stage_dir = tmp_path / "prompts" / "versions" / "l1_extraction"
    stage_dir.mkdir(parents=True)
    for name, data in files.items():
        (stage_dir / name).write_bytes(data)
    if index is not None:
        (stage_dir / ".index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    if manifest_versions is not None:
        manifest = {
            "stages": {"l1_extraction": {"active": "@working", "ab": {"enabled": False}}},
            "versions": {
                "l1_extraction": [
                    {"version": v, "file": f"versions/l1_extraction/{v}.txt",
                     "sha256": "0" * 64, "created_at": "2026-01-01T00:00:00Z", "note": ""}
                    for v in manifest_versions
                ],
            },
        }
        import yaml
        (tmp_path / "prompts" / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return stage_dir


# ---------- compute_sha256 ----------

def test_compute_sha256_matches_known_value(tmp_path):
    """sha256 与 hashlib 标准值一致（64 位小写十六进制）。"""
    f = tmp_path / "sample.txt"
    f.write_bytes(b"abc")
    assert prompt_registry.compute_sha256(f) == hashlib.sha256(b"abc").hexdigest()
    assert prompt_registry.compute_sha256(f) == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ---------- list_versions ----------

def test_list_versions_recognizes_version_files_ignores_others(tmp_path):
    """只识别 vNNN.txt，忽略 .index.json / 其他文件；version 字段为文件名 stem。"""
    root = _make_stage(tmp_path, {
        "v001.txt": "第一版".encode("utf-8"),
        "v002.txt": "第二版".encode("utf-8"),
        ".index.json": b"{}",
        "README.md": b"notes",
        "working.txt": b"not a version",
    })
    items = prompt_registry.list_versions(root)
    assert [i["version"] for i in items] == ["v001", "v002"]
    for item in items:
        assert set(item) == {"version", "sha256", "size", "registered"}
        assert item["sha256"] == hashlib.sha256(
            (root / f"{item['version']}.txt").read_bytes()).hexdigest()
        assert item["size"] == (root / f"{item['version']}.txt").stat().st_size
        assert item["registered"] is False


def test_list_versions_sorts_numerically_v010_after_v002(tmp_path):
    """版本号按数值排序：v002 < v010（字典序会错排成 v010 < v002）。"""
    root = _make_stage(tmp_path, {
        "v010.txt": "十".encode("utf-8"),
        "v002.txt": "二".encode("utf-8"),
    })
    items = prompt_registry.list_versions(root)
    assert [i["version"] for i in items] == ["v002", "v010"]


def test_list_versions_registered_by_manifest(tmp_path):
    """项目 manifest.yaml versions 段已登记的版本 → registered=True。"""
    root = _make_stage(
        tmp_path,
        {"v001.txt": b"a", "v002.txt": b"b"},
        manifest_versions=["v002"],
    )
    items = {i["version"]: i for i in prompt_registry.list_versions(root)}
    assert items["v002"]["registered"] is True
    assert items["v001"]["registered"] is False


def test_list_versions_registered_by_index_without_manifest(tmp_path):
    """无 manifest 时回落 .index.json 判断。"""
    root = _make_stage(
        tmp_path,
        {"v001.txt": b"a", "v002.txt": b"b"},
        index={"v001": {"sha256": "x" * 64, "size": 1, "registered_at": "2026-01-01T00:00:00Z"}},
    )
    items = {i["version"]: i for i in prompt_registry.list_versions(root)}
    assert items["v001"]["registered"] is True
    assert items["v002"]["registered"] is False


# ---------- register ----------

def test_register_writes_index_and_returns_result(tmp_path):
    """register 计算 sha256/size 并写 .index.json（键=版本名，值含三字段）。"""
    data = "版本内容".encode("utf-8")
    root = _make_stage(tmp_path, {"v019.txt": data})
    result = prompt_registry.register(root, "v019")

    assert result["version"] == "v019"
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["size"] == len(data)
    assert "registered_at" in result and result["registered_at"]

    index = json.loads((root / ".index.json").read_text(encoding="utf-8"))
    entry = index["v019"]
    assert entry["sha256"] == result["sha256"]
    assert entry["size"] == result["size"]
    assert entry["registered_at"] == result["registered_at"]


def test_register_is_idempotent_for_same_content(tmp_path):
    """同版本同内容重复登记：registered_at 不变（幂等）。"""
    root = _make_stage(tmp_path, {"v019.txt": "same".encode("utf-8")})
    first = prompt_registry.register(root, "v019")
    second = prompt_registry.register(root, "v019")
    assert second["registered_at"] == first["registered_at"]
    index = json.loads((root / ".index.json").read_text(encoding="utf-8"))
    assert index["v019"]["registered_at"] == first["registered_at"]


def test_register_updates_entry_when_content_changes(tmp_path):
    """同版本不同内容（文件被重写）：更新 sha256/size 并刷新 registered_at。"""
    root = _make_stage(tmp_path, {"v019.txt": "old".encode("utf-8")})
    prompt_registry.register(root, "v019")
    (root / "v019.txt").write_bytes("new content".encode("utf-8"))
    result = prompt_registry.register(root, "v019")
    assert result["sha256"] == hashlib.sha256(b"new content").hexdigest()
    index = json.loads((root / ".index.json").read_text(encoding="utf-8"))
    assert index["v019"]["sha256"] == result["sha256"]


def test_register_missing_version_file_raises(tmp_path):
    """版本文件不存在 → 报错，不写索引。"""
    root = _make_stage(tmp_path, {"v001.txt": b"a"})
    with pytest.raises(FileNotFoundError):
        prompt_registry.register(root, "v099")
    assert not (root / ".index.json").exists()


def test_register_rejects_invalid_version_name(tmp_path):
    """非 vNNN 版本名拒绝（防把工作副本/任意文件登记进索引）。"""
    root = _make_stage(tmp_path, {"v001.txt": b"a"})
    with pytest.raises(ValueError):
        prompt_registry.register(root, "working")


# ---------- verify_active ----------

def test_verify_active_match(tmp_path):
    """工作副本与版本文件内容一致 → (True, 说明)。"""
    data = "现役提示词".encode("utf-8")
    root = _make_stage(tmp_path, {"v007.txt": data})
    work = tmp_path / "prompts" / "l1_extraction.txt"
    work.write_bytes(data)
    ok, message = prompt_registry.verify_active(root, "v007")
    assert ok is True
    assert "v007" in message


def test_verify_active_mismatch(tmp_path):
    """工作副本与版本文件内容不一致 → (False, 说明含双方 sha 线索)。"""
    root = _make_stage(tmp_path, {"v019.txt": "新版本".encode("utf-8")})
    work = tmp_path / "prompts" / "l1_extraction.txt"
    work.write_bytes("旧工作副本".encode("utf-8"))
    ok, message = prompt_registry.verify_active(root, "v019")
    assert ok is False
    assert "v019" in message
    # 说明里能对出双方指纹（sha 前 8 位）
    assert hashlib.sha256("新版本".encode("utf-8")).hexdigest()[:8] in message
    assert hashlib.sha256("旧工作副本".encode("utf-8")).hexdigest()[:8] in message


def test_verify_active_missing_version_file(tmp_path):
    """版本文件缺失 → (False, 说明)，不抛异常。"""
    root = _make_stage(tmp_path, {"v001.txt": b"a"})
    work = tmp_path / "prompts" / "l1_extraction.txt"
    work.write_bytes(b"a")
    ok, message = prompt_registry.verify_active(root, "v099")
    assert ok is False
    assert "v099" in message


def test_verify_active_missing_working_copy(tmp_path):
    """工作副本缺失 → (False, 说明)，不抛异常。"""
    root = _make_stage(tmp_path, {"v007.txt": b"a"})
    ok, message = prompt_registry.verify_active(root, "v007")
    assert ok is False
    assert "l1_extraction.txt" in message
