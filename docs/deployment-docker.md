# SGME Docker 部署指南

> 适用版本：v1.0.0b1（镜像 tag `sgme:1.0.0b1`）
> 验收状态：✅ 笔记本 Docker Desktop + 群晖 NAS（DSM Container Manager）双环境实测通过（2026-08-14）

## 1. 交付物

| 文件 | 用途 |
|------|------|
| `Dockerfile` | 基于 `python:3.11-slim`，程序资源内置 `/app`，用户数据经 `SGME_HOME=/data` 落到挂载卷 |
| `docker-compose.yml` | 单机/NAS 一键部署（含 healthcheck、`restart: unless-stopped`） |
| `.dockerignore` | 构建排除（密钥/数据/开发产物一律不进镜像） |
| `.env.example` | 密钥模板（复制为 `docker.env` 填真实值） |
| `docker.env` | 真实密钥（**不入 git**，见 `.gitignore` 的 `*.env` 规则） |

## 2. 布局约定

镜像内布局（对齐 T-23 标准安装布局）：

```
/app            # 程序资源（sgme 包 / config / registry / templates / prompts / roles）
/data           # 用户数据根（SGME_HOME，挂载卷）——data / raw / logs / config / install.json
```

- 程序资源随镜像更新，用户数据独立于镜像（`docker compose down` 不丢数据，`--build` 升级无损）。
- 密钥经 `env_file: docker.env` 注入，不落盘进镜像。

## 2.5 一键部署脚本（ST-12，2026-08-20）

项目根提供 `deploy.sh` 一键脚本，封装 构建→导出→传输→NAS 导入→启动→验证 全流程：

```bash
./deploy.sh build          # 仅本地构建
./deploy.sh deploy <host>  # 一键部署到 NAS（如 leo@192.168.10.10）
./deploy.sh up / down / logs / verify [host]
```

> NAS 部署路径约定：/vol1/1000/Docker/sgme（bind mount 数据卷）。脚本为 bash 编写，Windows 本机可用 Git Bash / WSL 执行；纯本机部署直接 `docker compose up -d --build` 即可。

## 3. 快速开始（单机）

```bash
# 1. 准备密钥（复制模板填真实值）
cp .env.example docker.env

# 2. 构建并启动
docker compose up -d --build

# 3. 验证（host 按部署机地址：本机 localhost / NAS IP，或 ~/.sgme/install.json 的 http.host）
curl http://<sgme-host>:9910/v1/health
# 期望：{"status":"ok","llm":{"available":true},...}
```

## 4. NAS（群晖 Synology）部署

已实测环境：DSM + Docker 29.1.2，用户 `LEO` 可用 docker（免 sudo），共享文件夹 `/vol1/1000/Docker`。

### 4.1 构建镜像（在能联网的机器上，如笔记本）

中国网络需先配置镜像加速（`~/.docker/daemon.json` 的 `registry-mirrors`），否则 `pip install` 拉不动：

```json
{
  "registry-mirrors": ["https://docker.1ms.run", "https://docker.m.daocloud.io", "https://dockerproxy.net"]
}
```

```bash
docker compose build          # 产出 sgme:1.0.0b1（约 463 MB）
docker save sgme:1.0.0b1 -o sgme-1.0.0b1.tar   # 导出约 109 MB
```

### 4.2 上传并加载到 NAS

```bash
scp sgme-1.0.0b1.tar LEO@<NAS_IP>:/vol1/1000/Docker/sgme/
ssh LEO@<NAS_IP> "docker load -i /vol1/1000/Docker/sgme/sgme-1.0.0b1.tar"
```

### 4.3 NAS 专用 compose

群晖共享文件夹路径用 **bind mount**（便于「文件站」直接备份数据），不 build：

```yaml
# /vol1/1000/Docker/sgme/docker-compose.yml
services:
  sgme:
    image: sgme:1.0.0b1
    container_name: sgme
    restart: unless-stopped
    ports:
      - "9910:9910"
      - "9913:9913"
    environment:
      SGME_HOME: /data
      SGME_HOST: 0.0.0.0
      SGME_PORT: "9910"
      TZ: Asia/Shanghai
    env_file:
      - docker.env          # 复制 .env.example 填真实密钥
    volumes:
      - /vol1/1000/Docker/sgme/data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9910/v1/health', timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
```

```bash
ssh LEO@<NAS_IP> "cd /vol1/1000/Docker/sgme && docker compose up -d"
```

### 4.4 NAS 验收清单（2026-08-14 实测 ✅）

- [x] `docker ps --filter name=sgme` 显示 `Up ... (healthy)`
- [x] `GET /v1/health` → `status: ok`，`llm.available: true`
- [x] 端到端链路：`append`（status=new）→ `refine/trigger_async` → `search` 命中「用户的家目录部署在群晖 NAS 上」
- [x] 数据持久化：`/data` 下 `data/ raw/ logs/ install.json` 均生成

## 5. 注意事项

- **端口冲突**：笔记本若本机已跑 SGME Gateway（Python，占用 `127.0.0.1:9910`），Docker 容器绑定 `0.0.0.0:9910`（IPv6 `::`）可能冲突。NAS 无此问题；本地验证时先停本机 Gateway 再起容器。
- **时区**：`TZ: Asia/Shanghai` 保证日报/备份定时用本地时区。
- **备份**：NAS 数据卷 `/vol1/1000/Docker/sgme/data` 可直接用群晖「文件站」或 Hyper Backup 备份；`remote_dir` 挂载见 runbook。
- **升级**：新版本 `docker compose build` 后 `docker compose up -d`（数据卷不变，不丢数据）；镜像 tag 推进时同步 `docker-compose.yml` 的 `image:` 与 `docker save` 文件名。

## 6. 安全

- `docker.env` / `config/.env` 含真实密钥，**禁止提交 git**（`.gitignore` 已含 `*.env`）。
- `SGME_ADMIN_KEY` / `SGME_AGENT_KEY` 为空时用 dev 默认 key（仅本机回环可用，`0.0.0.0` 部署必须显式设置）。
- 默认 key 公开后，`0.0.0.0` 监听时任何人可用 admin 权限——生产/NAS 部署**必须**在 `docker.env` 填自定义 key。
