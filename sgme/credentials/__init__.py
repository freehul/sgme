"""sgme/credentials/：凭据模块叶子包（T-267，EP-11）。

职责收窄（方案 §六）：Fernet 加解密封装（crypto.py）、常量、key_name 校验——
零内部依赖（不 import data/operations/server 等上层），供 operations / data 层复用。
"""
from __future__ import annotations

import re

from sgme.credentials.crypto import (  # noqa: F401  (re-export)
    ENV_CRED_KEY,
    STATE_INVALID_FORMAT,
    STATE_MISSING,
    STATE_OK,
    STATE_PROBE_FAILED,
    CredKeyError,
    decrypt_value,
    encrypt_value,
    get_master_key,
    is_valid_key_format,
    master_key_state,
)

# key_name 校验（方案 §三 / D2）：大写字母起始 + 大写字母/数字/下划线
KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 保留名：模块自用 / 引导凭据空间（§七 边界——agent key 为 bootstrap 凭据，
# 不进库、不归本模块管理；主密钥自指也不允许）
RESERVED_KEY_NAMES = frozenset({"SGME_CRED_KEY", "SGME_ADMIN_KEY", "SGME_AGENT_KEY"})


def validate_key_name(name: str | None) -> str:
    """校验并返回 key_name；非法抛 ValueError（operations 层转 ERR_INVALID_ARGS）。"""
    if not isinstance(name, str) or not KEY_NAME_RE.match(name):
        raise ValueError(
            "key_name 非法：须为大写字母起始的大写字母/数字/下划线（例：DEEPSEEK_API_KEY）"
        )
    if name in RESERVED_KEY_NAMES:
        raise ValueError(f"key_name 为保留名（引导/主密钥空间，不进库）：{name}")
    return name


__all__ = [
    "ENV_CRED_KEY",
    "STATE_OK",
    "STATE_MISSING",
    "STATE_INVALID_FORMAT",
    "STATE_PROBE_FAILED",
    "CredKeyError",
    "decrypt_value",
    "encrypt_value",
    "get_master_key",
    "is_valid_key_format",
    "master_key_state",
    "KEY_NAME_RE",
    "RESERVED_KEY_NAMES",
    "validate_key_name",
]
