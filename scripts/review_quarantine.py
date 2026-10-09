"""隔离表人工复核 CLI（L1.5 F1，P1 验收⑥）。

四个子命令：
  list [--status {pending,discarded,restored}] [--limit N] [--offset N]
      列出隔离行（按 id 倒序）
  show <id>
      查看单条隔离行全文（含 source_ref / dimension_ids / 补录追溯）
  mark-discarded <id>
      标记丢弃（仅 pending 可标记；隔离表只留痕不物理删除）
  restore <id> --reason <text>
      旁路补录：走 near_dup_dao.restore（全字段写入 memories + 向量即时回填）；
      embed 不可达只告警不失败（无参重跑 scripts/backfill_vectors.py 兜底）

用法（项目根）：
  .venv/Scripts/python scripts/review_quarantine.py list --status pending
  .venv/Scripts/python scripts/review_quarantine.py show 3
  .venv/Scripts/python scripts/review_quarantine.py mark-discarded 2
  .venv/Scripts/python scripts/review_quarantine.py restore 5 --reason "复核判定为新事实"

退出码：0=成功；1=业务未完成（id 不存在 / 状态拒绝 / 补录拒绝）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sgme.config as sgme_config
from sgme.data import near_dup_dao
from sgme.data.db import connect_memory


def _clip(text: str, width: int = 60) -> str:
    """列表视图单行截断（防刷屏；全文看 show）。"""
    one = " ".join((text or "").split())
    return one if len(one) <= width else one[:width] + "…"


def cmd_list(args) -> int:
    conn = connect_memory()
    try:
        items = near_dup_dao.list_items(
            conn, status=args.status, limit=args.limit, offset=args.offset
        )
    finally:
        conn.close()
    scope = f"status={args.status}" if args.status else "全部状态"
    if not items:
        print(f"无隔离记录（{scope}）")
        return 0
    print(f"隔离记录 {len(items)} 条（{scope}；limit={args.limit} offset={args.offset}，按 id 倒序）：")
    for it in items:
        print(
            f"  #{it['id']:<4} [{it['status']:<9}] score={it['score']:.4f} "
            f"matched={it['matched_memory_id']} ref={it.get('source_ref') or '-'} "
            f"at={it['created_at']}"
        )
        print(f"        {_clip(it['content'])}")
    print("提示：show <id> 看全文；mark-discarded <id> 丢弃；restore <id> --reason <text> 补录")
    return 0


def cmd_show(args) -> int:
    conn = connect_memory()
    try:
        item = near_dup_dao.get_item(conn, args.id)
    finally:
        conn.close()
    if item is None:
        print(f"隔离行不存在: id={args.id}", file=sys.stderr)
        return 1
    print(f"id            : {item['id']}")
    print(f"status        : {item['status']}")
    print(f"score         : {item['score']:.6f}")
    print(f"matched       : {item['matched_memory_id']}")
    print(f"source_ref    : {item.get('source_ref') or '-'}")
    print(f"dimension_ids : {item['dimension_ids']}")
    print(f"created_at    : {item['created_at']}")
    if item.get("restored_memory_id"):
        print(f"restored_to   : {item['restored_memory_id']}")
        print(f"restore_rsn   : {item.get('restore_reason') or '-'}")
    print("content:")
    print(item["content"])
    return 0


def cmd_mark_discarded(args) -> int:
    conn = connect_memory()
    try:
        ok = near_dup_dao.mark_discarded(conn, args.id)
    finally:
        conn.close()
    if not ok:
        print(
            f"标记失败: id={args.id} 不存在或非 pending（仅 pending 可标记丢弃）",
            file=sys.stderr,
        )
        return 1
    print(f"已标记丢弃: 隔离行 #{args.id} → status=discarded")
    return 0


def cmd_restore(args) -> int:
    cfg = sgme_config.load_config()  # 补录的 TTL 回填需要 dimensions 注册表
    conn = connect_memory()
    try:
        try:
            new_id = near_dup_dao.restore(conn, args.id, args.reason, cfg)
        except ValueError as e:
            print(f"补录拒绝: {e}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"已补录: 隔离行 #{args.id} → memory_id={new_id}（status=restored）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="L1.5 近似查重隔离表人工复核（list / show / mark-discarded / restore）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出隔离行（按 id 倒序）")
    p_list.add_argument("--status", choices=("pending", "discarded", "restored"),
                        default=None, help="按状态过滤（缺省全部）")
    p_list.add_argument("--limit", type=int, default=50, help="返回条数上限（缺省 50）")
    p_list.add_argument("--offset", type=int, default=0, help="偏移（缺省 0）")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看单条隔离行全文")
    p_show.add_argument("id", type=int, help="隔离行 id")
    p_show.set_defaults(func=cmd_show)

    p_discard = sub.add_parser("mark-discarded", help="标记丢弃（仅 pending 可标记）")
    p_discard.add_argument("id", type=int, help="隔离行 id")
    p_discard.set_defaults(func=cmd_mark_discarded)

    p_restore = sub.add_parser("restore", help="旁路补录（全字段写入 memories + 向量即时回填）")
    p_restore.add_argument("id", type=int, help="隔离行 id")
    p_restore.add_argument("--reason", required=True, help="补录理由（写入 restore_reason）")
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
