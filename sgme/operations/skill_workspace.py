# -*- coding: utf-8 -*-
"""operations/skill_workspace.py：技能编写工作流（SGME 2.0 设计 §5.2）。

职责：把「技能库里的一行/一包」拉成**本地可编辑目录**，编辑后比对差异并提交入库。
库 = ``skills.db``（2.0 全存库口径）：

- ``skills`` 表存 SKILL.md 正文（``content`` 仅正文，frontmatter 拆成列）；
- ``skill_files`` 表存附属文件（references/scripts/assets …，由
  ``data/skill_package_dao.py`` 维护）；
- ``skill_revisions`` 表存 append-only 修订史（替代 git log）。

工作副本目录布局::

    <dir>/
      SKILL.md                 # 由库内 frontmatter+正文重建，可直接编辑
      .sgme-manifest.json      # 同步清单（拉取时库内状态；push 后刷新）
      references/… scripts/…   # 附属文件，原字节保真

三个核心操作（显式 conn 的业务函数，CLI 只做协议翻译）：

``pull_skill``
    库 → 目录。先校验目标目录：有未提交改动 / 非本技能工作副本时拒绝覆盖
    （``--force`` 才整目录原子替换）。写盘复用
    ``data/skill_package.write_skill_package``（临时目录 + rename，失败不留残件）。

``status_skill``
    目录 vs 库。SKILL.md 按**语义**比较（正文 sha + 归一化 frontmatter），避免
    YAML 键序/空白差异造成假脏；附属文件按逐文件 sha256 比较。方向判断借助
    manifest：本地没动而库变了 → 「库已更新」，反之「本地已修改」。

``push_skill``
    目录 → 库。顺序：解析 SKILL.md → 名称校验 → ``gates.lint_skill`` 格式门禁
    （error 即拒绝，返回 violations 清单，零写入）→ 附属文件差异 → 写
    ``skills`` 主表（``skills_dao.upsert_skill``）→ 写 ``skill_files``
    （``skill_package_dao.put_files`` / ``delete_files``，表未建先 ensure_tables）
    → 记修订史（``record_revision``）→ 刷新 manifest。各 DAO 自行提交，
    中途失败可能留下部分写入（单人自用工具，暂不做跨表事务）。

设计取舍：
- **不做双向合并**：单人自用，库为准。manifest 记录拉取时的包指纹，push 时若
  库已变化（别处 push 过）默认拒绝，提示先 pull；``force=True`` 强制覆盖。
- **附属文件全量重写**：push 把本地全部附属文件整批 ``put_files``（幂等覆盖），
  先删库内多余路径，保证聚合哈希恰好覆盖当前包，不做逐文件增量推断。
- **不碰 git**：1.x 的 ``skills/store.py`` 仍写 git 工作区；本模块面向 2.0
  全存库，两者在迁移完成前不互通（迁移脚本负责一次性入库）。
- **未知 frontmatter 字段不保留**：库表只存 description/category/tags/version/
  pattern/uses，pull 重建时只写这些字段（避免把列里没有的元数据凭空造出来）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sgme.data import skill_package_dao, skills_dao
from sgme.data.skill_package import write_skill_package
from sgme.operations.skills import iter_package_files
from sgme.skills.gates import lint_skill
from sgme.skills.indexer import parse_skill_md, validate_name
from sgme.skills.store import render_skill_md

__all__ = [
    "MANIFEST_NAME",
    "SKILL_FILE",
    "library_state",
    "list_workspaces",
    "local_state",
    "pull_skill",
    "push_skill",
    "read_manifest",
    "status_skill",
]

# 工作副本清单（D-59）：本文件是本地缓存元数据，**不**属于技能包内容
MANIFEST_NAME = ".sgme-manifest.json"
SKILL_FILE = "SKILL.md"

# frontmatter 中由库表承载、pull 时可重建的字段（顺序即渲染顺序）
_META_FIELDS = ("description", "category", "version", "pattern")


# ---------- 基础工具 ----------


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（与 skill_package_dao 同口径）。"""
    return datetime.now(UTC).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_list(value: Any) -> list[str]:
    """YAML 标量/列表归一为去空白字符串列表（镜像 indexer._to_list）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    s = str(value).strip()
    return [s] if s else []


def _normalize_newlines(text: str) -> str:
    """统一换行为 LF：库内正文不应因编辑器/平台携带 CRLF 噪声。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_meta(meta: dict | None) -> dict[str, Any]:
    """把 frontmatter 归一成库表可承载的字段集，供比较与落库。

    tags 与 indexer 同规：缺省/空列表一律补 ``["skill"]``，否则 push 写入后
    再 status 会因「库有 skill 标签、本地没有」恒判脏。
    """
    src = meta or {}
    out: dict[str, Any] = {}
    for field in _META_FIELDS:
        value = _normalize_newlines(str(src.get(field) or "")).strip()
        if value:
            out[field] = value
    out["tags"] = _as_list(src.get("tags")) or ["skill"]
    uses = _as_list(src.get("uses"))
    if uses:
        out["uses"] = uses
    return out


def _meta_signature(meta: dict[str, Any]) -> str:
    """元数据指纹：键排序 + 列表顺序无关（tags/uses 排序后参与）。"""
    payload = {
        k: (sorted(v) if isinstance(v, list) else v) for k, v in (meta or {}).items()
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _package_sha256(skill_md_sha: str, files: dict[str, str]) -> str:
    """包指纹：SKILL.md + 全部附属文件按 path 字节序拼接后取 sha256。

    公式与 ``data/skill_package._package_sha256`` 同构（``<sha>  <path>\\n``），
    只是 SKILL.md 的 sha 取**规范化重建文本**的哈希（库内只存正文，无原始字节）。
    该值用于 manifest 的「库是否已变化」判定与修订史锚点。
    """
    rows = [("SKILL.md", skill_md_sha), *files.items()]
    rows.sort(key=lambda r: r[0].encode("utf-8"))
    concat = "".join(f"{sha}  {path}\n" for path, sha in rows)
    return _sha256(concat.encode("utf-8"))


def _canonical_skill_md(name: str, meta: dict[str, Any], body: str) -> str:
    """库内记录 → 规范 SKILL.md 文本（frontmatter 键序稳定）。"""
    full_meta = {"name": name, **{k: v for k, v in meta.items() if k != "name"}}
    return render_skill_md(full_meta, body)


def read_manifest(directory: Path | str) -> dict | None:
    """读取工作副本清单；不存在返回 None，JSON 损坏抛 ValueError。"""
    path = Path(directory) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"本地清单损坏（{path}）: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"本地清单格式非法（应为 JSON 对象）: {path}")
    return data


def _write_manifest(directory: Path, manifest: dict) -> Path:
    """写工作副本清单（UTF-8、缩进 2、末尾换行；覆盖写）。"""
    path = Path(directory) / MANIFEST_NAME
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _local_matches_manifest(manifest: dict | None, local: dict | None) -> bool:
    """本地副本是否与拉取时完全一致（SKILL.md 原始字节 + 全部附属文件）。"""
    if not manifest or not local:
        return False
    if manifest.get("local_skill_sha256") != local.get("raw_skill_sha256"):
        return False
    recorded = {
        f["path"]: f["sha256"]
        for f in (manifest.get("files") or [])
        if isinstance(f, dict) and f.get("path")
    }
    return recorded == local.get("files")


def _manifest_from_state(
    *,
    name: str,
    meta: dict[str, Any],
    body_sha: str,
    skill_md: str,
    files: dict[str, str],
    local_skill_md: str | None = None,
) -> dict[str, Any]:
    """构造清单（content_hash=库内规范文本哈希；local_skill_sha256=本地原始字节哈希）。"""
    canonical_sha = _sha256(skill_md.encode("utf-8"))
    manifest = {
        "name": name,
        "version": str(meta.get("version") or ""),
        "content_hash": canonical_sha,
        "skill_sha256": body_sha,
        "local_skill_sha256": _sha256(
            (local_skill_md if local_skill_md is not None else skill_md).encode("utf-8")
        ),
        "files": [{"path": p, "sha256": files[p]} for p in sorted(files)],
        "package_sha256": _package_sha256(canonical_sha, files),
        "fetched_at": _now_iso(),
    }
    return manifest


# ---------- 库侧读取 ----------


def library_state(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """读取库内技能完整状态；不存在返回 None。

    Returns:
        ``{name, meta, body, skill_sha256, skill_md, files{path: sha256},
        package_sha256}``。meta 为归一化后的 frontmatter 字段（不含 name）。
    """
    skill_package_dao.ensure_tables(conn)  # 附属文件表可能尚未建（迁移期兜底）
    rec = skills_dao.get_skill(conn, name)
    if rec is None:
        return None
    meta = _normalize_meta(
        {
            "description": rec.get("description"),
            "category": rec.get("category"),
            "tags": rec.get("tags"),
            "version": rec.get("version"),
            "pattern": rec.get("pattern"),
            "uses": skills_dao.find_outgoing(conn, name),
        }
    )
    body = str(rec.get("content") or "")
    skill_md = _canonical_skill_md(name, meta, body)
    files = {
        row["path"]: row["sha256"]
        for row in skill_package_dao.list_files(conn, name)
    }
    return {
        "name": name,
        "meta": meta,
        "body": body,
        "skill_sha256": str(rec.get("sha256") or ""),
        "skill_md": skill_md,
        "files": files,
        "package_sha256": _package_sha256(_sha256(skill_md.encode("utf-8")), files),
    }


# ---------- 本地侧读取 ----------


def local_state(directory: Path | str) -> dict[str, Any]:
    """读取工作副本目录的本地状态（不依赖 manifest）。

    Returns:
        ``{name, dir, skill_md, parsed, meta, body, skill_sha256,
        raw_skill_sha256, files{path: sha256}}``。

    Raises:
        FileNotFoundError: 目录不存在或缺少 SKILL.md。
        ValueError: SKILL.md 不是 UTF-8 文本。
    """
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"工作副本目录不存在: {root}")
    skill_path = root / SKILL_FILE
    if not skill_path.is_file():
        raise FileNotFoundError(f"工作副本缺少 {SKILL_FILE}: {root}")
    try:
        text = skill_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{SKILL_FILE} 不是 UTF-8 文本: {e}") from e

    parsed = parse_skill_md(_normalize_newlines(text))
    meta = _normalize_meta(parsed["meta"])
    body = _normalize_newlines(str(parsed["body"] or "")).strip()
    files: dict[str, str] = {}
    for rel, src in iter_package_files(root):
        if rel in (SKILL_FILE, MANIFEST_NAME):
            continue
        try:
            files[rel] = _sha256(src.read_bytes())
        except OSError as e:
            raise ValueError(f"附属文件不可读: {rel}（{e}）") from e
    return {
        "name": root.name,
        "dir": str(root),
        "skill_md": text,
        "parsed": parsed,
        "meta": meta,
        "body": body,
        "skill_sha256": _sha256(body.encode("utf-8")),
        "raw_skill_sha256": _sha256(text.encode("utf-8")),
        "files": files,
    }


# ---------- status ----------


def status_skill(conn: sqlite3.Connection, directory: Path | str) -> dict[str, Any]:
    """对比工作副本与库内状态，返回差异清单（不写任何一方）。

    差异口径：
    - SKILL.md：正文 sha256 + 归一化元数据指纹任一不同 → ``modified``；
      本地缺 SKILL.md → ``missing``；库内无此技能 → ``added``。
    - 附属文件：同路径 sha 不同 → modified；本地多出 → added；库内多出 →
      deleted（本地删除或库新增未拉取都会落在这里，配合 ``library_changed``
      提示方向）。

    Returns:
        ``{name, dir, manifest_exists, library_exists, skill{state,local_sha256,
        library_sha256,changed_by}, files{modified|added|deleted}, dirty,
        library_changed, package_sha256}``。
    """
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"工作副本目录不存在: {root}")
    manifest = read_manifest(root)
    name = str((manifest or {}).get("name") or root.name)
    validate_name(name)

    lib = library_state(conn, name)
    has_skill_md = (root / SKILL_FILE).is_file()
    local = local_state(root) if has_skill_md else None

    # 只拿文件表，不重复读 SKILL.md 内容
    local_files = local["files"] if local else {}
    lib_files = lib["files"] if lib else {}

    modified = sorted(
        p for p in local_files.keys() & lib_files.keys()
        if local_files[p] != lib_files[p]
    )
    added = sorted(local_files.keys() - lib_files.keys())
    deleted = sorted(lib_files.keys() - local_files.keys())

    if lib is None:
        skill_state = "added" if local else "missing"
        changed_by = "local"
    elif local is None:
        skill_state = "missing"
        changed_by = "local"
    else:
        same = (
            local["skill_sha256"] == lib["skill_sha256"]
            and _meta_signature(local["meta"]) == _meta_signature(lib["meta"])
        )
        skill_state = "same" if same else "modified"
        changed_by = ""
        if not same:
            # 本地未动（原始字节与拉取时一致）而库变了 → 库侧更新
            local_untouched = bool(
                manifest
                and manifest.get("local_skill_sha256")
                and manifest["local_skill_sha256"] == local["raw_skill_sha256"]
            )
            changed_by = "library" if local_untouched else "local"

    dirty = skill_state != "same" or bool(modified or added or deleted)
    library_changed = bool(
        manifest
        and (
            lib is None
            or manifest.get("package_sha256") != lib["package_sha256"]
        )
    )

    return {
        "name": name,
        "dir": str(root),
        "manifest_exists": manifest is not None,
        "library_exists": lib is not None,
        "skill": {
            "state": skill_state,
            "local_sha256": local["skill_sha256"] if local else None,
            "library_sha256": lib["skill_sha256"] if lib else None,
            "changed_by": changed_by or None,
        },
        "files": {"modified": modified, "added": added, "deleted": deleted},
        "dirty": dirty,
        "library_changed": library_changed,
        "package_sha256": lib["package_sha256"] if lib else None,
        "local_files": local_files,
        "library_files": lib_files,
        "manifest": manifest,
        "local": local,
        "library": lib,
    }


# ---------- pull ----------


def pull_skill(
    conn: sqlite3.Connection,
    name: str,
    directory: Path | str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """把库内技能拉成可编辑目录（SKILL.md + 附属文件 + 清单）。

    目标目录已存在时：空的直接写；是本技能工作副本且无未提交改动 → 允许重拉；
    有改动或不是本技能 → 拒绝（``code`` = ``dirty`` / ``occupied``），
    ``force=True`` 才整目录原子替换（会丢弃目录内全部未入库内容）。

    Returns:
        ``{"ok": True, "name", "dir", "files", "package_sha256"}`` 或
        ``{"ok": False, "code", "message"}``。
    """
    try:
        name = validate_name(name)
    except ValueError as e:
        return {"ok": False, "code": "invalid_name", "message": str(e)}

    lib = library_state(conn, name)
    if lib is None:
        return {"ok": False, "code": "not_found", "message": f"库内不存在技能: {name}"}

    dest = Path(directory)
    if dest.exists() and not dest.is_dir():
        return {"ok": False, "code": "not_a_dir", "message": f"目标路径不是目录: {dest}"}

    if dest.is_dir():
        try:
            non_empty = any(dest.iterdir())
        except OSError as e:
            return {"ok": False, "code": "io_error", "message": f"目标目录不可读: {e}"}
        if non_empty:
            manifest = read_manifest(dest)
            same_name = bool(manifest and manifest.get("name") == name)
            if same_name:
                st = status_skill(conn, dest)
                if st["dirty"] and not force:
                    # 本地副本与拉取时完全一致（只是库被别人改了）→ 允许刷新；
                    # 本地确实动过才拒绝，防覆盖未入库改动。
                    if not _local_matches_manifest(manifest, st.get("local")):
                        return {
                            "ok": False,
                            "code": "dirty",
                            "message": (
                                f"工作副本有未入库改动（{st['dir']}）；"
                                "先 push，或加 --force 丢弃后重拉"
                            ),
                        }
            elif not force:
                return {
                    "ok": False,
                    "code": "occupied",
                    "message": (
                        f"目标目录非空且不是 {name} 的工作副本: {dest}；"
                        "换空目录或加 --force 覆盖"
                    ),
                }

    payload: list[dict[str, Any]] = [
        {"path": SKILL_FILE, "content": lib["skill_md"].encode("utf-8")}
    ]
    for rel in sorted(lib["files"]):
        data = skill_package_dao.get_file(conn, name, rel)
        if data is None:  # list/get 之间被并发删除：按缺件报错，不写半包
            return {
                "ok": False,
                "code": "missing_file",
                "message": f"库内附属文件读取失败（可能被并发删除）: {rel}",
            }
        payload.append({"path": rel, "content": data})

    manifest = _manifest_from_state(
        name=name,
        meta=lib["meta"],
        body_sha=lib["skill_sha256"],
        skill_md=lib["skill_md"],
        files=lib["files"],
    )
    payload.append(
        {
            "path": MANIFEST_NAME,
            "content": json.dumps(
                manifest, ensure_ascii=False, indent=2
            ).encode("utf-8") + b"\n",
        }
    )

    try:
        written = write_skill_package({"files": payload}, dest)
    except (ValueError, OSError) as e:
        return {"ok": False, "code": "write_failed", "message": f"写工作副本失败: {e}"}

    return {
        "ok": True,
        "name": name,
        "dir": str(dest),
        "files": written,
        "package_sha256": manifest["package_sha256"],
    }


# ---------- push ----------


def _lint_violations(
    name: str, local: dict[str, Any], conn: sqlite3.Connection
) -> list[str]:
    """名称与格式门禁：返回违规清单（空 = 通过）。"""
    violations: list[str] = []
    declared = str(local["parsed"]["meta"].get("name") or "").strip()
    if declared and declared != name:
        violations.append(
            f"frontmatter name（{declared}）与清单/技能名（{name}）不一致"
        )
    existing = {n for n in skills_dao.list_all_sha(conn) if n != name}
    violations.extend(
        lint_skill(
            local["parsed"]["meta"],
            local["body"],
            name,
            existing,
            skill_dir=Path(local["dir"]),
        )
    )
    return violations


def push_skill(
    conn: sqlite3.Connection,
    directory: Path | str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """把工作副本的改动提交入库（lint 门禁 → skills 主表 → 附属文件 → 修订史）。

    Args:
        conn: skills.db 连接（``data.db.connect_skills`` 产出）。
        directory: 工作副本目录（含 SKILL.md）。
        force: 库内该技能自上次 pull 后已变化时仍强制覆盖（默认拒绝，提示先 pull）。

    Returns:
        成功 ``{"ok": True, "name", "changed": bool, ...}``；
        拒绝 ``{"ok": False, "code": "lint_failed"|"conflict"|..., "violations"/"message"}``。
    """
    root = Path(directory)
    if not root.is_dir():
        return {"ok": False, "code": "not_found", "message": f"工作副本目录不存在: {root}"}
    if not (root / SKILL_FILE).is_file():
        return {
            "ok": False,
            "code": "missing_skill_md",
            "message": f"工作副本缺少 {SKILL_FILE}: {root}",
        }

    manifest = read_manifest(root)
    name = str((manifest or {}).get("name") or root.name)
    try:
        name = validate_name(name)
    except ValueError as e:
        return {"ok": False, "code": "invalid_name", "message": str(e)}

    try:
        local = local_state(root)
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "code": "invalid_workdir", "message": str(e)}

    violations = _lint_violations(name, local, conn)
    if violations:
        return {
            "ok": False,
            "code": "lint_failed",
            "message": "格式 lint 未通过，拒绝入库",
            "violations": violations,
        }

    st = status_skill(conn, root)
    if not st["dirty"]:
        return {
            "ok": True,
            "name": name,
            "changed": False,
            "message": "工作副本与库一致，无变更（未入库）",
        }
    if manifest and st["library_changed"] and not force:
        return {
            "ok": False,
            "code": "conflict",
            "message": (
                f"库内技能 {name} 在 pull 之后已被修改，拒绝覆盖；"
                "先重新 pull 合并，或加 --force 以本地为准"
            ),
            "library_package_sha256": st["package_sha256"],
            "manifest_package_sha256": manifest.get("package_sha256"),
        }

    meta = local["meta"]
    body = local["body"]
    canonical_md = _canonical_skill_md(name, meta, body)
    canonical_sha = _sha256(canonical_md.encode("utf-8"))
    old_sha = st["library"]["skill_sha256"] if st["library"] else ""

    rec = {
        "name": name,
        "sha256": local["skill_sha256"],
        "description": str(meta.get("description") or ""),
        "category": str(meta.get("category") or "") or None,
        "tags": list(meta.get("tags") or []),
        "version": str(meta.get("version") or "") or None,
        "pattern": str(meta.get("pattern") or "") or None,
        "source": "workspace",
        "origin_path": str(root.resolve()),
        "content": body,
    }
    skills_dao.upsert_skill(conn, rec)
    skills_dao.replace_uses(conn, name, list(meta.get("uses") or []))
    conn.commit()

    # 先删库内多余文件，再整批覆盖写当前文件 → put_files 返回的聚合哈希恰好
    # 覆盖当前包（避免只算增量片段）。
    removed = st["files"]["deleted"]
    deleted = skill_package_dao.delete_files(conn, name, removed) if removed else 0

    attachments: list[dict[str, Any]] = []
    total_bytes = len(canonical_md.encode("utf-8"))
    for rel in sorted(local["files"]):
        data = (root / rel).read_bytes()
        attachments.append({"path": rel, "content": data})
        total_bytes += len(data)
    put_result = skill_package_dao.put_files(conn, name, attachments) if attachments else {
        "written": 0,
        "total_bytes": 0,
        "package_sha256": _sha256(b""),
    }

    file_shas = {a["path"]: _sha256(a["content"]) for a in attachments}
    package_sha = _package_sha256(canonical_sha, file_shas)
    revision_id = skill_package_dao.record_revision(
        conn,
        name,
        str(meta.get("version") or ""),
        package_sha,
        file_count=len(attachments) + 1,
        total_bytes=total_bytes,
        note="workspace push",
    )

    new_manifest = _manifest_from_state(
        name=name,
        meta=meta,
        body_sha=local["skill_sha256"],
        skill_md=canonical_md,
        files=file_shas,
        local_skill_md=local["skill_md"],
    )
    _write_manifest(root, new_manifest)

    return {
        "ok": True,
        "name": name,
        "changed": True,
        "old_sha256": old_sha,
        "new_sha256": local["skill_sha256"],
        "attachments_written": put_result["written"],
        "attachments_deleted": deleted,
        "package_sha256": package_sha,
        "revision_id": revision_id,
    }


# ---------- list ----------


def list_workspaces(conn: sqlite3.Connection, root: Path | str) -> list[dict[str, Any]]:
    """列出根目录下全部工作副本状态（根自身是工作副本时只列它）。

    Returns:
        ``[{"name", "dir", "status": status_skill(...)}]``；单个目录读取失败时
        该项带 ``"error"`` 字段，不中断其余条目。
    """
    base = Path(root)
    if not base.is_dir():
        raise FileNotFoundError(f"根目录不存在: {base}")

    candidates: list[Path]
    if (base / MANIFEST_NAME).is_file():
        candidates = [base]
    else:
        candidates = sorted(
            (p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name,
        )

    entries: list[dict[str, Any]] = []
    for d in candidates:
        if not (d / MANIFEST_NAME).is_file() and not (d / SKILL_FILE).is_file():
            continue  # 非技能目录（普通子目录）静默跳过
        try:
            st = status_skill(conn, d)
        except Exception as e:  # 单个坏目录不影响整表
            entries.append({"name": d.name, "dir": str(d), "error": str(e)})
        else:
            entries.append({"name": st["name"], "dir": str(d), "status": st})
    return entries
