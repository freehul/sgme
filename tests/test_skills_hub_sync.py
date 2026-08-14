"""0.8 ST-11 skills-hub copy 模式真实同步测试（git subprocess 双方向 + LWW 冲突）。

覆盖（对齐 SGME-SkillsHub同步设计-v0.1.md §9 验收标准）：
- §9.1 双方向同步实测：本地 git init --bare + file:// 模拟 NAS 权威仓
  （不依赖真实网络）；全量镜像 / 增删改推送 / 清空重拉一致
- §9.2 冲突可解：local_wins / remote_wins 双向；备份 ref 可恢复；冲突报告生成
- §9.3 安全用例：恶意技能名跳过 + warning；remote.source 注入不生效；
  map 模式/禁用态/空 source 报错
- §9.4 幂等用例：连续两次同步 no-op；断网（不可达 URL）报错且本地编辑不受影响
- 配置解析新字段（branch/conflict_policy/timeout_s/backup_refs）+ 非法值拒绝
- API 端点 POST /v1/admin/skills/sync（200/400/403/500）

全部用例通过 subprocess 调系统 git（与实现同一机制），不 mock git 行为。
"""
from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from sgme.skills_hub import GitSyncError, SKILL_FILE, init, parse_skills_hub_config
from sgme.skills_hub import _validate_source  # 安全关键校验，直接单测

ADMIN_HEADERS = {"X-API-Key": "test-admin-key"}
AGENT_HEADERS = {"X-API-Key": "test-agent-key"}

_GITIGNORE = "*\n!*/\n!*/SKILL.md\n"


# ---------- git 工具（真实 subprocess，与实现同机制） ----------


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git 命令失败 {args}: rc={proc.returncode} stderr={proc.stderr}")
    return proc


def _file_url(path: Path) -> str:
    """Windows 安全的 file:// URL。

    native Windows git 把 ``file:///C:/...`` 解析为 ``/C:/...``（无效），
    盘符路径须用 ``file://C:/...`` 形态；POSIX 保持 ``file:///tmp/...``。
    """
    s = str(path.resolve()).replace("\\", "/")
    if s[1:2] == ":":
        return "file://" + s
    return "file:///" + s


def _rand() -> str:
    return uuid.uuid4().hex[:8]


def _make_bare(tmp_path: Path) -> Path:
    """本地 bare 仓模拟 NAS 权威仓。"""
    bare = tmp_path / "skills-hub.git"
    _run(["git", "init", "--bare", "-q", str(bare)])
    return bare


def _write_skills(wd: Path, skills: dict[str, str]) -> None:
    """在工作树写技能目录（含恶意名目录，与正常技能同机制）。"""
    for name, content in skills.items():
        d = wd / name
        d.mkdir(parents=True, exist_ok=True)
        (d / SKILL_FILE).write_text(content, encoding="utf-8")


def _seed_remote(bare: Path, skills: dict[str, str], branch: str = "main") -> None:
    """首次给 bare 播种内容（模拟 NAS 权威仓已有技能）。"""
    wd = bare.parent / f"seed-{_rand()}"
    wd.mkdir()
    _run(["git", "init", "-q", str(wd)])
    _run(["git", "-C", str(wd), "config", "user.email", "seed@local"])
    _run(["git", "-C", str(wd), "config", "user.name", "SeedBot"])
    (wd / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    _write_skills(wd, skills)
    _run(["git", "-C", str(wd), "add", "-A"])
    _run(["git", "-C", str(wd), "add", "-f", ".gitignore"])
    _run(["git", "-C", str(wd), "commit", "-q", "-m", "seed"])
    _run(["git", "-C", str(wd), "branch", "-M", branch])
    _run(["git", "-C", str(wd), "remote", "add", "origin", str(bare)])
    _run(["git", "-C", str(wd), "push", "-q", "-u", "origin", branch])
    # bare HEAD 指向 main，便于后续 clone 默认检出
    _run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", f"refs/heads/{branch}"])


def _advance_remote(bare: Path, skills: dict[str, str], branch: str = "main") -> None:
    """在 bare 上追加一个提交（模拟另一端编辑并推送，制造分叉）。"""
    wd = bare.parent / f"adv-{_rand()}"
    _run(["git", "clone", "-q", "-b", branch, str(bare), str(wd)])
    _run(["git", "-C", str(wd), "config", "user.email", "remote@local"])
    _run(["git", "-C", str(wd), "config", "user.name", "RemoteBot"])
    _write_skills(wd, skills)
    _run(["git", "-C", str(wd), "add", "-A"])
    _run(["git", "-C", str(wd), "commit", "-q", "-m", "advance"])
    _run(["git", "-C", str(wd), "push", "-q", "origin", branch])


def _clone_state(bare: Path, dest: Path) -> dict[str, str]:
    """克隆 bare 并读取技能状态（验证权威仓内容）。"""
    if dest.exists():
        shutil.rmtree(dest)
    _run(["git", "clone", "-q", "-b", "main", str(bare), str(dest)])
    state: dict[str, str] = {}
    for p in sorted(dest.iterdir()):
        if p.is_dir() and (p / SKILL_FILE).is_file():
            state[p.name] = (p / SKILL_FILE).read_text(encoding="utf-8")
    return state


def _git(root: Path, *args: str) -> str:
    return _run(["git", "-C", str(root), *args]).stdout.strip()


def _git_head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _backup_refs(root: Path) -> list[str]:
    out = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/conflict-backup-*")
    return [line for line in out.splitlines() if line]


def _copy_hub(
    tmp_path: Path,
    bare: Path,
    cache_name: str = "cache",
    branch: str = "main",
    conflict_policy: str = "local_wins",
    backup_refs: bool = True,
    timeout_s: int = 60,
    source: str | None = None,
):
    """构造 copy 模式 hub（cache 工作区隔离于 tmp_path 下）。"""
    cache = tmp_path / cache_name
    cfg = {
        "skills_hub": {
            "enabled": True,
            "mode": "copy",
            "path": str(tmp_path / "map-path-ignored"),
            "remote": {
                "source": source if source is not None else _file_url(bare),
                "cache": str(cache),
                "branch": branch,
                "conflict_policy": conflict_policy,
                "timeout_s": timeout_s,
                "backup_refs": backup_refs,
            },
        }
    }
    hub = init(cfg)
    assert hub is not None
    return hub


# ---------- 配置解析：0.8 ST-11 新字段（§7） ----------


def test_config_new_remote_fields() -> None:
    """新字段完整解析：branch/conflict_policy（大小写归一）/timeout_s/backup_refs。"""
    c = parse_skills_hub_config(
        {
            "skills_hub": {
                "mode": "copy",
                "remote": {
                    "source": "user@nas-host:/path/to/skills-hub.git",
                    "cache": "./c/",
                    "branch": "dev/2.0",
                    "conflict_policy": "REMOTE_WINS",
                    "timeout_s": 120,
                    "backup_refs": False,
                },
            }
        }
    )
    assert c.remote_branch == "dev/2.0"
    assert c.remote_conflict_policy == "remote_wins"
    assert c.remote_timeout_s == 120
    assert c.remote_backup_refs is False


def test_config_new_remote_defaults() -> None:
    """新字段缺省兜底：无 remote 子段 / 缺字段 → 全默认（兼容旧配置）。"""
    c = parse_skills_hub_config({"skills_hub": {"mode": "copy"}})
    assert c.remote_branch == "main"
    assert c.remote_conflict_policy == "local_wins"
    assert c.remote_timeout_s == 60
    assert c.remote_backup_refs is True
    c = parse_skills_hub_config({"skills_hub": {"mode": "copy", "remote": {"source": "file:///x"}}})
    assert c.remote_branch == "main" and c.remote_conflict_policy == "local_wins"
    assert c.remote_timeout_s == 60 and c.remote_backup_refs is True


@pytest.mark.parametrize(
    "bad_remote",
    [
        {"branch": "a b"},
        {"branch": "a..b"},
        {"branch": ""},
        {"branch": "-x"},
        {"conflict_policy": "merge"},
        {"conflict_policy": "LocalWins"},
        {"timeout_s": 0},
        {"timeout_s": -3},
        {"timeout_s": "60"},
        {"timeout_s": True},
        {"backup_refs": "true"},
    ],
)
def test_config_invalid_remote_fields_raise(bad_remote: dict) -> None:
    """非法新字段（分支含空白/..、非法枚举、非正数超时、非 bool 备份开关）→ ValueError。"""
    with pytest.raises(ValueError):
        parse_skills_hub_config({"skills_hub": {"remote": bad_remote}})


# ---------- §9.1 双方向同步实测（file:// 模拟 NAS） ----------


def test_sync_from_remote_full_mirror(tmp_path) -> None:
    """首次 sync_from_remote：cache 为空 → 与远端逐字节一致的全量镜像。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "内容A", "beta": "内容B"})
    hub = _copy_hub(tmp_path, bare)

    r = hub.sync_from_remote()
    assert r["status"] == "ok"
    assert r["added"] == ["alpha", "beta"]
    assert r["modified"] == [] and r["deleted"] == []
    assert r["conflict"] is None and r["warnings"] == []
    assert hub.list_skills() == ["alpha", "beta"]
    assert hub.get_skill("alpha") == "内容A"
    assert hub.get_skill("beta") == "内容B"
    # 工作区即 git 工作树：分支/远端/忽略规则就位
    assert _git(hub.root, "branch", "--show-current") == "main"
    assert (hub.root / ".gitignore").read_text(encoding="utf-8") == _GITIGNORE


def test_sync_from_remote_empty_remote_noop(tmp_path) -> None:
    """远端为空 bare（无分支）→ no-op 成功（不报错、不产生提交）。"""
    bare = _make_bare(tmp_path)
    hub = _copy_hub(tmp_path, bare)
    r = hub.sync_from_remote()
    assert r["status"] == "noop"
    assert r["added"] == [] and hub.list_skills() == []


def test_sync_from_remote_idempotent(tmp_path) -> None:
    """连续两次 sync_from_remote：第二次 no-op（无新 commit、无报错）。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare)
    assert hub.sync_from_remote()["status"] == "ok"
    head1 = _git_head(hub.root)
    r2 = hub.sync_from_remote()
    assert r2["status"] == "noop"
    assert r2["added"] == [] and r2["modified"] == [] and r2["deleted"] == []
    assert _git_head(hub.root) == head1  # 无新提交
    assert hub.list_skills() == ["alpha"]


def test_sync_to_remote_push_and_relaunch_mirror(tmp_path) -> None:
    """新增+修改+删除 → sync_to_remote 推送 → 清空 cache 重拉全量镜像一致（§9.1）。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1", "gamma": "g1"})
    hub = _copy_hub(tmp_path, bare)
    hub.sync_from_remote()

    hub.put_skill("beta", "b1")
    hub.put_skill("alpha", "v2")
    assert hub.remove_skill("gamma") is True
    r = hub.sync_to_remote()
    assert r["status"] == "ok"
    assert r["added"] == ["beta"]
    assert r["modified"] == ["alpha"]
    assert r["deleted"] == ["gamma"]
    assert r["conflict"] is None

    # 权威仓内容验证：只剩 alpha(v2)/beta
    assert _clone_state(bare, tmp_path / "verify") == {"alpha": "v2", "beta": "b1"}

    # 清空 cache 重拉 → 全量镜像与远端一致
    hub2 = _copy_hub(tmp_path, bare, cache_name="cache2")
    r2 = hub2.sync_from_remote()
    assert r2["status"] == "ok"
    assert sorted(r2["added"]) == ["alpha", "beta"]
    assert hub2.list_skills() == ["alpha", "beta"]
    assert hub2.get_skill("alpha") == "v2"
    assert hub2.get_skill("beta") == "b1"


def test_sync_to_remote_noop_idempotent(tmp_path) -> None:
    """连续两次 sync_to_remote（无本地变更）：no-op（无新 commit、up-to-date 成功）。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare)
    hub.sync_from_remote()
    r1 = hub.sync_to_remote()
    assert r1["status"] == "noop"
    head1 = _git_head(hub.root)
    r2 = hub.sync_to_remote()
    assert r2["status"] == "noop"
    assert _git_head(hub.root) == head1


# ---------- §9.2 冲突可解（LWW + 备份 ref + 冲突报告） ----------


def test_conflict_push_local_wins(tmp_path) -> None:
    """push 被拒（远端被另一端推进）→ local_wins：备份远端 → force-with-lease 覆盖
    → 败方提交可从 conflict-backup-<ts> 完整恢复 → 冲突报告生成且清单正确。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare)
    hub.sync_from_remote()
    hub.put_skill("alpha", "v2")
    hub.sync_to_remote()  # 本地 v2 已推送，远端=本地=v2
    _advance_remote(bare, {"alpha": "v3"})  # 另一端把远端推进到 v3
    hub.put_skill("alpha", "v4-local")

    r = hub.sync_to_remote()
    assert r["status"] == "conflict_resolved"
    assert r["conflict"]["policy"] == "local_wins"
    # 本地胜出：远端 = v4-local
    assert _clone_state(bare, tmp_path / "verify") == {"alpha": "v4-local"}
    # 败方（远端 v3）备份 ref 可完整恢复
    refs = _backup_refs(hub.root)
    assert len(refs) == 1, f"应有 1 个备份 ref，实际 {refs}"
    assert _git(hub.root, "show", f"{refs[0]}:alpha/{SKILL_FILE}") == "v3"
    # 冲突报告生成且清单含 alpha
    reports = list((hub.root / ".sync").glob("conflicts-*.md"))
    assert reports, "冲突报告未生成"
    text = reports[-1].read_text(encoding="utf-8")
    assert "alpha" in text and "conflict-backup-" in text
    assert r["conflict"]["report"] is not None


def test_conflict_push_remote_wins(tmp_path) -> None:
    """push 被拒 → remote_wins：备份本地 → reset 到远端；本地变更可从备份 ref 恢复。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare, conflict_policy="remote_wins")
    hub.sync_from_remote()
    hub.put_skill("alpha", "v2")
    hub.sync_to_remote()
    _advance_remote(bare, {"alpha": "v3"})
    hub.put_skill("alpha", "v4-local")

    r = hub.sync_to_remote()
    assert r["status"] == "conflict_resolved"
    assert r["conflict"]["policy"] == "remote_wins"
    # 远端胜出：本地 = v3（工作区被重置）
    assert hub.get_skill("alpha") == "v3"
    assert hub.list_skills() == ["alpha"]
    # 败方（本地 v4-local）备份 ref 可恢复
    refs = _backup_refs(hub.root)
    assert len(refs) == 1, f"应有 1 个备份 ref，实际 {refs}"
    assert _git(hub.root, "show", f"{refs[0]}:alpha/{SKILL_FILE}") == "v4-local"
    reports = list((hub.root / ".sync").glob("conflicts-*.md"))
    assert reports and "alpha" in reports[-1].read_text(encoding="utf-8")


def test_conflict_from_remote_local_wins_aborts(tmp_path) -> None:
    """拉取侧分叉（本地有未推送提交 + 远端被推进）→ local_wins 中止报错，
    本地提交/工作区原样保留（§4.3）。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare)
    hub.sync_from_remote()
    hub.put_skill("alpha", "v2")
    hub.sync_to_remote()  # 本地=远端=v2
    _advance_remote(bare, {"alpha": "v3"})
    # 本地制造未推送提交（构造真分叉：本地领先 + 远端也领先）
    hub.put_skill("alpha", "v4-local")
    _run(["git", "-C", str(hub.root), "add", "-A"])
    _run(["git", "-C", str(hub.root), "commit", "-q", "-m", "local unpushed"])

    with pytest.raises(GitSyncError, match="本地领先远端"):
        hub.sync_from_remote()
    # 本地资产不受影响
    assert hub.get_skill("alpha") == "v4-local"
    assert hub.list_skills() == ["alpha"]


def test_conflict_from_remote_remote_wins(tmp_path) -> None:
    """拉取侧分叉 → remote_wins：备份本地提交 → reset 到远端；备份 ref 可恢复。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare, conflict_policy="remote_wins")
    hub.sync_from_remote()
    hub.put_skill("alpha", "v2")
    hub.sync_to_remote()
    _advance_remote(bare, {"alpha": "v3"})
    hub.put_skill("alpha", "v4-local")
    _run(["git", "-C", str(hub.root), "add", "-A"])
    _run(["git", "-C", str(hub.root), "commit", "-q", "-m", "local unpushed"])

    r = hub.sync_from_remote()
    assert r["status"] == "conflict_resolved"
    assert r["conflict"]["policy"] == "remote_wins"
    assert hub.get_skill("alpha") == "v3"  # 远端胜出
    refs = _backup_refs(hub.root)
    assert len(refs) == 1
    assert _git(hub.root, "show", f"{refs[0]}:alpha/{SKILL_FILE}") == "v4-local"


def test_conflict_remote_wins_stash_uncommitted(tmp_path) -> None:
    """remote_wins 覆盖前：未提交变更（含未跟踪）入 stash，数据不丢。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare, conflict_policy="remote_wins")
    hub.sync_from_remote()
    _advance_remote(bare, {"alpha": "v3"})
    hub.put_skill("alpha", "v4-dirty")  # 未提交的本地编辑
    hub.put_skill("wip-skill", "未推送的新技能")

    r = hub.sync_from_remote()
    assert r["status"] == "conflict_resolved"
    assert r["conflict"]["stash"] == "stash@{0}"
    assert hub.get_skill("alpha") == "v3"
    stash_list = _git(hub.root, "stash", "list")
    assert "pre-remote-wins-" in stash_list


def test_conflict_backup_refs_disabled(tmp_path) -> None:
    """backup_refs=false（测试/清理模式）：冲突仍按策略解决，但不生成备份 ref。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    hub = _copy_hub(tmp_path, bare, backup_refs=False)
    hub.sync_from_remote()
    hub.put_skill("alpha", "v2")
    hub.sync_to_remote()
    _advance_remote(bare, {"alpha": "v3"})
    hub.put_skill("alpha", "v4-local")

    r = hub.sync_to_remote()
    assert r["status"] == "conflict_resolved"
    assert r["conflict"]["backup_ref"] is None
    assert r["conflict"]["report"] is None
    assert _backup_refs(hub.root) == []
    assert _clone_state(bare, tmp_path / "verify") == {"alpha": "v4-local"}


# ---------- §9.3 安全用例 ----------


def test_invalid_names_skipped_with_warning(tmp_path) -> None:
    """远端恶意技能名（含分隔符/非法字符/..）→ 跳过 + warning + 不进技能列表/
    不参与后续 push（§6.2）。"""
    bare = _make_bare(tmp_path)
    _seed_remote(
        bare,
        {
            "alpha": "ok",
            "a b": "space 名",
            "a..b": "双点变体",
            "bad;name": "分号名",
            "中文技能": "非白名单字符",
        },
    )
    hub = _copy_hub(tmp_path, bare)
    r = hub.sync_from_remote()
    assert r["status"] == "ok"
    assert hub.list_skills() == ["alpha"]  # 恶意名不进技能列表
    assert not (hub.root / "a b").exists()  # 已清理出工作区
    assert len(r["warnings"]) == 4
    for bad in ("a b", "a..b", "bad;name", "中文技能"):
        assert any(bad in w for w in r["warnings"]), f"缺 {bad} 的 warning: {r['warnings']}"
    # 后续推送不携带恶意条目（清理 commit 随下次 to_remote 传播到远端）
    r2 = hub.sync_to_remote()
    # 本次同步无新本地提交 → noop 语义（§3.3）；清理结果以远端状态为准
    assert r2["status"] in ("ok", "noop")
    assert _clone_state(bare, tmp_path / "verify") == {"alpha": "ok"}


def test_source_injection_rejected(tmp_path, monkeypatch) -> None:
    """remote.source 注入（内嵌 -c/--upload-pack/分号等）→ 同步前即拒绝，
    不产生任何 git 子进程（§6.2 注入防护）。"""
    cache = tmp_path / "c"
    bad_sources = [
        f"{_file_url(tmp_path / 'r')} --upload-pack=cat",
        f"{_file_url(tmp_path / 'r')} -c core.quotepath=0",
        "user@host:/vol1/x; touch /tmp/pwn",
        "ssh://host/x --receive-pack=evil",
        "file:///tmp/x\tfile:///tmp/y",
        "nas://nas-host/skills-hub/",
        "https://example.com/repo.git",
    ]

    def _forbid_git(*args, **kwargs):
        raise AssertionError(f"不应执行任何 git 子进程: {args}")

    monkeypatch.setattr(subprocess, "run", _forbid_git)
    for bad in bad_sources:
        hub = init(
            {
                "skills_hub": {
                    "enabled": True,
                    "mode": "copy",
                    "remote": {"source": bad, "cache": str(cache)},
                }
            }
        )
        assert hub is not None
        with pytest.raises(ValueError, match="仅允许|非法字符"):
            hub.sync_from_remote()
        with pytest.raises(ValueError, match="仅允许|非法字符"):
            hub.sync_to_remote()


def test_validate_source_forms() -> None:
    """remote.source 三形态白名单：ssh:// / user@host:path / file:// 放行，其余拒绝。"""
    for ok in (
        "ssh://user@nas-host/vol1/1000/git/skills-hub.git",
        "ssh://nas-host/vol1/x.git",
        "user@nas-host:/path/to/skills-hub.git",
        "file:///tmp/skills-hub.git",
        "file:///D:/Projects/skills-hub.git",
    ):
        assert _validate_source(ok) == ok, ok
    for bad in (
        "",
        "   ",
        "nas://h/",
        "https://x/y.git",
        "http://x",
        "git://x",
        "-c core.fsmonitor=1",
        "file:///tmp/x --upload-pack=cat",
        "user@host:",
        "ssh://",
        "D:/Projects/x.git",  # 裸 Windows 路径不属于三形态
    ):
        with pytest.raises(ValueError):
            _validate_source(bad)


def test_map_mode_and_disabled_and_empty_source(tmp_path) -> None:
    """map 模式 / 禁用态 / copy 但 source 为空 → 同步报错（§6.3）。"""
    # map 模式：同步仅 copy 模式可用
    hub = init({"skills_hub": {"enabled": True, "path": str(tmp_path / "m")}})
    assert hub is not None
    with pytest.raises(ValueError, match="map 模式无远端语义"):
        hub.sync_from_remote()
    with pytest.raises(ValueError, match="map 模式无远端语义"):
        hub.sync_to_remote()

    # 禁用态：_require_enabled 拦截
    from sgme.skills_hub import SkillsHub

    disabled = SkillsHub(parse_skills_hub_config({"skills_hub": {}}))
    with pytest.raises(RuntimeError, match="禁用"):
        disabled.sync_from_remote()
    with pytest.raises(RuntimeError, match="禁用"):
        disabled.sync_to_remote()

    # copy 但 source 为空：配置错误
    hub2 = init(
        {
            "skills_hub": {
                "enabled": True,
                "mode": "copy",
                "remote": {"cache": str(tmp_path / "c2")},
            }
        }
    )
    assert hub2 is not None
    with pytest.raises(ValueError, match="source 为空"):
        hub2.sync_from_remote()


def test_git_timeout_raises(tmp_path, monkeypatch) -> None:
    """git 子进程超时 → GitSyncError（含超时提示），不挂起。"""
    hub = _copy_hub(tmp_path, _make_bare(tmp_path), timeout_s=5)

    def _slow_git(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 5))

    monkeypatch.setattr(subprocess, "run", _slow_git)
    with pytest.raises(GitSyncError, match="超时"):
        hub.sync_from_remote()


# ---------- §9.4 断网幂等：远端不可达报错且本地编辑不受影响 ----------


def test_remote_unreachable_error_local_edits_ok(tmp_path) -> None:
    """断网（不可达 URL）：同步报错；本地 put/get/list 编辑不受影响（§3.3）。"""
    dead_url = _file_url(tmp_path / "no-such-repo.git")
    hub = _copy_hub(tmp_path, tmp_path / "unused.git", source=dead_url)

    with pytest.raises(GitSyncError, match="远端不可达"):
        hub.sync_from_remote()
    with pytest.raises(GitSyncError):
        hub.sync_to_remote()

    # 本地编辑不受影响（离线可用）
    hub.put_skill("offline-skill", "离线编辑")
    assert hub.get_skill("offline-skill") == "离线编辑"
    assert hub.list_skills() == ["offline-skill"]
    assert hub.remove_skill("offline-skill") is True


# ---------- API 端点 POST /v1/admin/skills/sync（§5） ----------


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """(client_factory, cleanup)：按用例定制 skills_hub 配置创建隔离 app。"""
    from fastapi.testclient import TestClient

    from sgme import config as sgme_config
    from sgme.data import db as db_mod
    from sgme.data import memory_dao
    from sgme.profile import tier0 as tier0_mod
    from sgme.raw import store as raw_store
    from sgme.server.app import create_app

    rd = tmp_path / "raw"
    rd.mkdir(exist_ok=True)
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    monkeypatch.setattr(raw_store, "config", sgme_config)
    monkeypatch.setattr(tier0_mod, "SUMMARY_PATH", tmp_path / "tier0_summary.json")
    monkeypatch.setenv("SGME_CONFIG_PATH", str(tmp_path / "sgme_test.yaml"))
    # 清除 Bearer 令牌全局副作用（create_app 的 os.environ.setdefault 会从
    # 宿主环境拾取 SGME_BEARER_TOKEN 开启传输层鉴权 → 只有 X-API-Key 的
    # 测试请求 401。惯例同 tests/test_operations_health.py 的 no_bearer）
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)

    created: list[tuple] = []

    def factory(skills_hub_section: dict | None = None) -> TestClient:
        cfg = sgme_config.load_config()
        if skills_hub_section is not None:
            cfg["skills_hub"] = skills_hub_section
        mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / f"data-{len(created)}")
        memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
        app = create_app(
            cfg=cfg,
            mem_conn=mem_conn,
            session_conn=session_conn,
            wiki_conn=wiki_conn,
            admin_key="test-admin-key",
            agent_key="test-agent-key",
            agent_store_path=tmp_path / "agent_keys.json",
        )
        created.append((mem_conn, session_conn, wiki_conn))
        return TestClient(app)

    yield factory
    for mem_conn, session_conn, wiki_conn in created:
        db_mod.close(mem_conn)
        db_mod.close(session_conn)
        db_mod.close(wiki_conn)


def _copy_section(tmp_path: Path, bare: Path, **remote_overrides) -> dict:
    """构造 copy 模式 skills_hub 配置段（API 用例用）。"""
    remote = {
        "source": _file_url(bare),
        "cache": str(tmp_path / "api-cache"),
        "branch": "main",
        "conflict_policy": "local_wins",
        "timeout_s": 60,
        "backup_refs": True,
    }
    remote.update(remote_overrides)
    return {"enabled": True, "mode": "copy", "path": str(tmp_path / "map-x"), "remote": remote}


def test_api_sync_from_remote_ok(tmp_path, api_env) -> None:
    """POST /v1/admin/skills/sync（from_remote）→ 200 + 结果 JSON。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "api 内容"})
    client = api_env(_copy_section(tmp_path, bare))

    resp = client.post("/v1/admin/skills/sync", json={"direction": "from_remote"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()["sync"]
    assert body["direction"] == "from_remote"
    assert body["status"] == "ok"
    assert body["added"] == ["alpha"]
    assert body["conflict"] is None
    assert isinstance(body["duration_ms"], int)


def test_api_sync_both_default(tmp_path, api_env) -> None:
    """默认 direction=both：先拉后推，results 双段。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    client = api_env(_copy_section(tmp_path, bare))

    resp = client.post("/v1/admin/skills/sync", json={}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()["sync"]
    assert body["direction"] == "both"
    assert len(body["results"]) == 2
    assert body["results"][0]["direction"] == "from_remote"
    assert body["results"][1]["direction"] == "to_remote"
    assert body["status"] == "ok"


def test_api_sync_requires_admin(tmp_path, api_env) -> None:
    """Agent Key 调 sync → 403。"""
    bare = _make_bare(tmp_path)
    client = api_env(_copy_section(tmp_path, bare))
    resp = client.post("/v1/admin/skills/sync", json={"direction": "from_remote"}, headers=AGENT_HEADERS)
    assert resp.status_code == 403


def test_api_sync_map_mode_400(tmp_path, api_env) -> None:
    """mode=map 调 sync → 400（map 模式无远端语义）。"""
    section = _copy_section(tmp_path, _make_bare(tmp_path))
    section["mode"] = "map"
    client = api_env(section)
    resp = client.post("/v1/admin/skills/sync", json={"direction": "from_remote"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert "map 模式" in resp.json()["error"]["message"]


def test_api_sync_disabled_400(tmp_path, api_env) -> None:
    """skills_hub 未启用调 sync → 400。"""
    client = api_env({"enabled": False, "mode": "copy"})
    resp = client.post("/v1/admin/skills/sync", json={}, headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert "未启用" in resp.json()["error"]["message"]


def test_api_sync_invalid_direction_400(tmp_path, api_env) -> None:
    """非法 direction → 400。"""
    client = api_env(_copy_section(tmp_path, _make_bare(tmp_path)))
    resp = client.post("/v1/admin/skills/sync", json={"direction": "sideways"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 400, resp.text
    assert "未知同步方向" in resp.json()["error"]["message"]


def test_api_sync_git_failure_500(tmp_path, api_env) -> None:
    """git 失败（远端不可达）→ 500 + stderr 摘要。"""
    section = _copy_section(tmp_path, tmp_path / "unused.git", source=_file_url(tmp_path / "no-such.git"))
    client = api_env(section)
    resp = client.post("/v1/admin/skills/sync", json={"direction": "from_remote"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 500, resp.text
    err = resp.json()["error"]
    assert "同步失败" in err["message"]
    assert "stderr" in err.get("details", {})


def test_api_sync_conflict_local_wins_200(tmp_path, api_env) -> None:
    """冲突路径经 API：local_wins 解决 → 200 + conflict 信息 + 备份 ref。"""
    bare = _make_bare(tmp_path)
    _seed_remote(bare, {"alpha": "v1"})
    client = api_env(_copy_section(tmp_path, bare))
    assert client.post("/v1/admin/skills/sync", json={"direction": "from_remote"}, headers=ADMIN_HEADERS).status_code == 200

    # 直接用 hub 对象做本地编辑（同一 cache 工作区），另一端推进远端
    from sgme.skills_hub import init as hub_init

    hub = hub_init(
        {
            "skills_hub": {
                "enabled": True,
                "mode": "copy",
                "remote": {"source": _file_url(bare), "cache": str(tmp_path / "api-cache")},
            }
        }
    )
    assert hub is not None
    hub.put_skill("alpha", "v2")
    assert hub.sync_to_remote()["status"] == "ok"
    _advance_remote(bare, {"alpha": "v3"})
    hub.put_skill("alpha", "v4-local")

    resp = client.post("/v1/admin/skills/sync", json={"direction": "to_remote"}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()["sync"]
    assert body["status"] == "conflict_resolved"
    assert body["conflict"]["policy"] == "local_wins"
    assert _backup_refs(hub.root) != []
