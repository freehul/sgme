"""SGME 配置加载：启动时读取 config/llm.yaml + registry/*.yaml。

输出统一字典供各模块使用。T-142 起程序资源（config/registry/templates/prompts）
内迁至 sgme/resources/（包内，随 wheel 分发）；只读资源经 RESOURCE_ROOT 取，
可写配置（sgme.yaml/.env/llm.yaml/providers.yaml）经 _config_overlay_dir() 取。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
import logging

logger = logging.getLogger("sgme.config")

# 项目根目录（本文件位于 sgme/config.py，根 = 父目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# T-23 标准安装布局：SGME_HOME 重定向用户数据/配置（程序资源不跟随）
# 未设 → 项目根（零回归）；设置 → data/raw/logs/config 落到 $SGME_HOME
# 用法示例（Windows 惯例）：程序 %LOCALAPPDATA%\sgme，数据/配置 ~\.sgme
_env_home = os.environ.get("SGME_HOME", "").strip()
SGME_HOME = Path(_env_home).expanduser() if _env_home else None
# 用户根（相对路径基准）：未设 SGME_HOME = 项目根（零回归），设置后 = $SGME_HOME
USER_ROOT = SGME_HOME if SGME_HOME is not None else PROJECT_ROOT
_USER_ROOT = USER_ROOT

# ---------- T-142 修复 wheel 打包崩溃 ----------
# 原 config/registry/templates/prompts 位于项目根同级顶层目录，被 pyproject exclude
# 且未声明 package-data → wheel 不携带 → pip install . 后启动报
# FileNotFoundError: config/llm.yaml。现统一内迁至 sgme/resources/（包内），
# 随 wheel 分发；运行时只读资源从 RESOURCE_ROOT 取，
# 可写配置（sgme.yaml/.env/llm.yaml/providers.yaml）走 _config_overlay_dir() 覆盖目录。
PKG_DIR = Path(__file__).resolve().parent
RESOURCE_ROOT = PKG_DIR / "resources"


def _config_overlay_dir() -> Path:
    """可写配置覆盖目录（sgme.yaml/.env/llm.yaml/providers.yaml 运行时落盘位置）。

    解析优先级（与 T-23 标准安装布局一致）：
      1. SGME_HOME 设置 → $SGME_HOME/config（Docker/NAS/显式重定向，挂载卷可持久化）
      2. 源码开发态（项目根含 pyproject.toml/.git）→ 沿用项目根 config/（零回归，
         不污染包内只读资源）
      3. 只读安装（wheel/pip 安装到 site-packages）→ ~/.sgme/config（用户主目录，可写）
    """
    if SGME_HOME is not None:
        return SGME_HOME / "config"
    # 源码开发态判定：项目根存在 pyproject.toml 或 .git
    is_repo = (PROJECT_ROOT / "pyproject.toml").exists() or (PROJECT_ROOT / ".git").exists()
    if is_repo:
        return PROJECT_ROOT / "config"
    return Path.home() / ".sgme" / "config"


# 只读程序资源（随包分发，sgme/resources/ 内）
BUNDLE_LLM_CONFIG = RESOURCE_ROOT / "config" / "llm.yaml"
BUNDLE_PROVIDERS_CONFIG = RESOURCE_ROOT / "config" / "providers.yaml"
# 默认读取路径 = 包内默认；测试可 monkeypatch DEFAULT_* 重定向到临时文件
DEFAULT_LLM_CONFIG = BUNDLE_LLM_CONFIG
DEFAULT_PROVIDERS_CONFIG = BUNDLE_PROVIDERS_CONFIG
DEFAULT_DIMENSIONS_FILE = RESOURCE_ROOT / "registry" / "dimensions.yaml"
DEFAULT_ALIASES_FILE = RESOURCE_ROOT / "registry" / "aliases.yaml"
# 检索术语别名表（ST-19：查询端旧术语 → 标准术语归一化；与维度别名表语义不同）
DEFAULT_TERM_ALIASES_FILE = RESOURCE_ROOT / "registry" / "term_aliases.yaml"
# 关系类型注册表（T-14：wiki_links.rel_type 枚举权威来源，DB 不做 CHECK 约束）
DEFAULT_RELATIONS_FILE = RESOURCE_ROOT / "registry" / "relations.yaml"
# 可写用户配置（sgme.yaml/.env）跟随覆盖目录，不进包内只读资源
DEFAULT_SGME_CONFIG = _config_overlay_dir() / "sgme.yaml"
# 包内默认 sgme.yaml 模板（随 wheel 分发；覆盖目录缺失时回退，保证零回归 + 首次启动可用）
BUNDLE_SGME_CONFIG = RESOURCE_ROOT / "config" / "sgme.yaml"
# 密钥文件（gitignore；Gateway 自持，不依赖外部应用/服务环境注入）
SECRETS_FILE = _config_overlay_dir() / ".env"


def load_env_file() -> None:
    """启动时加载 config/.env 到进程环境（setdefault——已有环境变量优先）。

    密钥单一来源：SGME 自持密钥文件，服务环境注入（nssm）仅作可选叠加。
    2026-08-07 事故教训：nssm AppEnvironmentExtra 覆盖式 set 曾冲掉
    DEEPSEEK_API_KEY → 提炼链 401 静默降级本地模型。
    """
    if not SECRETS_FILE.exists():
        return
    for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env_file()

# L2 默认配置（sgme.yaml 缺失时兜底，保证测试可在无 sgme.yaml 环境运行）
# v0.7 扩展模块默认配置（sgme.yaml 缺失时兜底）
DEFAULT_WIKI_CONFIG = {
    "enabled": True,
}

DEFAULT_SKILLS_HUB_CONFIG = {
    "enabled": False,
    "path": "",
    "mode": "map",
    "sync_policy": "manual",
    # 0.8 ST-11：remote 段默认值（copy 模式同步；缺失时按此兜底）
    "remote": {
        "source": "",
        "cache": "",
        "branch": "main",
        "conflict_policy": "local_wins",
        "timeout_s": 60,
        "backup_refs": True,
    },
}

# ST-34：自动更新检测配置段默认值（sgme.yaml 缺失时兜底）
DEFAULT_UPDATE_CHECK_CONFIG = {
    "enabled": True,
    "interval_hours": 24,
    "source": "github",  # github 优先；可换 gitee
}


# ---------- env 覆盖字段（ST-20，2026-08-11：GitHub 发布前脱敏） ----------
# 键 = 配置点分路径，值 = 环境变量名。
# 语义：读取时 env 值优先于 yaml（部署时注入真实 NAS 地址，仓库内只留占位符）；
#       落盘（persist_config）恢复文件现值——env 注入值仅存于进程内存，防泄漏进 git；
#       更新接口（apply_section）在 env 设置期间忽略该字段（env 优先）。
ENV_OVERRIDES: dict[str, str] = {
    "skills_hub.remote.source": "SGME_SKILLS_HUB_REMOTE",
    "backup.remote_dir": "SGME_BACKUP_REMOTE",
}


def _get_dotted(data: dict, dotted: str) -> Any:
    """按点分路径读取嵌套 dict 值（路径断裂返回 None）。"""
    node: Any = data
    for p in dotted.split("."):
        if not isinstance(node, dict) or p not in node:
            return None
        node = node[p]
    return node


def _set_dotted(data: dict, dotted: str, value: Any) -> None:
    """按点分路径写入嵌套 dict（中间节点缺失时补 dict）。"""
    parts = dotted.split(".")
    node = data
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = value


def env_override_value(dotted: str) -> str | None:
    """读取 env 覆盖字段的环境变量值（未设置/空串 → None，回落 yaml/默认）。"""
    name = ENV_OVERRIDES.get(dotted)
    if not name:
        return None
    return get_env(name)


def _merge_skills_hub_config(user_cfg: dict | None) -> dict:
    """合并 skills_hub 段默认值与用户配置（remote 子段深层合并，0.8 ST-11）。"""
    base = {
        "enabled": DEFAULT_SKILLS_HUB_CONFIG["enabled"],
        "path": DEFAULT_SKILLS_HUB_CONFIG["path"],
        "mode": DEFAULT_SKILLS_HUB_CONFIG["mode"],
        "sync_policy": DEFAULT_SKILLS_HUB_CONFIG["sync_policy"],
        "remote": dict(DEFAULT_SKILLS_HUB_CONFIG["remote"]),
    }
    if not isinstance(user_cfg, dict):
        return _apply_skills_hub_env_overrides(base)
    for key in ("enabled", "path", "mode", "sync_policy"):
        if key in user_cfg:
            base[key] = user_cfg[key]
    if isinstance(user_cfg.get("remote"), dict):
        # 旧配置无 remote 子段/缺字段 → 新字段按默认值兜底（§7 兼容性）
        base["remote"].update(
            {k: v for k, v in user_cfg["remote"].items() if v is not None}
        )
    return _apply_skills_hub_env_overrides(base)


def _apply_skills_hub_env_overrides(cfg: dict) -> dict:
    """ST-20：env 覆盖 skills_hub 段字段（当前仅 remote.source；env 值优先于 yaml）。"""
    env_source = env_override_value("skills_hub.remote.source")
    if env_source:
        cfg["remote"]["source"] = env_source
    return cfg

# ST-36 M1：skills 管理模块段解析（缺失兜底全默认；取值非法降级告警不阻断启动）
def _parse_skills_section(user_cfg) -> dict:
    """解析 skills 段（委托 parse_skills_config，转回 dict 并透传写侧扩展键）。"""
    import logging

    from sgme.skills.config import DEFAULT_SKILLS_CONFIG, parse_skills_config

    try:
        sc = parse_skills_config({"skills": user_cfg} if isinstance(user_cfg, dict) else {})
        parsed = {
            "enabled": sc.enabled,
            "source_dirs": list(sc.source_dirs),
            "budget": sc.budget,
            "vector_cache_policy": sc.vector_cache_policy,
        }
        if isinstance(user_cfg, dict):
            for k, v in user_cfg.items():
                parsed.setdefault(k, v)
        return parsed
    except ValueError as e:
        logging.getLogger("sgme.config").warning("skills 配置非法，已兜底默认: %s", e)
        return dict(DEFAULT_SKILLS_CONFIG)


DEFAULT_LOGGING_CONFIG = {
    "level": "INFO",
    "format": "console",
    "output": None,
}

DEFAULT_L2_CONFIG = {
    "max_scenes": 200,
    "warn_thresholds": {"yellow": 150, "orange": 180, "red": 200},
}

# 场景主动治理（T-97 治本）：后台自动合并 + 归档相似场景
DEFAULT_SCENE_GC_CONFIG = {
    "enabled": True,            # 总开关
    "merge_threshold": 0.70,    # 相似度入选阈值（>= 才合并）；B117 由 0.80 下调，收掉 AIRDT/SGME/hermes 等弱相似度重复场景（下限 min_threshold=0.70）
    "min_threshold": 0.70,      # 兜底下限（候选不足时可降至此重算，当前未启用降级）
    "trigger_at": None,         # 仅当 active >= 此值才执行；None = 回退 l2.warn_thresholds.orange
    "max_merges": 20,           # 单次合并上限（防一次 LLM 消耗过大）
}


def _merge_scene_gc_config(user_cfg: dict | None) -> dict:
    """合并 scene_gc 段默认值与用户配置（T-97 场景主动治理）。

    类型校验规则：
    - enabled：必须 bool，否则回退默认
    - merge_threshold / min_threshold：数值，否则回退默认
    - trigger_at：int 或 None（None = 运行时回退 l2 橙色阈值），否则回退默认
    - max_merges：正整数，否则回退默认
    """
    base = dict(DEFAULT_SCENE_GC_CONFIG)
    if not isinstance(user_cfg, dict):
        return base
    if isinstance(user_cfg.get("enabled"), bool):
        base["enabled"] = user_cfg["enabled"]
    if isinstance(user_cfg.get("merge_threshold"), (int, float)):
        base["merge_threshold"] = float(user_cfg["merge_threshold"])
    if isinstance(user_cfg.get("min_threshold"), (int, float)):
        base["min_threshold"] = float(user_cfg["min_threshold"])
    if user_cfg.get("trigger_at") is None or isinstance(user_cfg.get("trigger_at"), int):
        base["trigger_at"] = user_cfg.get("trigger_at")
    if isinstance(user_cfg.get("max_merges"), int) and user_cfg["max_merges"] > 0:
        base["max_merges"] = user_cfg["max_merges"]
    return base

# search 段默认兜底（向量检索 + RRF）
DEFAULT_SEARCH_CONFIG = {
    "vector": {
        "enabled": True,
        "model": "text-embedding-nomic-embed-text-v1.5",
    },
    "rrf": {"k": 60},
    # ST-38 T-134：图召回 v1（memory_edges 1-hop 邻居增量候选，独立权重键）
    "graph": {
        "enabled": True,   # 图路开关（A/B 双臂对照用；关闭时行为与 T-133 前逐字节等价）
        "weight": 1.0,     # graph 路 RRF 贡献权重（独立配置键；1.0=与 bm25 rank0 同权，
                           # A/B 实测最优：<1 排不进 top-10 无效果，>1.5 挤掉直接命中致 precision 劣化）
        "top_n": 20,       # graph 候选上限（防邻居洪泛；final limit 仍由调用方截断）
        "fill_only": True, # T-134 A/B 定夺（生产默认）：True=图候选 rank 从 len(bm25) 起算
                           # （fill-only 语义，只在直接命中稀疏时填空位、密集时零干预）——
                           # A/B 实测唯一同时满足「全量 recall@5 不劣化 + scene 类提升」的形态；
                           # False=与直接命中同台竞争（scene 增益更大但单跳 recall 劣化，弃用）
        # T-137 图召回 v2（纳入语义边）：关系级过滤/加权（edge_dao.neighbors 透传）
        "exclude_relations": ["contradicts"],  # 否定边不参与联想召回（矛盾是负信号，召回污染结果）
        "relation_weights": {"belongs_to": 0.3},  # 共现边尺度压缩：LLM 置信 0-1 vs 场景数 1-N
                            # （语义边 similar/causes 保持 1.0；supersedes/evolves_from 1.0）
    },
    # ST-39 T-138：有效期间过滤（memories.valid_to 过期不召回；NULL=永久有效，
    # 存量记忆全 NULL → 过滤零影响，T-129 基线天然无回归）
    "valid_period": {
        "enabled": True,
    },
}

# 数据目录（T-23：未设 SGME_HOME = 项目根，零回归；设置后跟随 $SGME_HOME）
DATA_DIR = _USER_ROOT / "data"
RAW_DIR = _USER_ROOT / "raw"
# 日志目录（T-23：跟随 SGME_HOME，程序与运行数据分离）
LOG_DIR = _USER_ROOT / "logs"
# 角色层目录（T-35：角色卡 = 项目内文件随 git 管理；persona = 运行数据）
ROLES_DIR = _USER_ROOT / "roles"
PERSONA_DIR = DATA_DIR / "personas"

# care 段默认兜底（T-35：角色层 + 关怀信号，ST-25）
DEFAULT_CARE_CONFIG = {
    "enabled": True,
    "persona_max_chars": 2000,   # persona 物化上限（TencentDB 方法论）
}

# 备份段默认兜底（0.8 方案 B 每日自动备份；旧版 schedule cron 格式已升级为 HH:MM）
DEFAULT_BACKUP_CONFIG = {
    "enabled": True,
    "schedule": "04:00",       # 每日 HH:MM（本地时区），避开 Dream 03:00
    "level": "incremental",    # incremental / full / monthly
    "dir": "data/backups",     # 本地快照目录（既有契约字段，operations/backup 读取）
    "keep_full": 7,            # full 快照轮转保留份数
    "remote_dir": "",          # 异地目录（本机另一盘/NAS 挂载），空 = 跳过
    "raw_cold_days": 90,       # 原始层冷归档阈值（保留）
}

# L1 分块默认兜底（甜点区实测：qwythos-9b-v2-i1 输入 6-8K 字符最佳，2026-08-04）
# chunk_size：单块字符上限（超过即分块）
# overlap：相邻块重叠字符（防切碎话题）
DEFAULT_L1_CONFIG = {
    "chunk_size": 8000,
    "overlap": 1500,
}

# L1.5 候选池向量预筛默认兜底（2026-08-12 T-25 成本治理）
# enabled：总开关（默认关=回退全量召回现状；生产 sgme.yaml 显式开启）
# vector_top_k：向量语义候选数；dimension_top_n：维度 OR 候选截断数（priority 降序）
# 预筛开启时单记忆候选 ≤ vector_top_k + dimension_top_n，embed 不可达自动回退全量（宁贵勿漏）
# fallback：embed 不可达时的降级策略（2026-08-16 T-4x 成本治理，搬家 401 实锤）：
#   - full_recall（默认，保持历史行为）：回退维度 OR 全量召回——单次 l1_conflict 可达
#     80 万+ tokens（08-11/12 单日 9800 万 tokens 的元凶），宁贵勿漏
#   - skip_conflict：跳过冲突检测直接 store（不调 LLM，零额外 token）——embed 不可达时
#     说明向量链路本身异常，冲突检测的召回质量不可信，先保数据落地，事后补检
DEFAULT_L15_CONFIG = {
    "prescreen": {
        "enabled": False,
        "vector_top_k": 50,
        "dimension_top_n": 50,
        "fallback": "full_recall",
    },
    # T-135 语义边（搭 l1_conflict 顺风车，零新增调用）：关系判定写入 memory_edges
    # source='l1_conflict' 可溯源关闭（delete_edges_by_source）；min_weight 为 LLM confidence
    # 阈值（脏边控制，见 l15.py _write_semantic_edges）
    "semantic_edges": {
        "enabled": True,
        "min_weight": 0.6,
    },
}

# 提炼调度默认兜底（2026-08-04 新增：文件到达联动 + Batch 兜底扫描）
# refine_on_append：append 写入后是否立即触发该文件提炼（默认关——高频写入场景每轮提炼浪费）
# batch_scan.enabled：内部定时器扫 status=new 批量提炼（兜底，防会话异常退出导致记忆滞留）
# batch_scan.interval_min：扫描间隔（分钟）
DEFAULT_REFINE_CONFIG = {
    "refine_on_append": False,
    "batch_scan": {
        "enabled": True,
        "interval_min": 10,
    },
    # T-43 提炼 LLM 动态链（2026-08-13 用户定）：
    # llm_override 为空 = 跟随 agent 声明模型（agent_model，provider/model）；
    # 用户指定专用提炼 LLM 时填 {"provider": "...", "model": "...", "max_tokens": N}，
    # 此时 agent 声明模型降为备用。provider 必须在 providers.yaml 有连接。
    "llm_override": {},
}

# ST-39 T-139 Guardrail：敏感信息写前/召回后过滤层（顶层配置，pipeline 与 search 共用）
# ⚠️ 默认关（灰度）：误脱敏可控——先观察规则命中率再开；规则见 operations/guardrail.py
DEFAULT_GUARDRAIL_CONFIG = {
    "enabled": False,          # 总开关（默认关：行为与 T-139 前一致）
    "write_mode": "mask",      # 写前：block=拦截丢弃 | mask=脱敏放行
    "read_mode": "filter",     # 召回后：filter=敏感记忆不返回 | off
    "llm_fallback": {"enabled": False},  # 规则未命中时的 LLM 兜底判定（慢，默认关）
}

# ST-39 T-140 多 Agent scope：写侧按来源 agent 打标（raw_files.agent_id → memories.agent_tag），
# 读侧按请求方 agent 隔离（NULL=历史无主记忆全通 + 'default'=共享 + 同 agent 可见）
# ⚠️ 默认关（灰度）：enabled=False 时全通行为与 T-140 前逐字节一致（Hermes/DSH/Trae/WorkBuddy 共存不受影响）
DEFAULT_AGENT_SCOPE_CONFIG = {
    "enabled": False,          # 隔离开关（默认关=当前全通行为保留）
}

# 限流段默认兜底（§6 限流，T-7）：默认 120 req/min/Key；0 = 关闭
DEFAULT_SERVER_CONFIG = {
    "rate_limit_per_min": 120,
}

# Dream 夜间整理段默认兜底（0.8 ST-10，设计文档 SGME-Dream夜间整理设计-v0.1.md §3）
# enabled：总开关；schedule：每日执行时刻（本地时区 HH:MM，空 = 不自动只手动）
# max_files：单次抽取上限；ttl_mark：是否执行 TTL 主动标记
# archive_days：冷归档阈值（raw_files refined 且 started_at 超期天数）
# report_dir：日报目录（相对项目根，如 data/reports/）
DEFAULT_DREAM_CONFIG = {
    "enabled": True,
    "schedule": "03:00",
    "max_files": 200,
    "ttl_mark": True,
    "archive_days": 90,
    "report_dir": "data/reports/",
}


def _read_yaml(path: Path) -> Any:
    """读取 YAML 文件并返回解析结果。"""
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_llm_config(
    path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> dict:
    """加载 LLM 降级链配置（v0.7 §13：llm.yaml 编排层 + providers.yaml 连接层合并）。

    返回字段：
      chains: dict[str, list[dict]]   # 各链 provider 列表（连接字段已注入）
      rules: dict                     # timeout/retries/budget/allowed_models

    合并规则：
      1. 读取 providers.yaml 建供应商连接表；文件缺失 → 原样返回（兼容旧内联结构）
      2. chains 节点按 provider 名注入连接字段（base_url/api_key_env/context_window 等）
      3. 节点内已显式字段优先（新旧格式可混用）
      4. 引用未知供应商且节点无内联 base_url → ValueError（rule 节点除外）
    """
    cfg_path = Path(path) if path else DEFAULT_LLM_CONFIG
    # T-142：未显式重定向（含测试 monkeypatch DEFAULT_LLM_CONFIG）时才考虑运行时覆盖目录。
    # 覆盖文件损坏/结构非法时回退包内默认，避免启动崩溃（NAS 旧布局遗留无效 llm.yaml 等）。
    if path is None and cfg_path == BUNDLE_LLM_CONFIG:
        ov = _config_overlay_dir() / "llm.yaml"
        if ov.exists() and ov.stat().st_size > 0:
            try:
                _ov = _read_yaml(ov)
                if isinstance(_ov, dict) and "chains" in _ov and "rules" in _ov:
                    cfg_path = ov
                else:
                    logger.warning("覆盖 llm.yaml 缺 chains/rules，回退包内默认: %s", ov)
            except Exception as e:
                logger.warning("覆盖 llm.yaml 读取失败，回退包内默认: %s (%s)", ov, e)
    def _build(cfg: Path) -> dict:
        """读取 + 合并供应商连接表 + 白名单校验；任一步 ValueError 由调用方决策。"""
        _raw = _read_yaml(cfg)
        if not isinstance(_raw, dict) or "chains" not in _raw or "rules" not in _raw:
            raise ValueError(f"LLM 配置缺失必要字段: {cfg}")
        # 供应商连接表（v0.7 §13.1）
        providers = load_providers_config(providers_path)
        if providers:
            for chain_name, nodes in _raw.get("chains", {}).items():
                if not isinstance(nodes, list):
                    continue
                for node in nodes:
                    if isinstance(node, dict):
                        _merge_provider_into_node(node, providers, chain_name)
            # T-43：连接表随配置保留（动态链构造 resolve_refinement_chain 用）
            _raw["providers"] = providers
        # 白名单校验：命中 deny_prefixes/deny_exact 的模型拒绝加载（铁律 #9）
        from sgme.llm.chain import validate_models

        validate_models(_raw)
        return _raw

    try:
        raw = _build(cfg_path)
    except ValueError as e:
        # T-142 后续（2026-09-04 生产实证）：覆盖层的历史 llm.yaml 可能引用
        # providers.yaml 中已移除的供应商（如 NAS 遗留 zhipu 单链），合并即抛错
        # → 服务启动崩溃 → 自动更新健康验证失败并回滚。
        # 覆盖层属用户可编辑数据，配置漂移不应导致服务不可用：降级为
        #「警告 + 回退包内默认」，保证与升级前行为一致。
        # 包内默认自身非法则照常抛出（属发布缺陷，必须暴露而非掩盖）。
        if cfg_path == BUNDLE_LLM_CONFIG:
            raise
        logger.warning(
            "覆盖 llm.yaml 与当前供应商表不兼容（%s），回退包内默认配置: %s -> %s",
            e,
            cfg_path,
            BUNDLE_LLM_CONFIG,
        )
        cfg_path = BUNDLE_LLM_CONFIG
        raw = _build(cfg_path)
    return raw


def load_providers_config(path: Path | str | None = None) -> dict:
    """加载 config/providers.yaml 供应商连接表（v0.7 §13.1/13.2）。

    返回 {provider_name: {连接字段...}}；文件缺失返回 {}（触发 llm.yaml 内联回退）。
    结构校验：必须含顶层 `providers` 字典，每个供应商节点必须含 base_url。
    """
    cfg_path = Path(path) if path else DEFAULT_PROVIDERS_CONFIG
    # T-142：未显式重定向时才考虑运行时覆盖目录（sentinel 守卫，测试 monkeypatch 不受影响）
    if path is None and cfg_path == BUNDLE_PROVIDERS_CONFIG:
        ov = _config_overlay_dir() / "providers.yaml"
        if ov.exists() and ov.stat().st_size > 0:
            try:
                _ov = _read_yaml(ov)
                if isinstance(_ov, dict) and isinstance(_ov.get("providers"), dict):
                    cfg_path = ov
                else:
                    logger.warning("覆盖 providers.yaml 缺 providers 段，回退包内默认: %s", ov)
            except Exception as e:
                logger.warning("覆盖 providers.yaml 读取失败，回退包内默认: %s (%s)", ov, e)
    if not cfg_path.exists():
        return {}
    raw = _read_yaml(cfg_path)
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        raise ValueError(f"providers.yaml 格式错误: {cfg_path}")
    providers = raw["providers"]
    for name, p in providers.items():
        if not isinstance(p, dict):
            raise ValueError(f"供应商 {name!r} 配置必须是字典: {cfg_path}")
        if "base_url" not in p:
            raise ValueError(f"供应商 {name!r} 缺 base_url: {cfg_path}")
    return providers


def write_providers_config(providers: dict[str, dict], path: Path | str | None = None) -> Path:
    """写回 config/providers.yaml 供应商连接表（供应商管理页添删用）。

    - 密钥不落盘铁律：#10——本函数只落**连接字段**（含 api_key_env 环境变量名），
      调用方负责校验不得写入明文 key 值。
    - 用 ``yaml.safe_dump`` 落盘会丢失原有注释（项目仅依赖 pyyaml），
      作为管理功能取舍，写回前基于现值 merge。
    - **保留 embedding 段**（2026-08-13 修复）：providers.yaml 顶层除 ``providers``
      外还有 ``embedding`` 段（向量提供商，T-43）。写回时只覆盖 ``providers``，
      其余段原样保留，避免供应商管理操作抹掉向量配置。

    Args:
        providers: {provider_name: {连接字段...}}，须含 base_url。
        path: 目标文件；默认 DEFAULT_PROVIDERS_CONFIG。

    Returns:
        写入的文件路径。
    """
    # T-142：未重定向（含测试 monkeypatch DEFAULT_PROVIDERS_CONFIG）→ 写到可写覆盖目录，
    # 不写包内只读默认；读现有段优先覆盖目录，否则包内默认（保留 embedding 等其余段）
    if path is not None:
        cfg_path = Path(path)
    elif DEFAULT_PROVIDERS_CONFIG == BUNDLE_PROVIDERS_CONFIG:
        cfg_path = _config_overlay_dir() / "providers.yaml"
    else:
        cfg_path = Path(DEFAULT_PROVIDERS_CONFIG)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"providers": providers}
    src = cfg_path if cfg_path.exists() else BUNDLE_PROVIDERS_CONFIG
    if src.exists():
        try:
            existing = _read_yaml(src)
            if isinstance(existing, dict):
                for k, v in existing.items():
                    if k != "providers":
                        data[k] = v
        except Exception:
            pass  # 文件损坏 → 仅写 providers（不丢主数据）
    for name, p in providers.items():
        if not isinstance(p, dict):
            raise ValueError(f"供应商 {name!r} 配置必须是字典: {cfg_path}")
        if "base_url" not in p:
            raise ValueError(f"供应商 {name!r} 缺 base_url: {cfg_path}")
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    logger.info("供应商连接表已落盘: %s（%s 个供应商）", cfg_path, len(providers))
    return cfg_path


# ---------- LLM 链编排字段白名单 ----------
# 连接字段（base_url/api_key_env/context_window/timeout_s/...）由 providers.yaml 注入，
# 写盘时剥离，防 WebUI 编辑把注入字段落盘（内联旧值会覆盖 providers.yaml 新值，
# 复发「降级链与 provider 不一致」）。rule 节点只含 provider+rule。
CHAIN_ORCH_FIELDS = {
    "provider", "model", "max_tokens", "extra_body", "sampling", "rule",
}


def _strip_chain_conn_fields(node: dict) -> dict:
    """剥离链节点连接字段，只留编排字段（与 load 注入语义对称）。"""
    return {k: v for k, v in node.items() if k in CHAIN_ORCH_FIELDS}


def write_llm_config(chains: dict[str, list[dict]], path: Path | str | None = None) -> Path:
    """写回 config/llm.yaml 降级链（T-44 降级链编辑用）。

    - 只覆盖顶层 ``chains`` 段；``rules`` 等其余段原样保留（避免管理操作抹掉链级参数）。
    - 密钥/连接字段不落 llm.yaml——链节点只存 provider 名 + 模型/采样等编排字段，
      连接信息由 providers.yaml 注入（与 load_llm_config 的合并语义对称）。
    - **写盘前剥离连接字段**（2026-08-14 修复）：入参节点可能已带 load 时注入的
      连接字段（WebUI 编辑链路传回），统一按 ``CHAIN_ORCH_FIELDS`` 白名单清洗，
      防止内联旧值覆盖 providers.yaml 的新配置。

    Args:
        chains: {chain_name: [节点...]}，每个节点须含 provider（rule 节点含 rule）。
        path: 目标文件；默认 DEFAULT_LLM_CONFIG。

    Returns:
        写入的文件路径。
    """
    # T-142：未重定向（含测试 monkeypatch DEFAULT_LLM_CONFIG）→ 写到可写覆盖目录，
    # 不写包内只读默认；读现有段优先覆盖目录，否则包内默认（保留 rules 等其余段）
    if path is not None:
        cfg_path = Path(path)
    elif DEFAULT_LLM_CONFIG == BUNDLE_LLM_CONFIG:
        cfg_path = _config_overlay_dir() / "llm.yaml"
    else:
        cfg_path = Path(DEFAULT_LLM_CONFIG)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    src = cfg_path if cfg_path.exists() else BUNDLE_LLM_CONFIG
    if src.exists():
        try:
            existing = _read_yaml(src)
            if isinstance(existing, dict):
                data = existing
        except Exception:
            data = {}
    cleaned: dict[str, list[dict]] = {}
    for name, nodes in chains.items():
        if isinstance(nodes, list):
            cleaned[name] = [
                _strip_chain_conn_fields(n) if isinstance(n, dict) else n for n in nodes
            ]
        else:
            cleaned[name] = nodes
    data["chains"] = cleaned
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    logger.info("降级链已落盘: %s（链: %s）", cfg_path, sorted(cleaned.keys()))
    return cfg_path


def _merge_provider_into_node(node: dict, providers: dict, chain_name: str) -> None:
    """把供应商连接字段注入链节点（节点内联字段优先，向后兼容旧 llm.yaml）。

    - rule 节点（provider=rule，无连接需求）跳过
    - 供应商在表中 → 注入其全部连接字段（name 由节点 provider 字段承载，不重复）
    - 供应商不在表中：节点带内联 base_url → 保持原样；否则抛 ValueError
    """
    p_name = node.get("provider")
    if not p_name or p_name == "rule":
        return
    p = providers.get(p_name)
    if p is None:
        # 供应商表未定义：节点内联连接字段兜底（兼容旧 llm.yaml 内联结构）
        if "base_url" not in node:
            raise ValueError(
                f"链 {chain_name} 节点引用了未知供应商 {p_name!r}"
                "（providers.yaml 未定义且节点无内联 base_url）"
            )
        return
    for k, v in p.items():
        if k == "name":
            continue
        if k not in node:
            node[k] = v


def load_dimensions(path: Path | str | None = None) -> list[dict]:
    """加载维度注册表，返回维度列表。"""
    cfg_path = Path(path) if path else DEFAULT_DIMENSIONS_FILE
    raw = _read_yaml(cfg_path)
    if not isinstance(raw, dict) or "dimensions" not in raw:
        raise ValueError(f"维度注册表格式错误: {cfg_path}")
    dims = raw["dimensions"]
    if not isinstance(dims, list) or not dims:
        raise ValueError(f"维度注册表为空: {cfg_path}")
    return dims


def load_aliases(path: Path | str | None = None) -> dict[str, list[str]]:
    """加载别名表，返回 {dimension_id: [alias,...]}。"""
    cfg_path = Path(path) if path else DEFAULT_ALIASES_FILE
    raw = _read_yaml(cfg_path)
    if not isinstance(raw, dict) or "aliases" not in raw:
        raise ValueError(f"别名表格式错误: {cfg_path}")
    return raw["aliases"]


def load_term_aliases(path: Path | str | None = None) -> dict[str, str]:
    """加载检索术语别名表（ST-19），返回 {旧术语: 标准术语}。

    与 load_aliases（维度别名表）语义不同：本表是检索层查询端归一化用，
    键为旧术语（如 daemon / SGME Server），值为标准术语（如 gateway）。
    键值均须为字符串，否则抛 ValueError（防格式漂移）。
    """
    cfg_path = Path(path) if path else DEFAULT_TERM_ALIASES_FILE
    raw = _read_yaml(cfg_path)
    if not isinstance(raw, dict) or "term_aliases" not in raw:
        raise ValueError(f"检索术语别名表格式错误: {cfg_path}")
    aliases = raw["term_aliases"]
    if not isinstance(aliases, dict):
        raise ValueError(f"检索术语别名表须为字典: {cfg_path}")
    for k, v in aliases.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"检索术语别名表键值须为字符串: {cfg_path} ({k!r} -> {v!r})")
    return aliases


def load_relations(path: Path | str | None = None) -> list[dict]:
    """加载关系类型注册表（T-14），返回关系类型列表。

    与维度注册表同模式；wiki_links.rel_type 枚举由此文件定义（注册表权威，
    DB 不做 CHECK 约束，支持热扩展）。
    """
    cfg_path = Path(path) if path else DEFAULT_RELATIONS_FILE
    raw = _read_yaml(cfg_path)
    if not isinstance(raw, dict) or "relations" not in raw:
        raise ValueError(f"关系注册表格式错误: {cfg_path}")
    rels = raw["relations"]
    if not isinstance(rels, list) or not rels:
        raise ValueError(f"关系注册表为空: {cfg_path}")
    return rels


def load_sgme_config(path: Path | str | None = None) -> dict:
    """加载主配置 sgme.yaml（L2 场景聚合阈值等）。

    文件缺失时返回 DEFAULT_L2_CONFIG 兜底（保证测试可在无 sgme.yaml 环境运行）。
    目前识别顶层 `l2` 与 `search` 字段，其余字段忽略。
    """
    cfg_path = Path(path) if path else DEFAULT_SGME_CONFIG
    # T-142：覆盖目录（SGME_HOME/config 或 ~/.sgme/config）优先；缺失时回退包内默认模板，
    # 保证源码开发态 / 首次 wheel 安装读到与旧 repo/config/sgme.yaml 同内容的默认配置（零回归）
    if not path and not cfg_path.exists() and BUNDLE_SGME_CONFIG.exists():
        cfg_path = BUNDLE_SGME_CONFIG
    if not cfg_path.exists():
        return {
            "l2": dict(DEFAULT_L2_CONFIG),
            "search": _merge_search_config(None),
            "backup": dict(DEFAULT_BACKUP_CONFIG),
            "l1": dict(DEFAULT_L1_CONFIG),
            "l15": dict(DEFAULT_L15_CONFIG),
            "refine": _merge_refine_config(None),
            "server": dict(DEFAULT_SERVER_CONFIG),
            "dream": _merge_dream_config(None),
            "care": _merge_care_config(None),
            "update_check": dict(DEFAULT_UPDATE_CHECK_CONFIG),
            "scene_gc": _merge_scene_gc_config(None),
        }
    raw = _read_yaml(cfg_path)
    if not isinstance(raw, dict):
        raise ValueError(f"sgme.yaml 格式错误: {cfg_path}")
    # 缺 l2 段时用默认兜底
    if "l2" not in raw or not isinstance(raw["l2"], dict):
        raw["l2"] = dict(DEFAULT_L2_CONFIG)
    else:
        # 合并默认值（保证缺字段时兜底）
        merged = dict(DEFAULT_L2_CONFIG)
        merged.update(raw["l2"])
        raw["l2"] = merged
    # search 段合并默认值
    raw["search"] = _merge_search_config(raw.get("search"))
    # backup 段合并默认值
    raw["backup"] = _merge_backup_config(raw.get("backup"))
    # l1 段合并默认值
    raw["l1"] = _merge_l1_config(raw.get("l1"))
    # l15 段合并默认值（T-25 候选池向量预筛）
    raw["l15"] = _merge_l15_config(raw.get("l15"))
    # refine 段合并默认值
    raw["refine"] = _merge_refine_config(raw.get("refine"))
    # server 段合并默认值（T-7 §6 限流阈值）
    raw["server"] = _merge_server_config(raw.get("server"))
    # dream 段合并默认值（0.8 ST-10 夜间整理）
    raw["dream"] = _merge_dream_config(raw.get("dream"))
    # care 段合并默认值（ST-25 角色层）
    raw["care"] = _merge_care_config(raw.get("care"))
    # update_check 段合并默认值（ST-34 自动更新检测）
    raw["update_check"] = _merge_update_check_config(raw.get("update_check"))
    # scene_gc 段合并默认值（T-97 场景主动治理）
    raw["scene_gc"] = _merge_scene_gc_config(raw.get("scene_gc"))
    return raw


def _merge_update_check_config(user_cfg: dict | None) -> dict:
    """合并 update_check 段默认值与用户配置。"""
    base = dict(DEFAULT_UPDATE_CHECK_CONFIG)
    if isinstance(user_cfg, dict):
        base.update({k: v for k, v in user_cfg.items() if v is not None})
    return base


def _merge_care_config(user_cfg: dict | None) -> dict:
    """合并 care 段默认值与用户配置。"""
    base = dict(DEFAULT_CARE_CONFIG)
    if isinstance(user_cfg, dict):
        base.update({k: v for k, v in user_cfg.items() if v is not None})
    return base


def _merge_search_config(user_cfg: dict | None) -> dict:
    """合并 search 段默认值与用户配置（深层 merge）。

    T-43：search.vector 缺 base_url/api_key_env 时从 providers.yaml 的
    embedding 段补默认（供应商设置里指定向量模型的位置；显式配置优先）。
    """
    base = {
        "vector": dict(DEFAULT_SEARCH_CONFIG["vector"]),
        "rrf": dict(DEFAULT_SEARCH_CONFIG["rrf"]),
        "graph": dict(DEFAULT_SEARCH_CONFIG["graph"]),
    }
    if not isinstance(user_cfg, dict):
        user_cfg = {}
    if isinstance(user_cfg.get("vector"), dict):
        base["vector"].update(user_cfg["vector"])
    if isinstance(user_cfg.get("rrf"), dict):
        base["rrf"].update(user_cfg["rrf"])
    if isinstance(user_cfg.get("graph"), dict):
        base["graph"].update(user_cfg["graph"])
    # embedding 段兜底（providers.yaml）：search.vector.provider 引用优先，
    # 否则缺连接字段时取默认（volc-plan）补
    if not base["vector"].get("base_url") or not base["vector"].get("api_key_env"):
        try:
            provider_ref = (user_cfg.get("vector") or {}).get("provider") or "volc-plan"
            emb = _load_embedding_config(provider_ref)
            if emb:
                base["vector"].setdefault("base_url", emb.get("base_url"))
                base["vector"].setdefault("api_key_env", emb.get("api_key_env"))
                base["vector"].setdefault("model", emb.get("default_model") or base["vector"]["model"])
        except Exception:
            pass  # providers.yaml 缺失/格式问题 → 保持现状（向后兼容）
    return base


def load_embeddings_config(path: Path | str | None = None) -> dict:
    """读 providers.yaml 顶层 embedding 段（向量提供商，T-43）。

    返回 {provider_name: {连接字段...}}；文件缺失 / 无 embedding 段 → {}。
    供向量模型管理（WebUI T-43）读取全部向量提供商；密钥只复制环境变量名引用（铁律 #10）。
    """
    cfg_path = Path(path) if path else DEFAULT_PROVIDERS_CONFIG
    if not cfg_path.exists():
        return {}
    raw = _read_yaml(cfg_path)
    emb = (raw or {}).get("embedding") or {}
    if not isinstance(emb, dict):
        return {}
    return {k: v for k, v in emb.items() if isinstance(v, dict)}


def _load_embedding_config(provider: str = "volc-plan") -> dict:
    """读 providers.yaml 顶层 embedding 段的指定提供商（向量，T-43）。

    provider 不存在 → {}（调用方保持现状）；密钥只复制环境变量名引用（铁律 #10）。
    """
    return load_embeddings_config().get(provider, {})


def _merge_l1_config(user_cfg: dict | None) -> dict:
    """合并 l1 段默认值与用户配置（分块参数）。"""
    base = dict(DEFAULT_L1_CONFIG)
    if not isinstance(user_cfg, dict):
        return base
    for k in ("chunk_size", "overlap"):
        if k in user_cfg and isinstance(user_cfg[k], int) and user_cfg[k] > 0:
            base[k] = user_cfg[k]
    return base


def _merge_l15_config(user_cfg: dict | None) -> dict:
    """合并 l15 段默认值与用户配置（候选池向量预筛 T-25；语义边 T-135）。"""
    base = {
        "prescreen": dict(DEFAULT_L15_CONFIG["prescreen"]),
        "semantic_edges": dict(DEFAULT_L15_CONFIG["semantic_edges"]),
    }
    if not isinstance(user_cfg, dict):
        return base
    ps = user_cfg.get("prescreen")
    if isinstance(ps, dict):
        for k in ("enabled", "vector_top_k", "dimension_top_n", "fallback"):
            if k in ps:
                base["prescreen"][k] = ps[k]
    se = user_cfg.get("semantic_edges")
    if isinstance(se, dict):
        for k in ("enabled", "min_weight"):
            if k in se:
                base["semantic_edges"][k] = se[k]
    return base


def _merge_agent_scope_config(user_cfg: dict | None) -> dict:
    """合并 agent_scope 段默认值（T-140：多 Agent 隔离；默认关=灰度全通）。"""
    base = {"enabled": DEFAULT_AGENT_SCOPE_CONFIG["enabled"]}
    if isinstance(user_cfg, dict) and "enabled" in user_cfg:
        base["enabled"] = bool(user_cfg["enabled"])
    return base


def _merge_guardrail_config(user_cfg: dict | None) -> dict:
    """合并 guardrail 段默认值（T-139：敏感信息过滤层；默认关=灰度安全）。"""
    base = {
        "enabled": DEFAULT_GUARDRAIL_CONFIG["enabled"],
        "write_mode": DEFAULT_GUARDRAIL_CONFIG["write_mode"],
        "read_mode": DEFAULT_GUARDRAIL_CONFIG["read_mode"],
        "llm_fallback": dict(DEFAULT_GUARDRAIL_CONFIG["llm_fallback"]),
    }
    if not isinstance(user_cfg, dict):
        return base
    for k in ("enabled", "write_mode", "read_mode"):
        if k in user_cfg:
            base[k] = user_cfg[k]
    lf = user_cfg.get("llm_fallback")
    if isinstance(lf, dict) and "enabled" in lf:
        base["llm_fallback"]["enabled"] = lf["enabled"]
    return base


def _merge_refine_config(user_cfg: dict | None) -> dict:
    """合并 refine 段默认值与用户配置（提炼调度）。

    llm_override（T-43 防劫持：显式指定优先于 agent_model 声明）2026-08-29 起
    参与合并——此前该键被静默丢弃，sgme.yaml 里的专用提炼 LLM 从未生效（B121）。
    """
    base = dict(DEFAULT_REFINE_CONFIG)
    if not isinstance(user_cfg, dict):
        return base
    if isinstance(user_cfg.get("refine_on_append"), bool):
        base["refine_on_append"] = user_cfg["refine_on_append"]
    bs = user_cfg.get("batch_scan")
    if isinstance(bs, dict):
        if isinstance(bs.get("enabled"), bool):
            base["batch_scan"]["enabled"] = bs["enabled"]
        if isinstance(bs.get("interval_min"), int) and bs["interval_min"] > 0:
            base["batch_scan"]["interval_min"] = bs["interval_min"]
    ov = user_cfg.get("llm_override")
    if (isinstance(ov, dict)
            and isinstance(ov.get("provider"), str) and ov["provider"].strip()
            and isinstance(ov.get("model"), str) and ov["model"].strip()):
        merged = {"provider": ov["provider"].strip(), "model": ov["model"].strip()}
        if isinstance(ov.get("max_tokens"), int) and ov["max_tokens"] > 0:
            merged["max_tokens"] = ov["max_tokens"]
        base["llm_override"] = merged
    return base


def _merge_dream_config(user_cfg: dict | None) -> dict:
    """合并 dream 段默认值与用户配置（0.8 ST-10 夜间整理）。

    类型校验规则：
    - enabled / ttl_mark：必须 bool，否则回退默认
    - schedule：str（可为空串 = 不自动只手动）；非法类型回退默认
    - max_files / archive_days：正整数，否则回退默认
    - report_dir：str，否则回退默认
    """
    base = dict(DEFAULT_DREAM_CONFIG)
    if not isinstance(user_cfg, dict):
        return base
    if isinstance(user_cfg.get("enabled"), bool):
        base["enabled"] = user_cfg["enabled"]
    if isinstance(user_cfg.get("schedule"), str):
        base["schedule"] = user_cfg["schedule"]
    if isinstance(user_cfg.get("max_files"), int) and user_cfg["max_files"] > 0:
        base["max_files"] = user_cfg["max_files"]
    if isinstance(user_cfg.get("ttl_mark"), bool):
        base["ttl_mark"] = user_cfg["ttl_mark"]
    if isinstance(user_cfg.get("archive_days"), int) and user_cfg["archive_days"] > 0:
        base["archive_days"] = user_cfg["archive_days"]
    if isinstance(user_cfg.get("report_dir"), str) and user_cfg["report_dir"].strip():
        base["report_dir"] = user_cfg["report_dir"]
    return base


def _merge_backup_config(user_cfg: dict | None) -> dict:
    """合并 backup 段默认值与用户配置（0.8 方案 B 每日自动备份）。

    类型校验规则：
    - enabled：必须 bool，否则回退默认
    - schedule：str（可为空串 = 不自动只手动）；非法类型回退默认
    - level：str 且 in (incremental/full/monthly)，否则回退默认
    - dest_dir / remote_dir：str（remote_dir 空 = 跳过异地）
    - keep_full：正整数，否则回退默认
    """
    base = dict(DEFAULT_BACKUP_CONFIG)
    if not isinstance(user_cfg, dict):
        return base
    if isinstance(user_cfg.get("enabled"), bool):
        base["enabled"] = user_cfg["enabled"]
    if isinstance(user_cfg.get("schedule"), str):
        base["schedule"] = user_cfg["schedule"]
    if isinstance(user_cfg.get("level"), str) and user_cfg["level"] in (
        "incremental",
        "full",
        "monthly",
    ):
        base["level"] = user_cfg["level"]
    if isinstance(user_cfg.get("dest_dir"), str) and user_cfg["dest_dir"].strip():
        base["dest_dir"] = user_cfg["dest_dir"]
    if isinstance(user_cfg.get("remote_dir"), str):
        base["remote_dir"] = user_cfg["remote_dir"]
    if isinstance(user_cfg.get("keep_full"), int) and user_cfg["keep_full"] > 0:
        base["keep_full"] = user_cfg["keep_full"]
    return base


def _merge_server_config(user_cfg: dict | None) -> dict:
    """合并 server 段默认值与用户配置（T-7 §6 限流阈值）。

    ``rate_limit_per_min`` 必须为非负整数；非法值（负数/非整数/缺失）回退默认 120。
    """
    base = dict(DEFAULT_SERVER_CONFIG)
    if not isinstance(user_cfg, dict):
        return base
    rlm = user_cfg.get("rate_limit_per_min")
    if isinstance(rlm, int) and rlm >= 0:
        base["rate_limit_per_min"] = rlm
    return base


def load_config(
    llm_path: Path | str | None = None,
    dimensions_path: Path | str | None = None,
    aliases_path: Path | str | None = None,
    term_aliases_path: Path | str | None = None,
    relations_path: Path | str | None = None,
    sgme_path: Path | str | None = None,
) -> dict:
    """加载全部配置并组装为统一字典。

    返回结构：
      {
        "llm": {chains, rules},
        "dimensions": [dim_dict, ...],
        "aliases": {dim_id: [alias,...]},
        "term_aliases": {旧术语: 标准术语},  # ST-19 检索术语别名表
        "relations": [rel_dict, ...],  # T-14 关系类型注册表
        "l2": {max_scenes, warn_thresholds},  # 来自 sgme.yaml，缺失用默认兜底
        "paths": {"project_root", "data_dir", "raw_dir"}
      }
    启动时打印加载摘要（维度数/链长）。
    """
    llm_cfg = load_llm_config(llm_path)
    dimensions = load_dimensions(dimensions_path)
    aliases = load_aliases(aliases_path)
    term_aliases = load_term_aliases(term_aliases_path)
    relations = load_relations(relations_path)
    sgme_cfg = load_sgme_config(sgme_path)

    # 一致性校验：别名表的 key 必须都在注册表 id 中
    dim_ids = {d["id"] for d in dimensions}
    for alias_key in aliases:
        if alias_key not in dim_ids:
            raise ValueError(f"别名表引用了未知维度 id: {alias_key}")

    # 确保运行时目录存在（data/ 由 storage 层创建 db 文件时使用）
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    cfg = {
        "llm": llm_cfg,
        "dimensions": dimensions,
        "aliases": aliases,
        "term_aliases": term_aliases,
        "relations": relations,
        "l2": sgme_cfg.get("l2", dict(DEFAULT_L2_CONFIG)),
        "search": sgme_cfg.get("search", _merge_search_config(None)),
        "backup": sgme_cfg.get("backup", dict(DEFAULT_BACKUP_CONFIG)),
        "l1": sgme_cfg.get("l1", dict(DEFAULT_L1_CONFIG)),
        "l15": sgme_cfg.get("l15", dict(DEFAULT_L15_CONFIG)),
        "refine": sgme_cfg.get("refine", _merge_refine_config(None)),
        "guardrail": _merge_guardrail_config(sgme_cfg.get("guardrail")),
        "agent_scope": _merge_agent_scope_config(sgme_cfg.get("agent_scope")),
        "server": sgme_cfg.get("server", dict(DEFAULT_SERVER_CONFIG)),
        "dream": sgme_cfg.get("dream", _merge_dream_config(None)),
        "scene_gc": sgme_cfg.get("scene_gc", _merge_scene_gc_config(None)),
        "wiki": sgme_cfg.get("wiki", dict(DEFAULT_WIKI_CONFIG)),
        "skills": sgme_cfg.get("skills", {"enabled": False}),
        "skills_hub": _merge_skills_hub_config(sgme_cfg.get("skills_hub")),
        # ST-36 M1：skills 管理模块配置（parse_skills_config 缺失/类型错误兜底全默认）
        "skills": _parse_skills_section(sgme_cfg.get("skills")),
        "logging": sgme_cfg.get("logging", dict(DEFAULT_LOGGING_CONFIG)),
        "paths": {
            "project_root": str(PROJECT_ROOT),
            "data_dir": str(DATA_DIR),
            "raw_dir": str(RAW_DIR),
        },
    }

    # 启动加载摘要
    chain_names = list(llm_cfg.get("chains", {}).keys())
    print(
        f"[SGME config] 加载完成: 维度数={len(dimensions)} "
        f"关系类型数={len(relations)} "
        f"别名维度={len(aliases)} 术语别名={len(term_aliases)} 链={chain_names} "
        f"链长={ {n: len(v) for n, v in llm_cfg.get('chains', {}).items()} }"
    )
    return cfg


def get_env(name: str) -> str | None:
    """读取环境变量（密钥不落盘，只引用变量名）。"""
    val = os.environ.get(name)
    return val if val else None


# ---------- 配置写入（2026-08-07 模块化重构 B30：config = 配置唯一读写方） ----------

# 可写段白名单（sgme.yaml 顶层键；llm.yaml/registry 属机密与注册表，不由接口改）
CONFIG_SECTIONS = {"l1", "l2", "refine", "search", "backup", "wiki", "skills_hub", "logging", "dream", "scene_gc"}

# 各段可写字段（防未知键注入；缺省 = 整段白名单）
SECTION_KEYS: dict[str, set[str]] = {
    "l1": {"chunk_size", "overlap"},
    "l2": {"max_scenes", "warn_thresholds"},
    "refine": {"refine_on_append", "batch_scan"},
    "search": {"vector", "rrf"},
    "backup": {"dir", "schedule", "raw_cold_days", "remote_dir"},
    "dream": {"enabled", "schedule", "max_files", "ttl_mark", "archive_days", "report_dir"},
    "scene_gc": {"enabled", "merge_threshold", "min_threshold", "trigger_at", "max_merges"},
}


def filter_keys(section: str, values: dict) -> dict:
    """按段白名单过滤可写键（防未知键注入）。"""
    allowed = SECTION_KEYS.get(section)
    if allowed is None:
        return values  # 未知段：交给段校验
    return {k: v for k, v in values.items() if k in allowed}


def apply_section(cfg: dict, section: str, values: dict) -> None:
    """把 values 合并进 cfg[section]（深层合并，缺键保留）。

    ST-20：env 覆盖字段（如 skills_hub.remote.source）在对应环境变量设置期间
    不接受更新接口写入——env 优先，且防 NAS 地址经接口落盘（脱敏）。
    """
    current = cfg.get(section)
    if not isinstance(current, dict):
        current = {}
    updates = _strip_env_managed(section, values)
    _deep_merge(current, updates)
    cfg[section] = current


def _strip_env_managed(section: str, values: dict) -> dict:
    """剔除 values 中被 env 覆盖管理的字段（返回新 dict，不污染调用方）。"""
    if not isinstance(values, dict):
        return values
    result = dict(values)
    prefix = section + "."
    for dotted, name in ENV_OVERRIDES.items():
        if not dotted.startswith(prefix) or not os.environ.get(name):
            continue
        parts = dotted[len(prefix):].split(".")
        node: Any = result
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                node = None
                break
            nxt = dict(nxt)  # 复制嵌套 dict，避免污染调用方 values
            node[p] = nxt
            node = nxt
        if node is not None and parts[-1] in node:
            del node[parts[-1]]
            logger.warning("配置更新忽略 %s：由环境变量 %s 管理（env 优先）", dotted, name)
    return result


def _deep_merge(base: dict, updates: dict) -> None:
    """递归合并 updates 进 base。"""
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def persist_config(cfg: dict, config_path: str | Path | None = None) -> None:
    """把可写段写回 sgme.yaml（保留注释与其他段）。

    config_path 缺省用 DEFAULT_SGME_CONFIG；测试可用环境变量
    SGME_CONFIG_PATH 覆盖写入路径（防污染真实配置）。
    """
    override = os.environ.get("SGME_CONFIG_PATH")
    path: Path = Path(override) if override else (config_path or DEFAULT_SGME_CONFIG)
    data: dict = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    # ST-20：env 覆盖字段（skills_hub.remote.source）落盘前记录文件现值，
    # 写回时恢复——env 注入值仅存于进程内存，防 NAS 地址泄漏进 git。
    file_values = {dotted: _get_dotted(data, dotted) for dotted in ENV_OVERRIDES}
    # 只覆盖白名单段
    for s in CONFIG_SECTIONS:
        if s in cfg:
            data[s] = _scrub_section_for_persist(s, cfg[s], file_values)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    logger.info("配置已落盘: %s（段: %s）", path, sorted(CONFIG_SECTIONS))


# ---------- T-23② 安装清单 install.json（ST-23⑦ 服务发现落地） ----------

def install_json_path() -> Path:
    """安装清单路径：SGME_HOME 设置时写其下，未设时固定 ~/.sgme/install.json。

    语义（ST-23⑦）：Agent 找不到 SGME 时按此固定位置读安装清单——
    地址/端口/Key 引用，零人工依赖。本机不泄露目录结构等个人信息。
    """
    if SGME_HOME is not None:
        return SGME_HOME / "install.json"
    return Path.home() / ".sgme" / "install.json"


def write_install_json(
    cfg: dict,
    host: str = "127.0.0.1",
    port: int = 9910,
) -> Path:
    """生成安装清单 install.json（agent 服务发现，ST-23⑦ 落地）。

    内容：版本 / HTTP 地址端口 / MCP 端口 / data_dir / raw_dir /
    Key 的**环境变量名引用**（SGME_ADMIN_KEY/SGME_AGENT_KEY/SGME_BEARER_TOKEN）——
    不落任何明文密钥（铁律 #10：密钥不落盘）。

    Args:
        cfg: load_config() 返回的完整配置（paths 段含 data_dir/raw_dir）。
        host: HTTP 绑定地址（__main__ 传入 SGME_HOST 生效值）。
        port: HTTP 端口（__main__ 传入 SGME_PORT 生效值）。

    Returns:
        写入的 install.json 路径。
    """
    import sgme  # 延迟导入取版本（避免模块加载期循环）

    paths = cfg.get("paths", {}) if isinstance(cfg, dict) else {}
    data = {
        "schema_version": 1,
        "sgme_version": getattr(sgme, "__version__", "unknown"),
        "http": {
            "host": host,
            "port": port,
        },
        "mcp": {
            "port": int(os.environ.get("SGME_MCP_PORT", "9913")),
        },
        "data_dir": str(paths.get("data_dir") or DATA_DIR),
        "raw_dir": str(paths.get("raw_dir") or RAW_DIR),
        "keys": {
            "admin": "SGME_ADMIN_KEY",
            "agent": "SGME_AGENT_KEY",
            "bearer": "SGME_BEARER_TOKEN",
        },
    }
    path = install_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("安装清单已生成: %s", path)
    return path


def write_client_install_json(
    host: str,
    port: int = 9910,
    mcp_port: int | None = None,
) -> Path:
    """生成**客户端模式**安装清单 install.json（T-121：纯远程接入端服务发现）。

    分工（T-121）：
      - 服务端模式 write_install_json：本机跑 SGME，生产启动时 app.py lifespan
        自动调用生成，data_dir/raw_dir 指向本机数据目录。
      - 客户端模式 write_client_install_json：本机**不跑 SGME**、只作为远程
        接入端，由 CLI（scripts/install_client.py）手动生成——http 记录远程
        SGME 地址，data_dir/raw_dir 置 None（表示本地无数据目录，防止接入机
        上残留的本机安装清单误导服务发现第二步）。

    结构与 write_install_json 同 schema（schema_version=1），Key 仍为
    **环境变量名引用**，不落任何明文密钥（铁律 #10：密钥不落盘）。

    Args:
        host: 远程 SGME 主机地址（如 192.168.10.10）。
        port: 远程 HTTP 端口（默认 9910）。
        mcp_port: 远程 MCP 端口；未传时取 SGME_MCP_PORT env（默认 9913），
            与 write_install_json 同逻辑。

    Returns:
        写入的 install.json 路径。
    """
    import sgme  # 延迟导入取版本（避免模块加载期循环）

    data = {
        "schema_version": 1,
        "sgme_version": getattr(sgme, "__version__", "unknown"),
        "http": {
            "host": host,
            "port": port,
        },
        "mcp": {
            "port": int(mcp_port if mcp_port is not None
                        else os.environ.get("SGME_MCP_PORT", "9913")),
        },
        "data_dir": None,  # 客户端模式：本地无数据目录（服务发现不得误读本机路径）
        "raw_dir": None,   # 同上
        "keys": {
            "admin": "SGME_ADMIN_KEY",
            "agent": "SGME_AGENT_KEY",
            "bearer": "SGME_BEARER_TOKEN",
        },
    }
    path = install_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("客户端模式安装清单已生成: %s（http://%s:%s）", path, host, port)
    return path


def _scrub_section_for_persist(section: str, section_cfg: dict, file_values: dict) -> dict:
    """落盘前处理段内 env 覆盖字段：env 设置期间恢复文件现值（占位符），否则原样返回。

    返回新 dict 且嵌套节点逐层复制——不就地修改 cfg，env 注入值必须留在进程内存。
    """
    prefix = section + "."
    managed = [d for d in ENV_OVERRIDES if d.startswith(prefix)]
    if not managed or not any(os.environ.get(ENV_OVERRIDES[d]) for d in managed):
        return section_cfg
    scrubbed = dict(section_cfg)
    for dotted in managed:
        if not os.environ.get(ENV_OVERRIDES[dotted]):
            continue
        parts = dotted[len(prefix):].split(".")
        node = scrubbed
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
            else:
                nxt = dict(nxt)  # 复制嵌套 dict，避免污染 cfg
            node[p] = nxt
            node = nxt
        file_val = file_values.get(dotted)
        node[parts[-1]] = file_val if file_val is not None else ""
    return scrubbed
