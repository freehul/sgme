#!/usr/bin/env python3
"""角色离线生活数据工具（2026-11-02 任务 B 步 1/步 4 辅助）。

用途：
1. ``seed``   —— 给管家灌一批「自己的日子」（幂等：同内容不重复写）
2. ``list``   —— 看某角色身上有哪些生活事件/秘密
3. ``add``    —— 手工加一条（联调/演示用）
4. ``secrets``—— 看还有哪些没说出口的秘密（模拟「被问起才说」的取数路径）

数据落 ``role_life_events``（memory.db 内独立表），**永不进 memories**。
内容纪律：只写类型级描述（「看到有人在争论本地模型的显存调优」），
禁专名、禁数字断言——编到专名级，用户一句追问就穿帮。

用法：
    python scripts/role_life.py seed --role butler
    python scripts/role_life.py list --role butler
    python scripts/role_life.py add --role butler --kind see --content "去了趟旧书摊" --mood "安静"
    python scripts/role_life.py secrets --role butler
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgme.data import db as db_mod
from sgme.data import role_life_dao

# 管家（butler）的示例生活：类型级、含糊但具体、指得回人设。
# 3 条可注入的日常 + 2 条秘密（永不主动提，被问才说）。
BUTLER_SEED: list[dict] = [
    {
        "kind": "see",
        "content": "路过旧书摊翻了半天，看到一本讲小城邮局的老册子，站着看了很久",
        "mood": "有点说不上来的感慨",
    },
    {
        "kind": "think",
        "content": "听人聊起现在都用手机记事，想起自己还是拿纸笔记账",
        "mood": "没觉得过时",
    },
    {
        "kind": "browse",
        "content": "看到有人在争论本地模型的显存怎么分配，本来跟自己的事没关系，听出火气来了",
        "mood": "想插嘴又忍住了",
    },
    {
        "kind": "wonder",
        "content": "早上跑完步在楼道口坐了一会儿，想起以前值夜班的那些晚上",
        "mood": "安静",
    },
    {
        "kind": "think",
        "content": "前几天在以前做事的那条街上绕了一圈，到门口没进去",
        "why_secret": "还没想明白该不该去看",
        "is_secret": True,
    },
    {
        "kind": "think",
        "content": "有件事一直没跟人提：这些年夜里醒来，头一件事是听动静",
        "why_secret": "说了像在讨人同情",
        "is_secret": True,
    },
]

SEEDS: dict[str, list[dict]] = {"butler": BUTLER_SEED}


def _conn():
    return db_mod.connect_memory()


def cmd_seed(args) -> int:
    seeds = SEEDS.get(args.role)
    if not seeds:
        print(f"没有 {args.role} 的预置数据（现有: {sorted(SEEDS)}）", file=sys.stderr)
        return 2
    conn = _conn()
    try:
        existing = {e["content"] for e in role_life_dao.list_events(conn, args.role, limit=500)}
        added = skipped = 0
        for item in seeds:
            if item["content"] in existing:
                skipped += 1
                continue
            role_life_dao.add_event(
                conn, args.role, item["content"],
                kind=item.get("kind", "see"),
                mood=item.get("mood"),
                why_secret=item.get("why_secret"),
                is_secret=bool(item.get("is_secret")),
            )
            added += 1
        print(f"灌入 {added} 条，跳过已存在 {skipped} 条（角色 {args.role}）")
        return 0
    finally:
        db_mod.close(conn)


def cmd_list(args) -> int:
    conn = _conn()
    try:
        items = role_life_dao.list_events(
            conn, args.role, limit=args.limit,
            only_secret=True if args.only_secret else (False if args.public_only else None),
        )
        if not items:
            print("（空）")
            return 0
        for e in items:
            flag = "秘密" if e["is_secret"] else "日常"
            injected = e["injected_at"] or "-"
            shared = e["shared_at"] or "-"
            print(f"[{flag}] {e['at']} {e['kind']:6s} {e['content']}")
            print(f"        情绪={e['mood'] or '-'} 已注入={injected} 已说出口={shared}")
        print(f"共 {len(items)} 条")
        return 0
    finally:
        db_mod.close(conn)


def cmd_add(args) -> int:
    conn = _conn()
    try:
        eid = role_life_dao.add_event(
            conn, args.role, args.content,
            kind=args.kind, mood=args.mood,
            why_secret=args.why_secret, is_secret=args.secret,
        )
        print(f"已写入 {eid}")
        return 0
    finally:
        db_mod.close(conn)


def cmd_secrets(args) -> int:
    """模拟「被问起才说」：取一条还没说出口的秘密。"""
    conn = _conn()
    try:
        item = role_life_dao.pick_secret(conn, args.role)
        if not item:
            print("（没有还没说出口的秘密）")
            return 0
        print(f"{item['event_id']}  {item['content']}")
        print(f"当时没说，是因为：{item['why_secret'] or '（未记录）'}")
        return 0
    finally:
        db_mod.close(conn)


def main() -> int:
    ap = argparse.ArgumentParser(description="角色离线生活数据工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seed", help="灌入预置生活数据（幂等）")
    p.add_argument("--role", default="butler")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("list", help="列出生活事件")
    p.add_argument("--role", default="butler")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--only-secret", action="store_true", help="只看秘密")
    p.add_argument("--public-only", action="store_true", help="只看可注入的日常")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="手工加一条")
    p.add_argument("--role", default="butler")
    p.add_argument("--content", required=True)
    p.add_argument("--kind", default="see", choices=sorted(role_life_dao.LIFE_KINDS))
    p.add_argument("--mood", default=None)
    p.add_argument("--why-secret", default=None)
    p.add_argument("--secret", action="store_true")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("secrets", help="取一条未说出口的秘密（调试）")
    p.add_argument("--role", default="butler")
    p.set_defaults(func=cmd_secrets)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
