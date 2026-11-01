"""scripts/prompt_registry.py：提示词版本登记与激活校验（T-315）。

独立脚本模块（不依赖 sgme.config，只吃传入路径，便于测试与手工调用）：

- 背景：2.0 提炼提示词已迭代到 v019，但 manifest（sgme/resources/prompts/manifest.yaml
  的 versions 段，由 sgme/prompts/manager.py 的 PromptStore.publish 维护）只登记到 v009，
  v010–v019 未登记；全量重建靠手工 cp 工作副本、跑完再拷回，缺一致性校验。

- 路径约定：prompt_root = 某 stage 的版本目录（如
  sgme/resources/prompts/versions/l1_extraction/）。由此可反推：
  工作副本 = prompt_root.parent.parent / f"{stage}.txt"（prompts/<stage>.txt），
  项目 manifest = prompt_root.parent.parent / "manifest.yaml"。

- 登记文件：版本目录内 .index.json（JSON，键=版本名，值={sha256, size, registered_at}）。
  registered 判断优先看项目 manifest 的 versions 段；无 manifest 时回落 .index.json。

- sha256 口径：本模块按**文件字节**计算（compute_sha256）；PromptStore.publish 写入
  manifest 的 sha256 是 read_text() 文本模式 CRLF→LF 归一化后的**文本 sha**。
  对 CRLF 文件两者不同（如 l1_extraction v007：字节 99eb592d… / 文本 126037f0…）。
  本模块的 registered 只判断「是否登记过」，不做 sha 对账；校验现役请用
  verify_active（工作副本 vs 版本文件，同一口径内部自洽）。

用法（登记 v019 并核对现役）：
  python scripts/prompt_registry.py list   <versions_dir>
  python scripts/prompt_registry.py register <versions_dir> v019
  python scripts/prompt_registry.py verify <versions_dir> v019
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# 版本文件名规则：vNNN.txt（与 PromptStore._next_version 的 v%03d 一致）
_VERSION_RE = re.compile(r"v\d+")

# 登记索引文件名（版本目录内）
INDEX_NAME = ".index.json"


# ---------- 基础 ----------

def compute_sha256(path: Path) -> str:
    """文件 sha256：64 位小写十六进制（流式读取，兼容大文件）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    """UTC ISO 时间戳（与 PromptStore.publish 的 created_at 同格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_version_name(name: str) -> bool:
    """版本名必须形如 vNNN（防把工作副本/任意文件登记进索引）。"""
    return bool(_VERSION_RE.fullmatch(name))


# ---------- 登记依据 ----------

def _manifest_registered_versions(prompt_root: Path) -> set[str] | None:
    """读项目 manifest.yaml 中该 stage 已登记版本集合；无 manifest 返回 None（回落索引）。

    manifest 由 PromptStore（sgme/prompts/manager.py）维护，versions 段结构：
    versions: {<stage>: [{version: vNNN, file: ..., sha256: ..., ...}, ...]}
    """
    prompts_root = prompt_root.parent.parent          # versions/<stage>/ → prompts/
    manifest_path = prompts_root / "manifest.yaml"
    if not manifest_path.exists():
        return None
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    entries = (raw.get("versions") or {}).get(prompt_root.name)
    if not isinstance(entries, list):
        return None
    return {str(e.get("version")) for e in entries
            if isinstance(e, dict) and e.get("version")}


def _load_index(prompt_root: Path) -> dict:
    """读版本目录内 .index.json；缺失/损坏视为空（不抛，列表仍可用）。"""
    index_path = prompt_root / INDEX_NAME
    if not index_path.exists():
        return {}
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# ---------- 契约函数 ----------

def list_versions(prompt_root: Path) -> list[dict]:
    """列出版本目录下全部 vNNN.txt，每条 {version, sha256, size, registered}。

    - 只识别 vNNN.txt，忽略 .index.json 及其他文件
    - 版本号按数值排序（v002 < v010，字典序会错排）
    - registered：优先按项目 manifest versions 段判断；无 manifest 时按 .index.json
    """
    prompt_root = Path(prompt_root)
    manifest_versions = _manifest_registered_versions(prompt_root)
    index = {} if manifest_versions is not None else _load_index(prompt_root)

    items = []
    for f in prompt_root.glob("v*.txt"):
        if not _is_version_name(f.stem):
            continue
        if manifest_versions is not None:
            registered = f.stem in manifest_versions
        else:
            registered = f.stem in index
        items.append({
            "version": f.stem,
            "sha256": compute_sha256(f),
            "size": f.stat().st_size,
            "registered": registered,
        })
    items.sort(key=lambda i: int(i["version"][1:]))
    return items


def register(prompt_root: Path, version: str) -> dict:
    """登记版本：计算 sha256 并写/更新版本目录内 .index.json，返回写入结果。

    - 幂等：同版本同内容重复登记不改动索引、registered_at 保持原值
    - 同版本内容变化（文件被重写）：更新 sha256/size 并刷新 registered_at
    - 版本名非 vNNN → ValueError；版本文件不存在 → FileNotFoundError（均不写索引）
    """
    prompt_root = Path(prompt_root)
    if not _is_version_name(version):
        raise ValueError(f"非法版本名（须为 vNNN）: {version!r}")
    version_file = prompt_root / f"{version}.txt"
    if not version_file.exists():
        raise FileNotFoundError(f"版本文件不存在: {version_file}")

    sha = compute_sha256(version_file)
    size = version_file.stat().st_size

    index_path = prompt_root / INDEX_NAME
    index = _load_index(prompt_root)
    existing = index.get(version)
    if isinstance(existing, dict) and existing.get("sha256") == sha and existing.get("size") == size:
        # 幂等：内容未变，registered_at 不动
        return {
            "version": version,
            "sha256": sha,
            "size": size,
            "registered_at": existing.get("registered_at", _now_iso()),
            "index_path": str(index_path),
            "updated": False,
        }

    now = _now_iso()
    index[version] = {"sha256": sha, "size": size, "registered_at": now}
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "version": version,
        "sha256": sha,
        "size": size,
        "registered_at": now,
        "index_path": str(index_path),
        "updated": True,
    }


def verify_active(prompt_root: Path, expected_version: str) -> tuple[bool, str]:
    """激活校验：工作副本 prompts/<stage>.txt 与 versions/<stage>/<version>.txt 比对 sha256。

    返回 (是否一致, 说明)；文件缺失不抛异常，返回 (False, 原因)。
    """
    prompt_root = Path(prompt_root)
    stage = prompt_root.name
    version_file = prompt_root / f"{expected_version}.txt"
    work_file = prompt_root.parent.parent / f"{stage}.txt"

    if not version_file.exists():
        return False, f"版本文件不存在: {expected_version}.txt（{version_file}）"
    if not work_file.exists():
        return False, f"工作副本不存在: {work_file.name}（预期与 {expected_version} 一致）"

    version_sha = compute_sha256(version_file)
    work_sha = compute_sha256(work_file)
    if work_sha == version_sha:
        return True, (f"一致：工作副本 {stage}.txt 与 {expected_version}.txt "
                      f"sha256 相同（{version_sha[:8]}）")
    return False, (f"不一致：工作副本 {stage}.txt sha256={work_sha[:8]} "
                   f"vs {expected_version}.txt sha256={version_sha[:8]}")


# ---------- CLI（手工登记/核对用） ----------

def _main(argv: list[str]) -> int:
    usage = __doc__
    if len(argv) < 3:
        print(usage, file=sys.stderr)
        return 2
    cmd, root = argv[1], Path(argv[2])
    if cmd == "list":
        print(json.dumps(list_versions(root), ensure_ascii=False, indent=2))
    elif cmd == "register" and len(argv) >= 4:
        print(json.dumps(register(root, argv[3]), ensure_ascii=False, indent=2))
    elif cmd == "verify" and len(argv) >= 4:
        ok, message = verify_active(root, argv[3])
        print(("OK " if ok else "FAIL ") + message)
        return 0 if ok else 1
    else:
        print(usage, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
