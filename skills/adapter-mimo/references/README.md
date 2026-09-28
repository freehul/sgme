# MiMo Desktop × SGME 适配器

MiMo Desktop（小米 MiMo）的 SGME 记忆接入适配器，形态为 **MiMo Skill（SKILL.md）+ Python 客户端**（与豆包工作同构：会话型自律接入）。

MiMo Desktop 同时支持原生 MCP（Settings → MCP 挂 `sgme`）。适配器补全：纪律文件、41 基准能力 CLI、selfcheck、关怀守护、`~/.sgme/mimo-agent.json` 身份发现。

## 与其他适配器的差异

| 适配器 | 形态 | 运行时 Python | 常驻能力 |
|---|---|---|---|
| hermes | Python 插件（memory.provider） | 是 | 有 |
| dsh | TS 原生插件（Cordis） | 否 | 有 |
| doubao | Skill + Python 客户端 | 是 | 无（signal-pull / care_watch） |
| **mimo（本适配器）** | **Skill（`~/.config/mimocode/skills/mimo/`）+ Python 客户端 + 原生 MCP 优先** | 是 | 无（会话型；care_watch 可选） |

## 能力面（MCP 基准 41 工具）

与 `sgme/mcp_server.py` 基准一一对应；CLI 命令面同步 41 + 扩展 6（`events-pull` / `events-after` / `wiki-raw` / `capabilities` / `env-info` / `mcp`）。

```bash
python scripts/sgme_client.py capabilities   # 离线核对 41/41
python scripts/selfcheck.py                  # 连通性 + 能力矩阵
```

## 目录

```
adapters/mimo/
├── SKILL.md            # MiMo Skill 真源（name=mimo）
├── locales/            # Plugins 页显示名（zh-CN / en-US）
├── install.py          # 部署到 ~/.config/mimocode/skills/mimo/ + 写 client.env + 自检
├── .gitignore          # 部署配置 client.env 不入库
├── README.md
├── scripts/
│   ├── sgme_client.py  # HTTP+MCP 客户端（41 基准 + 扩展）
│   ├── care_watch.py   # 主动关怀守护（可选）
│   └── selfcheck.py    # 接入自检
└── tests/
    └── test_client.py  # 单元测试（mock 网络，离线可跑）
```

## 地址与密钥

三级地址：环境变量 → 部署配置 `client.env` → `~/.sgme/mimo-agent.json` → 回环默认。

密钥：`SGME_AGENT_KEY` 环境变量优先，否则读 `~/.sgme/mimo-agent.json` 的 `api_key`。**永不入库、不回显**。管理类另用 `SGME_ADMIN_KEY`。

## 安装

```bash
python adapters/mimo/install.py
# 或指定技能根 / 地址
python adapters/mimo/install.py --skills-root <path> --base-url http://<SGME_HOST>:9910
```

部署后**新开 MiMo Desktop 对话**加载技能。原生 MCP 已配置时优先走 MCP 工具。

## 验证

1. `python adapters/mimo/scripts/selfcheck.py` 全绿
2. 新会话触发本技能（提到「查记忆 / SGME / 之前…」）
3. `search` / `append` / `refine-trigger` 各走通一次

## 常见坑

- 仓库内默认回环；生产地址由 `install.py` 写入部署副本 `client.env` 或身份文件提供
- 身份文件 `~/.sgme/mimo-agent.json` 含真实 key，**禁止提交**
- MCP 配置改后需**新开对话**
- 旧薄技能 `skills/sgme/` 可在 Plugins 页停用，避免双触发
