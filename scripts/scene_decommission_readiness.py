# -*- coding: utf-8 -*-
"""scripts/scene_decommission_readiness.py：9.6 场景层下线 / enforce_gone 开启前置清单（只读）。

用途：在把 compat.enforce_gone 打开（GONE 端点统一 410）之前，一次看清——
1. GONE / CONFLICT 处置清单（直接读 sgme.compat，不复制规则）；
2. 兼容期现状（compat_phase：stable_since + window_days → phase / days_left）；
3. 库里场景数据量（复用 sgme.scene_scan.scan_db，以 mode=ro 打开 memory.db）；
4. 「开 enforce_gone 前置清单」+ 建议配置片段（**只打印，不代改**）。

设计边界：
- 配置一律从 sgme.config.load_config() 读（与 Gateway 同源），不读第二套；
- 本脚本**不写** config/sgme.yaml、不改 memory.db、不删场景行、不调 config_update；
  打开 enforce_gone 是生产显式决策，需人工改配置 + 维护窗口 + 回滚方案。

用法：
  .venv\\Scripts\\python.exe scripts\\scene_decommission_readiness.py
  .venv\\Scripts\\python.exe scripts\\scene_decommission_readiness.py --json
  .venv\\Scripts\\python.exe scripts\\scene_decommission_readiness.py --data-dir <数据目录>

退出码：0 = 报告生成成功（无论是否就绪）；1 = 配置加载失败。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 脚本模式（python scripts/xxx.py）不保证仓库根在 sys.path
    sys.path.insert(0, str(ROOT))

# 场景层相关表（9.6 四表冻结口径；memory_edges/memory_pages 只作上下文）
SCENE_TABLES = ("scenes", "scene_vectors", "scenes_fts")

# 建议配置片段：只打印，绝不落盘
SUGGESTED_YAML = """compat:
  enforce_gone: true
"""


def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK：中文/emoji 直接 print 会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _load_config():
    """加载 Gateway 同源配置。

    load_config() 会向 stdout 打印「加载完成」摘要——重定向到 stderr，
    保证 --json 模式下 stdout 只有一段可解析 JSON。
    """
    from sgme.config import load_config

    with contextlib.redirect_stdout(sys.stderr):
        return load_config()


def _table_rows(tables: dict, name: str) -> int | None:
    """取某表行数；表不存在返回 None（区分「0 行」与「没有这张表」）。"""
    info = tables.get(name)
    if not isinstance(info, dict) or not info.get("exists"):
        return None
    rows = info.get("rows")
    return int(rows) if isinstance(rows, int) else None


def build_report(cfg: dict | None = None, data_dir: str | Path | None = None) -> dict:
    """汇总「开 enforce_gone 前置清单」（只读；不写任何文件）。"""
    from sgme import compat
    from sgme.scene_scan import scan_db

    cfg = cfg if cfg is not None else _load_config()
    compat_cfg = dict(cfg.get("compat") or {})
    phase = compat.compat_phase(cfg)

    default_data_dir = (cfg.get("paths") or {}).get("data_dir")
    resolved_data_dir = Path(data_dir) if data_dir else Path(
        default_data_dir or (ROOT / "data")
    )
    db_report = scan_db(resolved_data_dir)
    tables = db_report.get("tables") or {}

    scene_rows = {name: _table_rows(tables, name) for name in SCENE_TABLES}
    known_rows = [rows for rows in scene_rows.values() if rows is not None]
    total_rows = sum(known_rows) if known_rows else None
    clear = (total_rows == 0) if total_rows is not None else None

    gone_rules = [
        {"method": method, "pattern": pattern.pattern, "upgrade_hint": hint}
        for method, pattern, hint in compat.GONE_RULES
    ]
    conflict_rules = [
        {"method": method, "pattern": pattern.pattern, "upgrade_hint": hint}
        for method, pattern, hint in compat.CONFLICT_RULES
    ]

    enforce = bool(compat_cfg.get("enforce", False))
    enforce_gone = bool(compat_cfg.get("enforce_gone", False))

    # 前置清单：done = 已满足，action = 需要人工动作，info = 现状说明
    prerequisites = [
        {
            "item": "GONE 规则已登记",
            "status": "done",
            "detail": f"sgme.compat.GONE_RULES 共 {len(gone_rules)} 条（D-64 ③）",
            "action": "",
        },
        {
            "item": "场景数据已盘点",
            "status": "done" if db_report.get("available") else "action",
            "detail": (
                f"memory.db 可读：{db_report.get('path')}"
                if db_report.get("available")
                else f"未找到 memory.db：{db_report.get('path')}（用 --data-dir 指定数据目录）"
            ),
            "action": "" if db_report.get("available") else "确认生产数据目录后重跑 --data-dir",
        },
        {
            "item": "场景四表已冻结 / 已备份",
            "status": (
                "done"
                if total_rows == 0
                else ("action" if total_rows is not None else "action")
            ),
            "detail": (
                f"场景表仍有 {total_rows} 行（scenes / scene_vectors / scenes_fts）："
                "下线前先备份或打 deprecated_2_0 标记（不删行）"
                if total_rows
                else (
                    "场景表无数据，可直接进入配置开关"
                    if total_rows == 0
                    else "场景表行数未知（库不可读）"
                )
            ),
            "action": (
                "备份 memory.db 或给场景数据打 deprecated_2_0 标记（保留行，不删除）"
                if total_rows
                else ("" if total_rows == 0 else "先确认数据目录可读")
            ),
        },
        {
            "item": "兼容期阶段可判定",
            "status": "done" if phase.get("phase") != "unknown" else "action",
            "detail": (
                f"phase={phase.get('phase')}｜stable_since={phase.get('stable_since') or '未设置'}"
                f"｜window_days={phase.get('window_days')}｜days_left={phase.get('days_left')}"
            ),
            "action": (
                ""
                if phase.get("phase") != "unknown"
                else "修正 compat.stable_since 为 YYYY-MM-DD（只读脚本不代改）"
            ),
        },
        {
            "item": "enforce_gone 当前值已确认",
            "status": "done",
            "detail": (
                f"当前 enforce_gone={enforce_gone}、enforce={enforce}"
                "（409 是否开启是另一项决策，不要顺手一起打开）"
            ),
            "action": "",
        },
        {
            "item": "生产窗口 + 回滚方案",
            "status": "action",
            "detail": (
                "打开 enforce_gone 属生产显式决策：需维护窗口、通知现役适配器、"
                "并确认回滚动作（改回 false + 重启 Gateway）"
            ),
            "action": "安排维护窗口并确认回滚步骤（本脚本不代改配置）",
        },
        {
            "item": "开启后 410 验证",
            "status": "action" if not enforce_gone else "info",
            "detail": "按「开启后验证」四条逐项核对（/v1/capabilities、两个 GONE 端点、只读列表）",
            "action": "" if enforce_gone else "开关打开并重启后执行验证清单",
        },
    ]
    summary = {
        "done": sum(1 for p in prerequisites if p["status"] == "done"),
        "action": sum(1 for p in prerequisites if p["status"] == "action"),
        "info": sum(1 for p in prerequisites if p["status"] == "info"),
    }

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "read_only": True,
        "config_write": "refused",
        "data_dir": str(resolved_data_dir.resolve()),
        "compat": {
            "api_family": compat_cfg.get("api_family"),
            "stable_since": compat_cfg.get("stable_since") or None,
            "window_days": compat_cfg.get("window_days"),
            "enforce": enforce,
            "enforce_gone": enforce_gone,
            "phase": phase,
            "phase_name": phase.get("phase"),
        },
        "gone_rules": {"count": len(gone_rules), "rules": gone_rules},
        "conflict_rules": {"count": len(conflict_rules), "rules": conflict_rules},
        "scene_data": {
            "available": bool(db_report.get("available")),
            "path": db_report.get("path"),
            "tables": tables,
            "scene_rows": scene_rows,
            "total_rows": total_rows,
            "clear": clear,
            "edge_sources": db_report.get("edge_sources"),
            "page_kinds": db_report.get("page_kinds"),
        },
        "prerequisites": prerequisites,
        "checklist_summary": summary,
        "suggested_config": {
            "file": "config/sgme.yaml",
            "yaml": SUGGESTED_YAML,
            "apply": "manual",
            "note": (
                "本脚本只打印建议，不写配置；人工编辑后用 Gateway 重启生效。"
                "compat.enforce（409）与 enforce_gone（410）是两项独立决策。"
            ),
        },
        "verify_steps": [
            "GET /v1/capabilities → enforcement.gone == true",
            "POST /v1/admin/scenes/<scene_id>/status → 410 endpoint_gone（带 upgrade_hint）",
            "POST /v1/admin/scene-gc/trigger → 410 endpoint_gone",
            "GET /v1/admin/scenes → 仍 200（只读列表保留，D-64 ②）",
        ],
        "refusal": (
            "本脚本只读：不写 config/sgme.yaml、不改 memory.db、不删场景行、"
            "不调 config_update；配置变更由人工执行。"
        ),
        "note": (
            "配置读取走 sgme.config.load_config()（与 Gateway 同源）；"
            "场景行数走 sgme.scene_scan.scan_db()（sqlite mode=ro）。"
        ),
    }


def render_text(report: dict) -> str:
    """人类可读的前置清单。"""
    compat = report["compat"]
    phase = compat["phase"]
    scene = report["scene_data"]
    rows = scene["scene_rows"]
    lines = [
        "=" * 78,
        "SGME 9.6 场景层下线 / enforce_gone 前置清单（只读 · 拒绝代改配置）",
        "=" * 78,
        f"数据目录：{report['data_dir']}",
        "",
        "【兼容期现状】",
        f"  phase={compat['phase_name']}"
        f"（stable_since={compat['stable_since'] or '未设置'}，"
        f"window_days={phase.get('window_days')}，"
        f"days_left={phase.get('days_left')}）",
        f"  enforce={compat['enforce']}｜enforce_gone={compat['enforce_gone']}"
        f"｜api_family={compat['api_family']}",
        "",
        "【端点处置清单】",
        f"  GONE 规则 {report['gone_rules']['count']} 条"
        f"｜CONFLICT 规则 {report['conflict_rules']['count']} 条",
    ]
    for rule in report["gone_rules"]["rules"]:
        lines.append(f"  {rule['method']} {rule['pattern']}")

    lines.append("")
    lines.append("【场景数据量】")
    if scene["available"]:
        lines.append(
            f"  scenes={rows.get('scenes')}｜scene_vectors={rows.get('scene_vectors')}"
            f"｜scenes_fts={rows.get('scenes_fts')}｜合计 {scene['total_rows']} 行"
        )
        lines.append(
            f"  场景边来源：{scene.get('edge_sources') or '无'}｜"
            f"memory_pages 分类：{scene.get('page_kinds') or '无'}"
        )
    else:
        lines.append(f"  ⚠️ 库不可读：{scene.get('path')}（用 --data-dir 指定数据目录）")

    lines.append("")
    lines.append("【开启 enforce_gone 前置清单】")
    for item in report["prerequisites"]:
        mark = {"done": "[x]", "action": "[ ]", "info": "[i]"}.get(
            item["status"], "[?]"
        )
        lines.append(f"  {mark} {item['item']}：{item['detail']}")
        if item.get("action"):
            lines.append(f"      → {item['action']}")

    summary = report["checklist_summary"]
    lines.append(
        f"  小计：已满足 {summary['done']}｜待人工动作 {summary['action']}"
        f"｜现状说明 {summary['info']}"
    )

    suggestion = report["suggested_config"]
    lines.extend(
        [
            "",
            "【建议配置片段（只打印，不代改）】",
            f"  文件：{suggestion['file']}",
        ]
    )
    lines.extend(f"  {line}" for line in suggestion["yaml"].rstrip().splitlines())
    lines.append(f"  备注：{suggestion['note']}")

    lines.extend(["", "【开启后验证】"])
    lines.extend(
        f"  {index}. {step}" for index, step in enumerate(report["verify_steps"], 1)
    )

    lines.extend(["", f"【拒绝代改】{report['refusal']}"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(
        description="9.6 场景层下线 / enforce_gone 前置清单（只读）"
    )
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（缺省取配置 paths.data_dir；只读打开 memory.db）",
    )
    args = ap.parse_args(argv)

    try:
        report = build_report(data_dir=args.data_dir)
    except Exception as exc:  # noqa: BLE001 - CLI 兜底：配置/库异常给可读错误而非堆栈
        if args.json:
            print(
                json.dumps(
                    {
                        "read_only": True,
                        "config_write": "refused",
                        "error": f"生成前置清单失败：{exc}",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"❌ 生成前置清单失败：{exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
