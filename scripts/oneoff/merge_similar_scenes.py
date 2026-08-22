"""场景超限治理：LLM 合并高相似场景对（T-97 第二步）。

对相似度 >= 0.80 的场景对，用 LLM 生成合并正文，走 l2._apply_merge 落库
（旧场景 archived 可恢复 + scene_versions 快照 + 新场景自动回填向量）。

容器内执行：docker exec sgme python3 /data/merge_similar_scenes.py
幂等：场景已非 active 的对跳过。
"""
import sqlite3
import sys

sys.path.insert(0, "/app")

from sgme import config
from sgme.engine import l2
from sgme.data import scene_dao
from sgme.llm import chain as llm_chain

cfg = config.load_config()
mem_conn = sqlite3.connect("/data/data/memory.db")
mem_conn.row_factory = sqlite3.Row

# 相似度 >= 0.80 的场景对（来自 scene_sim_analysis.py 结果）
PAIRS = [
    ("fed34b9b", "2552a0f5", 0.896),
    ("c4b3a7ed", "ce3debba", 0.862),
    ("5863b5d1", "55d1422c", 0.832),
    ("98c36791", "eee3f331", 0.817),
    ("53c34b64", "f015732c", 0.816),
    ("151e7e19", "e9b72cd8", 0.805),
    ("f9dc5c0d", "ac694105", 0.800),
]

PROMPT_TMPL = """你是记忆整合架构师。以下两个场景主题高度相似（相似度 {sim}），请将它们合并为一个**场景叙事文档**。

要求：
1. 忠实事实聚合，保留双方关键细节（数字/日期/名称/技术名词），不虚构、不文学修饰
2. 重复信息去重，用简洁条目或短段落组织
3. 第一行必须是标题：`# 合并后标题`
4. 只输出合并后的场景正文，无其他文字

## 场景 A（{aid}）
{ac}

## 场景 B（{bid}）
{bc}
"""

done, skipped, failed = 0, 0, 0
for aid_short, bid_short, sim in PAIRS:
    # scene_id 用短前缀匹配（分析输出截断为 8 位，需全表找）
    rows = mem_conn.execute(
        "SELECT scene_id FROM scenes WHERE status='active' AND scene_id LIKE ?", (aid_short + "%",)
    ).fetchall()
    a = rows[0]["scene_id"] if rows else None
    rows = mem_conn.execute(
        "SELECT scene_id FROM scenes WHERE status='active' AND scene_id LIKE ?", (bid_short + "%",)
    ).fetchall()
    b = rows[0]["scene_id"] if rows else None
    if not a or not b:
        print(f"SKIP {aid_short}~{bid_short}: 场景不存在或已归档")
        skipped += 1
        continue
    s1 = scene_dao.get_scene(mem_conn, a)
    s2 = scene_dao.get_scene(mem_conn, b)
    if not s1 or not s2 or s1["status"] != "active" or s2["status"] != "active":
        print(f"SKIP {a[:8]}~{b[:8]}: 场景不存在或非 active")
        skipped += 1
        continue
    prompt = PROMPT_TMPL.format(sim=sim, aid=a[:8], ac=s1["content"], bid=b[:8], bc=s2["content"])
    try:
        text, provider, usage = llm_chain.call_with_fallback(cfg["llm"], prompt, chain_name="refinement")
    except Exception as e:
        print(f"FAIL {a[:8]}~{b[:8]}: LLM 调用失败 {e}")
        failed += 1
        continue
    merged_content = text.strip().strip("`").strip()
    action = {
        "action": "merge",
        "target_scene_id": "placeholder",
        "merged_content": merged_content,
        "merged_from": [a, b],
        "reason": f"相似度 {sim} 自动合并",
    }
    result = l2.L2Result()
    try:
        l2._apply_merge(mem_conn, action, [], result, cfg)
        print(f"OK {a[:8]}~{b[:8]} ({sim}): merged={result.merged} archived={result.archived}")
        done += 1
    except Exception as e:
        print(f"FAIL {a[:8]}~{b[:8]}: 落库异常 {e}")
        failed += 1

print(f"\n完成: 合并 {done} / 跳过 {skipped} / 失败 {failed}")
print("当前 active 场景数:", scene_dao.count_scenes(mem_conn, "active"))
