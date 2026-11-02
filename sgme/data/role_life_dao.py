"""data/role_life_dao.py：角色离线生活 DAO（role_life_events 独立表）。

背景（2026-11-02 用户提出·任务 B）：角色在用户看不见的时候也该「活着」——自己看到
点东西、心里存点事，聊天时自然流露，**不做推送**（用户 2026-08-24 明确拒绝自动关怀
推送）。本模块只负责「角色自己的生活」的存取与注入选条。

三条硬边界（违反即返工）
------------------------
1. **绝不进用户记忆池**：角色人生是虚构设定，写进独立表 ``role_life_events``，
   与 ``memories`` 没有任何 route/外键关系——虚构剧情一旦混进事实池就会污染用户真实记忆。
2. **内容只到类型级**：写「看到有人在争论本地模型的显存调优」，不写「看到某某的文章」。
   专名级内容经不起一句追问，崩一次这个人格就废了。边界由上游（抓取+人设过滤）保证，
   本模块不擅自清洗文本，但 ``clean_content`` 提供了统一的自检钩子。
3. **秘密必须稳定**：``is_secret=1`` 的事件**永不参与注入**；只在用户主动问起时由
   ``pick_secret`` 取出，说出口立刻 ``mark_shared``，同一段只说一次。

注入频率闸
----------
- 每次装配最多给 **1 条**（``pick_for_injection`` 选完即消费，同一件事不重复讲）
- 每天最多 ``daily_max`` 条（按 ``injected_at`` 当日起算，日期用 UTC）
- 只给 ``lookback_days`` 内的新鲜事（放太久的事「今天心里还惦记」说不通）
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

# 事件类型白名单（交接 2026-11-02 文档 §3.5）
LIFE_KINDS: frozenset[str] = frozenset({"browse", "read", "think", "see", "wonder"})
DEFAULT_KIND = "see"

# 历史条数上限（防止单角色无限堆积；超出由调用方/运营侧清理，绝不自动删）
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 500


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """统一 UTC ISO（秒级，与仓库既有 `_now_iso` 同口径，便于字符串比较排序）。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_iso(value: str | None, fallback: datetime) -> str:
    """把调用方给的时间归一为 UTC ISO；解析不出按 fallback 处理（不抛错，不阻塞写入）。"""
    if not value:
        return _iso(fallback)
    try:
        # Python 3.11+ 的 fromisoformat 原生支持末尾 Z（3.12 基准解释器）
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            # 无时区的输入按 UTC 解释（仓库全链路 UTC 口径）
            dt = dt.replace(tzinfo=UTC)
        return _iso(dt.astimezone(UTC))
    except Exception:  # noqa: BLE001
        return _iso(fallback)


def _row_to_item(row: sqlite3.Row | tuple) -> dict[str, Any]:
    """行 → 对外字典（字段名与交接文档表结构一致）。"""
    keys = (
        "event_id", "role_id", "at", "kind", "content", "mood",
        "why_secret", "is_secret", "injected_at", "shared_at", "created_at",
    )
    item: dict[str, Any] = {k: row[i] for i, k in enumerate(keys)}
    item["is_secret"] = int(item.get("is_secret") or 0)
    return item


# ---------- 写入 ----------

def add_event(
    conn: sqlite3.Connection,
    role_id: str,
    content: str,
    *,
    kind: str = DEFAULT_KIND,
    mood: str | None = None,
    why_secret: str | None = None,
    is_secret: bool = False,
    at: str | None = None,
) -> str:
    """写一条角色生活事件，返回 event_id。

    Args:
        role_id: 角色 id（小写短横线，与角色卡同名）
        content: **类型级**描述（禁专名/数字断言）
        kind: browse / read / think / see / wonder
        mood: 角色当时的情绪（可空）
        why_secret: 为什么没说出口（羞耻/还没想明白/怕你担心），仅秘密有意义
        is_secret: 秘密不进注入，被问才说
        at: 发生时刻（ISO，缺省=现在）
    """
    if not (content or "").strip():
        raise ValueError("content 不能为空")
    if kind not in LIFE_KINDS:
        raise ValueError(f"非法 kind: {kind!r}（允许 {sorted(LIFE_KINDS)}）")
    now = _now_utc()
    event_id = str(uuid.uuid4())
    conn.execute(
        """insert into role_life_events
           (event_id, role_id, at, kind, content, mood, why_secret, is_secret,
            injected_at, shared_at, created_at)
           values (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, role_id, _norm_iso(at, now), kind, content.strip(),
            mood, why_secret, 1 if is_secret else 0, None, None, _iso(now),
        ),
    )
    conn.commit()
    return event_id


# ---------- 读取 ----------

def list_events(
    conn: sqlite3.Connection,
    role_id: str | None = None,
    *,
    limit: int = LIST_LIMIT_DEFAULT,
    only_secret: bool | None = None,
    unshared_only: bool = False,
) -> list[dict[str, Any]]:
    """按角色列出事件（at 降序）。

    only_secret: None=全部；True=只看秘密；False=只看可注入的公开见闻
    unshared_only: 只看还没说出口的秘密
    """
    limit = max(1, min(int(limit or LIST_LIMIT_DEFAULT), LIST_LIMIT_MAX))
    where: list[str] = []
    args: list[Any] = []
    if role_id:
        where.append("role_id = ?")
        args.append(role_id)
    if only_secret is True:
        where.append("is_secret = 1")
    elif only_secret is False:
        where.append("is_secret = 0")
    if unshared_only:
        where.append("is_secret = 1 and shared_at is null")
    sql = "select event_id, role_id, at, kind, content, mood, why_secret, is_secret, injected_at, shared_at, created_at from role_life_events"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by at desc, created_at desc limit ?"
    args.append(limit)
    return [_row_to_item(r) for r in conn.execute(sql, args).fetchall()]


def count_events(conn: sqlite3.Connection, role_id: str | None = None) -> int:
    if role_id:
        row = conn.execute(
            "select count(*) from role_life_events where role_id = ?", (role_id,)
        ).fetchone()
    else:
        row = conn.execute("select count(*) from role_life_events").fetchone()
    return int(row[0] or 0)


def _injected_today(conn: sqlite3.Connection, role_id: str, now: datetime) -> int:
    day_start = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    row = conn.execute(
        "select count(*) from role_life_events "
        "where role_id = ? and injected_at is not null and injected_at >= ?",
        (role_id, day_start),
    ).fetchone()
    return int(row[0] or 0)


# ---------- 注入选条 ----------

def pick_for_injection(
    conn: sqlite3.Connection,
    role_id: str,
    *,
    daily_max: int = 2,
    lookback_days: int = 7,
    now: datetime | None = None,
    consume: bool = True,
) -> dict[str, Any] | None:
    """挑一条可注入的生活事件（**永远不含秘密**），选完即消费。

    频率闸：当日已注入数 >= daily_max → None；只看 lookback_days 内的事；
    同一事件注入过就不再给（injected_at is null 是硬条件）。
    """
    now = now or _now_utc()
    if daily_max <= 0:
        return None
    if _injected_today(conn, role_id, now) >= daily_max:
        return None
    since = _iso(now - timedelta(days=max(1, int(lookback_days or 1))))
    row = conn.execute(
        """select event_id, role_id, at, kind, content, mood, why_secret, is_secret,
                  injected_at, shared_at, created_at
             from role_life_events
            where role_id = ? and is_secret = 0 and injected_at is null and at >= ?
            order by at desc, created_at desc limit 1""",
        (role_id, since),
    ).fetchone()
    if row is None:
        return None
    item = _row_to_item(row)
    if consume and not mark_injected(conn, item["event_id"], now=now):
        # 并发抢占失败：这次不给，下一轮再挑
        return None
    item["injected_at"] = _iso(now)
    return item


def mark_injected(
    conn: sqlite3.Connection, event_id: str, *, now: datetime | None = None
) -> bool:
    """标记已注入（幂等：已注入过返回 False）。"""
    now = now or _now_utc()
    cur = conn.execute(
        "update role_life_events set injected_at = ? where event_id = ? and injected_at is null",
        (_iso(now), event_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------- 秘密：被问才说 ----------

def pick_secret(conn: sqlite3.Connection, role_id: str) -> dict[str, Any] | None:
    """取一条**还没说出口**的秘密（仅供「被问起」时调用，禁止装配路径使用）。"""
    row = conn.execute(
        """select event_id, role_id, at, kind, content, mood, why_secret, is_secret,
                  injected_at, shared_at, created_at
             from role_life_events
            where role_id = ? and is_secret = 1 and shared_at is null
            order by at desc, created_at desc limit 1""",
        (role_id,),
    ).fetchone()
    return _row_to_item(row) if row else None


def mark_shared(
    conn: sqlite3.Connection, event_id: str, *, note: str | None = None
) -> bool:
    """标记秘密已说出口（同一个秘密只说一次；已说过返回 False）。"""
    now = _now_utc()
    stamp = _iso(now) if not note else f"{_iso(now)}#{note}"
    cur = conn.execute(
        "update role_life_events set shared_at = ? where event_id = ? and shared_at is null",
        (stamp, event_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """select event_id, role_id, at, kind, content, mood, why_secret, is_secret,
                  injected_at, shared_at, created_at
             from role_life_events where event_id = ?""",
        (event_id,),
    ).fetchone()
    return _row_to_item(row) if row else None


# ---------- 组装提示词 ----------

# 生活块的固定标题（装配侧拼进 system_prompt 末尾；测试按此定位）
LIFE_BLOCK_TITLE = "## 你的生活"


def render_life_block(item: dict[str, Any]) -> str:
    """把一条事件渲染成提示词片段（放 system_prompt 末尾——位置即权重）。

    写法遵循角色卡纪律：正向表述（「合适就提」而不是「绝不要提」），
    并把「细节不用展开」这条兜底写在同一段里——类型级内容的边界靠它守住。

    **刻意不用 ``{{user}}`` 宏**：适配器传 render=true 时 user_name 缺省是「你」，
    「不是你的事」渲染出来是「这是你自己的事，不是你的事」，语义正好拧反。
    """
    content = str(item.get("content") or "").strip()
    mood = str(item.get("mood") or "").strip()
    mood_line = f"你当时的情绪是{mood}。" if mood else ""
    return (
        f"{LIFE_BLOCK_TITLE}\n"
        f"你自己身上有件事：{content}。{mood_line}\n"
        "聊天里话题搭得上，就顺口带一句、给个态度；搭不上就搁在心里，不提也行。\n"
        "细节不用展开，说个大概就够了；被追问细节，就说记不太清了。"
    )
