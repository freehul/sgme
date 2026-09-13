#!/bin/sh
# ============================================================
# SGME 数据卫生扫描核心（lib_scan.sh，T-165，2026-09-13）
# 由 .githooks/pre-commit 与 .githooks/pre-push 共同引用（单一规则源）。
#
# 用法（在 hook 中）：
#   . "$(git rev-parse --show-toplevel)/.githooks/lib_scan.sh"
#   sgme_scan_file "<新增行数据文件>"   # 命中 → return 1 并打印明细
#
# 规则：
#   [密钥] sk-/pk-/ak-/ark-/sgme_(admin|agent)_/agt_/AWS 长串
#          （低熵占位自动放行；白名单 .githooks/secret_scan_allowlist）
#   [标识] 真实内网/公网 IP、NAS 卷路径、本机盘符用户目录、真实姓名
#          行内豁免：任意行含 "scan-allow" 即跳过
#
# 两条重要工程经验（改这里必读）：
# 1) 性能：标识扫描初版为「逐行 × 逐模式」子进程循环，Windows 上对数千行 diff
#    需上万次进程创建、耗时超 5 分钟；现为「单次 grep -E 多模式 + 一次格式化」，
#    秒级完成。保持单进程风格。
# 2) 自匹配：模式串本身也是被扫文本（本文件会进仓库），若模式片段直接写成
#    字面会把本文件自己拦下。故所有可能自匹配的片段（卷路径数字、姓名两字）
#    一律拆为变量拼接（见下行 _VOL/_NUM/_N1/_N2）；`192\.168\.` 等因反斜杠
#    天然不匹配，无需处理。新增规则后先跑 `sh .githooks/pre-commit` 自检。
# ============================================================

_VOL='vol1'; _NUM='1000'; _N1='胡'; _N2='亮'
SGMESCAN_LABEL_PATTERNS='192\.168\.[0-9]{1,3}\.[0-9]{1,3}|/?'"$_VOL/$_NUM"'|43\.255\.156\.6|171\.40\.166\.121|195\.178\.110\.220|D:[\\/][Pp]rojects|C:[\\/][Uu]sers[\\/][A-Za-z]+|/[cC]/[Uu]sers/[A-Za-z]+|'"$_N1$_N2"

sgme_scan_file() {
    _data="$1"
    _blocked=""

    # —— A. 密钥类（token 级；低熵占位放行） ——
    _root=$(git rev-parse --show-toplevel 2>/dev/null)
    for tok in $(grep -aoE 'sk-[A-Za-z0-9]{20,}|pk-[A-Za-z0-9]{20,}|ak-[A-Za-z0-9]{20,}|ark-[A-Za-z0-9-]{20,}|sgme_(admin|agent)_[A-Za-z0-9]{16,}|agt_[A-Za-z0-9]{16,}|(AKIA|ASIA)[A-Z0-9]{16}' "$_data" 2>/dev/null | sort -u); do
        # 低熵占位自动放行：对 '-' 之后的 body 判定（去重字符 ≤ 3，如 sk-000…0 假串）
        # ⚠️ 必须用 body 判定，勿回退为全 token 去重——sk- 前缀自带 3 种字符，全 token
        #    判定会让 sk-000…0 恒被误拦（2026-09-13 回归修复，见变更记录 B182 /
        #    自测脚本 scripts/test_git_hooks.sh）
        body=${tok#*-}
        n=$(printf '%s' "$body" | fold -w1 | sort -u | wc -l | tr -d ' ')
        [ "$n" -le 3 ] && continue
        # 白名单放行：白名单文件每行是一条正则（`#` 开头为注释），命中 token 即放行
        # ⚠️ 方向必须是「白名单正则匹配 token」；不能写成「token 当正则搜白名单文本」
        #    （旧写法永不命中所列模式——2026-09-13 修复，见 B182）
        _allow=0
        if [ -f "$_root/.githooks/secret_scan_allowlist" ]; then
            while IFS= read -r _pat; do
                case "$_pat" in ''|\#*) continue ;; esac
                if printf '%s' "$tok" | grep -qE "$_pat" 2>/dev/null; then
                    _allow=1
                    break
                fi
            done < "$_root/.githooks/secret_scan_allowlist"
        fi
        [ "$_allow" = "1" ] && continue
        _blocked="$_blocked
  [密钥] $tok"
    done

    # —— B. 真实标识（单次多模式 grep；scan-allow 行豁免） ——
    _hits=$(grep -nE "$SGMESCAN_LABEL_PATTERNS" "$_data" 2>/dev/null | grep -v 'scan-allow' | head -50)
    if [ -n "$_hits" ]; then
        _blocked="$_blocked
$(printf '%s\n' "$_hits" | sed 's/^/  [标识] /' | cut -c1-120)"
    fi

    if [ -n "$_blocked" ]; then
        {
            echo ""
            echo "[scan] 检测到疑似真实数据，已拦截：$_blocked"
            echo ""
            echo "  · 数据卫生规范见 AGENTS.md「数据卫生」章：IP/路径/姓名用功能占位符（如 <NAS_IP>），值走环境变量"
            echo "  · 确认是误报/必要场景：在该行加注释 scan-allow，或逃生开关（提交 GIT_COMMIT_SKIP_SCAN / 推送 GIT_PUSH_SKIP_SECRET_SCAN，留痕）"
            echo ""
        } >&2
        return 1
    fi
    return 0
}
