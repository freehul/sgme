"""AIXM 过时记忆清理 v2：DAO 直连批量标记（2026-08-11）
绕过 API 限流（429），走 memory_dao.reject_memory

T-427（2026-11-20）：场景层退役，原场景标记段已摘除。
"""
import re, sqlite3, sys
sys.path.insert(0, '.')
from sgme.data import memory_dao, db

REASON = 'AIXM 已被 SGME 替代（2026-08-09 移入 OLD/），该记忆描述已过时的项目状态'

KEEP_PATTERNS = [
    r'不再活跃', r'移入新建的 OLD', r'搁置项目在 OLD', r'OLD/ 目录',
    r'升级为SGME', r'升级为 SGME', r'已升级为 SGME', r'迁移到新项目SGME',
    r'计划将AIXM升级为SGME', r'将AIXM升级为SGME', r'AIXM升级为SGME',
    r'从旧项目aixm迁移到新项目SGME', r'AIXM已被.*替代', r'由SGME替代',
]

def is_keep(content):
    return any(re.search(p, content) for p in KEEP_PATTERNS)

conn = db.connect_memory()
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT memory_id, content, status FROM memories "
    "WHERE (content LIKE '%AIXM%' OR content LIKE '%aixm%') AND status='active'"
).fetchall()

to_reject = [r for r in rows if not is_keep(r['content'])]
to_keep = [r for r in rows if is_keep(r['content'])]
print(f"剩余 active AIXM 记忆: {len(rows)}（待标记 {len(to_reject)}，保留 {len(to_keep)}）")

# 幂等 reject（已 reject 的会再更新 reason，无害）
ok = 0
for r in to_reject:
    if memory_dao.reject_memory(conn, r['memory_id'], REASON):
        ok += 1
print(f"reject 完成: {ok}/{len(to_reject)}")

conn.close()
print("\n完成")
