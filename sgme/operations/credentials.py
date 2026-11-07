"""operations/credentials.py：凭据模块统一操作层（T-269，EP-11；三段式照抄 health.py 样板）。

D-37 v1.1（2026-11-08）扩展列接线：scope（可见范围）/ cred_type（类型）/
expires_at（过期时间）/ last_used_at（最近使用）。设计口径见
《SGME2.0-设计方案》§7.1 与《SGME-凭据模块设计》§v1.1：
- scope：all（含 NULL/空串，全部已注册 agent 可读）/ admin（仅管理员）/
  agents:<id,...>（白名单；管理员恒可读）。判定一律用**入口层反查出的
  agent_id**（HTTP 由 API key 反查；MCP 由 request.state.api_key 反查），
  绝不读请求体自称——与 D-65 隐私层同一纪律。
- expires_at：UTC ISO 8601；写入必须晚于当前；过期 = 不可读（403）。
- last_used_at：读成功且解密成功后 best-effort 更新，失败只告警不阻断。
- 写接口合并语义：可选字段缺省（None）= 不变；空串 = 清空（scope 清空=all，
  expires_at 清空=不过期）。仅更新元数据时可省略 value（仅限已存在键）。

权限（方案 §四 v1 裁决）：读（list/get/export）= 全部已注册 agent 平级，
但受 scope 与过期约束；写/删/恢复 = 仅 admin。

审计（方案 §三）：
- credential_audit 领域审计——写/删与业务写同事务（宁可失败不留无审计的写）；
  读/export best-effort（记账失败仅告警不阻断，对齐既有旁路惯例）；
  scope/过期拒绝记 denied（best-effort，便于观察误配置）。
- SecurityLog admin_op 双轨（写/删/恢复/export 四处）由入口层接线。

主密钥硬规定（方案 §三，K1）：值写入路径在 key 缺失/格式非法/非空库探测失败时
拒绝（check_master_key）——绝不自动生成、绝不明文兜底；软删/恢复不触碰密文；
仅更新元数据（省略 value）同样不触碰密文，不做 key 门禁。

审计身份（S3）：actor 由入口层组装为 default(admin,http) / <agent_id>(agent,mcp)
形态传入；本层只落库不解析身份。

导入规范：走完整子模块路径（operations/__init__.py「导入规范」）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from sgme import credentials as cred
from sgme.data import credentials_dao as dao
from sgme.operations.errors import (
    ERR_CONFLICT,
    ERR_FORBIDDEN,
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
AUDIT_DENIED = "denied"

# D-37 v1.1：scope / cred_type 值域
SCOPE_ALL = "all"
SCOPE_ADMIN = "admin"
SCOPE_AGENTS_PREFIX = "agents:"
CRED_TYPES = ("api_key", "token", "password", "other")

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

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
    """读类/拒绝审计 best-effort：失败仅告警（旁路审计绝不阻断读链）。"""
    try:
        dao.record_audit(
            creds_conn, key_name=key_name, action=action, agent_id=actor,
            value_fp=None, at=at,
        )
        creds_conn.commit()
    except Exception as e:  # noqa: BLE001 —— 旁路审计绝不阻断主链
        logger.warning("凭据读审计记账失败（不阻断，key=%s action=%s）: %s", key_name, action, e)


def _now() -> datetime:
    """当前 UTC 时间（测试可 monkeypatch）。"""
    return datetime.now(UTC)


def _parse_iso_utc(raw: str) -> datetime:
    """解析 ISO 8601 → UTC（无时区按 UTC 处理；非法抛 ValueError）。"""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"时间格式非法：{raw!r}（需 ISO 8601，如 2026-12-31T00:00:00Z）"
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_scope(raw: str | None) -> str | None:
    """校验/归一 scope；None 原样返回（= 不修改），空串 → None（= 清空回 all）。

    合法值：all / admin / agents:<agent_id,...>（逗号分隔，去空白去重）。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("scope 必须为字符串（all / admin / agents:<id,...>）")
    s = raw.strip()
    if not s:
        return None
    if s in (SCOPE_ALL, SCOPE_ADMIN):
        return s
    if s.startswith(SCOPE_AGENTS_PREFIX):
        names = [n.strip() for n in s[len(SCOPE_AGENTS_PREFIX):].split(",") if n.strip()]
        if not names:
            raise ValueError("scope 非法：agents: 后至少给一个 agent_id（例 agents:dsh,hermes）")
        bad = [n for n in names if not _AGENT_ID_RE.match(n)]
        if bad:
            raise ValueError(f"scope 中的 agent_id 非法: {', '.join(bad)}")
        deduped: list[str] = []
        for n in names:
            if n not in deduped:
                deduped.append(n)
        return f"{SCOPE_AGENTS_PREFIX}{','.join(deduped)}"
    raise ValueError("scope 非法：仅支持 all / admin / agents:<agent_id,...>")


def normalize_cred_type(raw: str | None) -> str | None:
    """校验/归一 cred_type；None 原样返回（= 不修改），空串 → None（= 清空）。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("cred_type 必须为字符串")
    s = raw.strip()
    if not s:
        return None
    if s not in CRED_TYPES:
        raise ValueError(f"cred_type 非法：仅支持 {' / '.join(CRED_TYPES)}")
    return s


def normalize_expires_at(raw: str | None) -> str | None:
    """校验/归一 expires_at（UTC ISO 秒级）；None 原样返回，空串 → None（= 不过期）。

    写入必须严格晚于当前时间。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("expires_at 必须为字符串")
    s = raw.strip()
    if not s:
        return None
    dt = _parse_iso_utc(s)
    if dt <= _now():
        raise ValueError("expires_at 必须晚于当前时间")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_expired(expires_at: str | None) -> bool:
    """是否已过期；NULL = 不过期。格式损坏按已过期处理（fail-closed）并告警。"""
    if not expires_at:
        return False
    try:
        return _parse_iso_utc(str(expires_at)) <= _now()
    except ValueError:
        logger.warning("凭据 expires_at 格式非法，按已过期处理: %r", expires_at)
        return True


def scope_allows(scope: str | None, agent_id: str | None, is_admin: bool) -> bool:
    """可见性判定：admin 恒可读；all/NULL 全可读；admin 仅管理员；白名单按 agent_id。

    未知 scope 值按「仅管理员」处理（fail-closed）。
    """
    if is_admin:
        return True
    if not scope or scope == SCOPE_ALL:
        return True
    if scope == SCOPE_ADMIN:
        return False
    if scope.startswith(SCOPE_AGENTS_PREFIX):
        return bool(agent_id) and agent_id in scope[len(SCOPE_AGENTS_PREFIX):].split(",")
    return False


def _row_view(row: dict) -> dict[str, Any]:
    """列表行视图（不含值；补 expired 计算字段）。"""
    return {
        "key_name": row["key_name"],
        "description": row.get("description"),
        "scope": row.get("scope"),
        "cred_type": row.get("cred_type"),
        "expires_at": row.get("expires_at"),
        "last_used_at": row.get("last_used_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
        "expired": is_expired(row.get("expires_at")),
    }


def _deny(
    creds_conn: sqlite3.Connection, *, key_name: str, actor: str, message: str
) -> OperationResult:
    """拒绝读（scope/过期）：记 denied 审计后返回 403。"""
    _record_read_audit(
        creds_conn, key_name=key_name, action=AUDIT_DENIED, actor=actor, at=dao.now_iso()
    )
    return OperationResult.fail(ERR_FORBIDDEN, message)


# ---------- 操作函数 ----------


def list_credentials(
    creds_conn: sqlite3.Connection,
    *,
    agent_id: str | None = None,
    is_admin: bool = True,
) -> OperationResult:
    """列表（键名 + 元数据，不含值；默认过滤已删）。

    - admin：全部 active 行（含 admin-only 与已过期，行内带 expired 标记）；
    - agent：仅 scope 可见且未过期的行。
    - list 不记账（方案 §三：高频、无值暴露）。
    """
    items = dao.list_credentials(creds_conn)
    if not is_admin:
        items = [
            it for it in items
            if scope_allows(it.get("scope"), agent_id, False) and not is_expired(it.get("expires_at"))
        ]
    rows = [_row_view(it) for it in items]
    return OperationResult.succeed({"credentials": rows, "count": len(rows)})


def get_credential(
    creds_conn: sqlite3.Connection,
    key_name: str,
    *,
    actor: str = "unknown",
    agent_id: str | None = None,
    is_admin: bool = True,
) -> OperationResult:
    """取单值（记 read 审计）。不存在/已删/名称非法 → 404；scope 拒绝/已过期 → 403。"""
    try:
        name = cred.validate_key_name(key_name)
    except ValueError:
        return OperationResult.fail(ERR_NOT_FOUND, f"凭据不存在: {key_name}")
    row = dao.get_credential(creds_conn, name)
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"凭据不存在: {name}")
    if not scope_allows(row.get("scope"), agent_id, is_admin):
        return _deny(
            creds_conn, key_name=name, actor=actor,
            message=f"无权读取凭据: {name}（scope={row.get('scope') or SCOPE_ALL}）",
        )
    if is_expired(row.get("expires_at")):
        return _deny(
            creds_conn, key_name=name, actor=actor,
            message=f"凭据已过期: {name}（expires_at={row.get('expires_at')}；请先续期）",
        )
    try:
        value = cred.decrypt_value(row["value_cipher"])
    except cred.CredKeyError:
        logger.warning("凭据解密失败（key=%s）——主密钥不可用或密文损坏", name)
        return OperationResult.fail(
            ERR_INTERNAL, "凭据解密失败：主密钥不可用或密文损坏（详见服务端日志）"
        )
    # D-37 v1.1：读成功才更新 last_used_at（best-effort，不阻断）
    try:
        dao.touch_last_used(creds_conn, name, at=dao.now_iso())
        creds_conn.commit()
    except Exception as e:  # noqa: BLE001 —— 旁路旁写绝不阻断读链
        logger.warning("更新 last_used_at 失败（不阻断，key=%s）: %s", name, e)
    _record_read_audit(
        creds_conn, key_name=name, action=AUDIT_READ, actor=actor, at=dao.now_iso()
    )
    return OperationResult.succeed(
        {
            "key_name": row["key_name"],
            "value": value,
            "description": row["description"],
            "scope": row.get("scope"),
            "cred_type": row.get("cred_type"),
            "expires_at": row.get("expires_at"),
            "last_used_at": dao.now_iso(),
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
    agent_id: str | None = None,
    is_admin: bool = True,
) -> OperationResult:
    """导出（JSON 原始值映射 {"credentials": {KEY: value}, "skipped": [...]}）。

    - only：逗号分隔键名——任一缺失（或已删）→ 404；任一不可读/已过期 → 403
      （宁可失败勿静默少发）。
    - prefix/全量：过滤后可读项导出，不可读/已过期项进 skipped
      （显式列出原因 scope|expired，不静默少发）。
    - 两参数互斥；逐键记 export 审计（best-effort）。
    """
    if only and prefix:
        return OperationResult.fail(ERR_INVALID_ARGS, "only 与 prefix 互斥：只传其一")
    items = dao.list_credentials(creds_conn)
    active = {it["key_name"]: it for it in items}
    skipped: list[dict[str, str]] = []
    if only:
        names = [n.strip() for n in only.split(",") if n.strip()]
        if not names:
            return OperationResult.fail(ERR_INVALID_ARGS, "only 为空：请给出逗号分隔的键名")
        missing = [n for n in names if n not in active]
        if missing:
            return OperationResult.fail(
                ERR_NOT_FOUND, f"凭据不存在（或已删除）: {', '.join(missing)}"
            )
        denied = [
            n for n in names
            if not scope_allows(active[n].get("scope"), agent_id, is_admin)
            or is_expired(active[n].get("expires_at"))
        ]
        if denied:
            _record_read_audit(
                creds_conn, key_name=denied[0], action=AUDIT_DENIED, actor=actor,
                at=dao.now_iso(),
            )
            return OperationResult.fail(
                ERR_FORBIDDEN,
                f"无权或已过期的凭据: {', '.join(denied)}（only 模式不做静默跳过）",
            )
        selected = names
    else:
        candidates = [n for n in active if not prefix or n.startswith(prefix)]
        selected = []
        for n in candidates:
            row = active[n]
            if not scope_allows(row.get("scope"), agent_id, is_admin):
                skipped.append({"key_name": n, "reason": "scope"})
            elif is_expired(row.get("expires_at")):
                skipped.append({"key_name": n, "reason": "expired"})
            else:
                selected.append(n)

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
    # D-37 v1.1：导出成功同样算「最近使用」（best-effort，不阻断）
    for n in values:
        try:
            dao.touch_last_used(creds_conn, n, at=at)
        except Exception as e:  # noqa: BLE001
            logger.warning("更新 last_used_at 失败（不阻断，key=%s）: %s", n, e)
    try:
        creds_conn.commit()
    except Exception:  # noqa: BLE001
        pass
    for n in values:
        _record_read_audit(
            creds_conn, key_name=n, action=AUDIT_EXPORT, actor=actor, at=at
        )
    return OperationResult.succeed(
        {"credentials": values, "count": len(values), "skipped": skipped}
    )


def write_credential(
    creds_conn: sqlite3.Connection,
    *,
    key_name: str | None,
    value: str | None = None,
    description: str | None = None,
    scope: str | None = None,
    cred_type: str | None = None,
    expires_at: str | None = None,
    actor: str = "unknown",
) -> OperationResult:
    """写入 / 更新（admin）。已删键名 → ERR_CONFLICT 引导 restore（不自动复活）。

    D-37 v1.1 合并语义：
    - value 省略（None）= 只更新元数据（仅限已存在键；不触碰密文、不做主密钥门禁）；
      新键必须给 value。
    - scope / cred_type / expires_at 省略（None）= 不变；空串 = 清空。
    - description 省略（None）= 不变；空串 = 清空（与旧版"None 即清空"不同，
      防旧客户端轮换值时误清元数据）。

    值写入前经主密钥门禁（缺失/非法/非空库探测失败 → 拒绝，绝不明文兜底）。
    审计（write + 值指纹）与业务写同事务提交；仅改元数据时 value_fp 为 NULL。
    """
    try:
        name = cred.validate_key_name(key_name)
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))

    existing = dao.get_credential(creds_conn, name, include_deleted=True)
    if existing is not None and existing["status"] == "deleted":
        return OperationResult.fail(
            ERR_CONFLICT,
            f"键名已存在但处于已删除状态：请先 POST /v1/credentials/{name}/restore "
            "恢复后再写入（不自动复活，防误覆盖）",
        )
    is_new = existing is None

    if value is not None and (not isinstance(value, str) or value == ""):
        return OperationResult.fail(
            ERR_INVALID_ARGS, "值不得为空（value 必填非空字符串；仅更新元数据时请省略 value）"
        )
    if is_new and value is None:
        return OperationResult.fail(ERR_INVALID_ARGS, "新键必须提供 value（非空字符串）")

    try:
        norm_scope = normalize_scope(scope) if scope is not None else None
        norm_type = normalize_cred_type(cred_type) if cred_type is not None else None
        norm_exp = normalize_expires_at(expires_at) if expires_at is not None else None
    except ValueError as e:
        return OperationResult.fail(ERR_INVALID_ARGS, str(e))

    base = existing or {}
    final_scope = norm_scope if scope is not None else base.get("scope")
    final_type = norm_type if cred_type is not None else base.get("cred_type")
    final_exp = norm_exp if expires_at is not None else base.get("expires_at")
    if description is None:
        final_desc = base.get("description")
    else:
        final_desc = description if description != "" else None

    if value is not None:
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
    else:
        cipher = existing["value_cipher"]  # type: ignore[index]
        fp = None

    at = dao.now_iso()
    try:
        if is_new:
            dao.insert_credential(
                creds_conn, key_name=name, value_cipher=cipher,
                description=final_desc, actor=actor, at=at,
                scope=final_scope, cred_type=final_type, expires_at=final_exp,
            )
        else:
            ok = dao.update_credential(
                creds_conn, key_name=name, value_cipher=cipher,
                description=final_desc, actor=actor, at=at,
                scope=final_scope, cred_type=final_type, expires_at=final_exp,
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
        {
            "ok": True,
            "key_name": name,
            "created": is_new,
            "updated_at": at,
            "scope": final_scope,
            "cred_type": final_type,
            "expires_at": final_exp,
        }
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
