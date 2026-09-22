# -*- coding: utf-8 -*-
"""
care_watch.py — MiMo Desktop主动关怀守护（可选组件）
==================================================

MiMo Desktop是会话型 agent（无常驻进程），主动关怀走官方「短连接」路径：
对话开始 signal_pull 拉未消费信号。本脚本把短连接变成「准常驻」——
定时轮询事件游标，发现 care_* 信号即输出可读提醒，供MiMo Desktop定时任务
捕获后推送给主人（兜底铁律：任何主动消息都必须同时在当前会话发一条）。

用法（脚本目录 = 部署副本 <MIMO_SKILLS_DIR>/sgme-bridge/scripts/）：
  python care_watch.py pull                  # 拉一次未消费信号（不认领，只展示）
  python care_watch.py pull --types care_*   # 只看关怀信号
  python care_watch.py watch --interval 300  # 常驻轮询（Ctrl+C 退出）
  python care_watch.py claim <event_id>      # 认领信号（关怀前，防重复打扰）
  python care_watch.py ack <event_id> acked  # 消费回执（acked/failed）
  python care_watch.py clear                 # 批量清空积压信号（幂等；管理操作）

配合MiMo Desktop定时任务：
  每小时执行：<project-root>/.venv/Scripts/python.exe care_watch.py pull
  有输出（非「暂无」）时，MiMo Desktop读取结果并：claim → 关怀主人 → ack
"""
from __future__ import annotations

import argparse
import sys
import time

from sgme_client import SGME, SGMEError


def fmt_pull(resp: dict, types_filter: str | None = "care_") -> str:
    """把 events_pull 结果格式化为可读清单；types_filter 为类型前缀（服务端不支持 types 过滤，须客户端筛）。"""
    items = resp.get("events") or resp.get("signals") or []
    if types_filter:
        items = [it for it in items if str(it.get("type") or it.get("event_type") or "").startswith(types_filter)]
    if not items:
        return "暂无未消费信号 ✅"
    lines = []
    for it in items:
        eid = it.get("event_id") or it.get("id") or "?"
        typ = it.get("type") or it.get("event_type") or "?"
        title = it.get("title") or it.get("summary") or it.get("content") or ""
        lines.append(f"[{typ}] {eid}  {title}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="SGME 主动关怀守护（MiMo Desktop）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pull", help="拉一次未消费信号（默认客户端过滤只看 care_*）")
    sp.add_argument("--types", default="care_", help="类型前缀过滤（客户端筛）；传空串看全部")
    sp.add_argument("--subscriber", default="mimo")

    sp = sub.add_parser("watch", help="常驻轮询")
    sp.add_argument("--interval", type=int, default=300, help="轮询间隔秒数")

    sp = sub.add_parser("claim", help="认领信号（关怀前）")
    sp.add_argument("event_id")

    sp = sub.add_parser("ack", help="消费回执")
    sp.add_argument("event_id")
    sp.add_argument("status", nargs="?", default="acked", choices=["acked", "failed"])

    sp = sub.add_parser("clear", help="批量清空未消费信号（幂等；管理操作，自动用管理员 Key）")
    sp.add_argument("--type", dest="signal_type", default=None, help="类型精确过滤（如 care_daily）")
    sp.add_argument("--subscriber", default="mimo", help="同步推进该订阅者游标")

    a = p.parse_args(argv)

    try:
        c = SGME()
        if a.cmd == "pull":
            print(fmt_pull(c.events_pull(a.subscriber), a.types or None))
        elif a.cmd == "watch":
            print(f"care_watch 常驻轮询开始（每 {a.interval}s）… Ctrl+C 退出")
            while True:
                try:
                    resp = c.events_pull("mimo")
                    text = fmt_pull(resp, "care_")
                    if "暂无" not in text:
                        print(f"[{time.strftime('%H:%M:%S')}]", text)
                except SGMEError as e:
                    print(f"[{time.strftime('%H:%M:%S')}] ❌ {e}", file=sys.stderr)
                time.sleep(a.interval)
        elif a.cmd == "claim":
            print(c.signal_claim(a.event_id))
        elif a.cmd == "ack":
            print(c.signal_ack(a.event_id, a.status))
        elif a.cmd == "clear":
            print(c.signal_clear(a.signal_type, a.subscriber))
    except SGMEError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
