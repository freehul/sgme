# -*- coding: utf-8 -*-
"""scripts/migrate_stage3_backfill.py：D-55 阶段 3 回填编排（默认 dry-run）。

顺序（设计 §9.2 阶段 3 / §9.3 规则）：
1. supersession 收口：1.x 的「已被…替代」rejected 行 → status=active +
   lifecycle=archived + superseded_by（D-56；缺指针进清单不猜）；
2. meaning_anchor 回填（D-50，首轮算 M）；
3. scene_key 回填（D-53；unknown 占比对照阶段 0 基线，导出清单）；
4. 知识库 2.0 回填（修订史 / page_type / category_class；category 迁移需
   显式 --migrate-category，见 C-10）。

只读优先：默认全部 dry-run；--apply 才写库。每步独立失败不吞错。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _run(name: str, argv: list[str]) -> dict:
    cmd = [sys.executable, str(SCRIPTS / name), *argv]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return {
        "step": name,
        "exit": proc.returncode,
        "stdout_tail": (proc.stdout or "").strip().splitlines()[-3:],
        "stderr_tail": (proc.stderr or "").strip().splitlines()[-3:],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-55 阶段 3 回填编排（默认 dry-run）")
    ap.add_argument("--data-dir", default=None, help="数据目录（缺省用 config.DATA_DIR）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--skip-wiki", action="store_true", help="跳过知识库面回填")
    ap.add_argument("--skip-supersession", action="store_true", help="跳过收口步骤")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    data_dir = args.data_dir or str(ROOT / "data")
    steps: list[dict] = []

    # 1) supersession 收口
    if not args.skip_supersession:
        from sgme.data import db as db_mod
        from sgme.data import memory_dao

        conn = db_mod.connect_memory(Path(data_dir))
        try:
            report = memory_dao.close_supersession_rejected(conn, apply=args.apply)
        finally:
            conn.close()
        steps.append({"step": "supersession_closeout", "exit": 0, **report})

    # 2) meaning_anchor
    common = ["--data-dir", data_dir]
    apply_arg = ["--apply"] if args.apply else []
    steps.append(_run("backfill_meaning_anchor.py", common + apply_arg))
    # 3) scene_key（导出 unknown 清单，阶段 3 交付物）
    stamp = datetime.now().strftime("%Y%m%d")
    unknown_out = ROOT / "docs" / "migration" / f"2.0-scene-unknown-{stamp}.json"
    unknown_out.parent.mkdir(parents=True, exist_ok=True)
    steps.append(_run("backfill_scene_key.py",
                      common + apply_arg + ["--export-unknown", str(unknown_out)]))
    # 4) 知识库面
    if not args.skip_wiki:
        unc_out = ROOT / "docs" / "migration" / f"2.0-wiki-unclassified-{stamp}.json"
        steps.append(_run("backfill_wiki_2_0.py",
                          common + apply_arg + ["--export-unclassified", str(unc_out)]))

    failed = [s for s in steps if int(s.get("exit", 0)) != 0]
    summary = {
        "apply": args.apply,
        "data_dir": data_dir,
        "steps": steps,
        "failed": len(failed),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"阶段 3 回填编排：{'APPLY' if args.apply else 'DRY-RUN'}（data_dir={data_dir}）")
        for s in steps:
            print(f"- {s['step']}: exit={s.get('exit')} " +
                  (" ".join(s.get("stdout_tail") or []) if s.get("stdout_tail") else ""))
        print(f"失败步骤：{len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
