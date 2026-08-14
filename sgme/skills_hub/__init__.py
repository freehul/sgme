"""技能仓库模块（v0.7 §11 / 0.8 ST-11，扩展模块，`skills_hub.enabled` 控制）。

用户自有的技能仓库，独立于 Hermes 的 %LOCALAPPDATA%/hermes/skills/。
每个技能 = 仓库根目录下一个子目录，内含 SKILL.md（约定同 Hermes skills）。

部署模式（§11.2）：
- map：直接读写 path 目录（软链接/目录映射，零拷贝）
- copy：cache 为本地工作区（实际读写位置），remote.source 为远端权威仓
  （bare）；真实网络同步（0.8 ST-11 已实现）：git subprocess 双方向同步，
  LWW 冲突策略（local_wins/remote_wins + 备份 ref + 冲突报告）

用法::

    hub = init({"skills_hub": {"enabled": True, "path": "./skills-hub/"}})
    hub.put_skill("my-skill", "# 我的技能\\n...")
    hub.list_skills()
    hub.get_skill("my-skill")
    # copy 模式（0.8 ST-11）：
    hub.sync_from_remote()   # NAS 权威仓 → cache 工作区
    hub.sync_to_remote()     # cache 工作区 → NAS 权威仓

同步设计详见 docs/design/SGME-SkillsHub同步设计-v0.1.md。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from sgme.skills_hub.config import (
    DEFAULT_MODE,
    SkillsHubConfig,
    parse_skills_hub_config,
)

__all__ = ["init", "SkillsHub", "SkillsHubConfig", "parse_skills_hub_config", "GitSyncError"]

# 技能清单文件（每个技能目录内的主文档，约定同 Hermes skills）
SKILL_FILE = "SKILL.md"

# 技能名白名单：字母/数字/下划线/中划线/点；显式排除路径分隔符与空名
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# 禁用态操作报错（init() 已返回 None 时正常不可达，防御直接构造场景）
_DISABLED_MSG = "技能仓库已禁用（skills_hub.enabled=false），操作被拒绝"

# ---------- copy 模式同步常量（0.8 ST-11） ----------

# git 远端名（与项目既有 NAS 备份链路同构）
_GIT_REMOTE = "origin"
# 冲突备份 ref 前缀（败方提交落地为本地 ref，数据永不丢）
_CONFLICT_PREFIX = "conflict-backup-"
# 工作区 .gitignore：只镜像 <name>/SKILL.md 单文件（§3.2）
_GITIGNORE_CONTENT = "*\n!*/\n!*/SKILL.md\n"
# 同步提交/备份时间戳格式（YYYYmmddHHMMSS）
_TS_FORMAT = "%Y%m%d%H%M%S"

# remote.source 允许的三种形态（§6.2）：
#   ssh://user@host/path | user@host:path（scp 式）| file:///abs/path
_RE_SSH_URL = re.compile(r"^ssh://[^\s/][^\s]*$")
_RE_SCP_URL = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s]+$")
_RE_FILE_URL = re.compile(r"^file://[^\s]+$")


class GitSyncError(Exception):
    """git 同步失败（网络/超时/git 非零退出），供 API 层翻译为 500。

    Attributes:
        message: 人可读错误摘要。
        command: 失败的 git 命令（参数数组，无 shell）。
        stderr: git 原始 stderr（截断收录，防刷屏）。
        exit_code: git 进程退出码（超时场景为 None）。
    """

    def __init__(
        self,
        message: str,
        command: list[str] | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.command = command
        self.stderr = stderr or ""
        self.exit_code = exit_code

    @property
    def stderr_summary(self) -> str:
        """stderr 截断摘要（最多 2000 字符，供错误响应携带）。"""
        return self.stderr[-2000:]


def _now_ts() -> str:
    """当前时间戳（YYYYmmddHHMMSS，用于备份 ref / 冲突报告 / 提交消息）。"""
    return datetime.now().strftime(_TS_FORMAT)


def _validate_name(name: str) -> str:
    """校验技能名安全性：非空、无路径穿越、无路径分隔符。

    防护点：
    - 空名 / 纯空白名直接拒绝
    - 含 "/" 或 "\\\\"（路径分隔符）拒绝
    - 含 ".."（含 "../x"、".\\\\x"、"a..b" 等变体）一律拒绝，防路径穿越
    - 剩余字符必须落在白名单 [A-Za-z0-9_.-] 内

    Raises:
        ValueError: 名字非法。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("技能名不能为空")
    name = name.strip()
    if name in (".", ".."):
        raise ValueError(f"非法技能名: {name!r}")
    if "/" in name or "\\\\" in name or ".." in name:
        raise ValueError(f"技能名含路径穿越/分隔符，已拒绝: {name!r}")
    if not _NAME_RE.match(name):
        raise ValueError(f"技能名含非法字符（仅允许字母/数字/下划线/中划线/点）: {name!r}")
    return name


def _validate_source(source: str) -> str:
    """校验 remote.source 形态（§6.2 安全防线）。

    只允许三种形态，其余一律拒绝（解析失败即拒绝）：
    - ``ssh://user@host/path``
    - ``user@host:path``（scp 式）
    - ``file:///abs/path``（本地测试/演练用）

    拒绝一切含空白/选项前缀（``-c``/``--upload-pack`` 等）的地址；
    配合「URL 整体作为单个参数传 git、禁 shell=True」，git 选项注入不可能。

    Raises:
        ValueError: 形态非法。
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("skills_hub.remote.source 为空：copy 模式同步需要远端权威仓地址")
    s = source.strip()
    if s.startswith("-") or any(ch.isspace() for ch in s):
        raise ValueError(
            f"skills_hub.remote.source 含非法字符（空白/选项前缀，防 git 选项注入）: {s!r}"
        )
    if _RE_SSH_URL.match(s) or _RE_SCP_URL.match(s) or _RE_FILE_URL.match(s):
        return s
    raise ValueError(
        f"skills_hub.remote.source 仅允许 ssh://、user@host:path、file:// 三种形态: {s!r}"
    )


def _validate_branch(branch: str) -> str:
    """校验同步分支名（§7：非空、无空白、无 ``..``、无选项前缀）。"""
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("skills_hub.remote.branch 不能为空")
    b = branch.strip()
    if any(ch.isspace() for ch in b) or ".." in b or b.startswith("-"):
        raise ValueError(f"非法同步分支名: {b!r}（含空白/.. /选项前缀）")
    return b


def _skill_dir(root: Path, name: str) -> Path:
    """技能目录 = root/<name>（name 已过 _validate_name 校验）。"""
    return root / _validate_name(name)


def _snapshot_skills(root: Path) -> dict[str, str]:
    """cache 工作区技能快照：技能名 → SKILL.md 内容 sha256。

    只统计通过 _validate_name 的目录（非法名目录不进技能视图，§6.2）。
    """
    snap: dict[str, str] = {}
    if not root.is_dir():
        return snap
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name == ".git":
            continue
        try:
            name = _validate_name(p.name)
        except ValueError:
            continue
        f = p / SKILL_FILE
        if f.is_file():
            snap[name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return snap


def _diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """两次快照的技能级差异（新增/修改/删除，按名排序）。"""
    return {
        "added": sorted(set(after) - set(before)),
        "modified": sorted(n for n in set(before) & set(after) if before[n] != after[n]),
        "deleted": sorted(set(before) - set(after)),
    }


class SkillsHub:
    """技能仓库实例（map/copy 双模式）。

    - map 模式：root = 配置 path，直接读写（零拷贝）
    - copy 模式：root = 配置 remote_cache（本地工作区）；remote_source 仅记录，
      同步接口以 NotImplementedError 标注（真实网络同步后续实现）

    所有技能操作（put/get/list/remove）统一走 root 下的 <name>/SKILL.md。
    """

    def __init__(self, config: SkillsHubConfig) -> None:
        self.config = config
        self.enabled = config.enabled
        self.mode = config.mode
        # 工作区根目录：map 用 path，copy 用 remote_cache
        root = config.path if config.mode == DEFAULT_MODE else config.remote_cache
        if not root:
            if config.enabled:
                raise ValueError(
                    f"skills_hub 启用但缺少工作区目录"
                    f"（mode={config.mode!r}，需要 path 或 remote.cache）"
                )
            # 禁用态无需工作区：root 兜底到当前目录，操作由 _require_enabled 拦截
            root = "."
        self.root = Path(root).resolve()

    # ---------- 技能操作 ----------

    def list_skills(self) -> list[str]:
        """列出仓库内全部技能名（含 SKILL.md 的子目录，按名排序）。

        Returns:
            技能名列表；仓库目录不存在时返回空列表。
        """
        self._require_enabled()
        if not self.root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and (p / SKILL_FILE).is_file()
        )

    def get_skill(self, name: str) -> str | None:
        """读取技能内容（SKILL.md 全文）。

        Args:
            name: 技能名（安全校验：禁路径穿越/分隔符/空名）。

        Returns:
            SKILL.md 文本；技能不存在时返回 None。

        Raises:
            ValueError: 技能名非法。
        """
        self._require_enabled()
        skill_file = _skill_dir(self.root, name) / SKILL_FILE
        if not skill_file.is_file():
            return None
        return skill_file.read_text(encoding="utf-8")

    def put_skill(self, name: str, content: str) -> Path:
        """写入技能：创建 <root>/<name>/SKILL.md（父目录自动创建，覆盖写）。

        Args:
            name: 技能名（安全校验：禁路径穿越/分隔符/空名）。
            content: SKILL.md 全文。

        Returns:
            写入后的 SKILL.md 路径。

        Raises:
            ValueError: 技能名非法。
        """
        self._require_enabled()
        skill_dir = _skill_dir(self.root, name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / SKILL_FILE
        skill_file.write_text(content, encoding="utf-8")
        return skill_file

    def remove_skill(self, name: str) -> bool:
        """删除技能（整个 <name>/ 目录，含 SKILL.md）。

        Args:
            name: 技能名（安全校验：禁路径穿越/分隔符/空名）。

        Returns:
            True=已删除；False=技能不存在（幂等）。

        Raises:
            ValueError: 技能名非法。
        """
        self._require_enabled()
        skill_dir = _skill_dir(self.root, name)
        if not skill_dir.is_dir():
            return False
        # 仅允许删除仓库内技能目录，杜绝误删仓库根
        if skill_dir == self.root or self.root not in skill_dir.parents:
            raise ValueError(f"拒绝删除仓库根目录外的路径: {skill_dir}")
        for child in skill_dir.iterdir():
            if child.is_dir():
                # 技能目录内仅允许 SKILL.md 单文件；防御残留子目录
                raise ValueError(f"技能目录含未预期子目录，拒绝删除: {child}")
            child.unlink()
        skill_dir.rmdir()
        return True

    # ---------- copy 模式同步（0.8 ST-11：git 双方向真实同步） ----------

    def sync_from_remote(self) -> dict:
        """从远端权威仓（bare）全量镜像到本地 cache 工作区（copy 模式）。

        语义（§3.1/§3.3）：远端全量镜像到本地；首次 = 全量，后续 = fast-forward
        增量；分叉时按 remote.conflict_policy（§4）：
        - local_wins（默认）：本地领先 → 中止报错（不自动 reset 本地提交），
          提示先 sync_to_remote 或改 remote_wins
        - remote_wins：先备份本地为 conflict-backup-<ts>（未提交变更入 stash），
          reset --hard 到远端，生成冲突报告
        远端条目名非法（不过白名单）→ 跳过 + warning + 清理出工作区（不进技能
        列表、不参与后续 push）。远端不可达/超时 → GitSyncError（本地编辑不受影响）。

        Returns:
            同步结果 dict（direction/status/added/modified/deleted/conflict/
            warnings/duration_ms/branch）。

        Raises:
            ValueError: map 模式 / remote.source 为空或形态非法 / 分支名非法。
            GitSyncError: git 失败（远端不可达、超时、merge 失败等）。
        """
        self._require_enabled()
        if self.mode != "copy":
            raise ValueError("map 模式无远端语义，同步仅 copy 模式可用")
        source = _validate_source(self.config.remote_source)
        branch = _validate_branch(self.config.remote_branch)
        t0 = time.perf_counter()
        self._ensure_git()
        self._ensure_worktree(source)
        before = _snapshot_skills(self.root)
        warnings: list[str] = []
        conflict: dict | None = None

        # 1) 远端分支探测：退出码 0=分支存在 2=分支不存在（空仓）其他=远端不可达
        ls = self._run_git(["ls-remote", "--exit-code", _GIT_REMOTE, branch])
        if ls.returncode == 2:
            # 远端分支不存在 → 无可镜像内容（no-op 成功，不报错）
            return self._result("from_remote", before, before, warnings, conflict, t0, "noop")
        if ls.returncode != 0:
            raise GitSyncError(
                f"远端不可达（ls-remote 失败，退出码 {ls.returncode}）: {source}",
                command=["git", "ls-remote", "--exit-code", _GIT_REMOTE, branch],
                stderr=ls.stderr,
                exit_code=ls.returncode,
            )

        # 2) 拉取远端分支（走命名远端 origin，增量传输由 git 承担）
        self._run_git(["fetch", _GIT_REMOTE, branch], check=True)
        if not self._git_ok(["rev-parse", "--verify", "-q", f"{_GIT_REMOTE}/{branch}"]):
            # 远端分支存在但无提交（异常边缘）：no-op
            return self._result("from_remote", before, before, warnings, conflict, t0, "noop")

        # 3) 镜像到本地：首次（无本地分支）→ checkout -B 全量；后续 → ff-only 增量
        if not self._git_ok(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"]):
            self._run_git(["checkout", "-B", branch, f"{_GIT_REMOTE}/{branch}"], check=True)
        else:
            if self._current_branch() != branch:
                self._run_git(["checkout", branch], check=True)
            merge = self._run_git(["merge", "--ff-only", f"{_GIT_REMOTE}/{branch}"])
            if merge.returncode != 0:
                # 本地无任何技能内容（仅 .gitignore 初始化 commit，如新设备首次
                # 接入/清空 cache 后重建）→ 安全全量镜像，不视为分叉（2026-08-10
                # 真实环境实测：cache 清空后 from_remote 报"本地领先"——根因是
                # 初始化 commit 与远端不相交，但本地零技能资产，直接重置无损失）
                if not before:
                    self._run_git(["checkout", "-B", branch, f"{_GIT_REMOTE}/{branch}"], check=True)
                # 分叉（本地领先/双方分叉）：按冲突策略处理（§4.3）
                elif self.config.remote_conflict_policy == "remote_wins":
                    backup_ref = None
                    stash_hint = None
                    if self.config.remote_backup_refs:
                        backup_ref = self._backup_ref("HEAD")
                        stash_hint = self._stash_uncommitted()
                    self._run_git(["reset", "--hard", f"{_GIT_REMOTE}/{branch}"], check=True)
                    conflict = self._make_conflict("from_remote", "remote_wins", backup_ref, stash_hint)
                else:
                    raise GitSyncError(
                        "本地领先远端（存在未推送提交），sync_from_remote 中止："
                        "请先 sync_to_remote 推送本地变更，"
                        "或将 remote.conflict_policy 设为 remote_wins 让远端覆盖本地"
                    )

        # 4) 远端条目名安全校验：非法目录名跳过 + warning + 清理（§6.2）
        cleaned = self._cleanup_invalid_entries(warnings)
        after = _snapshot_skills(self.root)
        status = "conflict_resolved" if conflict else ("ok" if (before != after or cleaned) else "noop")
        return self._result("from_remote", before, after, warnings, conflict, t0, status)

    def sync_to_remote(self) -> dict:
        """把 cache 工作区全部变更提交并推送到远端权威仓（copy 模式）。

        语义（§3.1/§3.3）：本地全部变更（新增/修改/删除）一次性提交推送；
        无本地变更 → 跳过 commit、push up-to-date → no-op 成功。push 被拒
        （远端领先/分叉）→ 按 remote.conflict_policy（§4.2）：
        - local_wins（默认）：备份远端状态为 conflict-backup-<ts> →
          --force-with-lease 覆盖远端（lease 校验失败即中止，绝不静默覆盖
          第三方提交）→ 冲突报告
        - remote_wins：备份本地提交 → reset --hard 到远端 → 冲突报告
        本地无任何提交（未先 sync_from_remote）→ 拒绝推送覆盖远端。

        Returns:
            同步结果 dict（同 sync_from_remote，added/modified/deleted 取自
            本次提交的技能级变更）。

        Raises:
            ValueError: map 模式 / remote.source 为空或形态非法 / 分支名非法。
            GitSyncError: git 失败（远端不可达、超时、push 被拒且非分叉等）。
        """
        self._require_enabled()
        if self.mode != "copy":
            raise ValueError("map 模式无远端语义，同步仅 copy 模式可用")
        source = _validate_source(self.config.remote_source)
        branch = _validate_branch(self.config.remote_branch)
        t0 = time.perf_counter()
        self._ensure_git()
        self._ensure_worktree(source)
        warnings: list[str] = []
        conflict: dict | None = None

        # 1) 暂存全部变更（.gitignore 保证只可能暂存 <name>/SKILL.md，§3.2）
        self._run_git(["add", "-A"], check=True)
        staged = not self._git_ok(["diff", "--cached", "--quiet"])
        committed = False
        if staged:
            self._run_git(["commit", "-m", f"sync: {_now_ts()}"], check=True)
            committed = True

        # 2) 推送（走命名远端 origin；up-to-date 视为成功 no-op；被拒按冲突策略处理）
        push = self._run_git(["push", _GIT_REMOTE, branch])
        if push.returncode == 0:
            stats = self._commit_stats() if committed else _diff_snapshots({}, {})
            status = "ok" if committed else "noop"
            return self._result("to_remote", {}, {}, warnings, conflict, t0, status, stats=stats)

        # 3) 推送失败：fetch 判定是否分叉（远端领先/双方分叉 → 冲突路径）
        fetch = self._run_git(["fetch", _GIT_REMOTE, branch])
        if fetch.returncode != 0:
            raise GitSyncError(
                f"推送失败且拉取失败（远端不可达?）: {source}",
                command=["git", "push", _GIT_REMOTE, branch],
                stderr=f"{push.stderr}\n{fetch.stderr}",
                exit_code=push.returncode,
            )
        if not self._git_ok(["rev-parse", "--verify", "-q", "HEAD"]):
            # 本地无任何提交：拒绝覆盖远端（防空分支 force 覆盖远端内容）
            raise GitSyncError(
                "本地 cache 工作区无任何提交（未先 sync_from_remote 或无技能内容），"
                "拒绝推送覆盖远端；请先执行 sync_from_remote"
            )
        if self._git_ok(["merge-base", "--is-ancestor", f"{_GIT_REMOTE}/{branch}", "HEAD"]):
            # 远端未领先本地（本地包含远端）→ 非分叉失败（鉴权/网络）→ 原样报错
            raise GitSyncError(
                f"push 失败（非分叉原因，退出码 {push.returncode}）: {source}",
                command=["git", "push", _GIT_REMOTE, branch],
                stderr=push.stderr,
                exit_code=push.returncode,
            )

        # 4) 分叉 → LWW 冲突解决（§4.2：数据永不丢 + force-with-lease）
        if self.config.remote_conflict_policy == "local_wins":
            backup_ref = None
            if self.config.remote_backup_refs:
                backup_ref = self._backup_ref(f"{_GIT_REMOTE}/{branch}")
            force = self._run_git(["push", "--force-with-lease", _GIT_REMOTE, branch])
            if force.returncode != 0:
                raise GitSyncError(
                    "force-with-lease 推送失败（远端在拉取后被第三方再次推进，"
                    "lease 校验失败，本次同步中止）",
                    command=["git", "push", "--force-with-lease", source, branch],
                    stderr=force.stderr,
                    exit_code=force.returncode,
                )
            conflict = self._make_conflict("to_remote", "local_wins", backup_ref, None)
            stats = self._commit_stats() if committed else _diff_snapshots({}, {})
            return self._result("to_remote", {}, {}, warnings, conflict, t0, "conflict_resolved", stats=stats)
        else:
            backup_ref = None
            stash_hint = None
            if self.config.remote_backup_refs:
                backup_ref = self._backup_ref("HEAD")
                stash_hint = self._stash_uncommitted()
            self._run_git(["reset", "--hard", f"{_GIT_REMOTE}/{branch}"], check=True)
            conflict = self._make_conflict("to_remote", "remote_wins", backup_ref, stash_hint)
            stats = _diff_snapshots({}, {})
            return self._result("to_remote", {}, {}, warnings, conflict, t0, "conflict_resolved", stats=stats)

    # ---------- 内部：git 子进程 / 工作区 ----------

    def _ensure_git(self) -> None:
        """探测系统 git（§2.2 风险对策：PATH 缺失 → 明确报配置错误）。"""
        try:
            proc = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.remote_timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise GitSyncError(
                f"git --version 探测超时（>{self.config.remote_timeout_s}s）"
            ) from None
        if proc.returncode != 0:
            raise GitSyncError(
                "系统 git 不可用（copy 模式同步依赖 git，请安装 git 并加入 PATH）",
                command=["git", "--version"],
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )

    def _run_git(self, args: list[str], check: bool = False) -> subprocess.CompletedProcess:
        """执行 git 子进程（参数数组、禁 shell=True；超时 kill；check 失败转异常）。

        git URL/分支名等一律作为整体参数传入，远端字符串内嵌 ``-c``/
        ``--upload-pack`` 等不会被解析为 git 选项（§2.2 注入防护）。
        """
        cmd = ["git", *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.remote_timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise GitSyncError(
                f"git 操作超时（>{self.config.remote_timeout_s}s）: {' '.join(cmd[:3])}…",
                command=cmd,
                stderr=str(e),
            ) from e
        if check and proc.returncode != 0:
            raise GitSyncError(
                f"git 命令失败（退出码 {proc.returncode}）: {' '.join(cmd[:3])}…",
                command=cmd,
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )
        return proc

    def _git_ok(self, args: list[str]) -> bool:
        """执行 git 并以退出码判定成功（0=True），失败不抛异常。"""
        return self._run_git(args).returncode == 0

    def _current_branch(self) -> str:
        """当前分支名（未出生 HEAD 返回空串）。"""
        r = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 else ""

    def _ensure_worktree(self, source: str) -> None:
        """初始化本地 git 工作树：init + 提交身份 + .gitignore + origin 远端。"""
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._run_git(["init"], check=True)
        # 分支对齐：git init 默认建 master，而同步分支由 remote.branch 配置
        # （默认 main）——不切换则首次 push <branch> 报 "src refspec ... does not
        # match any"（空仓首推路径，2026-08-10 真实环境实测）。init 后无提交，
        # 直接 symbolic-ref 切 HEAD 安全；已有提交且分支不同时重命名。
        branch = _validate_branch(self.config.remote_branch)
        if not self._git_ok(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"]):
            if self._git_ok(["rev-parse", "--verify", "-q", "HEAD"]):
                # 已有提交（.gitignore 初始化 commit 落在 master）→ 重命名当前分支
                self._run_git(["branch", "-M", branch], check=True)
            else:
                # 无提交：直接移动 HEAD 符号引用
                self._run_git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
        # 提交身份兜底：仓库级缺失时补默认（必须先于首次 commit，不影响全局配置）
        if not self._git_ok(["config", "user.email"]):
            self._run_git(["config", "user.email", "sgme-sync@local"], check=True)
        if not self._git_ok(["config", "user.name"]):
            self._run_git(["config", "user.name", "SGME Sync"], check=True)
        # .gitignore：只镜像 <name>/SKILL.md 单文件（§3.2，首次写入并提交）
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
        if not self._git_ok(["ls-files", "--error-unmatch", ".gitignore"]):
            self._run_git(["add", "-f", ".gitignore"], check=True)
            self._run_git(["commit", "-m", "chore: 初始化 skills-hub 工作区（.gitignore）"], check=True)
        # origin 远端：存在则 set-url（source 变更时更新），否则 add
        if self._git_ok(["remote", "get-url", _GIT_REMOTE]):
            self._run_git(["remote", "set-url", _GIT_REMOTE, source], check=True)
        else:
            self._run_git(["remote", "add", _GIT_REMOTE, source], check=True)

    # ---------- 内部：冲突处理 / 报告 ----------

    def _backup_ref(self, target: str) -> str:
        """创建败方备份 ref ``conflict-backup-<ts>`` 指向 target（数据永不丢）。

        Args:
            target: 被备份的 ref（HEAD = 本地状态；origin/<branch> = 远端状态）。
        """
        base = f"{_CONFLICT_PREFIX}{_now_ts()}"
        ref = base
        i = 1
        while self._git_ok(["show-ref", "--verify", "--quiet", f"refs/heads/{ref}"]):
            ref = f"{base}-{i}"
            i += 1
        self._run_git(["branch", ref, target], check=True)
        return ref

    def _stash_uncommitted(self) -> str | None:
        """remote_wins 覆盖前把未提交变更（含未跟踪）入 stash，防数据丢失。

        Returns:
            stash 引用名（如 stash@{0}）；工作区干净时返回 None。
        """
        status = self._run_git(["status", "--porcelain", "--untracked-files=normal"])
        if not status.stdout.strip():
            return None
        ts = _now_ts()
        self._run_git(["stash", "push", "-u", "-m", f"pre-remote-wins-{ts}"], check=True)
        return "stash@{0}"

    def _cleanup_invalid_entries(self, warnings: list[str]) -> bool:
        """远端拉取落地后的非法条目清理（§6.2）：跳过 + warning + 移出工作区。

        非法名目录（不过 _validate_name）不进技能列表、不参与后续 push：
        从 git 索引移除（历史/远端数据保留，可 git show 找回）+ 删除工作区目录；
        产生索引变更时提交一次清理 commit。

        Returns:
            True=本次清理过条目（有 warning 记入）。
        """
        removed = False
        for p in sorted(self.root.iterdir()):
            if not p.is_dir() or p.name == ".git":
                continue
            try:
                _validate_name(p.name)
            except ValueError as e:
                warnings.append(f"跳过非法技能条目 {p.name!r}（不进技能列表/不推送）: {e}")
                # :(literal) 前缀防路径规格通配（名内可能含 * ? [ 等）
                self._run_git(
                    ["rm", "-r", "--cached", "--ignore-unmatch", "--", f":(literal){p.name}"],
                    check=True,
                )
                shutil.rmtree(p, ignore_errors=True)
                removed = True
        if removed and not self._git_ok(["diff", "--cached", "--quiet"]):
            self._run_git(["commit", "-m", f"sync: {_now_ts()}（清理非法条目）"], check=True)
        return removed

    def _make_conflict(
        self, direction: str, policy: str, backup_ref: str | None, stash_hint: str | None
    ) -> dict:
        """组装冲突信息 + 生成冲突报告（.sync/conflicts-<ts>.md，不入 git）。"""
        report_path = None
        if backup_ref:
            report_path = self._write_conflict_report(direction, policy, backup_ref)
        info = {
            "resolved": True,
            "policy": policy,
            "backup_ref": backup_ref,
            "report": report_path,
        }
        if stash_hint:
            info["stash"] = stash_hint
        return info

    def _write_conflict_report(self, direction: str, policy: str, backup_ref: str) -> str | None:
        """写冲突报告：败方 → 胜方的逐技能变更清单 + 恢复指引（§4.2 ③）。"""
        try:
            diff = self._run_git(["diff", "--name-status", backup_ref, "HEAD"])
            diff_text = diff.stdout if diff.returncode == 0 else f"（diff 失败: {diff.stderr}）"
            sync_dir = self.root / ".sync"
            sync_dir.mkdir(parents=True, exist_ok=True)
            ts = _now_ts()
            path = sync_dir / f"conflicts-{ts}.md"
            content = "\n".join(
                [
                    "# Skills-Hub 同步冲突报告",
                    "",
                    f"- 时间: {ts}",
                    f"- 方向: {direction}",
                    f"- 策略: {policy}",
                    f"- 败方备份 ref: {backup_ref}",
                    f"- 恢复指引: git show {backup_ref}:<skill>/SKILL.md"
                    f"（或 git cherry-pick {backup_ref}）",
                    "",
                    "## 变更清单（败方状态 → 胜方状态，git diff --name-status）",
                    "",
                    diff_text,
                    "",
                ]
            )
            path.write_text(content, encoding="utf-8")
            return str(path)
        except GitSyncError:
            # 报告生成失败不阻断同步（冲突已按策略解决），仅提示缺失
            return None

    def _commit_stats(self) -> dict[str, list[str]]:
        """本次提交（HEAD）的技能级变更统计（A=新增 M/R=修改 D=删除）。"""
        r = self._run_git(["diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD"])
        added: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                status, path = parts[0], parts[1]
                if not path.endswith("/" + SKILL_FILE):
                    continue
                name = path[: -len("/" + SKILL_FILE)]
                try:
                    name = _validate_name(name)
                except ValueError:
                    continue
                if status.startswith("A"):
                    added.append(name)
                elif status.startswith("D"):
                    deleted.append(name)
                else:  # M / R / T / U …
                    modified.append(name)
        return {
            "added": sorted(set(added)),
            "modified": sorted(set(modified)),
            "deleted": sorted(set(deleted)),
        }

    def _result(
        self,
        direction: str,
        before: dict[str, str],
        after: dict[str, str],
        warnings: list[str],
        conflict: dict | None,
        t0: float,
        status: str,
        stats: dict[str, list[str]] | None = None,
    ) -> dict:
        """组装同步结果（方向/状态/新增/修改/删除/冲突报告/耗时，§5）。"""
        if stats is None:
            stats = _diff_snapshots(before, after)
        return {
            "direction": direction,
            "status": status,
            "branch": self.config.remote_branch,
            "added": stats["added"],
            "modified": stats["modified"],
            "deleted": stats["deleted"],
            "conflict": conflict,
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
        }

    # ---------- 内部 ----------

    def _require_enabled(self) -> None:
        """禁用态守卫：enabled=False 时所有技能操作拒绝。"""
        if not self.enabled:
            raise RuntimeError(_DISABLED_MSG)


def init(cfg: dict | None) -> SkillsHub | None:
    """按配置初始化技能仓库（v0.7 §11.3）。

    Args:
        cfg: 配置 dict（顶层含 skills_hub section）；None / 无 section 视为禁用。

    Returns:
        enabled=true 时返回 SkillsHub 实例；否则返回 None（禁用态）。

    Raises:
        ValueError: 配置非法（mode/sync_policy/enabled 类型）或启用后缺少工作区目录。
    """
    config = parse_skills_hub_config(cfg)
    if not config.enabled:
        return None
    return SkillsHub(config)
