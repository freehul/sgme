"""bridge.py 单元测试（PR #1：会话解析 + L0 转换 + append 调用）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import (  # noqa: E402
    encode_project_dir,
    find_session_file,
    parse_session_file,
    to_l0,
    _norm_ts,
    APPEND_PAYLOAD,
)

# ---------- 真实观察样例（2026-08-07 实测 reasonix v1.19.1） ----------
SAMPLE_JSONL = "\n".join([
    json.dumps({"role": "system", "content": "You are Reasonix, a coding agent.\nUse tools."}),
    json.dumps({"role": "user", "content": "<reasoning-language>\n必须使用简体中文\n</reasoning-language>\n\n你好，帮我看看这个项目",
                "raw_content": "你好，帮我看看这个项目", "createdAt": "2026-08-07T02:10:51.9678252Z"}),
    json.dumps({"role": "assistant", "content": "好的，我先看看目录结构。", "reasoning_content": "用户想看项目结构", "workDurationMs": 1531}),
    json.dumps({"role": "tool", "content": "src/main.py\nsrc/utils.py", "tool_call_id": "call_001", "name": "ls"}),
    # ST-23③：同一 tool 事件被 jsonl 重复记录（实锤 4 次）——解析必须去重
    json.dumps({"role": "tool", "content": "src/main.py\nsrc/utils.py", "tool_call_id": "call_001", "name": "ls"}),
    json.dumps({"role": "tool", "content": "src/main.py\nsrc/utils.py", "tool_call_id": "call_001", "name": "ls"}),
    json.dumps({"role": "tool", "content": "{\"pending\": true}", "tool_call_id": "__reasonix_local_only__", "name": "__reasonix_local_only__", "local_only": True}),
    json.dumps({"role": "assistant", "content": "", "reasoning_content": "只有思考没有输出", "workDurationMs": 100}),
    json.dumps({"role": "assistant", "content": "项目有 main.py 和 utils.py。"}),
])


def test_encode_project_dir():
    # 实测观察：D:\Projects\rx-hook-test → d--projects-rx-hook-test
    assert encode_project_dir(r"D:\Projects\rx-hook-test") == "d--projects-rx-hook-test"
    # 实测观察：C:\Users\test → c--users-test（原断言 c--users-leo 为笔误，修正）
    assert encode_project_dir(r"C:\Users\test") == "c--users-test"


def test_find_session_file(tmp_path, monkeypatch):
    sid = "20260807-021137.257664600-test"
    proj = tmp_path / "reasonix" / "projects" / "d--projects-rx-hook-test" / "sessions"
    proj.mkdir(parents=True)
    target = proj / f"{sid}.jsonl"
    target.write_text("{}", encoding="utf-8")
    (proj / f"{sid}.jsonl.meta").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("APPDATA", str(tmp_path))
    found = find_session_file(sid, r"D:\Projects\rx-hook-test")
    assert found == target


def test_find_session_file_scan_fallback(tmp_path, monkeypatch):
    """编码规则不匹配时，全局扫描兜底。"""
    sid = "20260807-021137.257664600-test"
    proj = tmp_path / "reasonix" / "projects" / "unknown-dir-name" / "sessions"
    proj.mkdir(parents=True)
    target = proj / f"{sid}.jsonl"
    target.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("APPDATA", str(tmp_path))
    found = find_session_file(sid, r"D:\Other\Path")
    assert found == target


def test_parse_session_file(tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text(SAMPLE_JSONL, encoding="utf-8")
    msgs = parse_session_file(f)

    # system 行过滤
    assert all(m["role"] != "system" for m in msgs)
    # local_only 内部噪音过滤
    assert all(m["role"] != "tool" or m.get("name") != "__reasonix_local_only__" for m in msgs)

    # user 优先用 raw_content（去掉 reasoning-language 注入前缀）
    # createdAt 为 epoch 毫秒 int（真实数据格式）→ 归一化
    assert msgs[0] == {"role": "user", "content": "你好，帮我看看这个项目",
                       "ts": "2026-08-07T02:10:51Z"}
    # assistant 提取 content（reasoning_content 丢弃）
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"] == "好的，我先看看目录结构。"
    # assistant 无时间戳 → 继承上一条
    assert msgs[1]["ts"] == "2026-08-07T02:10:51Z"
    # tool 保留 name
    assert msgs[2] == {"role": "tool", "content": "src/main.py\nsrc/utils.py",
                       "name": "ls", "tool_call_id": "call_001", "ts": "2026-08-07T02:10:51Z"}
    # 消息顺序保持
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]


def test_norm_ts_int_epoch_ms():
    """真实数据：createdAt 是 epoch 毫秒 int → 归一化为 ISO UTC。"""
    # 1785755032100 ms = 2026-08-03T11:03:52Z（实测换算）
    assert _norm_ts(1785755032100) == "2026-08-03T11:03:52Z"
    # 秒级 epoch 也支持
    assert _norm_ts(1785755032) == "2026-08-03T11:03:52Z"


def test_to_l0():
    msgs = [
        {"role": "user", "content": "你好", "ts": "2026-08-07T02:10:51Z"},
        {"role": "assistant", "content": "你好！", "ts": "2026-08-07T02:10:52Z"},
        {"role": "tool", "content": "文件列表", "name": "ls", "ts": "2026-08-07T02:10:52Z"},
    ]
    text = to_l0(msgs)
    assert "# 2026-08-07T02:10:51Z user\n你好" in text
    assert "## 2026-08-07T02:10:52Z assistant\n你好！" in text
    assert "## 2026-08-07T02:10:52Z tool\n**tool**: ls\n文件列表" in text


def test_append_payload():
    """append 请求体：session_key / started_at / content / agent_id 齐全。"""
    payload = APPEND_PAYLOAD(
        session_key="reasonix-20260807-021137_257664600-test",
        started_at="2026-08-07T02:10:51Z",
        l0_text="# 2026-08-07T02:10:51Z user\n你好",
        agent_id="reasonix",
    )
    assert payload["session_key"].startswith("reasonix-")
    assert payload["started_at"] == "2026-08-07T02:10:51Z"
    assert payload["agent_id"] == "reasonix"
    assert "你好" in payload["content"]
