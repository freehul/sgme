# SGME — 拾光记忆引擎 Docker 镜像
# 后端：python:3.11-slim（Debian bookworm，glibc 2.36，兼容 manylinux wheel）
#
# 布局约定（对齐 sgme/config.py T-23 标准安装布局）：
# - 程序资源（sgme 包 / config/llm.yaml / registry / templates / prompts / roles）在镜像内 /app
# - 用户数据（data / raw / logs / config/sgme.yaml / config/.env）经 SGME_HOME=/data 落到挂载卷
# - 密钥（DEEPSEEK_API_KEY / VOLC_API_KEY / SGME_ADMIN_KEY / SGME_AGENT_KEY）从环境变量注入

FROM python:3.11-slim

WORKDIR /app

# ---- 1. 依赖（先复制清单，利用 Docker 层缓存）----
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

# ---- 2. 程序（sgme 包 + 程序资源，不包含用户数据）----
# 注意：不 pip install 项目——保留 sgme/ 于 /app，使 PROJECT_ROOT=/app，
# 程序资源（config/llm.yaml 等）能正确定位；python -m sgme 从 /app 运行。
COPY sgme/ sgme/
COPY config/ config/
COPY registry/ registry/
COPY templates/ templates/
COPY prompts/ prompts/
COPY roles/ roles/

# ---- 3. 运行时 ----
ENV SGME_HOME=/data \
    SGME_HOST=0.0.0.0 \
    SGME_PORT=9910 \
    PYTHONUNBUFFERED=1

# HTTP API :9910 + MCP :9913
EXPOSE 9910 9913

# 数据卷挂载点（用户数据根，docker-compose 挂 volume 到 /data）
VOLUME ["/data"]

CMD ["python", "-m", "sgme"]
