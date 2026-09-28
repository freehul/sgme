"""SGME（拾光记忆）声明式配置面板 — 桌面端「设置 → 记忆与上下文 → 持久记忆」由本文件渲染。

机制（T-214）：Hermes 桌面端按插件目录读取模块级 ``CONFIG_SCHEMA``（同 honcho）；
本文件只允许 import 纯数据模块 plugins.memory.config_schema，不得引入运行时依赖。
保存：非密钥 → $HERMES_HOME/sgme/config.json（flat_json 存储）；密钥 → 框架写
$HERMES_HOME/.env（SGME_AGENT_KEY / SGME_ADMIN_KEY，面板只回显「已设置/未设置」）。
角色字段（T-211/T-214）：本机面板值优先于服务端「当前角色」；自定义提示词非空时
新会话开始时惰性生成/更新角色卡并启用（见 __init__.py 的 _role_prompt_block）。
"""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SECRET,
    KIND_SELECT,
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
)

# 角色下拉「空态」选项（与 __init__.py 的 _NO_ROLE_LABEL 保持同字面量）
_NO_ROLE = "（不使用角色）"


def _opts(*pairs: tuple[str, str]) -> tuple[ProviderFieldOption, ...]:
    return tuple(ProviderFieldOption(value, label) for value, label in pairs)


# 内置四角色（与 SGME 服务端 roles/ 对齐）；自建角色用下方自定义字段
_ROLE_OPTIONS = _opts(
    (_NO_ROLE, _NO_ROLE),
    ("butler", "管家"),
    ("companion", "伴侣"),
    ("friend", "朋友"),
    ("mentor", "导师"),
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="sgme",
    label="拾光记忆 SGME",
    fields=(
        # —— 连接与鉴权 ——
        ProviderField(
            key="base_url", label="服务地址", kind=KIND_TEXT,
            default="http://127.0.0.1:9910",
            description="SGME 服务地址（本机回环默认；远端填完整地址）",
            placeholder="http://127.0.0.1:9910",
            env_fallbacks=("SGME_BASE_URL",), inline=True, group="连接与鉴权",
        ),
        ProviderField(
            key="agent_key", label="Agent Key", kind=KIND_SECRET,
            description="远端接入必填；本机回环可留空用开发默认。建议用 register 签发的专属 key",
            env_key="SGME_AGENT_KEY", inline=True, group="连接与鉴权",
        ),
        ProviderField(
            key="admin_key", label="Admin Key", kind=KIND_SECRET,
            description="可选：记忆纠错 / wiki 写入 / 三池登记等管理类工具需要",
            env_key="SGME_ADMIN_KEY", group="连接与鉴权",
        ),
        # —— 行为偏好 ——
        ProviderField(
            key="inject_mode", label="注入模式", kind=KIND_SELECT, default="daily",
            description="画像注入场景模板",
            env_fallbacks=("SGME_INJECT_MODE",), inline=True, group="行为偏好",
            options=_opts(("daily", "日常"), ("coding", "编码"), ("work", "工作"), ("full", "全量")),
        ),
        ProviderField(
            key="inject_max_tokens", label="注入预算（tokens）", kind=KIND_NUMBER, default="800",
            description="画像注入 token 预算（建议 100–4000）",
            env_fallbacks=("SGME_INJECT_MAX_TOKENS",), inline=True, group="行为偏好",
        ),
        ProviderField(
            key="capture_enabled", label="自动落盘", kind=KIND_BOOL, default="true",
            description="会话写入开关（关闭 = 只读不记录）",
            env_fallbacks=("SGME_CAPTURE_ENABLED",), inline=True, group="行为偏好",
        ),
        ProviderField(
            key="refine_on_end", label="会话结束提炼", kind=KIND_BOOL, default="true",
            description="会话结束触发提炼（需写入开启）",
            env_fallbacks=("SGME_REFINE_ON_END",), inline=True, group="行为偏好",
        ),
        ProviderField(
            key="agent_id", label="Agent 标识", kind=KIND_TEXT, default="hermes",
            description="写入溯源标识（append 自报的 agent_id）",
            env_fallbacks=("SGME_HERMES_AGENT_ID",), inline=True, group="行为偏好",
        ),
        # —— 角色（换皮不换芯；本机值优先于服务端当前角色） ——
        ProviderField(
            key="role_id", label="会话角色", kind=KIND_SELECT, default=_NO_ROLE,
            description="本机 Hermes 的沟通角色；「不使用角色」= 关闭角色注入（不改服务端状态）",
            options=_ROLE_OPTIONS, inline=True, group="角色",
        ),
        ProviderField(
            key="custom_role_name", label="自定义角色名", kind=KIND_TEXT, default="",
            description="配合下一条：提示词非空时新会话自动生成/更新角色卡并启用",
            placeholder="如：私人秘书", group="角色",
        ),
        ProviderField(
            key="custom_role_prompt", label="自定义角色提示词", kind=KIND_TEXT, default="",
            description="非空时优先生效（覆盖上方选择）；长文本建议用 SGME WebUI 角色页编辑",
            placeholder="你是……（角色设定与沟通风格）", group="角色",
        ),
    ),
)
