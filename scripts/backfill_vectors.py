"""批量回填缺失的记忆向量（memory_vectors 表）。

背景：2026-08-06 全量提炼时 embedding 端点配置错位（借用 LLM 链 base_url 发往
DeepSeek，401），9293 条历史记忆全部未生成向量——搜索一直靠 BM25 单腿。
PR #6 修复 embed 独立端点后，用本脚本补全历史欠账。

- 复用 sgme.search.vector.upsert_memory_vector（本地 LM Studio，零成本）
- 天然可断点续跑：只处理 memory_vectors 里没有的行（LEFT JOIN 判空）
- 进度每 200 条打印一次；失败计数（可重跑补漏）

用法：
  .venv/Scripts/python scripts/backfill_vectors.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sgme.config as sgme_config
from sgme.llm import provider as llm_provider
from sgme.data.search import vector
from sgme.data.db import connect_memory

BATCH_LOG = 200


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="批量回填缺失的记忆向量")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    parser.add_argument("--force", action="store_true",
                        help="清空 memory_vectors 后全量重灌（向量模型切换时用）")
    args = parser.parse_args()

    cfg = sgme_config.load_config()
    conn = connect_memory()

    if args.force:
        conn.execute("DELETE FROM memory_vectors")
        conn.commit()
        print("已清空 memory_vectors（--force 重灌模式）")

    rows = conn.execute(
        """
        SELECT m.memory_id, m.content FROM memories m
        LEFT JOIN memory_vectors mv ON mv.memory_id = m.memory_id
        WHERE m.status = 'active' AND mv.memory_id IS NULL
        """
    ).fetchall()
    if args.limit > 0:
        rows = rows[: args.limit]
    total = len(rows)
    print(f"待回填向量: {total} 条")

    if total == 0:
        conn.close()
        print("无需回填")
        return 0

    ok = fail = 0
    t0 = time.time()
    client = llm_provider.make_client(timeout_s=30.0)
    for i, (memory_id, content) in enumerate(rows, 1):
        try:
            if vector.upsert_memory_vector(conn, memory_id, content or "", cfg, client=client):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  [{memory_id[:8]}] 异常: {e}")
        if i % BATCH_LOG == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  进度 {i}/{total} | 成功 {ok} 失败 {fail} | {rate:.1f} 条/s | "
                  f"剩余约 {(total - i) / rate / 60:.0f} 分钟")

    conn.close()
    print(f"完成: 成功 {ok}，失败 {fail}（失败项重跑本脚本即可补漏）")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
