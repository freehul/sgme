#!/bin/sh
# 启用 SGME 仓库 git 钩子（幂等）——把 core.hooksPath 指向随仓库分发的 .githooks/
# 用法： sh scripts/install_git_hooks.sh
# 背景：T-164（2026-09-13）密钥泄露事件后立推送前密钥扫描门禁，见 .githooks/pre-push
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "已启用：core.hooksPath = .githooks"
echo "--- pre-push 钩子就位确认 ---"
ls -l .githooks/pre-push
echo "--- 测试（空范围推演：不带参数直接手动跑会扫描 stdin，无输入=通过） ---"
echo "" | sh .githooks/pre-push origin 2>&1 | tail -2
