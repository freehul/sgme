#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/make_shards.py：把重建任务包按 file_id 切成 N 个分片（并行执行用）。

为什么要按 file_id 切：zcode_import.py 以「文件」为单位导入，一个文件的全部
chunk 必须都在同一分片里，否则该文件永远凑不齐、永远不收敛。

用法：
    python scripts/make_shards.py --staging rebuild-20261109/staging --n 4
产物：
    <staging>/shards/shard-1.txt ... shard-N.txt   每行一个 file_id
    <staging>/shards/README.md                     分片说明与命令
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按 file_id 把任务包切分片")
    ap.add_argument("--staging", required=True)
    ap.add_argument("--n", type=int, default=4, help="分片数（并行会话数）")
    ap.add_argument("--only-remaining", action="store_true",
                    help="只给还没跑完的文件分片（读 out/ 现状；用于下一窗口续跑）")
    ap.add_argument("--out-dir", default=None, help="分片输出子目录（默认 shards；续跑建议 shards-next）")
    args = ap.parse_args(argv)

    staging = Path(args.staging).resolve()
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files") or {}
    empty_files = manifest.get("empty_files") or []

    counts: dict[str, int] = {}
    for t in manifest.get("tasks") or []:
        counts[t["file_id"]] = counts.get(t["file_id"], 0) + 1
    for f in empty_files:
        counts.setdefault(f, 0)

    if args.only_remaining:
        done: dict[str, int] = {}
        for p in (staging / "out").glob("*.json"):
            fid = p.stem.split("__")[0]
            done[fid] = done.get(fid, 0) + 1
        keep = {k: v for k, v in counts.items() if done.get(k, 0) < v}
        skipped = len(counts) - len(keep)
        counts = keep or counts
        print("续跑分片：跳过已完成 %d 个文件，剩 %d 个文件" % (skipped, len(counts)))

    n = max(1, min(args.n, max(1, len(counts))))
    bins: list[list[str]] = [[] for _ in range(n)]
    loads = [0] * n
    for fid, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        i = loads.index(min(loads))
        bins[i].append(fid)
        loads[i] += c

    out_dir = staging / (args.out_dir or "shards")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# 并行分片（按 file_id 切，勿跨片拆分同一文件）", ""]
    for i, (ids, load) in enumerate(zip(bins, loads), start=1):
        p = out_dir / ("shard-%d.txt" % i)
        p.write_text("\n".join(ids) + "\n", encoding="utf-8")
        lines.append("- shard-%d.txt：%d 个文件 / %d 个任务" % (i, len(ids), load))
        print("shard-%d: files=%d tasks=%d" % (i, len(ids), load))

    lines += [
        "",
        "## 每个会话怎么用",
        "1. 只处理自己分片里的 file_id 对应的 in/*.txt："
        "   task_id 形如 file_id__000，用分片清单过滤后再逐个读。",
        "2. 只写 out/，不要改 in/、不要碰 home/ 下的数据库。",
        "3. 落库带上自己的清单（互斥锁 + 合并写已内置，可并发调用，但同一时刻只有一个在写）：",
        "",
        "   cd <project-root>",
        "   .\\.venv\\Scripts\\python.exe scripts\\zcode_import.py --home rebuild-20261109\\home "
        "     --staging rebuild-20261109\\staging --file-list rebuild-20261109\\staging\\shards\\shard-1.txt",
    ]
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
