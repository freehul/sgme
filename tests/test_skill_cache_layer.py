# -*- coding: utf-8 -*-
"""tests/test_skill_cache_layer.py：共用技能落盘缓存层（D-59 / T-406）。

被测对象 = ``adapters/_shared/skill_cache.py``（八适配器共用的真源）。
覆盖真源自身行为，各适配器接入点另见
``tests/test_hermes_skill_cache.py`` / ``tests/test_adapter_skill_cache.py``。

重点两条（D-59 的验收核心）：
- **manifest 命中不下载**：本地清单与服务端 ``package_sha256`` 相同 → 零包传输；
- **catalog_version 短路**：目录版本没变 → 连单技能 manifest 都不拉（零请求）。

纪律：零网络（FakeTransport）、零真实 ~/.sgme、假数据低熵。
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from adapters._shared import skill_cache as sc


# ---------- 假传输：包 + 目录/清单端点 ----------


def make_package(name: str, files: dict[str, bytes], *, tamper: bool = False) -> tuple[bytes, str]:
    """构造服务端同构包（根 MANIFEST.sha256 + <name>/…），返回 (bytes, 聚合 sha256)。"""
    entries = {rel: data for rel, data in files.items()}
    lines = [f"{hashlib.sha256(entries[rel]).hexdigest()}  {rel}"
             for rel in sorted(entries, key=lambda s: s.encode("utf-8"))]
    if tamper:
        _digest, _sep, rel = lines[0].partition("  ")
        lines[0] = f"{'0' * 64}  {rel}"
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(sc.PACKAGE_MANIFEST)
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        for rel, data in entries.items():
            member = tarfile.TarInfo(f"{name}/{rel}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return buf.getvalue(), hashlib.sha256(manifest).hexdigest()


class FakeTransport:
    """内存传输：可精确统计「包下载次数」与「请求总数」。"""

    def __init__(self) -> None:
        self.catalog_version = "v1"
        self.skills: dict[str, dict] = {}       # name -> {version, files, package(bytes), sha}
        self.calls: list[str] = []
        self.package_downloads = 0
        self.manifest_requests = 0
        self.catalog_requests = 0
        self.manifest_missing = False           # 模拟旧服务端（404）
        self.catalog_missing = False

    def set_skill(self, name: str, files: dict[str, bytes], *, version: str = "1.0.0",
                  tamper: bool = False) -> str:
        data, sha = make_package(name, files, tamper=tamper)
        self.skills[name] = {"version": version, "files": files, "package": data, "sha": sha}
        return sha

    def get_json(self, path: str):
        self.calls.append(path)
        if path == "/v1/skills/catalog-version":
            self.catalog_requests += 1
            if self.catalog_missing:
                raise sc.HTTPStatusError(404, "HTTP 404: not found")
            return {"catalog_version": self.catalog_version}
        if path.endswith("/manifest"):
            self.manifest_requests += 1
            if self.manifest_missing:
                raise sc.HTTPStatusError(404, "HTTP 404: not found")
            name = path.split("/")[3]
            skill = self.skills[name]
            return {
                "name": name,
                "version": skill["version"],
                "package_sha256": skill["sha"],
                "content_len": len(skill["files"]["SKILL.md"]),
                "updated_at": "2026-11-02T00:00:00+08:00",
            }
        raise AssertionError(f"未预期的 JSON 请求: {path}")

    def get_bytes(self, path: str):
        self.calls.append(path)
        name = path.split("/")[3]
        if not path.endswith("/package"):
            raise AssertionError(f"未预期的字节请求: {path}")
        self.package_downloads += 1
        skill = self.skills[name]
        return skill["package"], {"X-Skill-Sha256": skill["sha"], "X-Skill-Files": str(len(skill["files"]))}


FILES_V1 = {"SKILL.md": "# 演示技能 v1\n".encode(), "scripts/run.py": b"print(1)\n",
            "corpus/金匮要略/notes.md": "# 中文路径\n".encode()}


@pytest.fixture
def transport() -> FakeTransport:
    t = FakeTransport()
    t.set_skill("demo-skill", FILES_V1)
    return t


def test_first_fetch_writes_manifest_and_files(tmp_path, transport):
    """首次：下载 + 校验 + 落盘，清单字段齐全（D-59 契约）。"""
    cache = sc.SkillCache(transport, state_dir=tmp_path)
    dest = cache.ensure_skill("demo-skill", tmp_path)

    assert dest == tmp_path / "demo-skill"
    assert (dest / "scripts" / "run.py").read_bytes() == b"print(1)\n"
    assert (dest / "corpus" / "金匮要略" / "notes.md").is_file()
    assert not (dest / "MANIFEST.sha256").exists(), "包内清单不应落进技能目录"

    manifest = json.loads((dest / sc.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["name"] == "demo-skill"
    assert manifest["version"] == "1.0.0"
    assert manifest["content_hash"] == transport.skills["demo-skill"]["sha"]
    assert {item["path"] for item in manifest["files"]} == set(FILES_V1)
    assert manifest["fetched_at"]
    assert cache.last_hit is False


def test_catalog_unchanged_short_circuits_all_requests(tmp_path, transport):
    """一级短路：目录版本未变 → 第二次零请求（不拉 manifest、不拉包）。"""
    cache = sc.SkillCache(transport, state_dir=tmp_path)
    cache.ensure_skill("demo-skill", tmp_path)
    calls_after_first = list(transport.calls)

    result = cache.ensure_skill("demo-skill", tmp_path)

    assert result == tmp_path / "demo-skill"
    assert cache.last_hit is True
    assert transport.calls == calls_after_first, "目录版本未变时不得再发任何请求"
    assert transport.package_downloads == 1
    assert transport.manifest_requests == 1


def test_manifest_hit_skips_package_download(tmp_path, transport):
    """二级命中：目录版本变了（缓存状态丢失）但 package_sha256 相同 → 不下载包。"""
    sc.SkillCache(transport, state_dir=tmp_path).ensure_skill("demo-skill", tmp_path)
    # 模拟「新进程 / 目录状态被清」：删掉目录级缓存 → 必须走单技能比对
    (tmp_path / sc.CATALOG_FILE).unlink()

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    dest = cache.ensure_skill("demo-skill", tmp_path)

    assert dest == tmp_path / "demo-skill"
    assert cache.last_hit is True
    assert transport.package_downloads == 1, "manifest 命中时不得再下载包"
    assert transport.manifest_requests == 2


def test_local_file_tamper_triggers_redownload(tmp_path, transport):
    """本地文件被外部改动 → 清单自检失败 → 重新拉包恢复（失效触发项）。"""
    sc.SkillCache(transport, state_dir=tmp_path).ensure_skill("demo-skill", tmp_path)
    (tmp_path / "demo-skill" / "SKILL.md").write_text("被本地改过\n", encoding="utf-8")

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    cache.ensure_skill("demo-skill", tmp_path)

    assert transport.package_downloads == 2
    assert (tmp_path / "demo-skill" / "SKILL.md").read_text(encoding="utf-8").startswith("# 演示技能")
    assert cache.last_hit is False


def test_content_change_replaces_whole_directory(tmp_path, transport):
    """服务端内容变化 → 整目录替换（旧文件不残留），目录级状态同步刷新。"""
    sc.SkillCache(transport, state_dir=tmp_path).ensure_skill("demo-skill", tmp_path)
    transport.catalog_version = "v2"
    transport.set_skill("demo-skill", {"SKILL.md": b"# v2\n", "new.txt": b"new\n"}, version="2.0.0")

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    dest = cache.ensure_skill("demo-skill", tmp_path)

    assert (dest / "SKILL.md").read_bytes() == b"# v2\n"
    assert (dest / "new.txt").is_file()
    assert not (dest / "scripts").exists(), "旧版本文件必须整体消失"
    manifest = json.loads((dest / sc.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["version"] == "2.0.0"
    assert json.loads((tmp_path / sc.CATALOG_FILE).read_text(encoding="utf-8"))["catalog_version"] == "v2"


def test_corrupt_package_leaves_previous_copy_intact(tmp_path, transport):
    """包校验失败 → 报错 + 旧副本完好 + 无临时残件（原子替换语义）。"""
    sc.SkillCache(transport, state_dir=tmp_path).ensure_skill("demo-skill", tmp_path)
    transport.catalog_version = "v2"
    transport.set_skill("demo-skill", FILES_V1, version="2.0.0", tamper=True)

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    with pytest.raises(sc.SkillCacheError) as ctx:
        cache.ensure_skill("demo-skill", tmp_path)
    assert "sha256 校验失败" in str(ctx.value)

    assert (tmp_path / "demo-skill" / "SKILL.md").read_text(encoding="utf-8").startswith("# 演示技能")
    assert list(tmp_path.glob(".demo-skill.fetch-*")) == []


def test_old_server_without_cache_endpoints_still_downloads(tmp_path, transport):
    """旧服务端（catalog/manifest 均 404）→ 降级为直接拉包（1.x 行为）。"""
    transport.catalog_missing = True
    transport.manifest_missing = True

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    dest = cache.ensure_skill("demo-skill", tmp_path)

    assert dest == tmp_path / "demo-skill"
    assert transport.package_downloads == 1
    assert cache.last_hit is False


def test_refresh_catalog_reports_change_once(tmp_path, transport):
    """refresh_catalog：首次 True（写入状态），相同版本 False（跳过）。"""
    cache = sc.SkillCache(transport, state_dir=tmp_path)
    assert cache.refresh_catalog() is True
    assert json.loads((tmp_path / sc.CATALOG_FILE).read_text(encoding="utf-8"))["catalog_version"] == "v1"
    assert cache.refresh_catalog() is False

    transport.catalog_version = "v9"
    assert cache.refresh_catalog() is True


def test_manifest_file_missing_means_download(tmp_path, transport):
    """没有本地清单（例如首次 / 手工拷贝的目录）→ 下载，不误判命中。"""
    (tmp_path / "demo-skill").mkdir()
    (tmp_path / "demo-skill" / "SKILL.md").write_text("stale\n", encoding="utf-8")

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    cache.ensure_skill("demo-skill", tmp_path)

    assert transport.package_downloads == 1
    assert (tmp_path / "demo-skill" / "SKILL.md").read_text(encoding="utf-8").startswith("# 演示技能")


def test_failed_install_does_not_short_circuit_stale_copy(tmp_path, transport):
    """下载失败后不得把旧副本当最新：目录版本已记下也要重新拉取。"""
    sc.SkillCache(transport, state_dir=tmp_path).ensure_skill("demo-skill", tmp_path)
    transport.catalog_version = "v2"
    transport.set_skill("demo-skill", FILES_V1, version="2.0.0", tamper=True)
    with pytest.raises(sc.SkillCacheError):
        sc.SkillCache(transport, state_dir=tmp_path).ensure_skill("demo-skill", tmp_path)

    # 服务端修好（同一目录版本 v2）→ 下一次仍必须下载，而不是短路命中旧副本
    transport.set_skill("demo-skill", {"SKILL.md": b"# v2\n"}, version="2.0.0")
    cache = sc.SkillCache(transport, state_dir=tmp_path)
    dest = cache.ensure_skill("demo-skill", tmp_path)
    assert cache.last_hit is False
    assert transport.package_downloads == 3
    assert (dest / "SKILL.md").read_bytes() == b"# v2\n"


def test_rejects_illegal_skill_name(tmp_path, transport):
    """技能名 = 单个路径组件；穿越名直接拒绝（不触网）。"""
    cache = sc.SkillCache(transport, state_dir=tmp_path)
    for bad in ("../evil", "a/b", "", ".."):
        with pytest.raises(sc.SkillCacheError):
            cache.ensure_skill(bad, tmp_path)
    assert transport.calls == []


def test_package_traversal_member_rejected(tmp_path, transport):
    """包内 ../ 成员整包拒收，且不留残件。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    transport.skills["demo-skill"]["package"] = buf.getvalue()
    transport.skills["demo-skill"]["sha"] = "0" * 64

    cache = sc.SkillCache(transport, state_dir=tmp_path)
    with pytest.raises(sc.SkillCacheError) as ctx:
        cache.ensure_skill("demo-skill", tmp_path)
    assert "路径非法" in str(ctx.value)
    assert list(tmp_path.glob(".demo-skill.fetch-*")) == []
    assert not (tmp_path.parent / "escape.txt").exists()


def test_module_level_api_matches_contract(tmp_path, transport, monkeypatch):
    """模块级 ensure_skill / refresh_catalog 可被自检脚本直接调用。"""
    monkeypatch.setenv("SGME_BASE_URL", "http://127.0.0.1:9")
    cache = sc.SkillCache(transport, state_dir=tmp_path)
    path = cache.ensure_skill("demo-skill", tmp_path)
    assert isinstance(path, Path)

    import inspect
    params = list(inspect.signature(sc.ensure_skill).parameters)
    assert params[:2] == ["name", "dest_dir"], "模块级 ensure_skill 签名必须与契约一致"
    assert list(inspect.signature(sc.refresh_catalog).parameters) == [] or "transport" in params
