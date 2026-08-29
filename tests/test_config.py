"""T0 测试：配置加载与字段完整性校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sgme import config


# ---------- T0：load_config 基础断言 ----------

def test_load_config_returns_dict_with_required_keys():
    """load_config 返回字典，含 llm/dimensions/aliases/paths 四键。"""
    cfg = config.load_config()
    assert isinstance(cfg, dict)
    for key in ("llm", "dimensions", "aliases", "paths"):
        assert key in cfg, f"缺字段 {key}"


def test_dimensions_count():
    """维度注册表必须含 14 维（2026-08-18 三池重构移除 projects/tasks 两维，
    16 → 14；ideas 创意池维度 2026-08-12 加入）。"""
    cfg = config.load_config()
    assert len(cfg["dimensions"]) == 14


def test_dimension_fields_complete():
    """每条维度必须含 id/display_name/category/time_velocity/ttl_days/description。"""
    cfg = config.load_config()
    required = {"id", "display_name", "category", "time_velocity", "ttl_days", "description"}
    for d in cfg["dimensions"]:
        missing = required - set(d.keys())
        assert not missing, f"维度 {d.get('id')} 缺字段 {missing}"


def test_dimension_ids_unique():
    """维度 id 唯一。"""
    cfg = config.load_config()
    ids = [d["id"] for d in cfg["dimensions"]]
    assert len(ids) == len(set(ids)), "维度 id 有重复"


def test_known_dimension_ids_present():
    """关键维度 id 必须存在（identity/tech_stack/status 等）。

    projects/tasks 已移除（2026-08-18 三池重构：项目池 project_meta /
    待办池 demands 为专用落地点），不再出现在注册表。
    """
    cfg = config.load_config()
    ids = {d["id"] for d in cfg["dimensions"]}
    for must in ("identity", "family", "social", "values", "skills", "tech_stack",
                 "preferences", "habits", "environment", "style",
                 "focus", "goals", "status", "ideas"):
        assert must in ids, f"缺关键维度 {must}"
    assert "projects" not in ids and "tasks" not in ids, "projects/tasks 已移除，不应回潜"


def test_dynamic_dimensions_have_ttl():
    """time_velocity=dynamic 的维度必须有 ttl_days（status/focus/tasks/projects/goals）。

    ``ideas`` 例外：创意池维度语义为长期保存（无 TTL），靠 admin API 人工判定生命周期
    （ST-14：创意 = 带 ideas 维度 + ttl_days=NULL 的记忆）。
    """
    cfg = config.load_config()
    for d in cfg["dimensions"]:
        if d["time_velocity"] == "dynamic" and d["id"] != "ideas":
            assert d["ttl_days"] is not None and d["ttl_days"] > 0, \
                f"动态维度 {d['id']} 缺 ttl_days"


def test_static_dimensions_no_ttl():
    """time_velocity=static 的维度 ttl_days 应为 None。"""
    cfg = config.load_config()
    for d in cfg["dimensions"]:
        if d["time_velocity"] == "static":
            assert d["ttl_days"] is None, f"静态维度 {d['id']} 不应有 ttl_days"


def test_aliases_keys_subset_of_dimension_ids():
    """别名表 key 必须都在维度 id 集合中。"""
    cfg = config.load_config()
    dim_ids = {d["id"] for d in cfg["dimensions"]}
    for alias_key in cfg["aliases"]:
        assert alias_key in dim_ids, f"别名表引用未知维度 {alias_key}"


def test_aliases_contain_known_mappings():
    """关键中文别名必须存在（如技术栈→tech_stack）。"""
    cfg = config.load_config()
    assert "技术栈" in cfg["aliases"]["tech_stack"]
    assert "身份" in cfg["aliases"]["identity"]
    assert "状态" in cfg["aliases"]["status"]


def test_llm_config_has_refinement_chain():
    """LLM 配置含 refinement 链：首链 agnes 主模型 + 末链 rule drop_batch 兜底。

    2026-08-29 链序（B121）：agnes(agnes-2.5-flash) → siliconflow(DeepSeek-V4-Flash)
    → rule(drop_batch)；zhipu 免费节点已移出。
    """
    cfg = config.load_config()
    chains = cfg["llm"]["chains"]
    assert "refinement" in chains
    refinement = chains["refinement"]
    assert refinement, "refinement 链为空"
    # 语义化断言：不绑死链长（历史教训：断言 len>=3 在 lm-studio 移除后脆弱化）
    assert refinement[0]["provider"] == "agnes"
    assert refinement[0]["model"] == "agnes-2.5-flash"
    assert refinement[-1]["provider"] == "rule"
    assert refinement[-1].get("rule") == "drop_batch"


def test_llm_rules_fields():
    """LLM rules 含 timeout/retries/fallback_on/context/allowed_models。"""
    cfg = config.load_config()
    rules = cfg["llm"]["rules"]
    for k in ("timeout_s", "max_retries", "fallback_on", "context", "allowed_models"):
        assert k in rules, f"rules 缺 {k}"
    am = rules["allowed_models"]
    assert "deny_prefixes" in am and "deny_exact" in am
    assert "gemma-4-12b-qat" in am["deny_exact"]


def test_paths_returns_absolute():
    """paths 字段返回项目根绝对路径（兼容 worktree 命名，如 SGME_wt_providers）。"""
    cfg = config.load_config()
    p = cfg["paths"]
    root = Path(p["project_root"])
    assert root.is_absolute()
    # 项目根标志文件必须真实存在（llm.yaml 配置目录 + data/raw 目录）
    assert (root / "config" / "llm.yaml").exists()
    assert "data_dir" in p and "raw_dir" in p
    assert Path(p["data_dir"]).is_absolute() and Path(p["raw_dir"]).is_absolute()


def test_load_config_rejects_invalid_alias_ref(tmp_path):
    """别名表引用未知维度 id → 抛 ValueError。"""
    bad_aliases = tmp_path / "bad.yaml"
    bad_aliases.write_text("aliases:\n  unknown_dim:\n    - foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知维度"):
        config.load_config(aliases_path=str(bad_aliases))


def test_load_config_rejects_missing_yaml(tmp_path):
    """配置文件不存在 → FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        config.load_llm_config(tmp_path / "nope.yaml")


# ---------- ST-20：skills_hub.remote.source env 覆盖（GitHub 发布前脱敏） ----------

def _write_sgme_yaml(path: Path, remote_source: str = "${SGME_SKILLS_HUB_REMOTE}") -> Path:
    """写一份最小 sgme.yaml（含 skills_hub.remote.source 占位符）。"""
    path.write_text(
        "skills_hub:\n"
        "  enabled: true\n"
        "  mode: copy\n"
        "  remote:\n"
        f"    source: \"{remote_source}\"\n"
        "    cache: \"./cache/skills/\"\n",
        encoding="utf-8",
    )
    return path


def test_skills_hub_remote_source_env_override(monkeypatch, tmp_path):
    """SGME_SKILLS_HUB_REMOTE 设置时 remote.source 用 env 值（覆盖 yaml 占位符）。"""
    yaml_path = _write_sgme_yaml(tmp_path / "sgme.yaml")
    monkeypatch.setenv("SGME_SKILLS_HUB_REMOTE", "user@nas:/srv/skills-hub.git")
    cfg = config.load_config(sgme_path=str(yaml_path))
    assert cfg["skills_hub"]["remote"]["source"] == "user@nas:/srv/skills-hub.git"
    # 同步层解析同样吃到 env 值（load_config → parse_skills_hub_config 链路）
    from sgme.skills_hub.config import parse_skills_hub_config
    hub_cfg = parse_skills_hub_config(cfg)
    assert hub_cfg.remote_source == "user@nas:/srv/skills-hub.git"


def test_skills_hub_remote_source_env_not_set_falls_back(monkeypatch, tmp_path):
    """未设置 env → 回落 yaml 占位符，不报错。"""
    monkeypatch.delenv("SGME_SKILLS_HUB_REMOTE", raising=False)
    yaml_path = _write_sgme_yaml(tmp_path / "sgme.yaml")
    cfg = config.load_config(sgme_path=str(yaml_path))
    assert cfg["skills_hub"]["remote"]["source"] == "${SGME_SKILLS_HUB_REMOTE}"


def test_skills_hub_remote_source_env_empty_falls_back(monkeypatch, tmp_path):
    """env 为空串 → 视为未设置，回落 yaml 占位符。"""
    monkeypatch.setenv("SGME_SKILLS_HUB_REMOTE", "")
    yaml_path = _write_sgme_yaml(tmp_path / "sgme.yaml")
    cfg = config.load_config(sgme_path=str(yaml_path))
    assert cfg["skills_hub"]["remote"]["source"] == "${SGME_SKILLS_HUB_REMOTE}"


def test_skills_hub_remote_source_env_override_with_defaults(monkeypatch, tmp_path):
    """yaml 无 skills_hub 段（全默认兜底）时 env 仍覆盖 source。"""
    monkeypatch.setenv("SGME_SKILLS_HUB_REMOTE", "user@nas:/srv/skills-hub.git")
    yaml_path = tmp_path / "sgme.yaml"
    yaml_path.write_text("l2:\n  max_scenes: 200\n", encoding="utf-8")
    cfg = config.load_config(sgme_path=str(yaml_path))
    assert cfg["skills_hub"]["remote"]["source"] == "user@nas:/srv/skills-hub.git"


def test_persist_config_keeps_placeholder_when_env_set(monkeypatch, tmp_path):
    """env 设置期间落盘：yaml 保持占位符（env 值不进 git），内存 cfg 仍为 env 值。"""
    import yaml
    monkeypatch.delenv("SGME_CONFIG_PATH", raising=False)  # 显式 config_path 生效（conftest 默认覆盖写入路径）
    monkeypatch.setenv("SGME_SKILLS_HUB_REMOTE", "user@nas:/srv/skills-hub.git")
    yaml_path = _write_sgme_yaml(tmp_path / "sgme.yaml")
    cfg = config.load_config(sgme_path=str(yaml_path))
    assert cfg["skills_hub"]["remote"]["source"] == "user@nas:/srv/skills-hub.git"
    config.persist_config(cfg, yaml_path)
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert data["skills_hub"]["remote"]["source"] == "${SGME_SKILLS_HUB_REMOTE}"
    # 落盘过程不污染内存 cfg（env 注入值仍在，供运行时读取）
    assert cfg["skills_hub"]["remote"]["source"] == "user@nas:/srv/skills-hub.git"


def test_persist_config_writes_source_when_env_not_set(monkeypatch, tmp_path):
    """env 未设置时落盘按 cfg 现值写（update 接口改 source 正常生效，无回归）。"""
    import yaml
    monkeypatch.delenv("SGME_CONFIG_PATH", raising=False)  # 显式 config_path 生效（conftest 默认覆盖写入路径）
    monkeypatch.delenv("SGME_SKILLS_HUB_REMOTE", raising=False)
    yaml_path = _write_sgme_yaml(tmp_path / "sgme.yaml")
    cfg = config.load_config(sgme_path=str(yaml_path))
    cfg["skills_hub"]["remote"]["source"] = "user@backup:/srv/skills-hub.git"
    config.persist_config(cfg, yaml_path)
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert data["skills_hub"]["remote"]["source"] == "user@backup:/srv/skills-hub.git"


def test_apply_section_ignores_env_managed_source(monkeypatch, tmp_path):
    """env 设置期间 update 接口对 remote.source 的写入被忽略（env 优先），其余字段照常合并。"""
    monkeypatch.setenv("SGME_SKILLS_HUB_REMOTE", "user@nas:/srv/skills-hub.git")
    yaml_path = _write_sgme_yaml(tmp_path / "sgme.yaml")
    cfg = config.load_config(sgme_path=str(yaml_path))
    values = {
        "remote": {"source": "user@evil:/srv/hack.git", "branch": "dev"},
        "sync_policy": "auto",
    }
    config.apply_section(cfg, "skills_hub", values)
    assert cfg["skills_hub"]["remote"]["source"] == "user@nas:/srv/skills-hub.git"  # env 优先
    assert cfg["skills_hub"]["remote"]["branch"] == "dev"     # 非 env 字段正常合并
    assert cfg["skills_hub"]["sync_policy"] == "auto"
    # 调用方 values 不被污染（source 键仍保留）
    assert "source" in values["remote"]


# ---------- refine.llm_override 加载（B121 补遗：合并函数曾静默丢弃该键） ----------

def test_refine_llm_override_loaded_from_yaml(tmp_path):
    """refine.llm_override 必须从 sgme.yaml 加载——T-43 防劫持语义依赖它
    （显式 override 优先于 agent_model 声明）。"""
    yaml_path = tmp_path / "sgme.yaml"
    yaml_path.write_text(
        "refine:\n"
        "  refine_on_append: false\n"
        "  llm_override:\n"
        "    provider: agnes\n"
        "    model: agnes-2.5-flash\n"
        "    max_tokens: 8192\n",
        encoding="utf-8",
    )
    cfg = config.load_config(sgme_path=str(yaml_path))
    override = cfg["refine"]["llm_override"]
    assert override["provider"] == "agnes"
    assert override["model"] == "agnes-2.5-flash"
    assert override["max_tokens"] == 8192


def test_refine_llm_override_invalid_falls_back_to_empty(tmp_path):
    """llm_override 结构非法（缺 provider/model）→ 回退空 dict（跟随 agent 声明）。"""
    yaml_path = tmp_path / "sgme.yaml"
    yaml_path.write_text(
        "refine:\n"
        "  llm_override:\n"
        "    provider: agnes\n",
        encoding="utf-8",
    )
    cfg = config.load_config(sgme_path=str(yaml_path))
    assert cfg["refine"]["llm_override"] == {}
