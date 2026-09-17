# 任务卡 ① — 部署 / 发现 SGME

> **目标**：让 AI 独立判断「SGME 是否已在运行」；没有则把它跑起来。
> **铁律：先发现，后部署**——已有实例时绝不允许重复部署（会撞端口、撞数据目录）。

## 前置条件

- 一台可用的机器：本机 / NAS / 服务器（Docker 或 Python 3.12 环境）
- 网络能拉取依赖（Docker 镜像 / PyPI；国内网络自备镜像源）
- 协议细节见 `../agent-onboarding.md`（本卡只做编排）

## 步骤

### 1. 发现（必须先做）

| 序 | 动作 | 成功 → | 失败 → |
|---|---|---|---|
| ① | 探测健康端点 `GET http://<NAS_IP>:9910/v1/health` | 记下 `version`，跳去验收 | 下一步 |
| ② | 读安装清单 `~/.sgme/install.json`（或项目目录 `.sgme/install.json`），按其地址/端口/Key 引用连接 | 连通即成功 | 下一步 |
| ③ | 判定「未发现 SGME」，向用户报告并进入部署 | — | — |

### 2. 部署（三选一）

- **A. Docker（推荐）**：按仓库 `docs/deployment-docker.md` 起容器；首次启动自动初始化数据目录。数据卷要挂到持久目录，升级只换镜像不丢数据。
- **B. NAS / 服务器**：使用用户现有的 compose 与更新脚本（路径由用户提供，不写死）。注意：部署脚本类文件通常保留在本机配置中，不要复制到公开位置。
- **C. 本地 Python**：Python 3.12 + `pip install -r requirements.txt`，启动入口见 `../docs/runbook.md`。

### 3. 部署后必做

1. 初始化鉴权：在 `config/.env` 写入 `SGME_ADMIN_KEY` 与 `SGME_AGENT_KEY`（随机串，**只进环境文件，不进任何文档/对话**）。
2. 检查模型 Key：`health` 的 `model_config.missing_keys` 非空时，把 `../免费模型Key申请指南.md` 指给用户自行申请（零充值）；**不要要求用户在对话里粘贴 Key**，让用户写入 `config/.env`。
3. 再次探测 `health` 直到 `status: ok`。

## 验收（全部满足才算完成）

- [ ] `GET /v1/health` 返回 200，含 `version` 与 `capabilities`
- [ ] `model_config.missing_keys` 为空（或已明确告知用户缺哪把、如何补）
- [ ] 已向用户报告：**端点地址 + 版本号 + 缺失项（若有）**

## 失败处置

| 症状 | 处置 |
|---|---|
| 端口占用 | 换端口，或停掉占用进程后重启 |
| 镜像/依赖拉取超时 | 换镜像源、重试；仍不行走本地 Python 方式 |
| `health` 200 但 `missing_keys` 非空 | 不阻断接入，把申请指引发给用户 |
| Docker 不可用 | 降级走本地 Python（C 方式），不当"失败"处理 |

---

## 整段复制版（中文）

> 你是我的 AI 助手。请为我的 SGME（个人记忆引擎）完成「发现或部署」：
> 1. 先探测 `http://<NAS_IP>:9910/v1/health`；失败则读 `~/.sgme/install.json`；已有实例就直接连上，不要重复部署。
> 2. 没有实例时用 Docker 部署（参考仓库 `docs/deployment-docker.md`），启动后初始化 `config/.env` 里的 `SGME_ADMIN_KEY` / `SGME_AGENT_KEY`。
> 3. 检查 `health` 的 `model_config.missing_keys`；缺 Key 时把《免费模型Key申请指南》（`AI-INSTALL/免费模型Key申请指南.md`）发给我，由我自行申请——不要让我在对话里粘贴任何 Key。
> 4. 完成后向我报告：端点地址、版本号、缺失项（若有）。
> 全程不要跳步；每一步都先做后报。

## English

**Task card ① — Deploy / discover SGME.** Discover first, deploy only if nothing is found: probe `http://<NAS_IP>:9910/v1/health` → fall back to `~/.sgme/install.json` → otherwise report "not found" and deploy (Docker per `docs/deployment-docker.md`, or Python 3.12). After deploy: create `SGME_ADMIN_KEY` / `SGME_AGENT_KEY` in `config/.env`; if `model_config.missing_keys` is non-empty, point the user to `免费模型Key申请指南.md` (free keys) — never ask the user to paste secrets into chat. Acceptance: health 200 with `version` + `capabilities`, missing keys reported, endpoint + version reported to the user.

**Copy block (English):**

> Act as my AI assistant and set up my SGME (personal memory engine):
> 1. First probe `http://<NAS_IP>:9910/v1/health`; if that fails read `~/.sgme/install.json`; if an instance already runs, just connect — do not redeploy.
> 2. If none exists, deploy with Docker (see `docs/deployment-docker.md`) and initialize `SGME_ADMIN_KEY` / `SGME_AGENT_KEY` in `config/.env`.
> 3. Check `model_config.missing_keys`; if keys are missing, send me `AI-INSTALL/免费模型Key申请指南.md` — I will apply myself. Never ask me to paste any key into chat.
> 4. Report back: endpoint, version, missing items (if any). Do not skip steps.
