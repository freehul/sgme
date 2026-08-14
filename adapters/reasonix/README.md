# Reasonix × SGME 适配器

Reasonix（DeepSeek 原生编码代理）的 SGME 记忆接入适配器，走 **hooks 专用适配**（事件驱动），
不是 MCP 兜底方案（MCP 留给没有 hooks 的 Agent，触发时机由 LLM 决定不可靠）。

## 工作原理

| Hook 事件 | 动作 | 实现 |
|---|---|---|
| SessionStart | 注入 SGME 用户画像 + 项目相关记忆到 Reasonix 首轮上下文 | `bridge.py --start` |
| SessionEnd | 导出 Reasonix 会话 → SGME L0 → 触发提炼 | `bridge.py --end` |

## 知情三件套（让模型知道记忆系统存在——机制就位 ≠ 模型知情）

1. **AGENTS.md 声明**（install 生成）：Reasonix 把 REASONIX.md/AGENTS.md 加载进每个会话 system prompt——模型必然知道「会话自动记录 + 可主动查询」
2. **SessionStart 身份说明**：注入内容首段声明「这是 SGME 记忆系统…用 /sgme 查询」——API 失败也注入（知情不依赖可用性）
3. **`/sgme` 查询命令**（`.reasonix/commands/sgme.md`）：模型在会话中可主动检索记忆+场景（`bridge.py --query`）

## 目录

- `bridge.py` — 桥接核心（解析会话 → L0 → append → 提炼触发；注入画像/记忆）
- `install.py` — 一键安装（生成 `.reasonix/settings.json` + 注册 `agent_id=reasonix`）
- `.env` — 注册的 agent key（gitignore，不进仓库）
- `tests/` — 单元测试（16 个，全绿）

## 安装

```bash
<项目根>/.venv/Scripts/python.exe <项目根>/adapters/reasonix/install.py --dir D:/Projects/<目标项目>
# 示例（本仓库）：.venv/Scripts/python.exe adapters/reasonix/install.py --dir D:/Projects/SGME
```

步骤：
1. 生成 `<目标项目>/.reasonix/settings.json`（SessionStart/SessionEnd 两条 hook）
2. 注册 `agent_id=reasonix`，key 存 `adapters/reasonix/.env`（仅本地，勿外传）
3. 重启 Reasonix（或 `/reload`）使 hooks 生效

验证：

```bash
reasonix hook list --json --dir D:/Projects/<目标项目>
# 期望：SessionStart + SessionEnd 两条 active
```

## 数据流

```
Reasonix 会话开始 → SessionStart hook → bridge.py --start
    ├─ GET /v1/inject        （用户画像，Tier0）
    └─ GET /v1/search        （项目相关记忆）
    └─ stdout → hookSpecificOutput.additionalContext → 注入首轮模型上下文

Reasonix 会话结束 → SessionEnd hook → bridge.py --end
    ├─ 读 %APPDATA%\reasonix\projects\<编码>\sessions\<sessionId>.jsonl
    ├─ 解析（过滤 system/local_only/空消息；user 优先 raw_content）
    ├─ 转 L0 格式（# ISO时间戳 user / ## assistant / ## tool 带 **tool**: 前缀）
    ├─ POST /v1/append       （session_key=reasonix-<sessionId>，agent_id=reasonix）
    └─ POST /v1/admin/refine/trigger_async
```

## 关键设计

- **故障隔离**：任何异常只写 stderr 并 exit 0，绝不阻塞 Reasonix 会话生命周期
- **key 管理**：注册 key 只落 `.env`（`_load_env_file` setdefault 加载），代码零硬编码
- **时间戳**：Reasonix createdAt 是 epoch 毫秒 int，归一化为 ISO UTC（与 SGME L0 同口径）
- **会话定位**：优先按项目目录编码规则（`D:\Projects\X` → `d--projects-x`），全局扫描兜底
- **注入上限**：additionalContext 截断保护 ≤9800 字符（Reasonix 单 hook 上限约 10000）

## 复现坑位（2026-08-07 实测）

- Reasonix hooks 配置在 `.reasonix/settings.json`（项目级）或 `%APPDATA%\reasonix\settings.json`（全局），
  **不是** `.claude/settings.json`（后者仅插件包兼容层读取）
- 配置格式 `{"hooks": {"事件": [{"command": "..."}]}}`，无 type 嵌套；
  Claude Code 双层嵌套格式不识别
- hook 命令用绝对路径（Windows 经 `cmd /c` 执行，不依赖 shell PATH）
- SessionStart 的 stdout 会注入模型上下文（Claude Code 兼容 `hookSpecificOutput` JSON），
  其他事件 stdout 无特殊作用
- `reasonix hook list --json --dir D:/路径` 用正斜杠绝对路径（MSYS `/d/` 格式会传错）
