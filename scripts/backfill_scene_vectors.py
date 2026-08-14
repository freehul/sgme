"""批量回填缺失的场景向量（scene_vectors 表，L2 语义检索）。

背景：v5 场景检索升级——scenes_fts（BM25，PR#7）+ scene_vectors（语义，PR#8）。
本脚本只补 scene_vectors（active 场景，本地 LM Studio 零成本），可断点续跑。

用法：
  .venv/Scripts/python scripts/backfill_scene_vectors.py [--limit N]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sgme.config as sgme_config
from sgme.llm import provider as llm_provider
from sgme.data.search import vector
from sgme.data.db import get_memory_conn

BATCH_LOG = 50


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="批量回填缺失的场景向量")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    parser.add_argument("--force", action="store_true",
                        help="清空 scene_vectors 后全量重灌（向量模型切换时用）")
    args = parser.parse_args()

    cfg = sgme_config.load_config()
    conn = get_memory_conn()

    if args.force:
        conn.execute("DELETE FROM scene_vectors")
        conn.commit()
        print("已清空 scene_vectors（--force 重灌模式）")

    rows = conn.execute(
        """
        SELECT s.scene_id, s.title, s.content FROM scenes s
        LEFT JOIN scene_vectors v ON v.scene_id = s.scene_id
        WHERE s.status = 'active' AND v.scene_id IS NULL
        """
    ).fetchall()
    if args.limit > 0:
        rows = rows[: args.limit]
    total = len(rows)
    print(f"待回填场景向量: {total} 条")

    if total == 0:
        conn.close()
        print("无需回填")
        return 0

    ok = fail = 0
    t0 = time.time()
    client = llm_provider.make_client(timeout_s=30.0)
    for i, (scene_id, title, content) in enumerate(rows, 1):
        text = f"{title} {content}"
        try:
            if vector.upsert_scene_vector(conn, scene_id, text, cfg, client=client):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  [{scene_id[:8]}] 异常: {e}")
        if i % BATCH_LOG == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  进度 {i}/{total} | 成功 {ok} 失败 {fail} | {rate:.1f} 条/s")

    conn.close()
    print(f"完成: 成功 {ok}，失败 {fail}（失败项重跑本脚本即可补漏）")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
