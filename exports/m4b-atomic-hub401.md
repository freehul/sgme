# M4b 原子化候选扫描报告（hub 401 全量，2026-08-27）

> 方法：精确 SHA1 指纹扫描（段落 ≥30 字符、归一化后完全相同、跨 ≥2 技能）
> 扫描对象：skills-hub-work 401 技能（本地库 101 的完整母库）
> 结论：**78 组跨技能重复段落 = 原子候选真实存在**（此前本地 101 扫描 0 候选是范围错误）

## 总览

| 类别 | 组数 | 典型 |
|---|---|---|
| 模板框架（标题结构） | 25 | comfyui 14 技能共享 A1/A2 骨架 |
| 代码块（可抽函数/脚本） | 18 | gh CLI 初始化、agently-cli 安装 |
| 配置段（环境/URL） | 15 | lmstudio 引擎配置、openjarvis server 配置 |
| 知识段落（说明/教程） | 20 | VPS swap 加装、iframe 跨源限制 |

## 高价值原子候选（按涉及技能数排序）

### 1. comfyui 技能模板框架（14 技能）⭐
- 涉及：comfyui-cloud-api-polling / custom-node-skeleton / frontend-extension / install-decision-tree / is-changed-cache-strategy / local-api-integration / mcp-integration / model-architecture-match / model-deployment / performance-tuning-flags / registry-publishing / troubleshoot-bisect / vram-degradation / workflow-dag-design / workflow-format-conversion
- 重复：`## A1 — 书中的应用 (Past Application)` + `## A2 — 触发场景 (Future Trigger) ★` 两段骨架
- 建议：抽「comfyui-skill-template」原子模板技能，14 技能 uses 引用

### 2. Hermes WebUI 配置解析逻辑（2 技能，8+ 段重复）⭐
- 涉及：apikey-image-gen / grok-image-to-video
- 重复：Web UI base URL 解析顺序（HERMES_WEB_UI_URL env → 127.0.0.1:8647 dev → token 解析 → profile 处理 → BASE_URL 变量）
- 建议：抽「hermes-webui-endpoint-resolution」原子技能

### 3. gh CLI 初始化检查（3 技能）
- 涉及：github-code-review / github-issues / github-repo-management
- 重复：`if command -v gh &>/dev/null && gh auth status` 代码块
- 建议：抽「gh-cli-auth-check」原子（或并入 github-auth）

### 4. agent-email 系三技能重复（3 技能）⚠️
- 涉及：agent-email / agent-mail / agently-mail
- 重复：agently-cli 安装/auth 命令 3 段
- 判断：**三个技能是同一主题的历史重复**——应按用户偏好「同一产品不拆两个 skill，已有时合并更新」合并为一个

### 5. lmstudio/openjarvis 引擎配置（3-4 技能）
- 涉及：ai-desktop-assistant / local-ai-framework-setup / local-first-ai-deployment / openjarvis
- 重复：`[engine.lmstudio] host = "http://localhost:1014"` 等配置段
- 建议：抽「lmstudio-engine-config」原子

### 6. kanban 团队协作规则（2 技能）
- 涉及：nas-kanban-team / vps-kanban-design-team
- 重复：kanban_complete/kanban_block 强制规则段落
- 建议：抽「kanban-team-rules」原子

### 7. VPS 初始化知识（2 技能）
- 涉及：vps-hermes-deployment / vps-proxy-setup
- 重复：先加 swap（防 OOM）段落
- 建议：抽「vps-swap-setup」原子

## 判定

- **不是「无原子可抽」**——78 组候选里约 10 组有真实原子价值（模板/配置/初始化逻辑）
- 其中 comfyui 模板（14 技能）和 Hermes WebUI 解析（8 段）**价值最高**，抽出来能显著减少重复维护
- agent-email/agent-mail/agently-mail 是**合并项**而非拆分项（历史重复技能）
- 近似重复（ratio 0.85-0.99 的变体段落）未扫（O(n²) 太慢），后续可优化算法再补

## 待用户拍板

1. 先抽哪个原子？（推荐 comfyui 模板 + Hermes WebUI 解析）
2. agent-email 三技能是否合并？
3. 是否把高价值 hub 独有技能（nas-ssh/vps-*/trae-* 等 313 个）纳入本地库？
