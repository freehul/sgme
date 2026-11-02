"""技能仓库配置解析：从配置 dict 的 skills_hub section 解析（v0.7 §11.3 / 0.8 ST-11）。

约定：配置 dict 形如 ``{"skills_hub": {...}}``（即 sgme.config.load_config()
返回字典的顶层结构），section 缺失或类型错误时返回全默认配置：
enabled=False、mode=map、sync_policy=manual、path/remote 为空、
remote.branch=main、remote.conflict_policy=local_wins、remote.timeout_s=60、
remote.backup_refs=true。

示例（config/sgme.yaml）::

    skills_hub:
      enabled: true
      path: "./skills-hub/"
      mode: copy
      sync_policy: manual
      remote:                    # mode=copy 时才生效
        source: "user@nas-host:/path/to/skills-hub.git"
        cache: "./cache/skills/"
        branch: main              # 0.8 ST-11：同步分支（默认 main）
        conflict_policy: local_wins  # 0.8 ST-11：local_wins | remote_wins（默认 local_wins）
        timeout_s: 60             # 0.8 ST-11：单次 git 操作超时秒（默认 60）
        backup_refs: true         # 0.8 ST-11：冲突时保留败方备份 ref（默认 true）

remote.source 的形态校验（仅 ssh://、user@host:path、file://）在同步层执行
（§6.2 安全防线），本模块只做字段类型/取值校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 合法部署模式（§11.2）
VALID_MODES = ("map", "copy")
# 合法同步策略
VALID_SYNC_POLICIES = ("manual", "auto")
# 合法冲突策略（§4.2 LWW 胜方）
VALID_CONFLICT_POLICIES = ("local_wins", "remote_wins")

# 缺省值（§11.3 / §7）
DEFAULT_ENABLED = False
DEFAULT_MODE = "map"
DEFAULT_SYNC_POLICY = "manual"
DEFAULT_REMOTE_BRANCH = "main"
DEFAULT_CONFLICT_POLICY = "local_wins"
DEFAULT_TIMEOUT_S = 60
DEFAULT_BACKUP_REFS = True

# 分支名白名单（git ref 规则子集：字母/数字/点/下划线/中划线/斜杠）
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass
class SkillsHubConfig:
    """技能仓库配置（§11.3 / §7）。

    Attributes:
        enabled: 模块开关，False 时 init() 返回禁用态。
        path: map 模式直接读写的技能仓库目录。
        mode: 部署模式，map（本地零拷贝）| copy（远端拉取到缓存）。
        sync_policy: 同步策略，manual（手动）| auto（自动，预留）。
        remote_source: copy 模式远端权威仓（bare）地址。
        remote_cache: copy 模式本地缓存工作区（实际读写位置）。
        remote_branch: 同步分支（默认 main）。
        remote_conflict_policy: 分叉时胜方，local_wins（默认）| remote_wins。
        remote_timeout_s: 单次 git 操作超时秒（默认 60）。
        remote_backup_refs: 冲突时是否保留败方备份 ref conflict-backup-<ts>
            （默认 true；false 仅测试/清理用）。
    """

    enabled: bool = DEFAULT_ENABLED
    path: str = ""
    mode: str = DEFAULT_MODE
    sync_policy: str = DEFAULT_SYNC_POLICY
    remote_source: str = ""
    remote_cache: str = ""
    remote_branch: str = DEFAULT_REMOTE_BRANCH
    remote_conflict_policy: str = DEFAULT_CONFLICT_POLICY
    remote_timeout_s: int = DEFAULT_TIMEOUT_S
    remote_backup_refs: bool = DEFAULT_BACKUP_REFS


def _parse_remote(section: dict) -> dict:
    """解析 remote 子段（缺省兜底 + 新字段校验），返回字段 dict。"""
    remote = section.get("remote")
    if not isinstance(remote, dict):
        # remote 缺失/类型错误 → 全默认（兼容旧配置）
        return {
            "source": "",
            "cache": "",
            "branch": DEFAULT_REMOTE_BRANCH,
            "conflict_policy": DEFAULT_CONFLICT_POLICY,
            "timeout_s": DEFAULT_TIMEOUT_S,
            "backup_refs": DEFAULT_BACKUP_REFS,
        }

    # branch：非空、无空白、无 ".."、白名单字符（§7）
    branch = str(remote.get("branch", DEFAULT_REMOTE_BRANCH) or "").strip()
    if not branch:
        raise ValueError("skills_hub.remote.branch 不能为空")
    if any(ch.isspace() for ch in branch) or ".." in branch or branch.startswith("-"):
        raise ValueError(f"非法 skills_hub.remote.branch: {branch!r}（含空白/.. /选项前缀）")
    if not _BRANCH_RE.match(branch):
        raise ValueError(
            f"非法 skills_hub.remote.branch: {branch!r}"
            f"（仅允许字母/数字/点/下划线/中划线/斜杠）"
        )

    # conflict_policy：小写归一化，仅允许 local_wins/remote_wins（§4.2）
    conflict_policy = str(remote.get("conflict_policy", DEFAULT_CONFLICT_POLICY)).lower()
    if conflict_policy not in VALID_CONFLICT_POLICIES:
        raise ValueError(
            f"未知 skills_hub.remote.conflict_policy: {conflict_policy!r}"
            f"（可选: {', '.join(VALID_CONFLICT_POLICIES)}）"
        )

    # timeout_s：严格 int 且 > 0（bool 是 int 子类，显式拒绝防 YAML 歧义）
    timeout_s = remote.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        raise ValueError(
            f"skills_hub.remote.timeout_s 必须为正整数（当前: {timeout_s!r}）"
        )

    # backup_refs：严格 bool（"true" 字符串一律视为非法）
    backup_refs = remote.get("backup_refs", DEFAULT_BACKUP_REFS)
    if not isinstance(backup_refs, bool):
        raise ValueError(f"skills_hub.remote.backup_refs 必须为 bool（当前: {backup_refs!r}）")

    return {
        "source": str(remote.get("source", "") or ""),
        "cache": str(remote.get("cache", "") or ""),
        "branch": branch,
        "conflict_policy": conflict_policy,
        "timeout_s": timeout_s,
        "backup_refs": backup_refs,
    }


def parse_skills_hub_config(cfg: dict | None) -> SkillsHubConfig:
    """从配置 dict 解析技能仓库配置。

    Args:
        cfg: 配置 dict；skills_hub section 位于其顶层。
            传入 None / 空 dict / 无 skills_hub section 的 dict 均返回默认配置。

    Returns:
        SkillsHubConfig 实例。

    Raises:
        ValueError: enabled 非 bool、mode 非法（非 map/copy）、
            sync_policy 非法（非 manual/auto）、remote 新字段非法
            （branch 含空白/..、conflict_policy 非法枚举、timeout_s 非正数、
            backup_refs 非 bool）。
    """
    section = (cfg or {}).get("skills_hub")
    if not isinstance(section, dict):
        # section 缺失或类型错误 → 全默认兜底
        return SkillsHubConfig()

    # enabled：必须为严格 bool（"true" 字符串一律视为非法，防 YAML 类型歧义）
    enabled = section.get("enabled", DEFAULT_ENABLED)
    if not isinstance(enabled, bool):
        raise ValueError(f"skills_hub.enabled 必须为 bool（当前: {enabled!r}）")

    # mode：小写归一化，仅允许 map/copy
    mode = str(section.get("mode", DEFAULT_MODE)).lower()
    if mode not in VALID_MODES:
        raise ValueError(
            f"未知 skills_hub.mode: {mode!r}（可选: {', '.join(VALID_MODES)}）"
        )

    # sync_policy：小写归一化，仅允许 manual/auto
    sync_policy = str(section.get("sync_policy", DEFAULT_SYNC_POLICY)).lower()
    if sync_policy not in VALID_SYNC_POLICIES:
        raise ValueError(
            f"未知 skills_hub.sync_policy: {sync_policy!r}"
            f"（可选: {', '.join(VALID_SYNC_POLICIES)}）"
        )

    # remote section：copy 模式才生效，map 模式解析但仅记录
    remote = _parse_remote(section)

    return SkillsHubConfig(
        enabled=enabled,
        path=str(section.get("path", "") or ""),
        mode=mode,
        sync_policy=sync_policy,
        remote_source=remote["source"],
        remote_cache=remote["cache"],
        remote_branch=remote["branch"],
        remote_conflict_policy=remote["conflict_policy"],
        remote_timeout_s=remote["timeout_s"],
        remote_backup_refs=remote["backup_refs"],
    )
