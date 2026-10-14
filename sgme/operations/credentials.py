"""operations/credentials.py：凭据模块统一操作层（T-269，EP-11；三段式照抄 health.py 样板）。

权限（方案 §四 v1 裁决）：读（list/get/export）= 全部已注册 agent 平级；
写/删/恢复 = 仅 admin（"可配置放开"旋钮砍掉；agent scope 字段留作 v1.1 扩展位）。

审计（方案 §三）：
- credential_audit 领域审计——**写/删与业务写同事务**（宁可失败不留无审计的写）；
  读/export best-effort（记账失败仅告警不阻断，对齐既有旁路惯例）。
- SecurityLog admin_op 双轨（写/删/恢复/export 四处）由入口层接线。

主密钥硬规定（方案 §三，K1）：**值写入路径**在 key 缺失/格式非法/非空库探测失败时
拒绝（check_master_key）——绝不自动生成、绝不明文兜底；软删/恢复不触碰密文
（不产生"混合密钥"风险，且是密钥修复窗口的必要操作），不设 key 门禁。

审计身份（S3）：actor 由入口层组装为 ``default(admin,http)`` / ``<agent_id>(agent,mcp)``
形态传入；本层只落库不解析身份。

导入规范：走完整子模块路径（operations/__init__.py「导入规范」）。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from sgme import credentials as cred
from sgme.data import credentials_dao as dao
from sgme.operations.errors import (
    ERR_CONFLICT,
    ERR_INTERNAL,
    ERR_INVALID_ARGS,
    ERR_NOT_FOUND,
    OperationResult,
)

logger = logging.getLogger("sgme.operations.credentials")

# 审计动作（credential_audit.action；字符串稳定，供安全面查询）
AUDIT_READ = "read"
AUDIT_EXPORT = "export"
AUDIT_WRITE = "write"
AUDIT_DELETE = "delete"
AUDIT_RESTORE = "restore"

_KEY_UNAVAILABLE_MSG = (
    "凭据主密钥不可用（SGME_CRED_KEY 缺失/格式非法/与库内密文不匹配）——"
    "写路径已拒绝（绝不自动生成、绝不明文兜底）；请检查 config/.env 后重启服务"
)


# ---------- 内部工具 ----------


def check_master_key(creds_conn: sqlite3.Connection) -> dict:
    """主密钥状态自检（启动告警 / 写路径门禁 / health 只读字段共用）。

    Returns:
        {"ok": bool, "state": ok|missing|invalid_format|probe_failed}
    """
    return cred.master_key_state(dao.get_first_cipher(creds_conn))


def _record_read_audit(
    creds_conn: sqlite3.Connection,
    *,
    key_name: str,
    action: str,
    actor: str,
    at: str,
) -> None:
    """读类审计 best-effort：失败仅告警（旁路审计绝不阻断读链）。"""
    try:
        dao.record_audit(
            creds_conn, key_name=key_name, action=action, agent_id=actor,
            value_fp=None, at=at,
        )
        creds_conn.commit()
    except Exception as e:  # noqa: BLE001 —— 旁路审计绝不阻断主链
        logger.warning("凭据读审计记账失败（不阻断，key=%s action=%s）: %s", key_name, action, e)


# ---------- 操作函数 ----------


def list_credentials(creds_conn: sqlite3.Connection) -> OperationResult:
    """列表（键名 + 元数据，**不含值**；默认过滤已删）。

    list 不记账（方案 §三：高频、无值暴露）。
    """
    items = dao.list_credentials(creds_conn)
    return OperationResult.succeed(
        {
            "credentials": [
                {
                    "key_name": it["key_name"],
                    "description": it["description"],
                    "created_at": it["created_at"],
                    "updated_at": it["updated_at"],
                    "updated_by": it["updated_by"],
                }
                for it in items
            ],
            "count": len(items),
        }
    )


def get_credential(
    creds_conn: sqlite3.Connection, key_name: str, *, actor: str = "unknown"
) -> OperationResult:
    """取单值（记 read 审计）。不存在/已删/名称非法 → ERR_NOT_FOUND。"""
    try:
        name = cred.validate_key_name(key_name)
    except ValueError:
        return OperationResult.fail(ERR_NOT_FOUND, f"凭据不存在: {key_name}")
    row = dao.get_credential(creds_conn, name)
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"凭据不存在: {name}")
    try:
        value = cred.decrypt_value(row["value_cipher"])
    except cred.CredKeyError:
        logger.warning("凭据解密失败（key=%s）——主密钥不可用或密文损坏", name)
        return OperationResult.fail(
            ERR_INTERNAL, "凭据解密失败：主密钥不可用或密文损坏（详见服务端日志）"
        )
    _record_read_audit(
        creds_conn, key_name=name, action=AUDIT_READ, actor=actor, at=dao.now_iso()
    )
    return OperationResult.succeed(
        {
            "key_name": row["key_name"],
            "value": value,
            "description": row["description"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }
    )


def export_credentials(
    creds_conn: sqlite3.Connection,
    *,
    only: str | None = None,
    prefix: str | None = None,
    actor: str = "unknown",
) -> OperationResult:
    """导出（JSON 原始值映射 {"credentials": {KEY: value}}，供客户端格式化 dotenv）。

    - ``only``：逗号分隔键名清单——任一缺失（或已删）→ ERR_NOT_FOUND（宁可失败勿静默少发）；
    - ``prefix``：前缀过滤（过滤语义，零命中返回空集）；
    - 两参数互斥；逐键记 export 审计（best-effort）。
    """
    if only and prefix:
        return OperationResult.fail(ERR_INVALID_ARGS, "only 与 prefix 互斥：只传其一")
    items = dao.list_credentials(creds_conn)
    active = {it["key_name"]: it for it in items}
    if only:
        names = [n.strip() for n in only.split(",") if n.strip()]
        if not names:
            return OperationResult.fail(ERR_INVALID_ARGS, "only 为空：请给出逗号分隔的键名")
        missing = [n for n in names if n not in active]
        if missing:
            return OperationResult.fail(
                ERR_NOT_FOUND, f"凭据不存在（或已删除）: {', '.join(missing)}"
            )
        selected = names
    elif prefix:
        selected = [n for n in active if n.startswith(prefix)]
    else:
        selected = list(active)

    values: dict[str, str] = {}
    for n in selected:
        row = dao.get_credential(creds_conn, n)
        if row is None:  # 挑选后消失的极窄窗口：跳过
            continue
        try:
            values[n] = cred.decrypt_value(row["value_cipher"])
        except cred.CredKeyError:
            logger.warning("凭据解密失败（key=%s）——导出已中止", n)
            return OperationResult.fail(
                ERR_INTERNAL, "凭据解密失败：主密钥不可用或密文损坏（导出已中止）"
            )
    at = dao.now_iso()
    for n in values:
        _record_read_audit(
            creds_conn, key_name=n, action=AUDIT_EXPORT, actor=actor, at=at
        )
    return OperationResult.succeed({"credentials": values, "count": len(values)})


def write_credential(
    creds_conn: sqlite3.Connection,
    *,
    key_name: str | None,
    value: str | None,
    description: str | None = None,
    actor: str = "unknown",
) -> OperationResult:
    """写入 / 更新（admin）。已删键名 → ERR_CONFLICT 引导 restore（不自动复活）。

    值写入前经主密钥门禁（缺失/非法/非空库探测失败 → 拒绝，绝不明文兜底）。
    审计（write + 值指纹）与业务写**同事务**提交。
    """
    try:
        name = cred.validate_key_name(key_name)
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    if not isinstance(value, str) or value == "":
        return OperationResult.fail(ERR_INVALID_ARGS, "值不得为空（value 必填非空字符串）")

    existing = dao.get_credential(creds_conn, name, include_deleted=True)
    if existing is not None and existing["status"] == "deleted":
        return OperationResult.fail(
            ERR_CONFLICT,
            f"键名已存在但处于已删除状态：请先 POST /v1/credentials/{name}/restore "
            "恢复后再写入（不自动复活，防误覆盖）",
        )

    state = check_master_key(creds_conn)
    if not state["ok"]:
        return OperationResult.fail(
            ERR_INTERNAL, f"{_KEY_UNAVAILABLE_MSG}（state={state['state']}）"
        )
    try:
        cipher = cred.encrypt_value(value)
    except cred.CredKeyError:
        return OperationResult.fail(ERR_INTERNAL, _KEY_UNAVAILABLE_MSG)

    fp = dao.value_fingerprint(value)
    at = dao.now_iso()
    try:
        if existing is None:
            dao.insert_credential(
                creds_conn, key_name=name, value_cipher=cipher,
                description=description, actor=actor, at=at,
            )
        else:
            ok = dao.update_credential(
                creds_conn, key_name=name, value_cipher=cipher,
                description=description, actor=actor, at=at,
            )
            if not ok:  # 防御：并发把行改成 deleted（单用户场景理论窗口）
                creds_conn.rollback()
                return OperationResult.fail(ERR_CONFLICT, "写入冲突：目标键状态已变化，请重试")
        dao.record_audit(
            creds_conn, key_name=name, action=AUDIT_WRITE, agent_id=actor,
            value_fp=fp, at=at,
        )
        creds_conn.commit()
    except Exception as e:  # 业务写异常 → 整体回滚（不留无审计的写）
        try:
            creds_conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.exception("凭据写入失败（已回滚）")
        return OperationResult.fail(ERR_INTERNAL, f"凭据写入失败（已回滚）: {e}")

    return OperationResult.succeed(
        {"ok": True, "key_name": name, "created": existing is None, "updated_at": at}
    )


def delete_credential(
    creds_conn: sqlite3.Connection, *, key_name: str | None, actor: str = "unknown"
) -> OperationResult:
    """软删（admin；幂等：重复调用 no-op 成功）。不存在 → ERR_NOT_FOUND。

    不触碰密文（无需主密钥）；审计与状态变更同事务。
    """
    try:
        name = cred.validate_key_name(key_name)
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    existing = dao.get_credential(creds_conn, name, include_deleted=True)
    if existing is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"凭据不存在: {name}")
    at = dao.now_iso()
    try:
        changed = dao.soft_delete_credential(creds_conn, name, at=at)
        dao.record_audit(
            creds_conn, key_name=name, action=AUDIT_DELETE, agent_id=actor,
            value_fp=None, at=at,
        )
        creds_conn.commit()
    except Exception as e:  # noqa: BLE001
        try:
            creds_conn.rollback()
        except Exception:
            pass
        logger.exception("凭据软删失败（已回滚）")
        return OperationResult.fail(ERR_INTERNAL, f"凭据删除失败（已回滚）: {e}")
    return OperationResult.succeed({"ok": True, "key_name": name, "changed": changed})


def restore_credential(
    creds_conn: sqlite3.Connection, *, key_name: str | None, actor: str = "unknown"
) -> OperationResult:
    """恢复（admin；幂等：active 行 no-op 成功）。不存在 → ERR_NOT_FOUND。

    清空 deleted_at（方案 §三）；审计与状态变更同事务。
    """
    try:
        name = cred.validate_key_name(key_name)
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))
    existing = dao.get_credential(creds_conn, name, include_deleted=True)
    if existing is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"凭据不存在: {name}")
    at = dao.now_iso()
    try:
        changed = dao.restore_credential(creds_conn, name)
        dao.record_audit(
            creds_conn, key_name=name, action=AUDIT_RESTORE, agent_id=actor,
            value_fp=None, at=at,
        )
        creds_conn.commit()
    except Exception as e:  # noqa: BLE001
        try:
            creds_conn.rollback()
        except Exception:
            pass
        logger.exception("凭据恢复失败（已回滚）")
        return OperationResult.fail(ERR_INTERNAL, f"凭据恢复失败（已回滚）: {e}")
    return OperationResult.succeed({"ok": True, "key_name": name, "changed": changed})


# ---------- 投影 ----------


def http_payload(data: dict[str, Any]) -> dict[str, Any]:
    """HTTP 投影：本模块为新增能力，无历史契约差异——超集 == HTTP 形态，原样透传。"""
    return data


def mcp_payload(data: dict[str, Any]) -> dict[str, Any]:
    """MCP 投影：同上（新工具，无历史契约需要还原）。"""
    return data
