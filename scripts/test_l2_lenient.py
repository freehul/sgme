# -*- coding: utf-8 -*-
"""L2 lenient 解析测试 v2（含结构修复）"""
import sys
sys.path.insert(0, '.')
from sgme.engine.l2 import parse_l2_output

cases = [
    # 1. 裸控制字符（字符串里未转义换行）
    ('裸控制字符', '[{"action": "create", "target_scene_id": "x", "merged_content": "第一行\n第二行", "reason": "ok"}]'),
    # 2. 无效转义
    ('无效转义', '[{"action": "create", "merged_content": "路径 C:\\x\\test\\q", "reason": "ok"}]'),
    # 3. 缺 target_scene_id
    ('缺target', '[{"action": "merge", "merged_content": "内容", "reason": "ok"}]'),
    # 4. 正常
    ('正常', '[{"action": "create", "target_scene_id": "s1", "merged_content": "正常内容", "reason": "ok"}]'),
    # 5. 对象间缺逗号
    ('缺逗号', '[{"action": "create", "target_scene_id": "a", "merged_content": "1", "reason": "x"} {"action": "update", "target_scene_id": "b", "merged_content": "2", "reason": "y"}]'),
    # 6. 尾逗号
    ('尾逗号', '[{"action": "create", "target_scene_id": "a", "merged_content": "1", "reason": "x"},]'),
    # 7. 换行分隔缺逗号
    ('换行缺逗号', '[{"action": "create", "target_scene_id": "a", "merged_content": "1", "reason": "x"}\n{"action": "create", "target_scene_id": "b", "merged_content": "2", "reason": "y"}]'),
    # 8. 正常多对象（确保不误伤）
    ('正常多对象', '[{"action": "create", "target_scene_id": "a", "merged_content": "1", "reason": "x"}, {"action": "update", "target_scene_id": "b", "merged_content": "2", "reason": "y"}]'),
]

for name, t in cases:
    try:
        r = parse_l2_output(t)
        print(f'{name}: OK ({len(r)}条)')
    except Exception as e:
        print(f'{name}: FAIL {e}')
