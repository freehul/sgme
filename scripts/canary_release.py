# -*- coding: utf-8 -*-
"""scripts/canary_release.py：canary 发布计划 dry-run + 发布门禁汇总（只读）。

用途：发布前先看清「会打什么 tag、changelog 长什么样、三道门禁过没过」，
但本脚本**绝不** tag / push / 改文件——所有动作只打印成计划，由人工执行。
（本脚本没有 --execute 之类的执行开关：物理上不存在写 git/写文件的代码路径。）

tag 规则：vX.Y.Z-canary.N；无 canary 历史时基线 v2.0.0 → v2.0.0-canary.1。
有 canary 历史时沿用最高 canary 的基准版本，序号 +1；也可用 --base-version
显式指定基准版本（如 2.0.4），序号在该基准下取「已有最大 N + 1」。

用法：
  .venv\\Scripts\\python.exe scripts\\canary_release.py                 # 只出计划（dry-run）
  .venv\\Scripts\\python.exe scripts\\canary_release.py --check-gates    # 计划 + 三道发布门禁
  .venv\\Scripts\\python.exe scripts\\canary_release.py --json          # 机器可读

退出码：0 = 计划生成成功（未跑门禁或门禁全绿）；1 = 门禁失败 / git 不可用；
2 = 参数错误（argparse）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT = 60          # git 只读命令超时（秒）
GATE_TIMEOUT = 900        # 单道门禁超时（秒）；对账/新鲜度要扫全仓，留足余量
DEFAULT_BASE_VERSION = "2.0.0"
TEXT_TAIL_LINES = 15      # 文本模式：失败门禁只回显尾部若干行
JSON_TAIL_LINES = 40      # JSON 模式：尾部保留行数（避免输出过大）

# canary tag 与正式 tag 的解析口径
CANARY_RE = re.compile(
    r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)-canary\.(?P<seq>\d+)$"
)
STABLE_RE = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


def _project_version() -> str:
    """canary 基线默认取 pyproject 的 version（读不到回落 DEFAULT_BASE_VERSION）。"""
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m and STABLE_RE.match("v" + m.group(1).strip()):
            return m.group(1).strip()
    except OSError:
        pass
    return DEFAULT_BASE_VERSION

# 发布门禁：顺序即执行顺序；统一用 sys.executable 跑，避免 PATH 上多版本解释器漂移
GATES = (
    (
        "adapter_parity",
        "scripts/adapter_parity.py",
        ("--strict",),
        "八官方适配器能力面对账（待补齐也算失败）",
    ),
    (
        "adapter_freshness",
        "scripts/check_adapter_freshness.py",
        ("--strict-deploy",),
        "适配器分发副本 / dsh bridge / 部署态新鲜度",
    ),
    (
        "verify_decisions",
        "scripts/verify_decisions.py",
        ("--strict",),
        "设计决策 ↔ 代码契约对账（文档落后也算失败）",
    ),
)

SOURCE_LABELS = {
    "default": "无 canary 历史 → 默认基线",
    "history": "沿用最高 canary 的基准版本",
    "explicit": "--base-version 显式指定",
}


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK：中文/emoji 直接 print 会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _run(cmd: list[str], cwd: Path, timeout: int):
    """统一的只读 subprocess 入口（测试通过 monkeypatch 本模块的 subprocess.run 替换）。"""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _git(repo: Path, args: list[str]) -> tuple[str | None, str | None]:
    """执行只读 git 命令，返回 (stdout, error)；error 为 None 表示成功。"""
    try:
        proc = _run(["git", *args], repo, GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:  # 含 TimeoutExpired
        return None, f"无法执行 git {' '.join(args)}：{exc}"
    if proc.returncode != 0:
        detail = _merge_out(proc).strip() or f"exit {proc.returncode}"
        return None, f"git {' '.join(args)} 失败：{detail}"
    return proc.stdout or "", None


def _merge_out(proc) -> str:
    """合并 stdout/stderr（门禁脚本有的把结论写 stderr）。"""
    out = (proc.stdout or "").rstrip()
    err = (proc.stderr or "").rstrip()
    if out and err:
        return out + "\n" + err
    return out or err


def parse_canary(tag: str) -> tuple[int, int, int, int] | None:
    """解析 vX.Y.Z-canary.N；不是 canary tag 返回 None。"""
    m = CANARY_RE.match(tag.strip())
    if not m:
        return None
    return (
        int(m.group("major")),
        int(m.group("minor")),
        int(m.group("patch")),
        int(m.group("seq")),
    )


def parse_stable(tag: str) -> tuple[int, int, int] | None:
    """解析 vX.Y.Z；不是正式 tag 返回 None。"""
    m = STABLE_RE.match(tag.strip())
    if not m:
        return None
    return (int(m.group("major")), int(m.group("minor")), int(m.group("patch")))


def _tag_key(tag: str) -> tuple[int, int, int, int, int] | None:
    """可比较的 tag 排序键：(major, minor, patch, 阶段, 序号)。

    阶段 0 = canary（预发布），1 = 正式版——同基准下正式版优先于 canary。
    """
    canary = parse_canary(tag)
    if canary:
        return (*canary[:3], 0, canary[3])
    stable = parse_stable(tag)
    if stable:
        return (*stable, 1, 0)
    return None


def latest_tag(tags: list[str]) -> str | None:
    """按版本语义取最新 tag（不是 git tag 的字典序）；无解析得出的 tag 返回 None。"""
    keyed = [(key, tag) for tag in tags if (key := _tag_key(tag)) is not None]
    if not keyed:
        return None
    return max(keyed, key=lambda item: item[0])[1]


def next_canary(tags: list[str], base_version: str | None = None) -> dict:
    """计算下一个 canary tag。

    - 无 canary 历史：默认基线取 pyproject version（如 2.0.4 → v2.0.4-canary.1；可用 base_version 改基线）；
    - 有 canary 历史：沿用最高 canary 的基准版本 + 序号 1；
    - 显式 base_version：在该基准下取已有最大 N + 1（无则 1）。
    """
    canaries = [(v, tag) for tag in tags if (v := parse_canary(tag)) is not None]

    if base_version:
        base = parse_stable(f"v{base_version.strip()}")
        if base is None:
            raise ValueError(f"--base-version 需形如 X.Y.Z（收到 {base_version!r}）")
        source = "explicit"
        same_base = [(v, tag) for v, tag in canaries if v[:3] == base]
        seq = max((v[3] for v, _ in same_base), default=0) + 1
    elif canaries:
        top_version, _ = max(canaries, key=lambda item: item[0])
        base = top_version[:3]
        source = "history"
        same_base = [(v, tag) for v, tag in canaries if v[:3] == base]
        seq = max(v[3] for v, _ in same_base) + 1
    else:
        base = tuple(int(x) for x in _project_version().split("."))
        source = "default"
        same_base = []
        seq = 1

    previous = None
    if same_base:
        previous = max(same_base, key=lambda item: item[0])[1]

    base_text = ".".join(str(x) for x in base)
    return {
        "base_version": base_text,
        "sequence": seq,
        "next_tag": f"v{base_text}-canary.{seq}",
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "previous_canary": previous,
    }


def collect_changelog(repo: Path, last_tag: str | None) -> dict:
    """生成 changelog 草案：有 tag 用 <last-tag>..HEAD，否则取最近 50 条提交。"""
    if last_tag:
        out, err = _git(repo, ["log", "--oneline", f"{last_tag}..HEAD"])
        range_text = f"{last_tag}..HEAD"
    else:
        out, err = _git(repo, ["log", "--oneline", "-n", "50"])
        range_text = "HEAD（无历史 tag：取最近 50 条提交）"
    commits = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return {
        "last_tag": last_tag,
        "range": range_text,
        "commits": commits,
        "error": err,
    }


def build_plan(repo: Path, base_version: str | None = None) -> dict:
    """生成完整 dry-run 计划（纯读：只读 git tag / git log）。"""
    # 参数错误在访问 git 之前失败（argparse 退出码 2，不做任何子进程调用）
    if base_version is not None and parse_stable(f"v{base_version.strip()}") is None:
        raise ValueError(f"--base-version 需形如 X.Y.Z（收到 {base_version!r}）")

    tags_text, err = _git(repo, ["tag", "--list"])
    if err:
        return {"error": err}

    tags = [t.strip() for t in tags_text.splitlines() if t.strip()]
    canary_tags = sorted(
        (t for t in tags if parse_canary(t)), key=lambda t: _tag_key(t)
    )
    latest = latest_tag(tags)
    release = next_canary(tags, base_version)

    changelog = collect_changelog(repo, latest)
    if changelog["error"] and not changelog["commits"]:
        return {"error": changelog["error"]}

    next_tag = release["next_tag"]
    planned_actions = [
        {
            "step": 1,
            "action": "创建 annotated tag",
            "command": f'git tag -a {next_tag} -m "SGME canary {next_tag}"',
        },
        {
            "step": 2,
            "action": "推送 tag（先确认远端与权限）",
            "command": f"git push origin {next_tag}",
        },
        {
            "step": 3,
            "action": "发布 notes",
            "command": "把下方 changelog 草案贴到 Release notes（人工核对后）",
        },
    ]

    return {
        "mode": "dry-run",
        "read_only": True,
        "repo": str(repo),
        "git": {
            "available": True,
            "tag_count": len(tags),
            "version_tag_count": sum(1 for t in tags if _tag_key(t) is not None),
            "canary_tags": canary_tags,
            "latest_tag": latest,
        },
        "release": release,
        "changelog": changelog,
        "planned_actions": planned_actions,
        "notes": [
            "dry-run：计划动作仅打印，不创建 tag、不 push、不改任何文件",
            "默认基线 2.0.0（无 canary 历史）；可用 --base-version 显式指定",
        ],
    }


def run_gates(repo: Path, python_exe: str | None = None) -> dict:
    """依次运行三道发布门禁，汇总退出码；任一非 0 即 ok=False。"""
    py = python_exe or sys.executable
    results: list[dict] = []
    for name, rel_script, flags, desc in GATES:
        script = repo / rel_script
        cmd = [py, str(script), *flags]
        item = {
            "name": name,
            "script": rel_script,
            "args": list(flags),
            "description": desc,
            "command": cmd,
            "returncode": None,
            "ok": False,
            "error": None,
            "output_tail": [],
        }
        if not script.exists():
            item["error"] = f"门禁脚本不存在：{rel_script}"
            results.append(item)
            continue
        try:
            proc = _run(cmd, repo, GATE_TIMEOUT)
            text = _merge_out(proc)
            item["returncode"] = proc.returncode
            item["ok"] = proc.returncode == 0
            item["output_tail"] = [
                line for line in text.splitlines()[-JSON_TAIL_LINES:]
            ]
        except subprocess.TimeoutExpired:
            item["error"] = f"门禁超时（>{GATE_TIMEOUT}s）"
        except (OSError, subprocess.SubprocessError) as exc:
            item["error"] = f"门禁执行失败：{exc}"
        results.append(item)

    failed = sum(1 for r in results if not r["ok"])
    return {
        "ok": failed == 0,
        "passed": len(results) - failed,
        "failed": failed,
        "total": len(results),
        "gates": results,
    }


def render_text(report: dict, gates: dict | None = None) -> str:
    """人类可读的计划/门禁汇总。"""
    git = report["git"]
    release = report["release"]
    changelog = report["changelog"]
    lines = [
        "=" * 78,
        "SGME canary 发布计划（dry-run · 只读：不 tag / 不 push / 不改文件）",
        "=" * 78,
        f"仓库：{report['repo']}",
        f"git 标签：{git['tag_count']} 个（可解析版本 {git['version_tag_count']} 个，"
        f"canary {len(git['canary_tags'])} 个）",
        f"上一个 tag（changelog 起点）：{git['latest_tag'] or '（无）'}",
        "",
        f"下一个 canary tag：{release['next_tag']}",
        f"  基准版本 {release['base_version']} · 序号 {release['sequence']}"
        f" · 来源：{release['source_label']}",
        "",
        "计划动作（仅打印，本次不执行）：",
    ]
    for action in report["planned_actions"]:
        lines.append(f"  {action['step']}. {action['action']}：{action['command']}")

    lines.append("")
    if changelog["last_tag"]:
        lines.append(f"CHANGELOG 草案（git log --oneline {changelog['range']}）：")
    else:
        lines.append("CHANGELOG 草案（无历史 tag：取最近 50 条提交）：")
    if changelog["commits"]:
        lines.extend(f"  - {commit}" for commit in changelog["commits"])
    else:
        lines.append("  （无新提交）")
    if changelog["error"]:
        lines.append(f"  ⚠️ changelog 获取失败：{changelog['error']}")

    lines.append("")
    if gates is None:
        lines.append(
            "门禁：未执行（加 --check-gates 依次跑 adapter_parity --strict → "
            "check_adapter_freshness --strict-deploy → verify_decisions --strict）"
        )
        lines.append("dry-run 完成：未执行任何 tag/push/文件写入")
    else:
        lines.append(f"门禁（依次执行 · {sys.executable}）：")
        for gate in gates["gates"]:
            rc = gate["returncode"] if gate["returncode"] is not None else "N/A"
            mark = "✅" if gate["ok"] else "❌"
            lines.append(f"  {mark} {gate['name']} {' '.join(gate['args'])}（exit {rc}）")
            if not gate["ok"]:
                if gate["error"]:
                    lines.append(f"      {gate['error']}")
                lines.extend(
                    f"      {line}" for line in gate["output_tail"][-TEXT_TAIL_LINES:]
                )
        lines.append("")
        if gates["ok"]:
            lines.append(
                "✅ 门禁全绿，可发布（本次仍为 dry-run：未创建 tag、未 push、未改文件）"
            )
        else:
            lines.append(
                f"❌ 门禁未通过（{gates['failed']}/{gates['total']} 失败）——不要发布"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="canary 发布 dry-run 计划（只读）")
    ap.add_argument(
        "--check-gates",
        action="store_true",
        help="依次运行三道发布门禁（任一失败 → 退出码 1，且不宣告可发布）",
    )
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--repo", default=str(ROOT), help="仓库根（默认本脚本所在仓库）")
    ap.add_argument(
        "--base-version",
        default=None,
        help="显式指定基准版本 X.Y.Z（默认沿用 canary 历史，无历史则 2.0.0）",
    )
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()

    try:
        report = build_plan(repo, base_version=args.base_version)
    except ValueError as exc:
        ap.error(str(exc))  # argparse 统一以退出码 2 结束

    if report.get("error"):
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "read_only": True,
                        "repo": str(repo),
                        "error": report["error"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"❌ 无法生成发布计划：{report['error']}")
        return 1

    gates = run_gates(repo) if args.check_gates else None
    payload = dict(report)
    payload["gates"] = gates
    payload["gates_ok"] = None if gates is None else gates["ok"]
    payload["release_ready"] = None if gates is None else bool(gates["ok"])

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, gates))

    if gates is not None and not gates["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
