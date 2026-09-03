"""sgme/prompts/manager.py：提示词版本管理器（#33 提示词版本管理）。

PromptStore 实现 VersionedSource 协议（get(key, ctx) → (payload, version, variant)）：

- 工作副本 `prompts/<stage>.txt` 默认生效（active: "@working"，编辑即热更新，向后兼容）
- 版本快照 `prompts/versions/<stage>/vNNN.txt` 不可变（发布时临时文件 + os.replace 原子写）
- A/B 确定性分流：sha256(bucket_key) 前 8 字节取模 100，`< split*100` 走 A
- **每次调用实时读盘（无缓存）**；manifest ≤1KB、版本文件 ≤3KB，渲染批次级频率可忽略
- 禁止在 engine 侧再加缓存层（双缓存易脏）

字段英文；注释中文。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from sgme import config

logger = logging.getLogger("sgme.prompts.manager")


class PromptManifestError(Exception):
    """manifest 配置非法 / 版本操作失败。"""


# 全部受管 stage（渲染点：l1.py / l15.py / l2.py / tier0.py）
STAGES = ("tier0_summary", "l1_extraction", "l1_conflict", "l2_scene")

# 各 stage 必备占位符（publish 时校验，防发布残缺模板）
STAGE_PLACEHOLDERS = {
    "tier0_summary": ["{{memories}}"],
    "l1_extraction": ["{{conversation}}", "{{dimensions}}"],
    "l1_conflict": ["{{new_memories}}", "{{candidates}}"],
    "l2_scene": ["{{new_memories}}", "{{existing_scenes}}", "{{max_scenes}}"],
}

_VALID_BUCKET_BY = ("file_id", "memory_id", "random")


# ---------- 数据结构 ----------

@dataclass
class PromptVersion:
    """一次 get() 的返回：文本 + 版本身份。"""
    stage: str
    version: str                # vNNN / working-<sha256:8>
    variant: str | None         # A / B / None
    text: str
    source: Path


@dataclass
class VersionInfo:
    """发布版本元数据（manifest versions 段一条）。"""
    version: str
    file: str
    sha256: str
    created_at: str
    note: str


@dataclass
class BucketCtx:
    """A/B 分流上下文。

    - bucket_key：分流键（默认 file_id；提炼链路传 file_id）
    - overrides：钉版/测试用，`overrides[stage]` 优先于 manifest
      （值为 "@working" 或版本引用如 "v001" / "versions/<stage>/v001.txt"）
    """
    bucket_key: str = ""
    overrides: dict[str, str] = field(default_factory=dict)


class VersionedSource(Protocol):
    """版本感知资源协议（文件型提示词 / DB 型维度共用取用即读最新 + 版本可观测）。"""

    def get(self, key: str, ctx: BucketCtx | None = None) -> PromptVersion:
        ...

    def revision(self) -> str:
        ...


# ---------- PromptStore ----------

class PromptStore:
    """提示词版本管理器（文件型 VersionedSource 实现）。"""

    PROMPTS_ROOT: Path = config.RESOURCE_ROOT / "prompts"
    MANIFEST_PATH: Path = PROMPTS_ROOT / "manifest.yaml"

    def __init__(self, prompts_root: Path | str | None = None):
        """prompts_root 缺省取类属性（项目 prompts/）；测试可注入临时目录。"""
        if prompts_root is None:
            self.prompts_root = Path(self.PROMPTS_ROOT)
        else:
            self.prompts_root = Path(prompts_root)
        self.manifest_path = self.prompts_root / "manifest.yaml"

    # ---------- 主入口 ----------

    def get(self, stage: str, ctx: BucketCtx | None = None) -> PromptVersion:
        """取当前生效提示词（每次实时读盘，无缓存）。

        解析优先级：
        1. ctx.overrides[stage]（钉版/测试）
        2. manifest ab.enabled=true → 按 bucket_key 确定性分流 A/B
        3. manifest active（@working → 工作副本；否则钉版文件）
        """
        manifest = self._load_manifest()
        if stage not in manifest["stages"]:
            raise PromptManifestError(f"未知 stage: {stage}")

        # 1. overrides 优先（钉版）
        if ctx is not None and ctx.overrides and stage in ctx.overrides:
            ref = ctx.overrides[stage]
            path = self._resolve_ref(stage, ref)
            text = self._read_text(path)
            version = self._version_of(stage, path, ref)
            return PromptVersion(stage=stage, version=version, variant=None, text=text, source=path)

        scfg = manifest["stages"][stage]
        ab = scfg.get("ab") or {}

        # 2. A/B 分流
        if ab.get("enabled"):
            variant = self._bucket(stage, ctx)
            ref = ab["a"] if variant == "A" else ab["b"]
            path = self._resolve_ref(stage, ref)
            text = self._read_text(path)
            version = self._version_of(stage, path, ref)
            self._lazy_verify_sha(stage, path, version, manifest)
            return PromptVersion(stage=stage, version=version, variant=variant, text=text, source=path)

        # 3. active 指向
        active = scfg.get("active", "@working")
        if active == "@working":
            path = self.prompts_root / f"{stage}.txt"
            text = self._read_text(path)
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
            return PromptVersion(stage=stage, version=f"working-{sha}", variant=None, text=text, source=path)
        path = self._resolve_ref(stage, active)
        text = self._read_text(path)
        version = self._version_of(stage, path, active)
        self._lazy_verify_sha(stage, path, version, manifest)
        return PromptVersion(stage=stage, version=version, variant=None, text=text, source=path)

    def list_versions(self, stage: str) -> list[VersionInfo]:
        """列出该 stage 全部已发布版本（manifest versions 段）。"""
        manifest = self._load_manifest()
        return [VersionInfo(**v) for v in manifest.get("versions", {}).get(stage, [])]

    def stage_config(self, stage: str) -> dict:
        """返回该 stage 的 active/ab 配置（manifest 缺失时默认 @working）。"""
        manifest = self._load_manifest()
        return manifest["stages"].get(stage, {"active": "@working", "ab": {"enabled": False}})

    def publish(self, stage: str, note: str = "") -> VersionInfo:
        """发布新版本：工作副本 → versions/<stage>/vNNN.txt（临时文件 + os.replace 原子写）。

        - 校验工作副本存在 + 必备占位符完整
        - 版本号 vNNN 递增（基于 manifest versions 段）
        - 更新 manifest versions 段（sha256/created_at 自动维护）
        """
        manifest = self._load_manifest()
        if stage not in manifest["stages"]:
            raise PromptManifestError(f"未知 stage: {stage}")
        work = self.prompts_root / f"{stage}.txt"
        if not work.exists():
            raise PromptManifestError(f"工作副本不存在: {work}")
        text = self._read_text(work)
        self._validate_placeholders(stage, text)

        ver = self._next_version(stage, manifest)
        ver_dir = self.prompts_root / "versions" / stage
        ver_dir.mkdir(parents=True, exist_ok=True)
        target = ver_dir / f"{ver}.txt"
        # 原子写：临时文件写完再 rename，杜绝读到半写内容
        tmp = ver_dir / f".{ver}.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)

        created_at = _now_iso()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        info = VersionInfo(
            version=ver,
            file=f"versions/{stage}/{ver}.txt",
            sha256=sha,
            created_at=created_at,
            note=note,
        )
        stage_versions = manifest.setdefault("versions", {}).setdefault(stage, [])
        stage_versions = [v for v in stage_versions if v.get("version") != ver]
        stage_versions.append(asdict(info))
        stage_versions.sort(key=lambda v: v["version"])
        manifest["versions"][stage] = stage_versions
        self._write_manifest(manifest)
        logger.info("提示词发布: stage=%s version=%s sha=%s", stage, ver, sha[:8])
        return info

    def activate(self, stage: str, version_ref: str) -> None:
        """激活版本：'@working'（工作副本热更新）或 'vNNN' / 'versions/<stage>/vNNN.txt'（钉版）。"""
        manifest = self._load_manifest()
        if stage not in manifest["stages"]:
            raise PromptManifestError(f"未知 stage: {stage}")
        if version_ref != "@working":
            path = self._resolve_ref(stage, version_ref)
            if not path.exists():
                raise PromptManifestError(f"版本文件不存在: {version_ref}")
        manifest["stages"][stage]["active"] = version_ref
        self._write_manifest(manifest)
        logger.info("提示词激活: stage=%s active=%s", stage, version_ref)

    def configure_ab(
        self,
        stage: str,
        a: str,
        b: str,
        split: float,
        bucket_by: str = "file_id",
        enabled: bool = True,
    ) -> None:
        """配置 A/B 分流。

        - a/b：版本引用（"vNNN" / "versions/<stage>/vNNN.txt"），必须存在且不同
        - split：A 流量占比 0.0~1.0
        - bucket_by：file_id | memory_id | random
        - enabled=false：关闭 A/B（下次渲染起回落到 active 指向）
        """
        manifest = self._load_manifest()
        if stage not in manifest["stages"]:
            raise PromptManifestError(f"未知 stage: {stage}")
        if enabled:
            try:
                split_f = float(split)
            except (TypeError, ValueError) as e:
                raise PromptManifestError(f"split 须为数字: {split}") from e
            if not (0.0 <= split_f <= 1.0):
                raise PromptManifestError(f"split 须在 [0,1]: {split}")
            if bucket_by not in _VALID_BUCKET_BY:
                raise PromptManifestError(f"bucket_by 须为 {_VALID_BUCKET_BY}: {bucket_by}")
            pa = self._resolve_ref(stage, a)
            pb = self._resolve_ref(stage, b)
            if not pa.exists() or not pb.exists():
                raise PromptManifestError(f"ab 文件不存在: {a} / {b}")
            if a == b or pa == pb:
                raise PromptManifestError("ab 的 a/b 必须指向不同文件")
            scfg = manifest["stages"][stage].setdefault("ab", {})
            scfg.update({
                "enabled": True,
                "a": a,
                "b": b,
                "split": split_f,
                "bucket_by": bucket_by,
            })
        else:
            manifest["stages"][stage].setdefault("ab", {})["enabled"] = False
        self._write_manifest(manifest)
        logger.info("提示词 A/B 配置: stage=%s enabled=%s split=%s bucket_by=%s",
                    stage, enabled, split, bucket_by)

    def revision(self) -> str:
        """manifest 内容指纹（VersionedSource 协议；manifest 缺失返回 'default'）。"""
        if not self.manifest_path.exists():
            return "default"
        return hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()[:12]

    # ---------- 内部：manifest 读写与校验 ----------

    def _load_manifest(self) -> dict:
        """读取并校验 manifest；缺失时返回全 @working 默认（向后兼容老库/老测试）。"""
        if not self.manifest_path.exists():
            return self._default_manifest()
        try:
            raw = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise PromptManifestError(f"manifest YAML 解析失败: {e}") from e
        if not isinstance(raw, dict) or not isinstance(raw.get("stages"), dict):
            raise PromptManifestError("manifest 缺 stages 段（或格式错误）")
        stages = dict(raw["stages"])
        for s in STAGES:
            stages.setdefault(s, {"active": "@working", "ab": {"enabled": False}})
        raw["stages"] = stages
        raw.setdefault("versions", {})
        self._validate_manifest(raw)
        return raw

    def _default_manifest(self) -> dict:
        return {
            "stages": {s: {"active": "@working", "ab": {"enabled": False}} for s in STAGES},
            "versions": {},
        }

    def _validate_manifest(self, data: dict) -> None:
        """坏配置抛 PromptManifestError（读时校验；sha256 懒校验仅告警，不在此处）。"""
        for stage, scfg in data["stages"].items():
            active = scfg.get("active", "@working")
            if active != "@working":
                path = self._resolve_ref(stage, active)
                if not path.exists():
                    raise PromptManifestError(f"stage={stage} active 指向不存在文件: {active}")
            ab = scfg.get("ab") or {}
            if ab.get("enabled"):
                a = ab.get("a")
                b = ab.get("b")
                if not a or not b:
                    raise PromptManifestError(f"stage={stage} ab.enabled=true 但缺 a/b")
                pa = self._resolve_ref(stage, a)
                pb = self._resolve_ref(stage, b)
                if not pa.exists() or not pb.exists():
                    raise PromptManifestError(f"stage={stage} ab 文件不存在: {a} / {b}")
                if a == b or pa == pb:
                    raise PromptManifestError(f"stage={stage} ab 的 a/b 必须指向不同文件")
                try:
                    split = float(ab.get("split", 0.5))
                except (TypeError, ValueError) as e:
                    raise PromptManifestError(f"stage={stage} ab.split 须为数字") from e
                if not (0.0 <= split <= 1.0):
                    raise PromptManifestError(f"stage={stage} ab.split 须在 [0,1]")
                if ab.get("bucket_by", "file_id") not in _VALID_BUCKET_BY:
                    raise PromptManifestError(f"stage={stage} ab.bucket_by 非法: {ab.get('bucket_by')}")

    def _write_manifest(self, data: dict) -> None:
        """整文件重写（文件小，无需 round-trip；字段英文，注释不保留）。"""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ---------- 内部：版本解析 ----------

    def _resolve_ref(self, stage: str, ref: str) -> Path:
        """把版本引用解析为绝对路径：'@working' | 'versions/...' | 'vNNN'。"""
        if ref == "@working":
            return self.prompts_root / f"{stage}.txt"
        if ref.startswith("versions/"):
            return self.prompts_root / ref
        if re.fullmatch(r"v\d+", ref):
            return self.prompts_root / "versions" / stage / f"{ref}.txt"
        raise PromptManifestError(f"无法解析版本引用: {ref!r}")

    def _version_of(self, stage: str, path: Path, ref: str) -> str:
        """从引用/路径推导运行时版本号：@working → working-<sha8>；版本文件 → vNNN。"""
        if ref == "@working":
            text = self._read_text(path)
            return f"working-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"
        return path.stem

    def _next_version(self, stage: str, manifest: dict) -> str:
        nums = []
        for v in manifest.get("versions", {}).get(stage, []):
            m = re.fullmatch(r"v(\d+)", str(v.get("version", "")))
            if m:
                nums.append(int(m.group(1)))
        nxt = (max(nums) + 1) if nums else 1
        return f"v{nxt:03d}"

    def _validate_placeholders(self, stage: str, text: str) -> None:
        missing = [p for p in STAGE_PLACEHOLDERS.get(stage, []) if p not in text]
        if missing:
            raise PromptManifestError(f"stage={stage} 缺少必备占位符: {missing}")

    def _lazy_verify_sha(self, stage: str, path: Path, version: str, manifest: dict) -> None:
        """读取时懒校验 sha256（仅告警，不阻塞；发布时已强校验）。"""
        for vi in manifest.get("versions", {}).get(stage, []):
            if vi.get("version") == version:
                try:
                    actual = hashlib.sha256(self._read_text(path).encode("utf-8")).hexdigest()
                    if actual != vi.get("sha256"):
                        logger.warning(
                            "提示词版本 sha256 不一致: stage=%s version=%s（文件可能被篡改）",
                            stage, version,
                        )
                except OSError as e:
                    logger.warning("提示词版本读取失败: stage=%s version=%s err=%s", stage, version, e)
                return

    # ---------- A/B 分流 ----------

    def _bucket(self, stage: str, ctx: BucketCtx | None) -> str:
        """确定性分流：sha256(bucket_key) 前 8 字节取模 100，< split*100 走 A。

        bucket_by=file_id（默认）：同一 file_id 永远同一变体，A/B 指标可重复不串扰。
        bucket_by=random：仅供临时实验（每次调用随机）。
        """
        manifest = self._load_manifest()
        ab = manifest["stages"][stage].get("ab", {})
        bucket_by = ab.get("bucket_by", "file_id")
        if bucket_by == "random":
            key = str(uuid.uuid4())
        else:
            key = ctx.bucket_key if (ctx and ctx.bucket_key) else stage
        h = hashlib.sha256(key.encode("utf-8")).digest()[:8]
        val = int.from_bytes(h, "big") % 100
        split = float(ab.get("split", 0.5))
        return "A" if val < split * 100 else "B"

    # ---------- 内部：IO ----------

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
