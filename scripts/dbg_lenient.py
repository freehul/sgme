# -*- coding: utf-8 -*-
import re, sys, json
sys.path.insert(0, '.')
from sgme.engine.l2 import _parse_json_lenient

t = '[{"action": "create", "target_scene_id": "a", "merged_content": "1", "reason": "x"}\n{"action": "create", "target_scene_id": "b", "merged_content": "2", "reason": "y"}]'

try:
    r = _parse_json_lenient(t)
    print('lenient OK:', len(r))
except Exception as e:
    print('lenient FAIL:', e)
    # 复现步骤
    def _fix_control(m):
        ch = m.group(0)
        if ch == "\n":
            return "\\n"
        if ch == "\t":
            return "\\t"
        if ch == "\r":
            return "\\r"
        return "\\u%04x" % ord(ch)
    def _fix_escape(m):
        return "\\\\" + m.group(1)
    text = re.sub(r"[\x00-\x1f\x7f]", _fix_control, t)
    print('s1:', repr(text[:80]))
    text = re.sub(r"\\([^\\\"/bfnrtu])", _fix_escape, text)
    print('s2:', repr(text[:80]))
    def _fix_missing_comma(m):
        g1 = m.group(1)
        stripped = g1.rstrip()
        if stripped.endswith(","):
            return g1 + m.group(2)
        return stripped + "," + g1[len(stripped):] + m.group(2)
    text = re.sub(r"([}\]][\s\n]*)([{[])", _fix_missing_comma, text)
    print('s3:', repr(text[:100]))
    text = re.sub(r",\s*([}\]])", r"\1", text)
    print('s4:', repr(text[:100]))
    try:
        json.loads(text)
        print('最终 JSON OK')
    except Exception as e2:
        print('最终 JSON FAIL:', e2)
