#!/bin/sh
# ============================================================
# SGME 门禁规则自测（scripts/test_git_hooks.sh，2026-09-13 立）
#
# 用途：对 .githooks/lib_scan.sh 的规则做回归自测。起因（B182）：T-165
#   重构丢失「低熵占位按 '-' 后 body 判定」修正，导致 sk-000…0 假串在
#   推送预检中被误拦——改 lib_scan.sh 后必跑本脚本。
#
# 用法：sh scripts/test_git_hooks.sh   → 全部用例通过 exit 0；有失败 exit 1
#
# 自匹配说明：本文件也进仓库、也会被门禁扫描，故所有测试数据一律
#   「运行时拼接」——文件文本层不出现连续长密钥串 / 真实 IP / 路径 / 姓名。
# ============================================================
set -u

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
. "$ROOT/.githooks/lib_scan.sh"

TMP="$(mktemp -d 2>/dev/null || echo "/tmp/sgme_hooktest.$$")"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

# check <期望:0放行/1拦截> <场景名> <数据内容>
check() {
    _want="$1"; _name="$2"; _data="$3"
    printf '%s\n' "$_data" > "$TMP/case.txt"
    if sgme_scan_file "$TMP/case.txt" >/dev/null 2>&1; then _got=0; else _got=1; fi
    if [ "$_got" = "$_want" ]; then
        PASS=$((PASS + 1)); printf '  [ok]   %s\n' "$_name"
    else
        FAIL=$((FAIL + 1)); printf '  [FAIL] %s（期望 %s 实得 %s）\n' "$_name" "$_want" "$_got"
    fi
}

# —— 测试数据（全部运行时拼接） ——
LOWKEY="sk-$(printf '0%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32)"
LOWKEY2="$(printf 'sk-'; printf '9%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20)"
WLKEY_PREFIX='sk-abcdefghij'
WLKEY="${WLKEY_PREFIX}0123456789zyxwvutsrq"          # 高熵假串（白名单覆盖）
R1='Qw9zXy8AbC7dEf6'; R2='GhI5jK4lMn3oP'
RANDKEY="sk-${R1}${R2}${R1}"                          # 高熵随机（不在白名单）
I1='192'; I2='168'; I3='7'; I4='23'
IP="${I1}.${I2}.${I3}.${I4}"                          # 内网 IP 形态
VOL1='vol1'; VNUM='1000'
NAS_PATH="/${VOL1}/${VNUM}/Docker/sgme"
DRV='C:'; USR='someuser'
WIN_PATH="${DRV}/Users/${USR}/x"
N1='胡'; N2='亮'
NAME="${N1}${N2}"

echo "=== SGME 门禁规则自测（lib_scan.sh） ==="
check 0 "普通文本放行" "hello world, nothing sensitive"
check 0 "低熵占位放行（sk- + 32×0，B182 回归点）" "assert x == '${LOWKEY}'"
check 0 "低熵占位放行（sk- + 全同字符）" "sample='${LOWKEY2}'"
check 0 "高熵假串放行（白名单 sk-abcdefghij*）" "demo='${WLKEY}'"
check 1 "高熵随机串拦截" "leaked='${RANDKEY}'"
check 1 "内网 IP 拦截" "host = '${IP}'"
check 0 "内网 IP + scan-allow 豁免" "host = '${IP}'  # scan-allow：示例"
check 1 "NAS 卷路径拦截" "root = '${NAS_PATH}'"
check 1 "本机用户目录拦截" "p = '${WIN_PATH}'"
check 1 "真实姓名拦截" "author: ${NAME}"

echo ""
echo "=== 结果：通过 ${PASS} / 失败 ${FAIL} / 共 $((PASS + FAIL)) ==="
[ "$FAIL" = "0" ] || exit 1
exit 0
