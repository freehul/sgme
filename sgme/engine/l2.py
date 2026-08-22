"""engine/l2.py：L2 场景聚合（§架构约束 7 / §9.1）。

L2 输入 = 纯记忆（L1.5 裁决后标签化记忆，已含 memory_id），分批按 batch_budget。
L2 三动作（优先级从高到低）：
1. update：新记忆与某 active 场景主题相关 → 整合进该场景正文（heat+1，旧内容归档 scene_versions）
2. merge：多个 active 场景因新记忆主题重合 → 合并为新场景（heat=sum+1，旧场景 archived）
3. create：新记忆主题无对应场景 → 新建场景（heat=1）

max_scenes 上限 + 红/橙/黄三级预警（软策略，仅 anomaly_warn 不阻塞）。
软删除归档（status=archived），旧内容进 scene_versions 保留可溯源。

LLM 输出 schema（每批一个 JSON 数组）：
  {"action": "update|merge|create",
   "target_scene_id": "uuid（update=被更新场景id；merge/create=新场景uuid）",
   "merged_content": "该场景完整新正文",
   "reason": "理由",
   "merged_from": ["被合并旧场景id", ...]  # 仅 merge 必须
   "memory_id": "触发该动作的 memory_id"  # 可选，缺失则关联本批全部 memory
   }
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sgme import config
from sgme.llm import chain as llm_chain
from sgme.llm import provider as llm_provider
from sgme.prompts import BucketCtx, PromptStore
from sgme.data import memory_dao, scene_dao

logger = logging.getLogger("sgme.engine.l2")

# 单条记忆 token 估算（用于 batch_budget 转条数，沿用 l15.py 经验值）
AVG_TOKENS_PER_MEMORY = 30

# existing_scenes 摘要长度（content 前 N 字）
SCENE_SUMMARY_LEN = 200

# 拉 active 场景上限（避免 prompt 过长）
EXISTING_SCENES_LIMIT = 50

VALID_ACTIONS = {"update", "merge", "create"}


class L2Error(Exception):
    """L2 场景聚合失败（JSON 解析/校验失败）。"""


@dataclass
class L2Result:
    """L2 聚合结果。"""
    created: list[str] = field(default_factory=list)    # 新建场景 scene_id
    updated: list[str] = field(default_factory=list)    # 被更新场景 scene_id
    merged: list[str] = field(default_factory=list)     # 合并产生的新场景 scene_id
    archived: list[str] = field(default_factory=list)   # 被归档的旧 scene_id
    anomaly_warn: bool = False
    error: str | None = None
    prompt_meta: dict | None = None                     # #33：本次 L2 提示词版本元信息


# ---------- prompt 渲染 ----------

def render_l2(memories_batch: list[dict], existing_scenes: list[dict], cfg: dict,
              ctx: BucketCtx | None = None) -> str:
    """渲染 L2 场景聚合 prompt（模板经 PromptStore 读取，支持 A/B 与钉版）。

    - {{new_memories}}: 本批新记忆 JSON 列表（含 memory_id + content + dimension_ids）
    - {{existing_scenes}}: active 场景列表（scene_id + title + content 前 200 字摘要）
    - {{max_scenes}}: 配置的 active 场景数软上限
    """
    template = PromptStore().get("l2_scene", ctx).text
    return _render_l2_text(template, memories_batch, existing_scenes, cfg)


def _render_l2_text(template: str, memories_batch: list[dict], existing_scenes: list[dict],
                    cfg: dict) -> str:
    """渲染已读出的模板文本（{{new_memories}} + {{existing_scenes}} + {{max_scenes}}）。"""
    nm_text = _format_new_memories(memories_batch)
    scenes_text = _format_existing_scenes(existing_scenes)
    max_scenes = _get_max_scenes(cfg)
    return (
        template
        .replace("{{new_memories}}", nm_text)
        .replace("{{existing_scenes}}", scenes_text)
        .replace("{{max_scenes}}", str(max_scenes))
    )


def _format_new_memories(memories_batch: list[dict]) -> str:
    """格式化新记忆为 JSON 字符串（LLM 易解析）。"""
    items = []
    for m in memories_batch:
        items.append({
            "memory_id": m.get("memory_id", ""),
            "content": m.get("content", ""),
            "dimension_ids": m.get("dimension_ids", m.get("dimensions", [])),
            "memory_type": m.get("memory_type", "persona"),
        })
    return json.dumps(items, ensure_ascii=False, indent=2)


def _format_existing_scenes(existing_scenes: list[dict]) -> str:
    """格式化 active 场景：scene_id + title + content 前 200 字摘要。"""
    if not existing_scenes:
        return "(暂无 active 场景)"
    lines = []
    for s in existing_scenes:
        sid = s.get("scene_id", "")
        title = s.get("title", "")
        content = s.get("content", "") or ""
        summary = content[:SCENE_SUMMARY_LEN]
        if len(content) > SCENE_SUMMARY_LEN:
            summary += "..."
        lines.append(f"[scene_id={sid}] title={title}\n  摘要: {summary}")
    return "\n".join(lines)


def _get_max_scenes(cfg: dict) -> int:
    """从 cfg['l2'] 取 max_scenes，缺失用默认 200。"""
    l2_cfg = cfg.get("l2") or {}
    return int(l2_cfg.get("max_scenes", 200))


# ---------- JSON 解析 ----------

def _parse_json_lenient(text: str) -> list:
    """容错 JSON 解析：修复 qwen 关思考后的常见输出缺陷。

    1. 字符串值内部裸控制字符（x00-x1f，含未转义换行/制表）→ unicode 转义
    2. 无效 X 转义（如 x、q）→ 双反斜杠（保留反斜杠字面量）
    失败仍抛 L2Error。
    """
    import re as _re

    def _fix_control(m: _re.Match) -> str:
        ch = m.group(0)
        if ch == "\n":
            return "\\n"
        if ch == "\t":
            return "\\t"
        if ch == "\r":
            return "\\r"
        return f"\\\\u{ord(ch):04x}"

    def _fix_escape(m: _re.Match) -> str:
        return "\\\\" + m.group(1)

    # 1. 裸控制字符（含未转义换行/制表/回车）→ 转义序列
    text = _re.sub(r"[\x00-\x1f\x7f]", _fix_control, text)
    # 2. 无效转义：反斜杠 + 非(合法转义字符) → 双反斜杠
    text = _re.sub(r"\\([^\\\"\/bfnrtu])", _fix_escape, text)
    # 3. 结构修复：对象/数组间缺逗号（模型常见：} {、] [、}\n{）
    #    只在 }/] 后没有逗号时补逗号；已有逗号（正常结构）不误伤
    def _fix_missing_comma(m: _re.Match) -> str:
        g1 = m.group(1)
        stripped = g1.rstrip()
        if stripped.endswith(","):
            return g1 + m.group(2)
        return stripped + "," + g1[len(stripped):] + m.group(2)

    text = _re.sub(r"([}\]][\s\n]*)([{[])", _fix_missing_comma, text)
    # 4. 尾逗号（数组/对象最后一个元素后多逗号）
    text = _re.sub(r",\s*([}\]])", r"\1", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise L2Error(f"L2 JSON 解析失败: {e}") from e
    if not isinstance(data, list):
        raise L2Error(f"L2 期望 JSON 数组，得到 {type(data).__name__}")
    return data


def parse_l2_output(text: str) -> list[dict]:
    """解析 L2 输出为动作列表（对外入口，兼容旧名）。"""
    return _parse_l2_output(text)


def _parse_l2_output(text: str) -> list[dict]:
    """解析 L2 输出为动作列表。

    - 支持 ```json 代码块包裹
    - 严格 JSON 数组，每条 {action, target_scene_id, merged_content, reason, ...}
    - action 必须 ∈ {update, merge, create}，否则抛 L2Error
    - 坏 JSON 抛 L2Error（不重试）
    """
    text = text.strip()
    # 去除 ```json 包裹
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 容忍前后多余文字：尝试找第一个 [ 到最后一个 ]
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise L2Error(f"L2 JSON 解析失败: {text[:200]}")
        text = text[start:end + 1]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 清理层（2026-08-06）：qwen 关思考后模型偶发输出裸控制字符 /
            # 无效转义（Invalid control character / Invalid \escape）。
            # 策略：字符串值内部的裸控制字符转义为 \uXXXX；无效 \X 转义修复为 \\X。
            data = _parse_json_lenient(text)
    if not isinstance(data, list):
        raise L2Error(f"L2 期望 JSON 数组，得到 {type(data).__name__}")
    result = []
    for item in data:
        if not isinstance(item, dict):
            raise L2Error(f"L2 数组元素必须是对象，得到 {type(item).__name__}")
        action = item.get("action")
        if action not in VALID_ACTIONS:
            raise L2Error(f"L2 非法 action={action!r}，合法集合={VALID_ACTIONS}")
        target = item.get("target_scene_id")
        # 宽容（2026-08-06）：模型偶发省略 target_scene_id（尤其 merge/create），
        # 用占位符兜底——系统对 create 会重新生成 uuid，merge 的 target 仅作区分。
        if not target or not isinstance(target, str):
            target = f"placeholder-{action}"
        merged_content = item.get("merged_content")
        if not merged_content or not isinstance(merged_content, str):
            raise L2Error(f"L2 action={action} 缺 merged_content")
        result.append({
            "action": action,
            "target_scene_id": target,
            "merged_content": merged_content,
            "reason": item.get("reason", ""),
            "merged_from": [str(x) for x in item.get("merged_from", [])],
            "memory_id": item.get("memory_id"),
            "memory_ids": [str(x) for x in item.get("memory_ids", [])],
        })
    return result


# ---------- 分批 ----------

def _batch_memories(memories: list[dict], budget: int) -> list[list[dict]]:
    """按 batch_budget 分批记忆（每批最多 budget // AVG_TOKENS_PER_MEMORY 条）。"""
    if not memories:
        return []
    per_batch = max(1, budget // AVG_TOKENS_PER_MEMORY)
    batches = []
    for i in range(0, len(memories), per_batch):
        batches.append(memories[i:i + per_batch])
    return batches


def _prescreen_scenes(
    mem_conn: sqlite3.Connection,
    memories_batch: list[dict],
    cfg: dict,
    prescreen: dict,
) -> list[dict] | None:
    """场景级向量预筛（T-97，2026-08-22）：向量 Top-K ∪ 热度 Top-N（heat DESC）。

    背景：L2 场景数超 max_scenes 后，固定 EXISTING_SCENES_LIMIT=50 个摘要
    （updated_at DESC）覆盖不到与新记忆语义相关的场景，LLM 看不到就 merge
    不了，场景数只增不减（active 276 > max 200 红警）。本函数让 LLM 只看到
    「本批记忆语义相似的场景 + 高热度兜底场景」，提升 update/merge 命中率。

    对齐 L1.5 prescreen（l15._build_prescreened_candidates）模式：
    - 向量 Top-K：本批记忆拼接文本 embed → scene_vector_search（sqlite-vec/numpy 双路径）
    - 热度 Top-N：active 场景按 heat DESC 取前 N（无向量覆盖的高热度场景不丢）
    - 并集按 scene_id 去重（向量候选在前，热度补充）

    返回语义：
    - list[dict]：预筛成功，候选 = 向量 Top-K ∪ 热度 Top-N
    - None：embed 不可达 / 检索异常 → 调用方回退固定 EXISTING_SCENES_LIMIT
      摘要（fallback=full_recall，原行为零回归）
    """
    from sgme.data.search import vector as vector_mod

    vector_top_k = int(prescreen.get("vector_top_k", 30))
    heat_top_n = int(prescreen.get("heat_top_n", 20))
    fallback = prescreen.get("fallback", "full_recall")

    # 1. 向量 Top-K：本批记忆拼接文本 embed → 场景向量检索
    batch_text = "\n".join((m.get("content") or "") for m in memories_batch)
    if not batch_text.strip():
        batch_text = " ".join(str(m.get("memory_id") or "") for m in memories_batch)
    try:
        vec = vector_mod.embed(batch_text, cfg)
        if vec is None:
            logger.warning(
                "L2 场景预筛降级：embedding 端点不可达，回退固定摘要（fallback=%s）", fallback)
            return None
        vec_cands = vector_mod.scene_vector_search(mem_conn, vec, limit=vector_top_k)
    except Exception as e:
        logger.warning("L2 场景预筛异常，回退固定摘要（fallback=%s）: %s", fallback, e)
        return None

    # 2. 热度 Top-N（heat DESC）兜底
    heat_cands = scene_dao.list_active_scenes(mem_conn)
    heat_cands.sort(key=lambda s: int(s.get("heat", 1) or 1), reverse=True)
    heat_cands = heat_cands[:heat_top_n]

    # 3. 并集去重（向量在前，热度补充）
    merged: list[dict] = []
    seen: set[str] = set()
    for c in vec_cands:
        if c["scene_id"] not in seen:
            seen.add(c["scene_id"])
            merged.append(c)
    for c in heat_cands:
        if c["scene_id"] not in seen:
            seen.add(c["scene_id"])
            merged.append(c)
    logger.info("L2 场景预筛：向量 %d ∪ 热度 %d → 候选 %d 个场景",
                len(vec_cands), len(heat_cands), len(merged))
    return merged


def _resolve_budget(cfg: dict) -> int:
    """从 cfg['llm'] 取 refinement 链首批 provider 的 batch_budget。"""
    llm_cfg = cfg.get("llm", {})
    try:
        first_node = llm_cfg["chains"]["refinement"][0]
        return llm_chain.batch_budget(first_node, llm_cfg.get("rules", {}))
    except Exception:
        return 55360  # 默认 64K 链预算兜底


# ---------- 落库 ----------

def _refresh_scene_vector(
    mem_conn: sqlite3.Connection,
    scene_id: str,
    content: str,
    cfg: dict | None,
) -> None:
    """场景正文变更后刷新向量（T-97 补完，2026-08-22）。

    盲区：upsert_scene_vector 此前无调用方，8-17 一次性回填后新建/合并场景
    全部无向量 → L2 场景级向量预筛对它们不可见。create/merge/update 三动作
    落库后均刷新，失败不阻塞（embed 不可达仅告警，热度 Top-N 兜底）。
    """
    if not cfg:
        return
    try:
        from sgme.data.search import vector as vector_mod
        ok = vector_mod.upsert_scene_vector(mem_conn, scene_id, content, cfg)
        if not ok:
            logger.warning("场景向量刷新失败（embed 不可达），预筛对该场景盲区: %s", scene_id)
    except Exception as e:
        logger.warning("场景向量刷新异常（不阻塞）: %s: %s", scene_id, e)


def _extract_title(merged_content: str, fallback: str) -> str:
    """从 merged_content 第一行 `# xxx` 提取标题，否则用 fallback。"""
    for line in merged_content.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip() or fallback
    return fallback


def _apply_update(
    mem_conn: sqlite3.Connection,
    action: dict,
    batch_memory_ids: list[str],
    result: L2Result,
    cfg: dict | None = None,
) -> None:
    """update：归档旧内容到 scene_versions + 更新正文 heat+1 + 关联记忆。"""
    sid = action["target_scene_id"]
    old = scene_dao.get_scene(mem_conn, sid)
    if not old or old.get("status") != "active":
        logger.warning("L2 update 跳过：scene_id=%s 不存在或非 active", sid)
        return
    # 旧内容归档
    scene_dao.insert_scene_version(
        mem_conn, version_id=str(uuid.uuid4()),
        scene_id=sid, content=old["content"], reason="update",
    )
    # 更新正文（heat+1）
    scene_dao.update_scene_content(
        mem_conn, scene_id=sid, content=action["merged_content"],
        heat_increment=1,
    )
    # 关联本批 memory
    for mid in batch_memory_ids:
        scene_dao.add_memory_link(mem_conn, scene_id=sid, memory_id=mid)
    # T-97 补完：正文变更 → 刷新场景向量（保持预筛可见）
    _refresh_scene_vector(mem_conn, sid, action["merged_content"], cfg)
    result.updated.append(sid)


def _apply_merge(
    mem_conn: sqlite3.Connection,
    action: dict,
    batch_memory_ids: list[str],
    result: L2Result,
    cfg: dict | None = None,
) -> None:
    """merge：归档所有 merged_from 旧场景 + 新建合并场景 heat=sum+1 + 关联记忆。"""
    merged_from = action.get("merged_from", [])
    if not merged_from:
        # 无归档目标 → 退化为 create
        logger.warning("L2 merge 缺 merged_from，退化为 create: target=%s", action["target_scene_id"])
        _apply_create(mem_conn, action, batch_memory_ids, result)
        return

    new_id = action["target_scene_id"]
    content = action["merged_content"]
    title = _extract_title(content, fallback=f"merged_{new_id[:8]}")

    # 计算旧场景 heat 之和
    sum_heat = 0
    for old_sid in merged_from:
        old = scene_dao.get_scene(mem_conn, old_sid)
        if old and old.get("status") == "active":
            sum_heat += int(old.get("heat", 1))
            # 旧内容归档 + 软删除
            scene_dao.insert_scene_version(
                mem_conn, version_id=str(uuid.uuid4()),
                scene_id=old_sid, content=old["content"], reason="merge",
            )
            scene_dao.update_scene_status(mem_conn, scene_id=old_sid, status="archived")
            result.archived.append(old_sid)

    # 新建合并场景（id 由系统生成，不信 LLM 编造的假 uuid——模型可能重复/覆盖）
    new_id = str(uuid.uuid4())
    title = _extract_title(content, fallback=f"merged_{new_id[:8]}")
    scene_dao.insert_scene(
        mem_conn, scene_id=new_id, title=title, content=content,
    )
    # 修正 heat 为 sum+1（insert_scene 默认 heat=1）
    scene_dao.update_scene_content(
        mem_conn, scene_id=new_id, content=content,
        heat_increment=sum_heat,  # heat 从 1 涨到 1+sum_heat = sum+1
    )
    # 关联本批 memory
    for mid in batch_memory_ids:
        scene_dao.add_memory_link(mem_conn, scene_id=new_id, memory_id=mid)
    # T-97 补完：合并新场景 → 生成向量（保持预筛可见）
    _refresh_scene_vector(mem_conn, new_id, content, cfg)
    result.merged.append(new_id)


def _apply_create(
    mem_conn: sqlite3.Connection,
    action: dict,
    batch_memory_ids: list[str],
    result: L2Result,
    cfg: dict | None = None,
) -> None:
    """create：新建场景 heat=1 + 关联记忆。"""
    # id 由系统生成，不信 LLM 编造的假 uuid（模型会输出递增序列如 a1b2c3d4，重复则覆盖）
    new_id = str(uuid.uuid4())
    content = action["merged_content"]
    title = _extract_title(content, fallback=f"scene_{new_id[:8]}")
    scene_dao.insert_scene(
        mem_conn, scene_id=new_id, title=title, content=content,
    )
    for mid in batch_memory_ids:
        scene_dao.add_memory_link(mem_conn, scene_id=new_id, memory_id=mid)
    # T-97 补完：新建场景 → 生成向量（保持预筛可见）
    _refresh_scene_vector(mem_conn, new_id, content, cfg)
    result.created.append(new_id)


# ---------- 阈值预警 ----------

def check_scene_threshold(mem_conn: sqlite3.Connection, cfg: dict) -> tuple[str | None, int]:
    """检查 active 场景数是否触及红/橙/黄预警阈值。

    返回 (level, count)：
    - level ∈ {None, 'yellow', 'orange', 'red'}（取最高命中级别）
    - count = 当前 active 场景总数
    软策略：仅返回级别供调用方产 anomaly_warn，不阻塞。
    """
    count = scene_dao.list_scenes_over_threshold(mem_conn, 0)
    l2_cfg = cfg.get("l2") or {}
    thresholds = l2_cfg.get("warn_thresholds", {})
    red = int(thresholds.get("red", 200))
    orange = int(thresholds.get("orange", 180))
    yellow = int(thresholds.get("yellow", 150))
    if count >= red:
        return "red", count
    if count >= orange:
        return "orange", count
    if count >= yellow:
        return "yellow", count
    return None, count


# ---------- 完整 L2 聚合 ----------

def _backfill_ttl(ttl_days: int | None, dimension_ids: list[str], dimensions: list[dict]) -> int | None:
    """TTL 字段按维度默认回填：ttl_days=None 时取任一动态维度默认 ttl。"""
    if ttl_days is not None:
        return ttl_days
    dim_map = {d["id"]: d for d in dimensions}
    for dim_id in dimension_ids:
        d = dim_map.get(dim_id)
        if d and d.get("ttl_days"):
            return d["ttl_days"]
    return None


def _ensure_persisted(memories: list[dict], mem_conn: sqlite3.Connection, cfg: dict) -> list[dict]:
    """保证 memories 已落库：缺 memory_id 的条目调 memory_dao.insert_memory 落库。

    设计折中：refine.py 当前不调 L1.5 冲突裁决，L1 输出未落库。
    L2 需要 memory_id 做 scene_memories 关联，故在此兜底落库。
    已含 memory_id 的条目（如 L1.5 已落库场景）直接复用。
    """
    dimensions = cfg.get("dimensions", [])
    for m in memories:
        if m.get("memory_id"):
            continue
        dim_ids = m.get("dimension_ids", m.get("dimensions", []))
        ttl = _backfill_ttl(m.get("ttl_days"), dim_ids, dimensions)
        sources = []
        if m.get("file_id"):
            sources.append((m["file_id"], "session"))
        mid = memory_dao.insert_memory(
            mem_conn,
            content=m["content"],
            memory_type=m.get("memory_type", "persona"),
            priority=m.get("priority", 50),
            time_velocity=m.get("time_velocity", "static"),
            ttl_days=ttl,
            dimension_ids=dim_ids,
            sources=sources or None,
            agent_tag=m.get("agent_tag"),
            created_at=m.get("created_at"),
            updated_at=m.get("updated_at"),
            occurred_at=m.get("occurred_at"),
        )
        m["memory_id"] = mid
    return memories


def aggregate(
    memories: list[dict],
    mem_conn: sqlite3.Connection,
    cfg: dict,
    client=None,
    bucket_ctx: BucketCtx | None = None,
) -> L2Result:
    """L2 场景聚合主入口。

    - memories: L1.5 裁决后纯记忆列表；缺 memory_id 时内部兜底落库
    - 分批按 batch_budget，每批独立 LLM 调用 + 记录一条 refine_run（action 分布）
    - bucket_ctx：A/B 分流上下文（默认 file_id）
    - LLM 全挂 → error + anomaly_warn
    - LLM 输出坏 JSON → error + anomaly_warn（不重试，整批跳过）
    - 单批落库异常不阻塞其他批
    """
    from sgme.data.refine_dao import RefineRunRecorder

    result = L2Result()
    if not memories:
        return result

    bucket_key = bucket_ctx.bucket_key if (bucket_ctx and bucket_ctx.bucket_key) else "unknown"

    # 兜底落库（保证 memory_id 存在，供 scene_memories 关联）
    try:
        _ensure_persisted(memories, mem_conn, cfg)
    except Exception as e:
        result.error = f"L2 落库记忆失败: {e}"
        result.anomaly_warn = True
        return result

    budget = _resolve_budget(cfg)
    batches = _batch_memories(memories, budget)
    # T-97 场景级向量预筛（2026-08-22）：l2.prescreen.enabled=true 时
    # existing_scenes = 向量 Top-K ∪ 热度 Top-N；未配置/embed 不可达 → 固定 50 摘要（零回归）
    prescreen_cfg = ((cfg.get("l2") or {}).get("prescreen") or {}) or None
    prescreen_enabled = bool(prescreen_cfg and prescreen_cfg.get("enabled", False))
    existing_scenes = scene_dao.list_active_scenes(mem_conn, limit=EXISTING_SCENES_LIMIT)

    for batch in batches:
        if prescreen_enabled and prescreen_cfg:
            ps = _prescreen_scenes(mem_conn, batch, cfg, prescreen_cfg)
            if ps is not None:
                existing_scenes = ps
        batch_memory_ids = [m.get("memory_id") for m in batch if m.get("memory_id")]
        pv = PromptStore().get("l2_scene", bucket_ctx)
        prompt = _render_l2_text(pv.text, batch, existing_scenes, cfg)
        try:
            text, provider_name, usage = llm_chain.call_with_fallback(
                cfg["llm"], prompt, chain_name="refinement", client=client,
            )
        except llm_provider.LLMUnavailable as e:
            result.error = str(e)
            result.anomaly_warn = True
            run_id = RefineRunRecorder.start(
                mem_conn, file_id=bucket_key, stage="l2_scene",
                version=pv.version, variant=pv.variant,
                provider="unavailable", bucket_key=bucket_key,
            )
            RefineRunRecorder.finish(
                mem_conn, run_id, memories_count=0, action_counts={},
                status="error", error=str(e),
            )
            return result

        try:
            actions = parse_l2_output(text)
            run_status = "ok"
            run_error = None
        except L2Error as e:
            # 2026-08-18（T-53 免费托底）：zhipu 免费档偶发输出截断 → JSON 解析失败。
            # 同模型重试 1 次（提示只输出纯 JSON 数组）——截断是偶发，重试大概率拿完整输出；
            # 2 次仍失败才整批 error（原逻辑不重试直接跳过）。
            logger.warning("L2 输出解析失败，重试 1 次: %s", e)
            try:
                retry_prompt = prompt + "\n\n# 注意\n上次输出无法解析为 JSON 数组，请只输出纯 JSON 数组，无其他文字。"
                text, provider_name, usage = llm_chain.call_with_fallback(
                    cfg["llm"], retry_prompt, chain_name="refinement", client=client,
                )
                actions = parse_l2_output(text)
                run_status = "ok"
                run_error = None
                logger.info("L2 解析重试成功 provider=%s", provider_name)
            except (L2Error, llm_provider.LLMUnavailable) as e2:
                # 坏 JSON：整批标记 error + anomaly_warn
                result.error = str(e2)
                result.anomaly_warn = True
                logger.warning("L2 输出解析重试仍失败，整批跳过: %s", e2)
                actions = []
                run_status = "error"
                run_error = str(e2)

        # 记录 refine_run（每记忆批一条；L2 memories_count 记动作数 + action 分布）
        action_counts = dict(Counter(a.get("action", "") for a in actions))
        run_id = RefineRunRecorder.start(
            mem_conn, file_id=bucket_key, stage="l2_scene",
            version=pv.version, variant=pv.variant,
            provider=provider_name, bucket_key=bucket_key,
        )
        RefineRunRecorder.finish(
            mem_conn, run_id, memories_count=len(actions),
            action_counts=action_counts, status=run_status, error=run_error,
            usage=usage,
        )
        logger.info("L2 批完成: version=%s variant=%s provider=%s actions=%s",
                    pv.version, pv.variant, provider_name, action_counts)
        if result.prompt_meta is None:
            result.prompt_meta = {"stage": "l2_scene", "version": pv.version, "variant": pv.variant}

        # 落库
        for action in actions:
            try:
                # 优先用动作指定的 memory_ids（精确关联）；缺失回退本批全部
                mem_ids: list[str] = [x for x in (action.get("memory_ids") or batch_memory_ids) if x]
                if action["action"] == "update":
                    _apply_update(mem_conn, action, mem_ids, result, cfg)
                elif action["action"] == "merge":
                    _apply_merge(mem_conn, action, mem_ids, result, cfg)
                elif action["action"] == "create":
                    _apply_create(mem_conn, action, mem_ids, result, cfg)
            except Exception as e:
                # 单条 action 落库异常不阻塞其他
                logger.warning("L2 动作落库异常 action=%s: %s", action.get("action"), e)

    logger.info(
        "L2 聚合完成: created=%d updated=%d merged=%d archived=%d warn=%s error=%s",
        len(result.created), len(result.updated), len(result.merged),
        len(result.archived), result.anomaly_warn, result.error,
    )
    return result
