"""sgme/data/skill_gate.py：uses 依赖门禁（T-402）。

设计 §5.2「uses 门禁」落地：
1. **悬空引用**：uses 的每个目标必须在 skills 表存在且 status != 'tombstone'；
   否则拒绝写入。随包分发的 ``skill-registry-protocol`` 是内置技能，不在
   skills 表但恒可用，视为合法节点（否则 ``sgme`` 技能本身无法同步）。
2. **依赖环**：把本次写入的新边并入现有依赖图后 DFS；有环即拒绝，错误信息
   带完整环路径（``A→B→A``）。
3. **自引用**：uses 包含自身 → 拒绝。
4. **强/弱依赖**：skill_uses.strength 标记（strong/weak，默认 weak）。
   删除/墓碑目标时，strong 入向引用阻断，weak 只告警。

本模块只做**只读**校验：不写 skills / skill_uses，存量 15 条数据不受触碰。
真正的写入仍由 ``skills_dao.replace_uses`` 完成，写入口（operations.skills.
sync_index）先调本模块校验，通过后才落库——不得先写后校验。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

STATUS_ACTIVE = "active"
STATUS_TOMBSTONE = "tombstone"

STRENGTH_STRONG = "strong"
STRENGTH_WEAK = "weak"
VALID_STRENGTHS = (STRENGTH_STRONG, STRENGTH_WEAK)

# 随 sgme 包分发、cold_start 恒注入的内置技能：没有 skills 表行也永远可用。
BUILTIN_SKILLS = frozenset({"skill-registry-protocol"})


def normalize_strength(value: Any) -> str:
    """依赖强度归一：仅 'strong' 为强依赖，其余（含缺失/非法值）按 weak。"""
    return STRENGTH_STRONG if str(value or "").strip().lower() == STRENGTH_STRONG else STRENGTH_WEAK


def _clean_uses(uses: Iterable[Any]) -> list[str]:
    """去空、去重、保序（保持 frontmatter 声明顺序，错误信息可读）。"""
    out: list[str] = []
    for u in uses or []:
        s = str(u or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _load_statuses(conn: sqlite3.Connection) -> dict[str, str]:
    """{技能名: status}；缺失 status 列按 active（兼容未迁移的只读连接）。"""
    try:
        rows = conn.execute("SELECT name, status FROM skills").fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute("SELECT name FROM skills").fetchall()
        return {r["name"]: STATUS_ACTIVE for r in rows}
    return {r["name"]: (r["status"] or STATUS_ACTIVE) for r in rows}


def _load_graph(conn: sqlite3.Connection, exclude_src: str | None = None) -> dict[str, list[str]]:
    """现有依赖图（src → [dst]）；exclude_src 用于全量替换语义（旧出边将被覆盖）。"""
    graph: dict[str, list[str]] = {}
    for r in conn.execute("SELECT src, dst FROM skill_uses").fetchall():
        if exclude_src is not None and r["src"] == exclude_src:
            continue
        graph.setdefault(r["src"], []).append(r["dst"])
    return graph


def _find_cycle(start: str, graph: dict[str, list[str]]) -> list[str] | None:
    """从 start 起 DFS 找环；返回环路径（首尾同名，如 [a,b,a]），无环返回 None。

    迭代式实现（技能依赖链可能很长，避免递归深度限制）。
    """
    path: list[str] = [start]
    on_path: set[str] = {start}
    done: set[str] = set()
    stack: list[tuple[str, Any]] = [(start, iter(graph.get(start, [])))]
    while stack:
        node, it = stack[-1]
        try:
            nxt = next(it)
        except StopIteration:
            stack.pop()
            path.pop()
            on_path.discard(node)
            done.add(node)
            continue
        if nxt in on_path:
            i = path.index(nxt)
            return path[i:] + [nxt]
        if nxt in done:
            continue
        stack.append((nxt, iter(graph.get(nxt, []))))
        path.append(nxt)
        on_path.add(nxt)
    return None


def _cycle_errors(graph: dict[str, list[str]], names: Iterable[str] | None = None) -> list[str]:
    """对图中每个节点找环并按环去重（a→b→a 与 b→a→b 只报一条）。"""
    errors: list[str] = []
    seen_cycles: set[frozenset[str]] = set()
    for name in sorted(names if names is not None else graph):
        cycle = _find_cycle(name, graph)
        if not cycle:
            continue
        key = frozenset(cycle)
        if key in seen_cycles:
            continue
        seen_cycles.add(key)
        errors.append(f"依赖环: {'→'.join(cycle)}（技能 {name} 的 uses 引入）")
    return errors


def validate_uses(
    conn: sqlite3.Connection,
    name: str,
    uses: Iterable[str],
    *,
    strengths: dict[str, str] | None = None,
) -> list[str]:
    """校验某技能的 uses 出向依赖；返回错误清单（空 = 通过）。

    校验项：自引用 / 悬空引用（目标不存在或已墓碑）/ 依赖环。
    strengths 当前不改变单技能校验结果（悬空一律拒绝），保留形参供调用方
    传递强弱标记，避免未来签名再次变更。

    Args:
        conn: skills.db 连接。
        name: 本次写入的技能名。
        uses: 拟写入的依赖目标名列表。
        strengths: 可选 {dst: 'strong'|'weak'}；本函数不消费，语义见 check_removal。
    """
    del strengths  # 单技能校验不区分强弱：目标不可达一律拒绝
    uses = _clean_uses(uses)
    errors: list[str] = []

    for dst in uses:
        if dst == name:
            errors.append(f"自引用: 技能 {name} 的 uses 不能依赖自身")

    statuses = _load_statuses(conn)
    for dst in uses:
        if dst == name:
            continue
        if dst in BUILTIN_SKILLS:
            continue
        if dst not in statuses:
            errors.append(f"悬空引用: {dst}（技能 {name} 依赖的目标不存在）")
        elif statuses[dst] == STATUS_TOMBSTONE:
            errors.append(f"悬空引用: {dst}（技能 {name} 依赖的目标已墓碑）")

    # 全量替换语义：name 的旧出边不参与图（本次写入会覆盖）
    graph = _load_graph(conn, exclude_src=name)
    graph[name] = [d for d in uses if d != name]
    errors.extend(_cycle_errors(graph, names=[name]))
    return errors


def check_removal(conn: sqlite3.Connection, name: str) -> tuple[list[str], list[str]]:
    """删除/墓碑前的入向引用检查。

    Returns:
        (blocking_errors, warnings)
        - strong 入向引用 → blocking_errors（禁止删除）
        - weak 入向引用   → warnings（允许删除，但提示引用将悬空）
    """
    errors: list[str] = []
    warnings: list[str] = []
    rows = conn.execute(
        "SELECT src, strength FROM skill_uses WHERE dst=? ORDER BY src", (name,)
    ).fetchall()
    for r in rows:
        src = r["src"]
        strength = normalize_strength(r["strength"])
        if strength == STRENGTH_STRONG:
            errors.append(
                f"强依赖阻断: 技能 {src} 强依赖 {name}，禁止删除/墓碑该目标"
            )
        else:
            warnings.append(
                f"弱依赖警告: 技能 {src} 弱依赖 {name}，删除后其引用将悬空"
            )
    return errors, warnings


def validate_records_uses(
    conn: sqlite3.Connection, records: Iterable[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """批量校验一次 sync 的全部记录（写入口用，校验通过前不得落库）。

    与单技能校验的区别：本批记录同时包含新增技能，允许「A 依赖同批新写入的 B」；
    库内存在但本批源目录已不存在的目标 = 本次将被删除：
    - 被 strong 引用 → 阻断删除（error）
    - 被 weak 引用   → 仅告警，删除继续（warning）

    Args:
        conn: skills.db 连接。
        records: [{"name", "uses", "uses_strength", ...}]（operations._record_to_dict 形状）。

    Returns:
        (errors, warnings)：errors 非空时写入口必须拒绝整批写入。
    """
    recs = list(records)
    source_names = {r["name"] for r in recs}
    statuses = _load_statuses(conn)
    errors: list[str] = []
    warnings: list[str] = []
    graph: dict[str, list[str]] = {}

    for rec in recs:
        name = rec["name"]
        uses = _clean_uses(rec.get("uses") or [])
        strengths = rec.get("uses_strength") or {}
        # 环检测只保留本批真实存在的节点：将被删除/本就不存在的目标边
        # 已在上面的目标检查里报错或告警，不能参与环判定（否则会误报）。
        graph[name] = [
            d for d in uses
            if d != name and (d in source_names or d in BUILTIN_SKILLS)
        ]

        for dst in uses:
            if dst == name:
                errors.append(f"自引用: 技能 {name} 的 uses 不能依赖自身")
                continue
            if dst in BUILTIN_SKILLS:
                continue
            if dst in source_names:
                # 同批写入的技能：若库内已墓碑，说明源端仍带着墓碑记录，拒绝复活依赖
                if statuses.get(dst) == STATUS_TOMBSTONE:
                    errors.append(f"悬空引用: {dst}（技能 {name} 依赖的目标已墓碑）")
                continue
            if dst in statuses:
                # 库内存在但本批源目录已没有 → 本次同步会删除它
                if normalize_strength(strengths.get(dst)) == STRENGTH_STRONG:
                    errors.append(
                        f"强依赖阻断: 技能 {name} 强依赖 {dst}，但目标将被删除"
                    )
                else:
                    warnings.append(
                        f"弱依赖警告: 技能 {name} 弱依赖 {dst}，目标将被删除，引用将悬空"
                    )
                continue
            errors.append(f"悬空引用: {dst}（技能 {name} 依赖的目标不存在）")

    errors.extend(_cycle_errors(graph))
    return errors, warnings
