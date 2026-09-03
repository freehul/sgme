#!/usr/bin/env python3
"""Verify a NON-editable sgme installation resolves all program resources from
the installed package (site-packages), not from a source checkout.

Motivation (Backlog T-142 / B150):
    Before T-142, `pip install .` produced a wheel with ZERO yaml/txt files, so
    the server crashed at startup with
        FileNotFoundError: 配置文件不存在: .../site-packages/config/llm.yaml
    The fix moved config/ registry/ templates/ prompts/ into `sgme/resources/`
    and declared them via [tool.setuptools.package-data]. This script is the
    regression gate for that class of bug.

Usage (run with the SAME interpreter that has sgme installed):

    # sdist
    python -m build --sdist --outdir /tmp/dist .
    python -m venv /tmp/v && /tmp/v/bin/pip install /tmp/dist/sgme-*.tar.gz
    cd /tmp && /tmp/v/bin/python /path/to/verify_installed_resources.py

    # wheel
    pip wheel . --no-deps -w /tmp/wh && pip install --no-deps /tmp/wh/*.whl
    cd /tmp && python /path/to/verify_installed_resources.py

IMPORTANT: run it from a directory OUTSIDE the source repo (e.g. /tmp), so that
`_config_overlay_dir()` cannot silently fall back to the repo's config/ dir and
mask a packaging defect. The script asserts this.

Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import sys
import tempfile

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "OK  " if ok else "FAIL"
    print(f"[{status}] {label}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    import sgme
    import sgme.config as config

    pkg_dir = pathlib.Path(sgme.__file__).resolve().parent
    print(f"sgme version    : {sgme.__version__}")
    print(f"package dir     : {pkg_dir}")
    print(f"cwd             : {os.getcwd()}")

    # 0) Must NOT be running against a source checkout / editable install.
    here = pathlib.Path.cwd().resolve()
    is_repo = (here / "pyproject.toml").exists() or (here / ".git").exists()
    check("cwd is outside a source repo (else overlay masks packaging bugs)", not is_repo, str(here))
    editable_markers = [pkg_dir.parent / p for p in ("sgme.egg-info", "sgme.egg-link", "__editable__")]
    check(
        "install is NOT editable",
        not any(m.exists() for m in editable_markers),
        ", ".join(m.name for m in editable_markers if m.exists()) or "no editable markers",
    )

    # 1) Resource root lives inside the installed package.
    root = pathlib.Path(config.RESOURCE_ROOT)
    check("RESOURCE_ROOT exists", root.exists(), str(root))
    check("RESOURCE_ROOT is inside the installed package", pkg_dir in root.parents, f"{root} under {pkg_dir}")

    # 2) Read-only defaults load from the bundle.
    llm = config.load_llm_config()
    chains = list(llm.get("chains", {}).keys()) if isinstance(llm, dict) else []
    check("load_llm_config() returns chains", bool(chains), f"chains={chains}")

    providers = config.load_providers_config()
    check(
        "load_providers_config() returns providers",
        isinstance(providers, dict) and bool(providers),
        f"providers={sorted(providers)[:6] if isinstance(providers, dict) else providers}",
    )

    dims = config.load_dimensions()
    check("load_dimensions() returns the registry", len(dims) > 0, f"count={len(dims)}")

    sg = config.load_sgme_config()
    check("load_sgme_config() returns config", isinstance(sg, dict) and bool(sg), f"keys={len(sg) if isinstance(sg, dict) else 0}")

    # 3) Templates / prompts shipped.
    from sgme.profile.template import TEMPLATES_DIR
    from sgme.prompts.manager import PromptStore

    templates = sorted(p.name for p in TEMPLATES_DIR.glob("*.yaml"))
    check("TEMPLATES_DIR exists under the package", TEMPLATES_DIR.exists() and pkg_dir in TEMPLATES_DIR.parents, str(TEMPLATES_DIR))
    check("templates shipped", len(templates) > 0, f"{templates}")

    prompts_root = pathlib.Path(PromptStore.PROMPTS_ROOT)
    staged = sorted(p.name for p in prompts_root.glob("*.txt"))
    check("PROMPTS_ROOT exists under the package", prompts_root.exists() and pkg_dir in prompts_root.parents, str(prompts_root))
    check("prompts shipped", len(staged) > 0, f"{len(staged)} txt files")
    check("prompt manifest shipped", (prompts_root / "manifest.yaml").exists())

    # 4) Overlay resolution: with no SGME_HOME and outside a repo, the writable
    #    overlay must fall back to ~/.sgme/config (read-only install branch).
    overlay = config._config_overlay_dir()
    check("overlay falls back to ~/.sgme/config when no SGME_HOME", overlay.name == "config" and overlay.parent.name == ".sgme", str(overlay))

    # 5) Write path: with SGME_HOME set, writes must land in the overlay and an
    #    existing `embedding` section must be preserved (T-142 contract).
    home = tempfile.mkdtemp(prefix="sgme_verify_")
    os.environ["SGME_HOME"] = home
    config = importlib.reload(config)
    overlay = config._config_overlay_dir()
    check("overlay honours SGME_HOME", str(overlay).startswith(home), str(overlay))

    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "providers.yaml").write_text(
        "providers:\n  legacy:\n    name: legacy\n    base_url: http://legacy\n"
        "embedding:\n  provider: siliconflow\n  model: BAAI/bge-m3\n",
        encoding="utf-8",
    )
    written = pathlib.Path(
        config.write_providers_config({"smoke-test": {"base_url": "http://127.0.0.1:1", "model": "smoke"}})
    )
    check("write_providers_config() lands in the overlay", str(written).startswith(home) and written.exists(), str(written))
    if written.exists():
        text = written.read_text(encoding="utf-8")
        check("new provider written", "smoke-test" in text)
        check("existing `embedding` section preserved", "BAAI/bge-m3" in text)
        check("`providers` replaced wholesale (by design)", "legacy" not in text)
        back = config.load_providers_config()
        check("write then read-back is consistent", "smoke-test" in back, f"keys={sorted(back)[:6]}")

    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} check(s)) -> {FAILURES}")
        return 1
    print("RESULT: PASS — installed package ships and resolves all program resources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
