"""sgme/skills/store.py：技能写入编排层（ST-36 M3，设计 §四 写侧治理）。

统一入口 = [lint → 查重 → 落盘 → git commit]，全部包在 write_critical()
进程内写锁里（单点串行裁决：NAS Server 进程是唯一合法写入方）。

操作：
- write_skill：lint 通过 → 三层查重通过 → 落盘 <source_dir>/<name>/SKILL.md
  → git add + commit（镜像 skills_hub._run_git：参数数组、禁 shell=True）
- remove_skill：入向引用两级信号扫描（一级=frontmatter uses 有引用且未 force
  → 拒绝并列清单；二级=正文提及 → 只列 warnings 不拦）；默认软删 =
  frontmatter 加 ``deprecated: true`` 后 commit；hard=True 才物理删目录 + commit
- rename_skill：禁止原地改名——写新名完整副本 + 旧位置留墓碑（SKILL.md 只含
  frontmatter ``superseded_by: <new>``）+ commit；墓碑登记 tombstones.json（原子写）

结果约定：**业务拒绝一律返回 ``{"ok": False, "code": ..., ...}`` 字典**
（不抛异常，调用方按 code 翻译 HTTP 错误码）；仅环境故障（git 不可用/超时）
抛 StoreError。code ∈ lint_failed / duplicate / referenced / not_found / conflict。

git 策略：每个 source_dir 是独立 git 仓（真源工作区），commit 在该目录内完成；
无提交身份时兜底配置仓库级 user.name/email（不影响全局，镜像 _ensure_worktree 惯例）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from sgme.skills.dedupe import check_duplicate
from sgme.skills.gates import lint_skill
from sgme.skills.indexer import (
    SKILL_FILE,
    SkillRecord,
    _to_list,
    collect_from_dir,
    parse_skill_md,
    validate_name,
)
from sgme.skills.writesync import write_critical

# 墓碑登记默认路径（相对 SGME_HOME；调用方可用 cfg skills.tombstone_registry 覆盖）
DEFAULT_TOMBSTONE_REGISTRY = "data/skills/tombstones.json"

# git 子进程超时（秒）：本地 commit 秒级完成，超时视为环境异常
_GIT_TIMEOUT_S = 60

# 墓碑登记文件的进程内锁（原子写：tmp + os.replace）
_registry_lock = threading.Lock()


class StoreError(Exception):
    """环境级写侧失败（git 不可用/超时/非零退出），API 层翻译为 500。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


def _reject(code: str, messages: list[str]) -> dict:
    """统一业务拒绝形态：ok=False + code + violations 清单。"""
    return {"ok": False, "code": code, "violations": list(messages), "warnings": []}


# ---------- git 基元（镜像 sgme/skills_hub/__init__.py 的 _run_git 无 shell 参数数组写法） ----------


def _run_git(cwd: Path, args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """执行 git 子进程：参数数组、禁 shell=True、超时 kill；check 失败转 StoreError。"""
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise StoreError(f"git 操作超时（>{_GIT_TIMEOUT_S}s）: {' '.join(cmd[:3])}…") from e
    except FileNotFoundError:
        raise StoreError("系统 git 不可用（skills 写侧依赖 git，请安装并加入 PATH）") from None
    if check and proc.returncode != 0:
        raise StoreError(
            f"git 命令失败（退出码 {proc.returncode}）: {' '.join(cmd[:3])}… "
            f"{(proc.stderr or '').strip()[:500]}"
        )
    return proc


def _ensure_repo_identity(source_dir: Path) -> None:
    """仓库级提交身份兜底（缺失时补默认，不影响全局配置）。"""
    if not (source_dir / ".git").exists():
        raise StoreError(f"source_dir 不是 git 仓库（缺 .git）: {source_dir}")
    if not _run_git(source_dir, ["config", "user.email"]).stdout.strip():
        _run_git(source_dir, ["config", "user.email", "sgme-skills@local"], check=True)
    if not _run_git(source_dir, ["config", "user.name"]).stdout.strip():
        _run_git(source_dir, ["config", "user.name", "SGME Skills"], check=True)


def _commit_all(source_dir: Path, message: str) -> bool:
    """git add -A + commit；无暂存变更时跳过。返回是否产生了提交。"""
    _run_git(source_dir, ["add", "-A"], check=True)
    staged = _run_git(source_dir, ["diff", "--cached", "--quiet"]).returncode != 0
    if not staged:
        return False
    _run_git(source_dir, ["commit", "-m", f"skills: {message}"], check=True)
    return True


# ---------- 工作区 .gitignore（技能正文 + 资产目录白名单） ----------

# 允许作为技能资产的顶层目录（与 WORKSPACE_GITIGNORE 白名单一致）。
# 这三类正是 gates.py 承认的资产模型：references=引用资料、scripts=可执行脚本、
# assets=素材；其余目录/杂项一律不入库。
ASSET_DIRS: tuple[str, ...] = ("references", "scripts", "assets")

# 技能工作区 .gitignore 内容。
#
# 语义：`*` 忽略一切 → `!*/` 让目录可进入 → 再按白名单放行 SKILL.md 与三类资产目录。
#
# 为什么从「仅 SKILL.md 单文件」放开（2026-09-22 ST-45 / B195）：
# **收窄会冻结新增资产**——`.gitignore` 只影响「未跟踪」文件，存量 references/
# （全库 438 技能中 245 个引用、远端校验显示 244 个文件真实存在）因 git 已跟踪
# 故不受影响，于是形成「声明单文件、实际半多文件、新增进不来」的半冻结态。
# 白名单仍挡住杂项（__pycache__/tmp/*.log 等），保留防护意图。
WORKSPACE_GITIGNORE = (
    "*\n"
    "!*/\n"
    "!*/SKILL.md\n"
    "!*/references/**\n"
    "!*/scripts/**\n"
    "!*/assets/**\n"
)


def ensure_workspace_gitignore(source_dir: Path) -> bool:
    """确保 ``<source_dir>/.gitignore`` 为当前白名单版；内容变更时写回并提交。

    存量工作区持的是旧「单文件」版且**已 commit**，必须**幂等升级**——原实现
    （``skills_hub._ensure_worktree``）是 ``if not exists()``，只覆盖首次初始化，
    新白名单对既有部署永不生效。本函数是**升级的唯一触发点**，由写侧调用
    （``write_skill`` / ``write_skill_file``）。

    ⚠️ 刻意**不挂在 sync 路径**上：远端 hub 仓仍是旧版 .gitignore 时，sync 每次
    拉取都会把本地打回旧版、再升级又产生提交，``from_remote`` / ``to_remote`` 的
    「无变更 → noop」幂等契约双双失效（2026-09-22 实测 ``test_skills_hub_sync``
    两个用例挂）。升级随第一次写侧 PUT 落库、随后续 ``to_remote`` 推到远端，
    之后远端即新版，不再反复。

    Returns:
        True=本次发生了内容升级（旧版 → 白名单版）；False=已是目标内容。
    """
    path = Path(source_dir) / ".gitignore"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None

    upgraded = current != WORKSPACE_GITIGNORE
    if upgraded:
        path.write_text(WORKSPACE_GITIGNORE, encoding="utf-8")

    tracked = _run_git(source_dir, ["ls-files", "--error-unmatch", ".gitignore"]).returncode == 0
    # 初始化（未被跟踪）必须提交，否则 .gitignore 形同虚设；升级也要即时提交，
    # 免得这次 PUT 的资产被 .gitignore 变更混批或漏批。
    if upgraded or not tracked:
        _run_git(source_dir, ["add", "-f", ".gitignore"], check=True)
        note = ("技能工作区 .gitignore 升级为资产白名单（ST-45）" if upgraded
                else "初始化技能工作区 .gitignore")
        _run_git(source_dir, ["commit", "-m", f"chore: {note}"], check=True)
    return upgraded


# ---------- 记录收集 ----------


def _collect_records(source_dirs: list[str]) -> list[SkillRecord]:
    """汇总全部 source_dirs 的现有记录（查重/引用扫描的数据底座）。"""
    records: list[SkillRecord] = []
    for d in source_dirs or []:
        records.extend(collect_from_dir(d))
    return records


def _resolve_source_dir(name: str, source_dirs: list[str]) -> Path | None:
    """按名定位技能所在 source_dir（多目录时取首个命中；未命中返回 None）。"""
    for d in source_dirs or []:
        p = Path(d) / name / SKILL_FILE
        if p.is_file():
            return Path(d)
    return None


def _content_sha(content: str) -> str:
    """内容指纹（归一化口径与 indexer 一致：strip 后 SHA256）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_validate_uses(raw) -> list[str]:
    """uses 元素过名称校验，非法项跳过（仅用于变更判定，非法名由门禁拦截）。"""
    out = []
    for n in _to_list(raw):
        try:
            out.append(validate_name(n))
        except ValueError:
            out.append(str(n).strip())
    return out


def _build_record(name: str, meta: dict, body: str) -> SkillRecord:
    """从待写内容构造查重用记录。"""
    content = (body or "").strip()
    return SkillRecord(
        name=name,
        description=str(meta.get("description") or ""),
        sha256=_content_sha(content),
        content=content,
    )


def render_skill_md(meta: dict, body: str) -> str:
    """meta+body → SKILL.md 全文（YAML 围栏格式，键序稳定）。"""
    try:
        import io

        import yaml

        buf = io.StringIO()
        yaml.safe_dump(meta, buf, allow_unicode=True, sort_keys=False)
        fm = buf.getvalue().rstrip("\n")
    except Exception:
        # yaml 不可用时手写最简序列化（标量 / 字符串列表）
        lines = []
        for k, v in (meta or {}).items():
            if isinstance(v, list):
                lines.append(f"{k}:")
                lines.extend(f"  - {x}" for x in v)
            else:
                lines.append(f"{k}: {v}")
        fm = "\n".join(lines)
    return f"---\n{fm}\n---\n\n{(body or '').strip()}\n"


# ---------- 写入 ----------


def write_skill(
    name: str,
    meta: dict,
    body: str,
    source_dirs: list[str],
    query_vec=None,
    existing_vectors: dict[str, list[float]] | None = None,
    skip_limits: bool = False,
) -> dict:
    """写入技能（新建或覆盖更新）：lint → 查重 → 落盘 → commit。

    Args:
        name: 技能名（kebab-case）。
        meta: frontmatter dict。
        body: 正文（不含围栏）。
        source_dirs: 目标 git 工作区列表（写入第一个目录；其余参与查重）。
        query_vec / existing_vectors: 可选向量，透传 dedupe 第三层（宁缺勿误报）。
        skip_limits: 放宽大小类限制（PR-7「先整体入库」裁决）——超 8K 原子上限
            从拒绝降为警告放行；必填/pattern 枚举等语义违规仍拒绝。仅迁移批量
            入库场景使用，日常写入保持默认严格。

    Returns:
        成功 ``{"ok": True, "warnings": [...], "committed": bool, "path": str}``；
        拒绝 ``{"ok": False, "code": "lint_failed"|"duplicate", "violations": [...]}``。
    """
    warnings: list[str] = []
    with write_critical():
        # 1) 准入门禁（违规即拒绝，不落盘）；skip_limits 时大小超限降为警告
        target_dir = Path(source_dirs[0]) / name
        violations = lint_skill(meta, body, name, set(), skill_dir=target_dir)
        if skip_limits and violations:
            kept, relaxed = [], []
            for v in violations:
                if "原子超限" in v:
                    relaxed.append(v)
                else:
                    kept.append(v)
            warnings.extend(
                f"skip_limits 放行：{v}（历史存量整体入库，后续优化阶段拆分外置化）"
                for v in relaxed
            )
            violations = kept
        if violations:
            return _reject("lint_failed",
                           ["准入门禁拦截"] + violations)

        # 2) 全库现状（跨名查重排除自身；同名记录单独取用于重复提交判定）
        all_records = _collect_records(source_dirs)
        self_rec = next((r for r in all_records if r.name == name), None)
        records = [r for r in all_records if r.name != name]
        new_sha = _content_sha((body or "").strip())

        # 3) 三层查重：拒绝层（同名/同SHA）→ 拒绝；警告层 → 进 warnings 放行
        #    同名分支：body 与 frontmatter 全部无变化 = 无意义重复提交 → 拒；
        #    body 未变但元数据（category/tags/version/pattern/description/uses）有变
        #    = 合法元数据更新 → 放行（T-123 实锤：原实现只比 body sha，元数据
        #    变更被误判「无变更重复提交」409，写侧元数据更新路径全断）
        if self_rec is not None and self_rec.sha256 == new_sha:
            meta_same = (
                self_rec.description == str(meta.get("description") or "").strip()
                and self_rec.category == str(meta.get("category") or "").strip()
                and self_rec.version == str(meta.get("version") or "").strip()
                and self_rec.pattern == str(meta.get("pattern") or "").strip()
                and sorted(self_rec.tags or []) == sorted(_to_list(meta.get("tags")) or ["skill"])
                and sorted(self_rec.uses or []) == sorted(
                    _safe_validate_uses(meta.get("uses"))
                )
            )
            if meta_same:
                return _reject("duplicate",
                               [f"三层查重拒绝：同名冲突「{name}」已存在且内容完全相同"
                                f"（无变更的重复提交）"])
        verdict = check_duplicate(
            _build_record(name, meta, body), records,
            query_vec=query_vec, existing_vectors=existing_vectors,
        )
        if verdict == "reject_same_name":
            return _reject("duplicate",
                           [f"三层查重拒绝：同名冲突「{name}」已存在"])
        if verdict == "reject_same_sha":
            dup_names = sorted(r.name for r in records
                               if r.sha256 and r.sha256 == new_sha)

            return _reject(
                "duplicate",
                [f"三层查重拒绝：同内容异名（SHA256 与 {', '.join(dup_names)} 完全相同）"])
        if isinstance(verdict, tuple) and verdict[0] == "warn_similar":
            warnings.append(
                f"语义近亲警告：「{name}」与已有技能相似度 {verdict[1]:.2f} ≥ 阈值"
                f"（分层重叠合法，已放行，建议人工裁决是否合并）")

        # 4) 落盘 <source_dir>/<name>/SKILL.md + commit（临界区内）
        target = Path(source_dirs[0])
        skill_dir = target / validate_name(name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / SKILL_FILE).write_text(render_skill_md(meta, body), encoding="utf-8")
        _ensure_repo_identity(target)
        # ST-45：确保工作区 .gitignore 为资产白名单版（存量工作区幂等升级）
        ensure_workspace_gitignore(target)
        # ST-45：引用完整性提示（只提示不拦；判据与理由见 _ASSET_REF_RE 注释）
        warnings.extend(_dangling_asset_warnings(skill_dir, body))
        committed = _commit_all(target, f"write {name}")

    return {"ok": True, "warnings": warnings, "committed": committed,
            "path": str(skill_dir / SKILL_FILE)}


# ---------- 资产文件写入（ST-45：参考/脚本/素材随技能分发） ----------

# 正文里「资产路径」的粗匹配（references / scripts / assets 三类白名单目录）。
# ⚠️ 仅用于**提示**（warning），不可据此硬拦：全库 438 技能中有 245 个正文出现这类
# 字样，其中大量是占位与文档性提及（`xxx.md`、`0X-xxx.md`、`manual/`、`markdown/x.md`），
# 硬拦会大面积误伤（同 gates.py 规则 5 的「按目录实体判据避免误伤」）。真断链由远端
# hook 兜底拦截，本函数只让调用方**看得见**。
_ASSET_REF_RE = re.compile(r"(?:references|scripts|assets)/[A-Za-z0-9._\-/]+")


def _asset_usage(skill_dir: Path) -> tuple[int, int]:
    """统计技能目录下白名单资产的 ``(总字节, 文件数)``（供配额判定）。"""
    total = 0
    count = 0
    for d in ASSET_DIRS:
        base = skill_dir / d
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
                count += 1
    return total, count


def _dangling_asset_warnings(skill_dir: Path, body: str) -> list[str]:
    """正文引用的资产路径若不存在 → 返回提示清单（**只提示不拦**，ST-45）。"""
    if not body:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for rel in _ASSET_REF_RE.findall(body):
        if rel in seen:
            continue
        seen.add(rel)
        if not (skill_dir / rel).exists():
            out.append(
                f"引用完整性提示：正文出现 {rel}，但技能目录下无此文件——"
                f"若确为引用，请用 PUT /v1/admin/skills/{skill_dir.name}/files/{rel} 补上；"
                "若只是文档性提及可忽略")
    return out


def _validate_asset_relpath(relpath: str) -> str:
    """校验并归一资产相对路径；非法抛 ``ValueError``。

    规则（从严，防路径穿越）：
    - 必须是相对路径（禁绝对路径与盘符；反斜杠归一为正斜杠）
    - 禁 ``..`` 与空段
    - 首段必须在 :data:`ASSET_DIRS` 白名单内（references / scripts / assets）

    Returns:
        归一后的相对路径（如 ``references/notes.md``）。
    """
    raw = str(relpath or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("资产路径不能为空")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"资产路径必须是相对路径: {relpath!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"资产路径非法: {relpath!r}")
    if ".." in parts:
        raise ValueError(f"资产路径不得包含 ..（防路径穿越）: {relpath!r}")
    if parts[0] not in ASSET_DIRS:
        raise ValueError(
            f"资产目录不在白名单（仅允许 {'、'.join(ASSET_DIRS)}）: {parts[0]!r}")
    return "/".join(parts)


def write_skill_file(name: str, relpath: str, content: str, source_dirs: list[str],
                     max_file_bytes: int = 0, max_total_bytes: int = 0,
                     max_files: int = 0) -> dict:
    """写入技能资产文件（ST-45）：路径白名单 → 配额 → 落盘 → commit。

    与 :func:`write_skill` 同返回口径（ok / code / violations），供
    ``PUT /v1/admin/skills/{name}/files/{relpath}`` 调用。

    Args:
        name: 技能名；目标技能必须**已存在**（先 PUT SKILL.md）。
        relpath: 相对技能目录的路径，如 ``references/notes.md``。
        content: 文件全文（UTF-8 文本）。
        source_dirs: 技能源目录列表（取首个命中该技能者）。
        max_file_bytes: 单文件字节上限；``<=0`` 表示不限制。
        max_total_bytes: 该技能白名单资产**合计**字节上限；``<=0`` 表示不限制。
        max_files: 该技能白名单资产文件数上限；``<=0`` 表示不限制。

    Returns:
        成功 ``{"ok": True, "path", "bytes", "committed"}``；
        失败 ``{"ok": False, "code", "violations"}``，code ∈
        ``invalid_name`` / ``invalid_path`` / ``not_found`` / ``quota_exceeded``。
    """
    try:
        safe_name = validate_name(name)
    except ValueError as e:
        return _reject("invalid_name", [f"非法技能名: {e}"])

    try:
        rel = _validate_asset_relpath(relpath)
    except ValueError as e:
        return _reject("invalid_path", [str(e)])

    target = _resolve_source_dir(safe_name, source_dirs)
    if target is None:
        return _reject("not_found", [f"技能不存在，请先写入 SKILL.md: {safe_name}"])

    text = content if isinstance(content, str) else ""
    nbytes = len(text.encode("utf-8"))
    if max_file_bytes and nbytes > max_file_bytes:
        return _reject(
            "quota_exceeded",
            [f"单资产超限：{nbytes} 字节 > {max_file_bytes} 字节"
             "（配置项 skills.asset_quota.max_file_bytes）"])

    dest = target / safe_name / rel
    if max_total_bytes or max_files:
        # 覆盖已存在文件时，旧大小要扣掉，否则重复写入会自我膨胀
        prev_bytes = dest.stat().st_size if dest.is_file() else 0
        used_bytes, used_count = _asset_usage(target / safe_name)
        new_bytes = used_bytes - prev_bytes + nbytes
        new_count = used_count + (0 if dest.is_file() else 1)
        if max_total_bytes and new_bytes > max_total_bytes:
            return _reject(
                "quota_exceeded",
                [f"资产总量超限：{new_bytes} 字节 > {max_total_bytes} 字节"
                 "（配置项 skills.asset_quota.max_total_bytes）"])
        if max_files and new_count > max_files:
            return _reject(
                "quota_exceeded",
                [f"资产文件数超限：{new_count} > {max_files}"
                 "（配置项 skills.asset_quota.max_files）"])

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")

    _ensure_repo_identity(target)
    ensure_workspace_gitignore(target)
    committed = _commit_all(target, f"asset {safe_name}/{rel}")
    return {"ok": True, "path": str(dest), "bytes": nbytes, "committed": committed}


# ---------- 删除 ----------


def remove_skill(name: str, hard: bool = False, force: bool = False,
                 source_dirs: list[str] | None = None) -> dict:
    """删除技能：先扫入向引用，再软删（默认）/硬删（hard=True）。

    入向引用两级信号（设计 v0.2.1）：
    - 一级 = 其他技能 frontmatter uses 含该名：有引用且未 force → 拒绝并列清单
    - 二级 = 其他技能正文提及名字：只列 warnings 清单不拦（防同名巧合误伤）

    Returns:
        成功 ``{"ok": True, "mode": "soft"|"hard", "warnings": [...], "committed": bool}``；
        拒绝 ``{"ok": False, "code": "referenced"|"not_found", ...}``。
    """
    try:
        name = validate_name(name)
    except ValueError as e:
        return _reject("not_found", [f"非法技能名: {e}"])
    with write_critical():
        records = _collect_records(source_dirs or [])
        if not any(r.name == name for r in records):
            return _reject("not_found", [f"技能不存在: {name}"])

        # 一级信号：其他技能 frontmatter uses 显式声明依赖（机械可拦）
        referenced_by = sorted(
            r.name for r in records if r.name != name and name in (r.uses or [])
        )
        if referenced_by and not force:
            rej = _reject("referenced",
                          [f"删除被拒：{len(referenced_by)} 个技能的 uses 声明依赖「{name}」，"
                           f"请先解除引用或使用 force 强制删除"])
            rej["referenced_by"] = referenced_by
            return rej

        # 二级信号：其他技能正文自然语言提及（只列清单不拦）
        mention_warnings = [
            f"正文提及「{name}」: {r.name}"
            for r in records
            if r.name != name and name in (r.content or "") and r.name not in referenced_by
        ]

        src = _resolve_source_dir(name, source_dirs or [])
        if src is None:
            return _reject("not_found", [f"技能不存在: {name}"])

        if hard:
            # 硬删：物理删目录 + commit（git 历史永存兜底）
            shutil.rmtree(src / name, ignore_errors=True)
            committed = _commit_all(src, f"remove(hard) {name}")
            mode = "hard"
        else:
            # 软删：frontmatter 加 deprecated: true 后 commit（宽限期可恢复）
            parsed = parse_skill_md((src / name / SKILL_FILE).read_text(encoding="utf-8"))
            meta = dict(parsed["meta"])
            meta["deprecated"] = True
            (src / name / SKILL_FILE).write_text(render_skill_md(meta, parsed["body"]),
                                                 encoding="utf-8")
            committed = _commit_all(src, f"remove(soft) {name}")
            mode = "soft"

    return {"ok": True, "mode": mode, "warnings": mention_warnings, "committed": committed}


# ---------- 改名（墓碑制） ----------


def rename_skill(old: str, new: str, source_dirs: list[str],
                 registry_path: str | Path | None = None) -> dict:
    """改名：永不原地改名——写新名完整副本 + 旧位置留墓碑指向新名 + commit。

    墓碑 SKILL.md 只含 frontmatter ``superseded_by: <new>``；
    墓碑登记追加进 tombstones.json（原子写，供读侧别名解析与对账）。

    Returns:
        成功 ``{"ok": True, "old", "new", "warnings", "committed"}``；
        拒绝 ``{"ok": False, "code": "not_found"|"conflict"|"lint_failed", ...}``。
    """
    try:
        old = validate_name(old)
        new = validate_name(new)
    except ValueError as e:
        return _reject("not_found", [f"非法技能名: {e}"])
    if old == new:
        return _reject("conflict", ["改名目标与原名相同（原地改名被禁止）"])
    warnings: list[str] = []
    with write_critical():
        records = _collect_records(source_dirs or [])
        if not any(r.name == old for r in records):
            return _reject("not_found", [f"旧名不存在: {old}"])
        if any(r.name == new for r in records):
            return _reject("conflict", [f"新名已被占用: {new}"])

        # 新名内容过门禁（沿用原 frontmatter；名称规则按 new 校验）
        raw = _read_raw(old, source_dirs)
        parsed = parse_skill_md(raw)
        meta = dict(parsed["meta"])
        violations = lint_skill(meta, parsed["body"], new, {r.name for r in records})
        if violations:
            rej = _reject("lint_failed", ["新名未通过准入门禁"] + violations)
            return rej

        src = _resolve_source_dir(old, source_dirs)
        assert src is not None  # 记录存在 ⇒ 目录必在

        # ① 写新名副本（完整内容）；② 旧位置留墓碑（只含 superseded_by 最小 frontmatter）
        new_dir = src / new
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / SKILL_FILE).write_text(render_skill_md(meta, parsed["body"]), encoding="utf-8")
        (src / old / SKILL_FILE).write_text(render_skill_md({"superseded_by": new}, ""),
                                            encoding="utf-8")
        # ③ 登记 + 提交（同一临界区内完成）
        append_tombstone({"old": old, "new": new}, path=registry_path)
        committed = _commit_all(src, f"rename {old} -> {new}")

    warnings.append(f"墓碑已登记: {old} -> {new}（读侧按 superseded_by 解析别名）")
    return {"ok": True, "old": old, "new": new, "warnings": warnings, "committed": committed}


def _read_raw(name: str, source_dirs: list[str]) -> str:
    src = _resolve_source_dir(name, source_dirs or [])
    if src is None:
        raise StoreError(f"技能不存在: {name}")
    return (src / name / SKILL_FILE).read_text(encoding="utf-8")


# ---------- 墓碑登记（原子写） ----------


def _default_registry_path() -> Path:
    return Path(os.environ.get("SGME_HOME", ".")) / DEFAULT_TOMBSTONE_REGISTRY


def load_tombstones(path: str | Path | None = None) -> list[dict]:
    """读墓碑登记；文件缺失/损坏返回空列表（自愈）。"""
    p = Path(path) if path else _default_registry_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def append_tombstone(entry: dict, path: str | Path | None = None) -> Path:
    """追加一条墓碑登记（原子写：tmp + os.replace，镜像 vectors.save_cache 惯例）。"""
    p = Path(path) if path else _default_registry_path()
    with _registry_lock:
        items = load_tombstones(p)
        items.append(entry)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    return p
