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
        committed = _commit_all(target, f"write {name}")

    return {"ok": True, "warnings": warnings, "committed": committed,
            "path": str(skill_dir / SKILL_FILE)}


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
