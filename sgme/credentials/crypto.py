"""sgme/credentials/crypto.py：Fernet 加解密与主密钥状态叶子（T-267，EP-11）。

主密钥硬规定（方案 §三「主密钥生命周期」，DS 审查 K1 / 二轮边界写死）：
- 主密钥 = 环境变量 ``SGME_CRED_KEY``（经 config/.env → load_env_file 注入进程环境）；
- 缺失 / 格式非法 / **非空库解密探测失败** → 拒绝一切写路径（上层裁决，本模块提供判定）；
- **绝不自动生成新 key、绝不明文兜底**（自动生成会永久锁死旧数据）；
- 空库（无任何密文）视为探测通过。

纪律：错误信息绝不携带主密钥或任何明文值/密文；零内部依赖（叶子包）。
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

# 环境变量名（密钥不落盘铁律：只引用变量名，值经 config/.env 注入）
ENV_CRED_KEY = "SGME_CRED_KEY"

# 状态枚举（对外只读判定，字符串稳定供 health / 日志使用）
STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_INVALID_FORMAT = "invalid_format"
STATE_PROBE_FAILED = "probe_failed"

# Fernet key 形态：32 字节 urlsafe-base64 → 44 字符（含结尾 =）
_KEY_LEN = 44


class CredKeyError(Exception):
    """主密钥不可用 / 解密失败。消息不含主密钥与明文值。"""


def get_master_key() -> str | None:
    """读取主密钥环境变量（缺失/空串 → None）。"""
    val = os.environ.get(ENV_CRED_KEY)
    return val.strip() if val and val.strip() else None


def is_valid_key_format(key: str | None) -> bool:
    """Fernet key 格式校验：44 字符 + 可被 Fernet 构造（底层 base64 解码 + 32 字节）。"""
    if not isinstance(key, str) or len(key) != _KEY_LEN:
        return False
    try:
        Fernet(key.encode("utf-8"))
        return True
    except (ValueError, TypeError):
        return False


def master_key_state(sample_ciphertext: str | None = None) -> dict:
    """主密钥状态判定（供写路径门禁 / 启动自检 / health 只读字段）。

    Args:
        sample_ciphertext: 库内首条密文（无行传 None）——用于「非空库解密探测」。

    Returns:
        {"ok": bool, "state": STATE_*}；ok=False 时写路径必须拒绝。
    """
    key = get_master_key()
    if not key:
        return {"ok": False, "state": STATE_MISSING}
    if not is_valid_key_format(key):
        return {"ok": False, "state": STATE_INVALID_FORMAT}
    if sample_ciphertext is None:
        return {"ok": True, "state": STATE_OK}  # 空库无值可探测，视为通过
    try:
        Fernet(key.encode("utf-8")).decrypt(sample_ciphertext.encode("utf-8"))
        return {"ok": True, "state": STATE_OK}
    except (InvalidToken, ValueError, TypeError):
        return {"ok": False, "state": STATE_PROBE_FAILED}


def encrypt_value(plaintext: str) -> str:
    """加密明文 → Fernet 密文（base64 文本）。主密钥不可用抛 CredKeyError。"""
    key = get_master_key()
    if not key or not is_valid_key_format(key):
        raise CredKeyError("主密钥不可用（缺失或格式非法）——已拒绝加密写入；请检查 SGME_CRED_KEY")
    return Fernet(key.encode("utf-8")).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: str) -> str:
    """解密 Fernet 密文 → 明文。失败抛 CredKeyError（消息不含任何值）。"""
    key = get_master_key()
    if not key or not is_valid_key_format(key):
        raise CredKeyError("主密钥不可用（缺失或格式非法）——无法解密凭据")
    try:
        return Fernet(key.encode("utf-8")).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as e:
        raise CredKeyError("凭据解密失败（主密钥不匹配或密文损坏）") from e
