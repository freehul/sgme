"""#33 测试：PromptStore 提示词版本管理器。

覆盖：
- @working 默认（manifest 缺失/存在）行为不变（工作副本热更新）
- 发布（publish）：版本递增 / 原子写 / 占位符校验 / sha256
- 激活（activate）：@working 与钉版
- A/B：确定性分流 / split 边界 / bucket_by / overrides 钉版
- 坏配置 → PromptManifestError
- 版本解析与 sha256 懒校验告警
"""
from __future__ import annotations

import hashlib
import logging

import pytest

from sgme.prompts import (
    BucketCtx,
    PromptManifestError,
    PromptStore,
    PromptVersion,
    VersionInfo,
)


# ---------- fixtures ----------

STAGE_TEXTS = {
    "tier0_summary": "你是摘要器。\n{{memories}}",
    "l1_extraction": "你是提取器。\n{{dimensions}}\n{{conversation}}",
    "l1_conflict": "你是裁决器。\n{{new_memories}}\n{{candidates}}",
    "l2_scene": "你是聚合器。\n{{new_memories}}\n{{existing_scenes}}\n{{max_scenes}}",
}


@pytest.fixture
def prompts_root(tmp_path):
    """构造临时 prompts 目录：4 个工作副本（含占位符）。"""
    root = tmp_path / "prompts"
    root.mkdir()
    for stage, text in STAGE_TEXTS.items():
        (root / f"{stage}.txt").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def store(prompts_root):
    return PromptStore(prompts_root=prompts_root)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _manifest_has_versions(root) -> dict:
    import yaml
    return yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))


# ---------- 默认 @working ----------

def test_get_working_without_manifest(store, prompts_root):
    """manifest 缺失 → 全 @working：返回工作副本内容 + working-<sha8> 版本。"""
    pv = store.get("l1_extraction")
    assert isinstance(pv, PromptVersion)
    assert pv.text == STAGE_TEXTS["l1_extraction"]
    assert pv.version == f"working-{_sha8(STAGE_TEXTS['l1_extraction'])}"
    assert pv.variant is None
    assert pv.source == prompts_root / "l1_extraction.txt"


def test_working_hot_reload(store):
    """编辑工作副本 → 下次 get 立即生效（无缓存）+ 版本哈希变化。"""
    v1 = store.get("l1_extraction")
    (store.prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\n新的一行", encoding="utf-8",
    )
    v2 = store.get("l1_extraction")
    assert v2.text != v1.text
    assert v2.version != v1.version
    assert "新的一行" in v2.text


def test_get_unknown_stage_raises(store):
    """未知 stage → PromptManifestError。"""
    with pytest.raises(PromptManifestError):
        store.get("no_such_stage")


# ---------- publish ----------

def test_publish_creates_v001(store, prompts_root):
    """首次发布 → v001 文件落盘 + manifest versions 段记录（sha256/created_at）。"""
    info = store.publish("l1_extraction", note="基线")
    assert isinstance(info, VersionInfo)
    assert info.version == "v001"
    assert info.file == "versions/l1_extraction/v001.txt"
    target = prompts_root / "versions" / "l1_extraction" / "v001.txt"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == STAGE_TEXTS["l1_extraction"]
    assert info.sha256 == hashlib.sha256(STAGE_TEXTS["l1_extraction"].encode("utf-8")).hexdigest()
    # manifest 记录
    m = _manifest_has_versions(prompts_root)
    assert m["versions"]["l1_extraction"][0]["version"] == "v001"


def test_publish_increments_version(store, prompts_root):
    """连续发布 → v001, v002, v003。"""
    store.publish("l1_extraction")
    store.publish("l1_extraction")
    info = store.publish("l1_extraction")
    assert info.version == "v003"
    assert (prompts_root / "versions" / "l1_extraction" / "v003.txt").exists()


def test_publish_validates_placeholders(store, prompts_root):
    """工作副本缺必备占位符 → 发布拒绝。"""
    (prompts_root / "l1_extraction.txt").write_text("缺占位符", encoding="utf-8")
    with pytest.raises(PromptManifestError, match="占位符"):
        store.publish("l1_extraction")


def test_publish_atomic_no_partial_file(store, prompts_root):
    """发布后 versions 目录无临时残留文件（原子写）。"""
    store.publish("l1_extraction")
    leftover = [p.name for p in (prompts_root / "versions" / "l1_extraction").iterdir()]
    assert leftover == ["v001.txt"]


# ---------- activate ----------

def test_activate_pinned_version(store, prompts_root):
    """激活 v001 → get 返回钉版内容 + version=v001。"""
    store.publish("l1_extraction")
    store.activate("l1_extraction", "v001")
    pv = store.get("l1_extraction")
    assert pv.version == "v001"
    assert pv.text == STAGE_TEXTS["l1_extraction"]
    assert pv.variant is None
    # active 已写回 manifest
    m = _manifest_has_versions(prompts_root)
    assert m["stages"]["l1_extraction"]["active"] == "v001"


def test_activate_working_returns_hot_reload(store, prompts_root):
    """激活 @working → 回到工作副本热更新。"""
    store.publish("l1_extraction")
    store.activate("l1_extraction", "@working")
    pv = store.get("l1_extraction")
    assert pv.version.startswith("working-")
    (prompts_root / "l1_extraction.txt").write_text("改了", encoding="utf-8")
    assert "改了" in store.get("l1_extraction").text


def test_activate_missing_version_raises(store):
    """激活不存在的版本 → PromptManifestError。"""
    with pytest.raises(PromptManifestError):
        store.activate("l1_extraction", "v999")


# ---------- list_versions ----------

def test_list_versions(store):
    """list_versions 返回全部已发布版本（按 version 升序）。"""
    assert store.list_versions("l1_extraction") == []
    store.publish("l1_extraction", note="n1")
    store.publish("l1_extraction", note="n2")
    vers = store.list_versions("l1_extraction")
    assert [v.version for v in vers] == ["v001", "v002"]
    assert vers[1].note == "n2"


# ---------- A/B 分流 ----------

def _setup_ab(store):
    """发布两版并开启 A/B。"""
    store.publish("l1_extraction", note="A 版")
    (store.prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版措辞", encoding="utf-8",
    )
    store.publish("l1_extraction", note="B 版")
    store.configure_ab("l1_extraction", "v001", "v002", split=0.5, bucket_by="file_id")


def test_ab_deterministic_bucketing(store):
    """同一 bucket_key → 永远同一变体（确定性分流）。"""
    _setup_ab(store)
    first = store.get("l1_extraction", BucketCtx(bucket_key="file-abc"))
    assert first.variant in ("A", "B")
    for _ in range(5):
        again = store.get("l1_extraction", BucketCtx(bucket_key="file-abc"))
        assert again.variant == first.variant
        assert again.version == first.version


def test_ab_split_extremes(store):
    """split=1.0 → 全 A；split=0.0 → 全 B。"""
    store.publish("l1_extraction", note="A 版")
    (store.prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版措辞", encoding="utf-8",
    )
    store.publish("l1_extraction", note="B 版")

    store.configure_ab("l1_extraction", "v001", "v002", split=1.0)
    for key in ("f1", "f2", "f3"):
        assert store.get("l1_extraction", BucketCtx(bucket_key=key)).variant == "A"

    store.configure_ab("l1_extraction", "v001", "v002", split=0.0)
    for key in ("f1", "f2", "f3"):
        assert store.get("l1_extraction", BucketCtx(bucket_key=key)).variant == "B"


def test_ab_get_returns_version_of_variant(store):
    """A/B 命中 → version 为对应钉版版本号。"""
    _setup_ab(store)
    pv = store.get("l1_extraction", BucketCtx(bucket_key="file-xyz"))
    assert pv.version in ("v001", "v002")
    if pv.variant == "A":
        assert pv.version == "v001"
    else:
        assert pv.version == "v002"
    assert "{{conversation}}" in pv.text  # 内容为版本文件


def test_ab_disabled_ignores_split(store):
    """ab.enabled=false → 忽略 a/b/split，走 active 指向。"""
    _setup_ab(store)
    store.configure_ab("l1_extraction", "v001", "v002", split=0.5, enabled=False)
    pv = store.get("l1_extraction", BucketCtx(bucket_key="file-abc"))
    assert pv.variant is None
    assert pv.version.startswith("working-")  # active 仍是 @working


def test_ab_invalid_config_raises(store):
    """非法 A/B 配置（split 越界 / bucket_by 非法 / 文件不存在 / a==b）→ 报错。"""
    store.publish("l1_extraction")
    with pytest.raises(PromptManifestError):
        store.configure_ab("l1_extraction", "v001", "v002", split=1.5)
    with pytest.raises(PromptManifestError):
        store.configure_ab("l1_extraction", "v001", "v002", split=0.5, bucket_by="email")
    with pytest.raises(PromptManifestError):
        store.configure_ab("l1_extraction", "v001", "v999", split=0.5)
    with pytest.raises(PromptManifestError):
        store.configure_ab("l1_extraction", "v001", "v001", split=0.5)


def test_bucket_by_random_allows_both(store):
    """bucket_by=random：不同 key 可出现两变体（只验证不崩溃 + 返回合法变体）。"""
    store.publish("l1_extraction")
    (store.prompts_root / "l1_extraction.txt").write_text(
        STAGE_TEXTS["l1_extraction"] + "\nB 版措辞", encoding="utf-8",
    )
    store.publish("l1_extraction")
    store.configure_ab("l1_extraction", "v001", "v002", split=0.5, bucket_by="random")
    variants = set()
    for i in range(20):
        variants.add(store.get("l1_extraction", BucketCtx(bucket_key=f"k{i}")).variant)
    assert variants <= {"A", "B"}
    assert variants  # 非空


# ---------- overrides 钉版 ----------

def test_overrides_pin_version(store):
    """ctx.overrides[stage] 优先于 manifest（测试/钉版用）。"""
    _setup_ab(store)
    store.activate("l1_extraction", "v001")  # 钉 v001
    # overrides 钉到 @working
    pv = store.get("l1_extraction", BucketCtx(bucket_key="f", overrides={"l1_extraction": "@working"}))
    assert pv.version.startswith("working-")
    assert pv.variant is None


# ---------- 坏配置 ----------

def test_manifest_bad_yaml_raises(prompts_root):
    """manifest 是坏 YAML → PromptManifestError。"""
    (prompts_root / "manifest.yaml").write_text("{{{{ 不是 yaml", encoding="utf-8")
    store = PromptStore(prompts_root=prompts_root)
    with pytest.raises(PromptManifestError):
        store.get("l1_extraction")


def test_manifest_active_missing_file_raises(prompts_root):
    """manifest active 指向不存在文件 → PromptManifestError。"""
    (prompts_root / "manifest.yaml").write_text(
        "stages:\n  l1_extraction:\n    active: versions/l1_extraction/v999.txt\n",
        encoding="utf-8",
    )
    store = PromptStore(prompts_root=prompts_root)
    with pytest.raises(PromptManifestError, match="不存在"):
        store.get("l1_extraction")


# ---------- sha256 懒校验告警 ----------

def test_pinned_sha_mismatch_logs_warning(store, prompts_root, caplog):
    """钉版文件被篡改 → 读取时懒校验告警（不阻塞）。"""
    store.publish("l1_extraction")
    store.activate("l1_extraction", "v001")
    # 篡改版本文件
    target = prompts_root / "versions" / "l1_extraction" / "v001.txt"
    target.write_text("被篡改\n{{conversation}}", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sgme.prompts.manager"):
        pv = store.get("l1_extraction")
    assert pv.version == "v001"  # 仍返回（仅告警）
    assert any("sha256 不一致" in r.message for r in caplog.records)


# ---------- 协议 ----------

def test_revision_changes_after_publish(store):
    """revision() 随 manifest 变化（VersionedSource 协议）。"""
    r1 = store.revision()
    store.publish("l1_extraction")
    r2 = store.revision()
    assert r1 != r2
