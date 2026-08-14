"""scripts/prompts_cli.py：提示词版本管理命令行（#33）。

用法：
  python scripts/prompts_cli.py list [stage]
  python scripts/prompts_cli.py publish <stage> [--note 说明]
  python scripts/prompts_cli.py activate <stage> <version_ref>   # @working | vNNN
  python scripts/prompts_cli.py ab <stage> <a> <b> --split 0.5 [--bucket_by file_id]
  python scripts/prompts_cli.py ab <stage> --disable              # 关闭 A/B

示例：
  python scripts/prompts_cli.py publish l1_extraction --note "措辞优化"
  python scripts/prompts_cli.py activate l1_extraction v002
  python scripts/prompts_cli.py ab l1_extraction v001 v002 --split 0.5
"""

from __future__ import annotations

import argparse
import json
import sys

from sgme.prompts import PromptManifestError, PromptStore


def _store() -> PromptStore:
    return PromptStore()


def _dump(obj) -> None:
    """JSON 输出（中文保持可读）。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_list(args: argparse.Namespace) -> int:
    store = _store()
    if args.stage:
        stages = [args.stage]
    else:
        stages = ["tier0_summary", "l1_extraction", "l1_conflict", "l2_scene"]
    out = {"stages": []}
    for stage in stages:
        versions = [v.__dict__ for v in store.list_versions(stage)]
        # active 指向从 manifest 读
        try:
            manifest = store._load_manifest()
            scfg = manifest["stages"].get(stage, {})
            active = scfg.get("active", "@working")
            ab = scfg.get("ab") or {}
        except PromptManifestError as e:
            active = f"<manifest 错误: {e}>"
            ab = {}
        out["stages"].append({
            "stage": stage,
            "active": active,
            "ab_enabled": bool(ab.get("enabled")),
            "versions": versions,
        })
    _dump(out)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    store = _store()
    try:
        info = store.publish(args.stage, note=args.note)
    except PromptManifestError as e:
        print(f"发布失败: {e}", file=sys.stderr)
        return 1
    _dump(info.__dict__)
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    store = _store()
    try:
        store.activate(args.stage, args.version_ref)
    except PromptManifestError as e:
        print(f"激活失败: {e}", file=sys.stderr)
        return 1
    _dump({"status": "ok", "stage": args.stage, "active": args.version_ref})
    return 0


def cmd_ab(args: argparse.Namespace) -> int:
    store = _store()
    try:
        if args.disable:
            store.configure_ab(args.stage, "", "", 0.5, enabled=False)
            _dump({"status": "ok", "stage": args.stage, "ab_enabled": False})
        else:
            store.configure_ab(
                args.stage, args.a, args.b, args.split,
                bucket_by=args.bucket_by, enabled=True,
            )
            _dump({
                "status": "ok",
                "stage": args.stage,
                "ab_enabled": True,
                "a": args.a,
                "b": args.b,
                "split": args.split,
                "bucket_by": args.bucket_by,
            })
    except PromptManifestError as e:
        print(f"A/B 配置失败: {e}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SGME 提示词版本管理（#33）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出版本")
    p_list.add_argument("stage", nargs="?", default=None, help="stage 名（缺省列全部）")
    p_list.set_defaults(func=cmd_list)

    p_pub = sub.add_parser("publish", help="发布新版本（工作副本 → vNNN）")
    p_pub.add_argument("stage", help="stage 名")
    p_pub.add_argument("--note", default="", help="发布说明")
    p_pub.set_defaults(func=cmd_publish)

    p_act = sub.add_parser("activate", help="激活版本（@working 或 vNNN）")
    p_act.add_argument("stage", help="stage 名")
    p_act.add_argument("version_ref", help="@working / vNNN / versions/<stage>/vNNN.txt")
    p_act.set_defaults(func=cmd_activate)

    p_ab = sub.add_parser("ab", help="配置 A/B 分流")
    p_ab.add_argument("stage", help="stage 名")
    p_ab.add_argument("a", nargs="?", default=None, help="A 版本引用")
    p_ab.add_argument("b", nargs="?", default=None, help="B 版本引用")
    p_ab.add_argument("--split", type=float, default=0.5, help="A 流量占比 0.0~1.0")
    p_ab.add_argument("--bucket_by", default="file_id", help="file_id | memory_id | random")
    p_ab.add_argument("--disable", action="store_true", help="关闭 A/B")
    p_ab.set_defaults(func=cmd_ab)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
