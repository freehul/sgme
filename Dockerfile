# SGME — 拾光记忆引擎 Docker 镜像
# 多阶段构建：Stage 1 WebUI（node:20-alpine）→ Stage 2 运行时（python:3.11-slim，Debian bookworm，glibc 2.36，兼容 manylinux wheel）
#
# 布局约定（对齐 sgme/config.py T-23 标准安装布局）：
# - 程序资源（sgme 包 / config/llm.yaml / config/providers.yaml / registry / templates / prompts / roles / ui/dist）在镜像内 /app
# - 用户数据（data / raw / logs / config/sgme.yaml / config/.env）经 SGME_HOME=/data 落到挂载卷
# - 密钥（DEEPSEEK_API_KEY / VOLC_API_KEY / SGME_ADMIN_KEY / SGME_AGENT_KEY）从环境变量注入（env_file，不入镜像）
#
# sgme.yaml 语义（2026-08-16 T-72）：SGME_HOME 设置后运行时只读 $SGME_HOME/config/sgme.yaml，
# 镜像内 /app/config/sgme.yaml 仅作「首次启动模板」——entrypoint 在空卷首次启动时物化到 /data/config/，
# 用户可编辑后重启生效（含生产调优：l15 prescreen 开启 + fallback: skip_conflict 防全量召回烧钱）。

# ---- Stage 1: WebUI 构建 ----
FROM node:20-alpine AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# ---- Stage 2: 运行时 ----
FROM python:3.11-slim

WORKDIR /app

# git：skills_hub 同步依赖系统 git（B64 遗留：合入主 Dockerfile 单一入口，2026-08-16 T-70）
# safe.directory：容器内 root 访问属主 1000 的 bare 仓必需（NAS bind mount /git/skills-hub.git）
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && git config --global --add safe.directory /git/skills-hub.git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "pyyaml>=6.0" \
    "httpx>=0.27" \
    "numpy>=1.26" \
    "sqlite-vec>=0.1" \
    "requests>=2.31" \
    "mcp>=1.28,<2.0" \
    "jieba>=0.42.1"

# ---- 程序（sgme 包 + 程序资源，不包含用户数据）----
# 注意：不 pip install 项目——保留 sgme/ 于 /app，使 PROJECT_ROOT=/app，
# 程序资源（config/llm.yaml 等）能正确定位；python -m sgme 从 /app 运行。
COPY sgme/ sgme/
# config/ 内含：llm.yaml + providers.yaml（程序资源，运行时读取）+ sgme.yaml（首次启动模板）
COPY config/ config/
COPY registry/ registry/
COPY templates/ templates/
COPY prompts/ prompts/
COPY roles/ roles/

# WebUI 构建产物（Stage 1；app.py 检测 /app/ui/dist 存在即挂载 SPA，见 sgme/server/app.py §WebUI 静态托管）
COPY --from=ui-build /ui/dist ui/dist/

# 首次启动物化入口（T-72：空卷 → 复制默认 sgme.yaml 到 $SGME_HOME/config/）
COPY docker/entrypoint.sh /usr/local/bin/sgme-entrypoint.sh
RUN chmod +x /usr/local/bin/sgme-entrypoint.sh

# ---- 运行时 ----
ENV SGME_HOME=/data \
    SGME_HOST=0.0.0.0 \
    SGME_PORT=9910 \
    PYTHONUNBUFFERED=1

# HTTP API + WebUI :9910 + MCP :9913
EXPOSE 9910 9913

# 数据卷挂载点（用户数据根，docker-compose 挂 volume 到 /data）
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/sgme-entrypoint.sh"]
CMD ["python", "-m", "sgme"]
