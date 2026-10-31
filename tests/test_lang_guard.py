"""语言漂移判据：只看人话，不看工具输出。

2026-10-31 实测教训：全量重建前 20 个文件里，23 条中文记忆被当成"英文会话里的漂移"丢掉。
根因是判语言时把工具回执（英文 JSON/路径/命令）一起算了占比，中文会话被判成英文会话。

两条铁律：
1. 判会话语言必须**先剥掉 tool 段**
2. 逐条漂移判定的中文阈值不能太低——英文会话里夹个中文专名不该丢整条记忆
"""

from __future__ import annotations

from sgme.engine import l1

ZH_TALK = """# 2026-10-31T00:00:01Z user
帮我看看这个插件为什么加载不了
# 2026-10-31T00:00:02Z assistant
我查一下路径转换的问题
# 2026-10-31T00:00:03Z tool
{"path": "C:\\\\Users\\\\LEO\\\\AppData\\\\Roaming\\\\hermes\\\\plugins", "status": "ok", "resolved": true,
 "stack": "at Object.<anonymous> (C:\\\\node_modules\\\\something\\\\index.js:12:34)"}
{"error": "ENOENT: no such file or directory, open 'D:\\\\HermesAgent\\\\plugins\\\\x\\\\manifest.json'"}
"""

EN_TALK = """# 2026-10-31T00:00:01Z user
Can you check why this plugin fails to load? I keep getting an error about the manifest file,
and I have already tried reinstalling it twice without any change in the behaviour.
# 2026-10-31T00:00:02Z assistant
Sure, I will inspect the plugin directory and the manifest resolution order first, then compare
the resolved absolute path against the one written in the loader configuration file to see
whether the sandbox rewrites it before the plugin registry is initialised.
# 2026-10-31T00:00:03Z tool
{"path": "/home/u/plugins", "status": "ok"}
"""


def test_chinese_session_with_english_tool_noise_is_not_english():
    assert l1._is_english_session(ZH_TALK) is False
    assert "tool" not in l1._human_text(ZH_TALK)


def test_real_english_session_detected():
    assert l1._is_english_session(EN_TALK) is True


def test_short_chinese_name_in_english_memory_not_dropped():
    item = {"content": "User prefers WeChat (微信) for quick messages"}
    assert l1._item_lang_drift(item, True) is False


def test_fully_chinese_memory_in_english_session_dropped():
    item = {"content": "用户要求所有插件改动都先写测试再改代码，避免回归"}
    assert l1._item_lang_drift(item, True) is True


def test_strips_tool_blocks_in_refine_format():
    """提炼侧格式 `[msg#N] <ts> <role>:` 也必须能剥（第一次修复只认 L0 格式，形同虚设）。"""
    text = """[msg#1] 2026-10-31T00:00:01Z user:
  帮我查一下这个插件为什么加载不了
[msg#2] 2026-10-31T00:00:02Z assistant:
  我先看路径转换
[msg#3] 2026-10-31T00:00:03Z tool:
  (tool=bash)
  {"path": "C:\\Users\\LEO\\plugins", "error": "ENOENT", "stack": "at Object.<anonymous> (...)"}
"""
    human = l1._human_text(text)
    assert "ENOENT" not in human and "stack" not in human
    assert "插件" in human
    assert l1._is_english_session(text) is False


def test_drift_kept_by_default_and_dropped_only_on_request():
    """默认只告警不丢——判英文会话靠字符占比，误判代价是整条记忆消失。"""
    session = EN_TALK
    item = {"content": "用户要求所有插件改动都先写测试再改代码，避免回归"}
    kept = l1._drop_lang_drift_items([item], session)
    assert kept == [item], "默认必须保留"

    dropped = l1._drop_lang_drift_items([item], session, drop=True)
    assert dropped == [], "显式开启才丢"
