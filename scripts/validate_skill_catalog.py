"""Validate the checked-in SGME skill catalog.

This is intentionally small and dependency-light: the runtime indexer remains the
source of truth for parsing, while this command checks repository-level invariants.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from sgme.skills.indexer import parse_skill_md

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    names: dict[str, Path] = {}
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        path = directory / "SKILL.md"
        if not path.is_file():
            continue
        name = directory.name
        if not NAME_RE.fullmatch(name):
            errors.append(f"{directory}: name must be lowercase kebab-case")
        if name in names:
            errors.append(f"duplicate directory name: {name}")
        names[name] = directory
        parsed = parse_skill_md(path.read_text(encoding="utf-8"))
        meta = parsed["meta"]
        if meta.get("name") != name:
            errors.append(f"{path}: frontmatter name must be {name!r}")
        if not str(meta.get("description") or "").strip():
            errors.append(f"{path}: description is required")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", nargs="?", type=Path, default=Path(__file__).parents[1] / "skills"
    )
    args = parser.parse_args()
    errors = validate(args.root.resolve())
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"skill catalog valid: {args.root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
