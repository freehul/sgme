#!/usr/bin/env python
"""scripts/publish_adapter_skills.py：把官方适配器打包进技能库（A2 分发通道）。

真源 = ``adapters/<host>/``（只读）；分发包 = ``skills/adapter-<host>/``（可重建）。
agent 侧路径：``skill_search`` → ``skill_get`` → ``skill_materialize`` 落盘完整包。

打包布局（保证 materialize 后可直接 ``install.py``）::

    skills/adapter-<host>/
      SKILL.md          # frontmatter name=adapter-<host>（技能库唯一名）
      install.py        # 若真源有
      scripts/          # *.py
      locales/          # *.json
      package/          # hermes 插件本体 / dsh sgme-bridge 等
      references/       # README.md

幂等：目标目录先清再写；重复跑结果一致。排除 junk（__pycache__/.env/tests/.tmp-*）。

用法::

    python scripts/publish_adapter_skills.py            # 全部适配器
    python scripts/publish_adapter_skills.py workbuddy  # 指定宿主
    python scripts/publish_adapter_skills.py --check    # 只校验不写盘
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTERS = ROOT / "adapters"
SKILLS = ROOT / "skills"

# 六官方适配器（与 AGENTS.md 平级条款一致）
HOSTS = ("hermes", "dsh", "doubao", "mimo", "workbuddy", "zcode")

# 打包时保留的顶层条目（目录或文件名）；其余进 junk 过滤
KEEP_TOP = {
    "SKILL.md",
    "install.py",
    "import_history.py",
    "scripts",
    "locales",
    "package",
    "references",
    "assets",
    "plugin.yaml",
    "config_schema.py",
    "__init__.py",
    "README.md",
}

# 真源里「插件本体 / 桥接源码」目录 → 分发包 package/
PACKAGE_DIRS = {
    "hermes": (),  # hermes 插件文件是平铺的 __init__.py 等，走 KEEP_TOP
    "dsh": ("sgme-bridge",),
}

# junk：永不打包
JUNK_NAMES = {
    "__pycache__",
    ".env",
    ".gitignore",
    ".git",
    "node_modules",
    ".pytest_cache",
    ".tmp-events-test",
    "tests",
    "coverage",
    "htmlcov",
}
JUNK_SUFFIXES = (".pyc", ".pyo", ".log", ".env.bak")

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _is_junk(path: Path) -> bool:
    parts = set(path.parts)
    if parts & JUNK_NAMES:
        return True
    return path.name.endswith(JUNK_SUFFIXES) or path.name in JUNK_NAMES


def _rewrite_skill_md(text: str, host: str) -> str:
    """改写 frontmatter name 为 adapter-<host>，补 category/tags；正文原样保留。"""
    name = f"adapter-{host}"
    m = _FM_RE.match(text or "")
    if not m:
        head = (
            f"---\n"
            f"name: {name}\n"
            f"description: {host} 的 SGME 官方适配器分发包。\n"
            f"category: adapter\n"
            f"tags: [skill, adapter, sgme]\n"
            f"---\n\n"
        )
        return head + (text or "")
    meta = m.group(1)
    body = text[m.end() :]
    # name 一律覆盖为分发名（避免与客户端侧技能名 sgme/mimo 冲突）
    if re.search(r"(?m)^name:\s*", meta):
        meta = re.sub(r"(?m)^name:\s*.*$", f"name: {name}", meta, count=1)
    else:
        meta = f"name: {name}\n" + meta
    if not re.search(r"(?m)^category:\s*", meta):
        meta = meta.rstrip() + "\ncategory: adapter\n"
    if not re.search(r"(?m)^tags:\s*", meta):
        meta = meta.rstrip() + "\ntags: [skill, adapter, sgme]\n"
    return f"---\n{meta}\n---\n{body}"


def _copy_tree(src: Path, dest: Path) -> int:
    """递归拷贝（跳过 junk）；返回拷贝文件数。"""
    n = 0
    if src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return 1
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if _is_junk(rel):
            continue
        target = dest / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif p.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            n += 1
    return n


def publish_host(host: str, *, check_only: bool = False) -> dict:
    src_root = ADAPTERS / host
    if not src_root.is_dir():
        return {"host": host, "ok": False, "error": f"适配器目录不存在: {src_root}"}

    skill_md = src_root / "SKILL.md"
    if not skill_md.is_file():
        return {"host": host, "ok": False, "error": f"真源缺 SKILL.md: {skill_md}"}

    dest_root = SKILLS / f"adapter-{host}"
    planned: list[str] = []

    text = skill_md.read_text(encoding="utf-8")
    rewritten = _rewrite_skill_md(text, host)
    planned.append("SKILL.md")

    # 顶层保留项
    copies: list[tuple[Path, Path]] = []
    for name in sorted(KEEP_TOP):
        if name == "SKILL.md":
            continue
        s = src_root / name
        if not s.exists():
            continue
        if _is_junk(Path(name)):
            continue
        # README → references/README.md（技能资产白名单）
        if name == "README.md":
            copies.append((s, dest_root / "references" / "README.md"))
            planned.append("references/README.md")
            continue
        copies.append((s, dest_root / name))
        planned.append(name if s.is_file() else f"{name}/")

    # 插件/桥接真源目录 → package/
    for pkg_name in PACKAGE_DIRS.get(host, ()):
        s = src_root / pkg_name
        if s.is_dir():
            copies.append((s, dest_root / "package" / pkg_name))
            planned.append(f"package/{pkg_name}/")

    if check_only:
        return {"host": host, "ok": True, "check_only": True, "dest": str(dest_root),
                "planned": planned}

    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    (dest_root / "SKILL.md").write_text(rewritten, encoding="utf-8")
    file_count = 1
    for s, d in copies:
        file_count += _copy_tree(s, d)

    return {
        "host": host,
        "ok": True,
        "dest": str(dest_root),
        "files": file_count,
        "planned": planned,
        "skill_name": f"adapter-{host}",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包官方适配器进技能库（skills/adapter-<host>）")
    ap.add_argument("hosts", nargs="*", help="宿主名（默认全部）；如 workbuddy hermes")
    ap.add_argument("--check", action="store_true", help="只列出将打包内容，不写盘")
    args = ap.parse_args(argv)

    hosts = tuple(args.hosts) if args.hosts else HOSTS
    unknown = [h for h in hosts if h not in HOSTS]
    if unknown:
        print(f"未知适配器: {unknown}；可选: {list(HOSTS)}", file=sys.stderr)
        return 2

    ok = True
    for h in hosts:
        r = publish_host(h, check_only=args.check)
        if r.get("ok"):
            if args.check:
                print(f"[check] {h}: {r['planned']}")
            else:
                print(f"[ok] {h} → {r['dest']} ({r['files']} files, skill={r['skill_name']})")
        else:
            ok = False
            print(f"[fail] {h}: {r.get('error')}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
