"""第二轮合并：人工挑选的语义相近场景对（0.75-0.80 区间）。

容器内执行：docker exec sgme python3 /data/merge_similar_scenes2.py
用标题模糊匹配定位场景，走 l2._apply_merge（旧场景 archived + 新场景向量回填）。
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

# (标题关键词A, 标题关键词B, 相似度, 说明)
PAIRS = [
    ("NAS 环境配置", "NAS 代理与订阅", 0.755, "NAS 环境+代理"),
    ("VPS 环境配置", "VPS 43.255.156.6", 0.775, "VPS 环境+部署"),
    ("当前会话可用工具技能", "Agent Reach 工具使用", 0.798, "工具技能清单"),
    ("SGME npm 包发布工作流", "项目：SGME npm 包发布", 0.776, "npm 发布"),
    ("任务管理与开发偏好", "项目文件管理与编码纪律", 0.765, "开发纪律"),
]

def find(title_kw):
    r = mem_conn.execute(
        "SELECT scene_id FROM scenes WHERE status='active' AND title LIKE ? LIMIT 1",
        ("%" + title_kw + "%",)
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
for kw_a, kw_b, sim, note in PAIRS:
    a = find(kw_a)
    b = find(kw_b)
    if not a or not b:
        print(f"SKIP {kw_a}~{kw_b}: 未找到（a={a} b={b}）")
        skipped += 1
        continue
    s1 = scene_dao.get_scene(mem_conn, a)
    s2 = scene_dao.get_scene(mem_conn, b)
    if not s1 or not s2 or s1["status"] != "active" or s2["status"] != "active":
        print(f"SKIP {kw_a}~{kw_b}: 非 active")
        skipped += 1
        continue
    prompt = PROMPT_TMPL.format(sim=sim, note=note, aid=a[:8], ac=s1["content"],
                                bid=b[:8], bc=s2["content"])
    try:
        text, provider, usage = llm_chain.call_with_fallback(cfg["llm"], prompt, chain_name="refinement")
    except Exception as e:
        print(f"FAIL {kw_a}~{kw_b}: LLM 失败 {e}")
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
        print(f"OK {kw_a} <=> {kw_b}: merged={result.merged}")
        done += 1
    except Exception as e:
        print(f"FAIL {kw_a}~{kw_b}: 落库异常 {e}")
        failed += 1

print(f"\n完成: 合并 {done} / 跳过 {skipped} / 失败 {failed}")
print("当前 active 场景数:", scene_dao.count_scenes(mem_conn, "active"))
