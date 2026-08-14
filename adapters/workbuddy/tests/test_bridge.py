"""adapters/workbuddy/bridge.py 纯函数单测（对标 trae 适配层，无 hook）。

仅覆盖解析/格式化/编码等不依赖 SGME Server 的部分；网络调用由 import_history 的
dry-run + 真实批量测试覆盖。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # adapters/workbuddy

import bridge  # noqa: E402


def test_encode_project_dir():
    assert bridge.encode_project_dir(r"D:\Projects\SGME") == "d-Projects-SGME"
    assert bridge.encode_project_dir(r"D:\tmp") == "d-tmp"
    assert bridge.encode_project_dir(
        r"C:\Users\<user>\WorkBuddy\2026-08-12-01-32-00"
    ) == "c-Users--user--WorkBuddy-2026-08-12-01-32-00"
    # mac/linux 无盘符
    assert bridge.encode_project_dir("/Users/leo/projects/foo") == "Users-leo-projects-foo"


def test_norm_ts_epoch_ms():
    out = bridge._norm_ts(1786461701510)
    assert out.endswith("Z")
    assert "2026" in out
    # 秒级 epoch 也应能处理
    assert bridge._norm_ts(1786461701).endswith("Z")


def test_parse_and_to_l0(tmp_path):
    f = tmp_path / "sess.jsonl"
    recs = [
        {"id": "1", "timestamp": 1786461701510, "type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<system-reminder>noise</system-reminder>你好"}]},
        {"id": "2", "timestamp": 1786461713240, "type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "回复内容"}]},
        # 非消息行（file-history-snapshot，role=None）→ 跳过
        {"id": "3", "timestamp": 1786461714000, "type": "file-history-snapshot",
         "role": None, "snapshot": {}},
        # 空 assistant 块 → 跳过
        {"id": "4", "timestamp": 1786461715000, "type": "message", "role": "assistant",
         "content": [{"type": "reasoning", "text": "思考过程"}]},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")

    msgs = bridge.parse_workbuddy_jsonl(f)
    assert len(msgs) == 2, msgs
    assert msgs[0]["role"] == "user"
    assert "noise" not in msgs[0]["content"]
    assert "你好" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    assert "回复内容" in msgs[1]["content"]

    l0 = bridge.to_l0(msgs)
    assert "# " in l0 and "## " in l0
    assert "你好" in l0 and "回复内容" in l0
    # reasoning 块未被写入
    assert "思考过程" not in l0


def test_discover_and_started_at(tmp_path, monkeypatch):
    # 用临时 HOME 验证发现逻辑
    proj = tmp_path / "projects" / "d-Projects-SGME"
    proj.mkdir(parents=True)
    f = proj / "abc.jsonl"
    f.write_text(
        json.dumps({"id": "1", "timestamp": 1000, "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "x"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "_WORKBUDDY_HOME", tmp_path / ".workbuddy" if False else tmp_path)
    # 直接把 projects 放到 tmp_path 下（_WORKBUDDY_HOME/projects）
    home = tmp_path
    monkeypatch.setattr(bridge, "_WORKBUDDY_HOME", home)
    found = bridge.discover_sessions()
    assert any(p.name == "abc.jsonl" for p in found)
    assert bridge.session_started_at(f) == 1000
