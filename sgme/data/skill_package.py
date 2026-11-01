# -*- coding: utf-8 -*-
"""data/skill_package.py：技能包序列化（2.0 技能全存库的前置能力）。

把技能目录（SKILL.md + references/ + scripts/ + assets/ …）序列化成**可入库结构**：
``{"name", "package_sha256", "files": [{"path", "sha256", "content", "size"}]}``，
并支持反向落盘（原子替换）与逐文件完整性校验——技能真源从 git 迁往数据库后，
这三个函数构成「目录 ↔ 库记录」的双向通道。

哈希口径（与 operations/skills.py 的 canonical manifest 一致，T-281 血统）：
- 每行 ``<sha256 十六进制><两个空格><relpath>\\n``，按 relpath **字节序**排序；
- ``package_sha256`` = 该排序拼接串（UTF-8）的 sha256；空目录 = sha256(b"")。

安全（契约）：包内 path 一律视为**不可信输入**——拒绝 ``..``、绝对路径、盘符、
反斜杠；落盘前拒绝覆盖目标目录内已有的符号链接（不跟随、不篡改链接目标）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

# 包内相对路径的分段禁用表：空段（a//b、尾斜杠）、当前段、上级段
_FORBIDDEN_PARTS = frozenset({"", ".", ".."})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_member_path(rel: str) -> None:
    """校验包内成员相对路径，非法即抛 ValueError（防路径逃逸写穿目标目录）。

    serialize_skill_dir 生成的路径天然合规；此校验防的是**外部来源**
    （数据库 / 网络传输回读的 package）借 path 把文件写到目标目录之外。
    """
    if not isinstance(rel, str) or not rel:
        raise ValueError(f"非法包内路径（必须是非空字符串）: {rel!r}")
    if "\\" in rel:
        raise ValueError(f"非法包内路径（含反斜杠）: {rel!r}")
    if rel.startswith("/"):
        raise ValueError(f"非法包内路径（绝对路径）: {rel!r}")
    if ":" in rel:
        # Windows 盘符（C:/…）与 NTFS 交替数据流（file:stream）一并拒绝
        raise ValueError(f"非法包内路径（含盘符/冒号）: {rel!r}")
    parts = rel.split("/")
    if any(part in _FORBIDDEN_PARTS for part in parts):
        raise ValueError(f"非法包内路径（含空段/./..）: {rel!r}")


def _package_sha256(files: list[dict]) -> str:
    """聚合哈希：排序后的 (path, sha256) 拼接串取 sha256（口径见模块 docstring）。"""
    rows = sorted(((f["path"], f["sha256"]) for f in files), key=lambda r: r[0].encode("utf-8"))
    concat = "".join(f"{sha}  {path}\n" for path, sha in rows)
    return _sha256(concat.encode("utf-8"))


def serialize_skill_dir(path: Path) -> dict:
    """把技能目录序列化成可入库结构。

    - ``name`` = 目录名；遍历目录下**所有文件**（符号链接文件/目录跳过不跟随，
      隐藏目录 ``.`` 开头整支剔除）；相对路径用 posix 分隔符；
    - 每个 file 带 ``path`` / ``sha256``（内容哈希）/ ``content``（原始字节）/
      ``size``（字节数）；
    - 目录不存在或不是目录 → FileNotFoundError。

    files 按遍历序（同层字典序）排列，聚合哈希 ``package_sha256`` 与顺序无关
    （公式内部按 path 字节序重排）。
    """
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"技能目录不存在或不是目录: {root}")

    files: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # 原位修剪 dirnames 控制下钻：隐藏目录整支剔除；符号链接目录不进入
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))
        ]
        dirnames.sort()
        filenames.sort()
        for fn in filenames:
            full = Path(dirpath) / fn
            if full.is_symlink():
                continue  # 符号链接文件不读、不入包
            data = full.read_bytes()
            files.append(
                {
                    "path": full.relative_to(root).as_posix(),
                    "sha256": _sha256(data),
                    "content": data,
                    "size": len(data),
                }
            )

    return {"name": root.name, "package_sha256": _package_sha256(files), "files": files}


def _assert_no_symlinks(tree: Path) -> None:
    """目录树内存在任何符号链接即抛 ValueError（换包不得静默改写链接目标）。"""
    for p in tree.rglob("*"):
        if p.is_symlink():
            raise ValueError(f"目标目录内存在符号链接，拒绝原子替换: {p}")


def write_skill_package(package: dict, dest_dir: Path) -> int:
    """把 package 的 files 原子写进 dest_dir，返回写入文件数。

    原子替换策略：先在 dest 同级写临时暂存目录 → 旧目录整体挪到备份位 →
    暂存目录 rename 成 dest（同卷 rename，Windows/POSIX 均原子）→ 删备份。
    失败即回滚恢复旧目录并清理暂存，不留半成品（极端 I/O 故障下备份目录
    ``.<name>.old-<随机>`` 保留防数据丢失，可手动清理）。

    任一成员路径非法（``..`` / 绝对路径 / 盘符 / 反斜杠）→ ValueError，
    校验先于任何磁盘动作，零副作用；dest 本身是符号链接或树内含符号链接
    → ValueError 拒绝替换。
    """
    files = package.get("files") or []
    for f in files:
        _validate_member_path(f["path"])  # 先全量校验，后动盘

    dest = Path(dest_dir)
    if dest.is_symlink():
        raise ValueError(f"目标路径是符号链接，拒绝替换: {dest}")
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.tmp-", dir=parent))
    backup: Path | None = None
    try:
        for f in files:
            target = staging / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f["content"])
        if dest.exists():
            _assert_no_symlinks(dest)
            backup = parent / f".{dest.name}.old-{os.getpid()}-{uuid4().hex[:8]}"
            os.rename(dest, backup)
        os.chmod(staging, 0o755)  # mkdtemp 默认 0700；技能目录按常规可读权限落地
        os.rename(staging, dest)
        return len(files)
    except Exception:
        # 回滚：暂存目录必清；旧目录已挪走则尽力搬回（搬回失败时保留备份位）
        shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists() and not dest.exists():
            try:
                os.rename(backup, dest)
            except OSError:
                pass  # 恢复失败：备份目录留在原地，不让用户数据凭空消失
        raise
    else:
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def verify_manifest(dest_dir: Path, manifest: dict) -> tuple[bool, list[str]]:
    """逐文件 sha256 校验 dest_dir 是否与 manifest 一致。

    返回 ``(是否全通过, 不一致路径列表)``；缺失文件、非普通文件、符号链接、
    内容哈希不匹配、path 本身非法——都记入不一致列表（校验函数不抛异常，
    坏清单逐条报告）。
    """
    root = Path(dest_dir)
    bad: list[str] = []
    for f in manifest.get("files") or []:
        rel = f.get("path") if isinstance(f, dict) else None
        if not isinstance(rel, str):
            bad.append(str(rel))
            continue
        try:
            _validate_member_path(rel)
        except ValueError:
            bad.append(rel)
            continue
        target = root / rel
        if target.is_symlink() or not target.is_file():
            bad.append(rel)  # 缺失 / 目录 / 符号链接都算不一致
            continue
        if _sha256(target.read_bytes()) != f.get("sha256"):
            bad.append(rel)
    return (not bad, bad)
