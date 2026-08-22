"""第二轮合并补跑：用 scene_id 前缀定位（标题为默认 scene_xxx 的对）。

容器内执行：docker exec sgme python3 /data/merge_similar_scenes2b.py
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

# (scene_id 前缀 A, 前缀 B, 相似度, 说明)
PAIRS = [
    ("24c750f6", "32843080", 0.755, "NAS 环境+代理"),
    ("07a3a621", "53be8712", 0.775, "VPS 环境+部署"),
    ("acdc5e31", "160e9df7", 0.798, "工具技能清单"),
    ("2d8294cc", "efd0178c", 0.776, "npm 发布"),
]

def find(sid_prefix):
    r = mem_conn.execute(
        "SELECT scene_id FROM scenes WHERE status='active' AND scene_id LIKE ? LIMIT 1",
        (sid_prefix + "%",)
    ).fetchone()
    return r["scene_id"] if r else None

PROMPT_TMPL = """你是记忆整合架构师。以下两个场景主题相近（相似度 {sim}，说明：{note}），请合并为一个**场景叙事文档**。

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
for pa, pb, sim, note in PAIRS:
    a, b = find(pa), find(pb)
    if not a or not b:
        print(f"SKIP {pa}~{pb}: a={a} b={b}")
        skipped += 1
        continue
    s1 = scene_dao.get_scene(mem_conn, a)
    s2 = scene_dao.get_scene(mem_conn, b)
    if not s1 or not s2 or s1["status"] != "active" or s2["status"] != "active":
        print(f"SKIP {pa}~{pb}: 非 active")
        skipped += 1
        continue
    prompt = PROMPT_TMPL.format(sim=sim, note=note, aid=a[:8], ac=s1["content"],
                                bid=b[:8], bc=s2["content"])
    try:
        text, provider, usage = llm_chain.call_with_fallback(cfg["llm"], prompt, chain_name="refinement")
    except Exception as e:
        print(f"FAIL {pa}~{pb}: LLM 失败 {e}")
        failed += 1
        continue
    action = {
        "action": "merge",
        "target_scene_id": "placeholder",
        "merged_content": text.strip().strip("`").strip(),
        "merged_from": [a, b],
        "reason": f"相似度 {sim} 自动合并（{note}）",
    }
    result = l2.L2Result()
    try:
        l2._apply_merge(mem_conn, action, [], result, cfg)
        print(f"OK {pa}~{pb} ({note}): merged={result.merged}")
        done += 1
    except Exception as e:
        print(f"FAIL {pa}~{pb}: 落库异常 {e}")
        failed += 1

print(f"\n完成: 合并 {done} / 跳过 {skipped} / 失败 {failed}")
print("当前 active 场景数:", scene_dao.count_scenes(mem_conn, "active"))
