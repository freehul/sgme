# -*- coding: utf-8 -*-
"""导出全部 L2 场景完整叙事到文件"""
import sys
from pathlib import Path
sys.path.insert(0, '.')
from sgme.data.db import get_memory_conn

ROOT = Path(__file__).resolve().parents[1]

wconn = get_memory_conn()
rows = wconn.execute(
    'SELECT scene_id, title, content, heat, status, created_at, updated_at FROM scenes ORDER BY heat DESC, updated_at DESC'
).fetchall()

lines = []
lines.append(f"# L2 场景叙事完整导出（共 {len(rows)} 个场景）\n")
for i, (sid, title, content, heat, status, created, updated) in enumerate(rows, 1):
    lines.append(f"--- [{i}] {title} (heat={heat}, {status}, 创建={created[:16]}, 更新={updated[:16]}) ---")
    lines.append(content)
    lines.append("")

out = ROOT / "data" / "l2_scenes_export.md"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"已导出 {len(rows)} 个场景到 {out}")
print(f"总字数: {sum(len(c or '') for _, _, c, *_ in rows):,}")
