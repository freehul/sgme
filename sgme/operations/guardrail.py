"""sgme/operations/guardrail.py：T-139 Guardrail——敏感信息写前/召回后过滤层。

规则匹配优先（快，正则），LLM 方案兜底（慢，默认关——留接口）。
误脱敏可控：规则集中管理 + 总开关默认关（灰度，先观察再开）+ mask 保留脱敏标记。

模式（SENSITIVE_PATTERNS）：身份证 / 手机号 / 银行卡 / API 密钥 / 邮箱 / 内网 IP。

- detect(text) -> list[str]：命中的规则名列表（去重、保序）
- mask(text) -> (str, list[str])：命中段替换为 ***，返回 (脱敏后文本, 命中规则)
- decision(cfg, text) -> (action, masked, matched)：
  block=拦截（丢弃）/ mask=脱敏放行 / pass=放行
"""

from __future__ import annotations

import re

# 规则名 → 正则（谨慎：宁少勿滥，防误脱敏）
SENSITIVE_PATTERNS: dict[str, re.Pattern] = {
    # 身份证号（18 位，末位可为 X）
    "id_card": re.compile(r"\b\d{17}[\dXx]\b"),
    # 大陆手机号（1[3-9] 开头 11 位）
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # 银行卡号（16-19 位连续数字）
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    # API 密钥（sk-/pk-/ak- 前缀 + 长随机串，覆盖 DeepSeek/火山/Agnes 等格式）
    "api_key": re.compile(r"\b(?:sk|pk|ak|sk-[a-z0-9])[A-Za-z0-9_-]{16,}\b"),
    # 邮箱
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    # 内网 IP（10.x / 172.16-31.x / 192.168.x——部署拓扑泄露面）
    "private_ip": re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
}

# 默认脱敏替换符
MASK_TOKEN = "***"

# 决策常量
ACTION_BLOCK = "block"    # 拦截（写前丢弃该记忆）
ACTION_MASK = "mask"      # 脱敏放行（写前改写 content / 召回后替换展示）
ACTION_PASS = "pass"      # 放行（无命中）


def detect(text: str, rules: dict[str, re.Pattern] | None = None) -> list[str]:
    """命中规则名列表（去重、按规则定义序）。空文本/无命中 → []。"""
    if not text:
        return []
    rules = rules or SENSITIVE_PATTERNS
    return [name for name, pat in rules.items() if pat.search(text)]


def mask(text: str, rules: dict[str, re.Pattern] | None = None) -> tuple[str, list[str]]:
    """命中段替换为 MASK_TOKEN。返回 (脱敏后文本, 命中规则名)。"""
    if not text:
        return text, []
    rules = rules or SENSITIVE_PATTERNS
    hits = [name for name, pat in rules.items() if pat.search(text)]
    out = text
    for name in hits:
        out = rules[name].sub(MASK_TOKEN, out)
    return out, hits


def decision(cfg: dict, text: str) -> tuple[str, str, list[str]]:
    """按 guardrail 配置决策单条文本。返回 (action, 处理后文本, 命中规则)。

    - enabled=False（默认，灰度）：pass（行为与 T-139 前一致）
    - write_mode=block：命中 → block；否则 pass
    - write_mode=mask：命中 → mask（返回脱敏文本）；否则 pass
    """
    grd = cfg or {}
    if not grd.get("enabled", False):
        return ACTION_PASS, text, []
    matched = detect(text)
    if not matched:
        return ACTION_PASS, text, []
    mode = grd.get("write_mode", "mask")
    if mode == "block":
        return ACTION_BLOCK, text, matched
    masked, _ = mask(text)
    return ACTION_MASK, masked, matched
