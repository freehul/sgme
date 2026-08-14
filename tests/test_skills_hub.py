"""sgme/skills_hub 技能仓库模块测试（v0.7 §11）。

覆盖：配置解析（完整 / 缺省 / 非法 mode / 非法 enabled 类型）；map 模式
put/get/list/remove 真实文件操作（tmp_path 隔离）；路径穿越与非法名防护；
enabled=False 禁用态（init 返回 None / 直接构造的禁用实例拒绝操作）；
copy 模式 cache 文件操作 + remote_source 记录 + 同步接口安全校验
（0.8 ST-11 后同步已实现，详见 test_skills_hub_sync.py）。
"""

from __future__ import annotations

import pytest

from sgme.skills_hub import (
    SKILL_FILE,
    SkillsHub,
    init,
    parse_skills_hub_config,
)
from sgme.skills_hub.config import (
    DEFAULT_ENABLED,
    DEFAULT_MODE,
    DEFAULT_SYNC_POLICY,
)

# ---- 配置解析 ----


def test_parse_full_config() -> None:
    """① 完整配置：全部字段按 §11.3 解析。"""
    cfg = {
        "skills_hub": {
            "enabled": True,
            "path": "D:/Projects/skills-hub/",
            "mode": "copy",
            "sync_policy": "auto",
            "remote": {
                "source": "nas://nas-host/skills-hub/",
                "cache": "./cache/skills/",
            },
        }
    }
    c = parse_skills_hub_config(cfg)
    assert c.enabled is True
    assert c.path == "D:/Projects/skills-hub/"
    assert c.mode == "copy"
    assert c.sync_policy == "auto"
    assert c.remote_source == "nas://nas-host/skills-hub/"
    assert c.remote_cache == "./cache/skills/"


def test_parse_missing_section_uses_defaults() -> None:
    """② section 缺失（None / 空 dict / 无 skills_hub）→ 全默认：禁用 + map + manual。"""
    for cfg in (None, {}, {"logging": {"level": "INFO"}}):
        c = parse_skills_hub_config(cfg)
        assert c.enabled == DEFAULT_ENABLED is False
        assert c.mode == DEFAULT_MODE == "map"
        assert c.sync_policy == DEFAULT_SYNC_POLICY == "manual"
        assert c.path == "" and c.remote_source == "" and c.remote_cache == ""


def test_parse_partial_config_fills_defaults() -> None:
    """③ 部分配置：只给 enabled/path → mode/sync_policy/remote 走缺省值。"""
    c = parse_skills_hub_config(
        {"skills_hub": {"enabled": True, "path": "/tmp/hub"}}
    )
    assert c.enabled is True
    assert c.path == "/tmp/hub"
    assert c.mode == "map"
    assert c.sync_policy == "manual"
    assert c.remote_source == "" and c.remote_cache == ""


def test_parse_mode_case_insensitive() -> None:
    """④ mode 大小写归一化：COPY / Map 均合法。"""
    assert parse_skills_hub_config(
        {"skills_hub": {"mode": "COPY"}}
    ).mode == "copy"
    assert parse_skills_hub_config(
        {"skills_hub": {"mode": "Map"}}
    ).mode == "map"


def test_parse_invalid_mode_raises() -> None:
    """⑤ 非法 mode（非 map/copy）→ ValueError。"""
    with pytest.raises(ValueError, match="mode"):
        parse_skills_hub_config({"skills_hub": {"mode": "symlink"}})


def test_parse_invalid_enabled_type_raises() -> None:
    """⑥ enabled 非 bool（如字符串 "true"）→ ValueError。"""
    with pytest.raises(ValueError, match="enabled"):
        parse_skills_hub_config({"skills_hub": {"enabled": "true"}})


def test_parse_invalid_sync_policy_raises() -> None:
    """⑦ 非法 sync_policy → ValueError。"""
    with pytest.raises(ValueError, match="sync_policy"):
        parse_skills_hub_config({"skills_hub": {"sync_policy": "daily"}})


# ---- init 与禁用态 ----


def test_init_disabled_returns_none() -> None:
    """⑧ enabled=false 或 section 缺失 → init 返回 None（禁用态）。"""
    assert init(None) is None
    assert init({}) is None
    assert init({"skills_hub": {"enabled": False, "path": "/tmp/hub"}}) is None


def test_init_enabled_returns_hub() -> None:
    """⑨ enabled=true → 返回 SkillsHub 实例。"""
    hub = init({"skills_hub": {"enabled": True, "path": "/tmp/hub"}})
    assert isinstance(hub, SkillsHub)
    assert hub.enabled is True and hub.mode == "map"


def test_init_enabled_missing_workdir_raises() -> None:
    """⑩ 启用但缺工作区目录（map 无 path / copy 无 remote.cache）→ ValueError。"""
    with pytest.raises(ValueError, match="工作区"):
        init({"skills_hub": {"enabled": True}})
    with pytest.raises(ValueError, match="工作区"):
        init({"skills_hub": {"enabled": True, "mode": "copy", "path": "/tmp/hub"}})


def test_disabled_hub_methods_raise() -> None:
    """⑪ 直接构造禁用态实例 → 任何技能操作抛 RuntimeError。"""
    hub = SkillsHub(parse_skills_hub_config({"skills_hub": {}}))
    assert hub.enabled is False
    with pytest.raises(RuntimeError, match="禁用"):
        hub.list_skills()
    with pytest.raises(RuntimeError, match="禁用"):
        hub.put_skill("x", "content")
    with pytest.raises(RuntimeError, match="禁用"):
        hub.get_skill("x")
    with pytest.raises(RuntimeError, match="禁用"):
        hub.remove_skill("x")


# ---- map 模式：真实文件操作 ----


def test_map_put_get_list_remove(tmp_path) -> None:
    """⑫ map 模式 put/get/list/remove 全链路真实文件操作。"""
    hub = init(
        {"skills_hub": {"enabled": True, "path": str(tmp_path)}}
    )
    assert hub is not None

    # put：创建 <name>/SKILL.md 且内容落盘
    written = hub.put_skill("my-skill", "# 我的技能\n\n正文")
    assert written == tmp_path / "my-skill" / SKILL_FILE
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "# 我的技能\n\n正文"

    # get：读回内容；list：可见该技能
    assert hub.get_skill("my-skill") == "# 我的技能\n\n正文"
    assert hub.list_skills() == ["my-skill"]

    # remove：目录整体删除、list 清空；重复 remove 幂等返回 False
    assert hub.remove_skill("my-skill") is True
    assert not (tmp_path / "my-skill").exists()
    assert hub.list_skills() == []
    assert hub.remove_skill("my-skill") is False


def test_map_put_overwrites_existing(tmp_path) -> None:
    """⑬ put 覆盖写：同技能名二次写入替换旧内容。"""
    hub = init({"skills_hub": {"enabled": True, "path": str(tmp_path)}})
    assert hub is not None
    hub.put_skill("s", "v1")
    hub.put_skill("s", "v2")
    assert hub.get_skill("s") == "v2"


def test_map_get_missing_returns_none(tmp_path) -> None:
    """⑭ get 不存在的技能 → None；仓库目录不存在 → list 为空列表。"""
    hub = init({"skills_hub": {"enabled": True, "path": str(tmp_path)}})
    assert hub is not None
    assert hub.get_skill("ghost") is None
    assert hub.list_skills() == []


def test_map_list_skips_dir_without_skill_file(tmp_path) -> None:
    """⑮ list 只认含 SKILL.md 的子目录，无关目录被跳过。"""
    (tmp_path / "no-skill").mkdir()
    (tmp_path / "has-skill").mkdir()
    (tmp_path / "has-skill" / SKILL_FILE).write_text("ok", encoding="utf-8")
    hub = init({"skills_hub": {"enabled": True, "path": str(tmp_path)}})
    assert hub is not None
    assert hub.list_skills() == ["has-skill"]


# ---- 路径穿越 / 非法名防护 ----


@pytest.mark.parametrize(
    "bad_name",
    ["", "   ", "../x", "..\\x", "a/b", "a\\b", "..", ".", "a..b", "a/b/c", "x/../y"],
)
def test_map_rejects_unsafe_names(tmp_path, bad_name) -> None:
    """⑯ put/get/remove 一律拒绝路径穿越与非法名（含空名）。"""
    hub = init({"skills_hub": {"enabled": True, "path": str(tmp_path)}})
    assert hub is not None
    with pytest.raises(ValueError):
        hub.put_skill(bad_name, "content")
    with pytest.raises(ValueError):
        hub.get_skill(bad_name)
    with pytest.raises(ValueError):
        hub.remove_skill(bad_name)
    # 防护生效：仓库外未写入任何文件
    assert not (tmp_path.parent / "x").exists()


def test_map_traversal_never_escapes_root(tmp_path) -> None:
    """⑰ 路径穿越尝试不得在仓库根之外留下文件（纵深防御断言）。"""
    outside = tmp_path.parent / "escaped"
    hub = init({"skills_hub": {"enabled": True, "path": str(tmp_path)}})
    assert hub is not None
    for name in ("../escaped", "a/../../escaped", "..\\escaped"):
        with pytest.raises(ValueError):
            hub.put_skill(name, "evil")
    assert not outside.exists()


# ---- copy 模式 ----


def test_copy_mode_operations_on_cache(tmp_path) -> None:
    """⑱ copy 模式：读写发生在 remote.cache 工作区，remote_source 仅记录。"""
    cache = tmp_path / "cache" / "skills"
    hub = init(
        {
            "skills_hub": {
                "enabled": True,
                "mode": "copy",
                "path": str(tmp_path / "ignored-map-path"),
                "remote": {
                    "source": "nas://nas-host/skills-hub/",
                    "cache": str(cache),
                },
            }
        }
    )
    assert hub is not None
    assert hub.mode == "copy"
    assert hub.config.remote_source == "nas://nas-host/skills-hub/"
    assert hub.root == cache.resolve()

    # 文件操作全落在 cache 工作区（map path 目录不被触碰）
    hub.put_skill("nas-skill", "cache 内容")
    assert (cache / "nas-skill" / SKILL_FILE).read_text(encoding="utf-8") == "cache 内容"
    assert not (tmp_path / "ignored-map-path").exists()
    assert hub.get_skill("nas-skill") == "cache 内容"
    assert hub.list_skills() == ["nas-skill"]
    assert hub.remove_skill("nas-skill") is True
    assert hub.list_skills() == []


def test_copy_mode_sync_validates_source(tmp_path) -> None:
    """⑲ copy 模式同步接口：0.8 ST-11 已实现真实同步（见 test_skills_hub_sync.py）；
    非法 remote.source（nas:// 非三形态）在同步时被拒绝（ValueError）。"""
    hub = init(
        {
            "skills_hub": {
                "enabled": True,
                "mode": "copy",
                "remote": {"source": "nas://h/", "cache": str(tmp_path / "c")},
            }
        }
    )
    assert hub is not None
    with pytest.raises(ValueError, match="仅允许"):
        hub.sync_from_remote()
    with pytest.raises(ValueError, match="仅允许"):
        hub.sync_to_remote()
