#!/bin/sh
# 启用 SGME 仓库 git 钩子（幂等）——把 core.hooksPath 指向随仓库分发的 .githooks/
# 用法： sh scripts/install_git_hooks.sh
# 背景：数据卫生门禁（T-164 密钥扫描 + T-165 真实标识扫描）——
#       pre-commit 扫暂存区、pre-push 扫推送新增行，命中即拦截（见 .githooks/lib_scan.sh）
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "已启用：core.hooksPath = $(git config core.hooksPath)"
echo "生效的钩子："
ls -l .githooks/pre-commit .githooks/pre-push 2>/dev/null || ls -l .githooks/
echo
echo "说明：提交/推送时自动扫描；行内 'scan-allow' 注释可豁免单行；"
echo "      逃生开关：GIT_COMMIT_SKIP_SCAN=1（提交）/ GIT_PUSH_SKIP_SECRET_SCAN=1（推送），均会留痕。"
