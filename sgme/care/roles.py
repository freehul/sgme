# -*- coding: utf-8 -*-
"""sgme/care/roles.py：角色层数据结构（T-35，ST-25 地基）。

角色 = 沟通外皮（皮）；记忆池 = 芯（用户画像保持模板查询零物化，架构铁律不变）。
角色文件采用 **Character Card V2 兼容子集**（spec/data 双层结构，2026-08-13
调研定案）：可移植、生态兼容；``extensions.sgme_care`` 命名空间挂关怀策略
（问候模板/触发规则/频率档位），供 T-38 消费方读取。

角色 persona 是**唯一物化例外**（2026-08-13 用户拍板）：LLM 生成
（Persona Architect 四层深度扫描方法论），存 ``data/personas/<role_id>.md``。

铁律：
- 原件永不删：delete_role 只移入 ``.archive/``，不物理删除
- 角色卡 = 项目内文件（roles/ 目录随 git 管理，可移植交付）
- persona = 运行数据（data/personas/，不入 git）
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sgme.care.roles")

# ---------- CC V2 兼容子集字段定义 ----------

# 必填字段（CC V2 最低要求：name + description）
REQUIRED_FIELDS = ("name", "description")
# 可选标准字段（CC V2 spec 子集；character_book 为 V2 新增，extensions 任意命名空间）
OPTIONAL_FIELDS = (
    "personality", "scenario", "first_mes", "mes_example",
    "system_prompt", "post_history_instructions", "character_book", "extensions",
)
# 合法顶层键（spec/spec_version/data）
TOP_LEVEL_KEYS = ("spec", "spec_version", "data")
# D-65 隐私层字段：data.privacy_agents 白名单（允许装配该角色的 agent_id 列表）。
# 不并入 OPTIONAL_FIELDS——normalize 会给每张老卡补 null，而「字段缺失」本身
# 就是「未启用隔离」的契约语义，不该把 null 写进所有存量角色卡。
PRIVACY_FIELD = "privacy_agents"
# save_role 维护的时间戳键（读回再保存时必须合法，否则既有卡 upsert 必失败）
TIMESTAMP_FIELDS = ("created_at", "updated_at")
# 关怀策略扩展键（extensions.sgme_care，供消费方读取）
# D-61（2026-11-04）：care_level 三档按角色独立配置（无全局默认档）；新角色默认 silent。
CARE_LEVELS = ("silent", "session", "push")
CARE_LEVEL_DEFAULT = "silent"
CARE_LEVEL_LABELS = {
    "silent": "静默关怀（只记录不推送）",
    "session": "会话内关怀（聊天中自然带出）",
    "push": "主动推送（关键信号才推）",
}
CARE_EXT_KEYS = ("greeting_templates", "trigger_rules", "frequency", "care_level")

# ---------- 活人化角色基线层（设计文档 4.2 / 前端契约） ----------

# 基线层扩展键（extensions.sgme_profile）：传记/三观/爱好/语气/性格六槽位
PROFILE_EXT_KEY = "sgme_profile"
# sgme_profile 白名单键（五件套；多余键拒绝，防拼写漂移）
PROFILE_KEYS = ("biography", "values", "hobbies", "voice", "personality_slots")
# personality_slots 六槽位（与前端工作台对齐，中文槽位名是契约的一部分）
PERSONALITY_SLOTS = (
    "语言风格", "情绪反应", "价值观取舍", "社交姿态", "冲突处理", "亲密表达",
)

# ---------- 演化层（运行时数据：data/role_evolution/<role_id>.json，不入 git） ----------

# 演化层四类条目（kind 白名单）：共同经历时间线 / 心情带触发 / 性格槽位变化 / 三观变化
EVOLUTION_KINDS = ("timeline", "feelings", "personality_changes", "values_changes")
# 各 kind 的必填字段与允许字段（多余字段拒绝，防 timeline/feelings 串味）
EVOLUTION_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # (必填, 可选)
    "timeline": (("event",), ("source",)),
    "feelings": (("thought",), ("trigger",)),
    "personality_changes": (("slot", "to"), ("from", "reason")),
    "values_changes": (("change",), ("reason",)),
}
# 演化层空结构（load 缺文件时返回；GET 端点同样以此为兜底）
EVOLUTION_EMPTY: dict[str, list[dict[str, Any]]] = {k: [] for k in EVOLUTION_KINDS}

# 角色 id 白名单：小写字母/数字/连字符/下划线（防路径穿越）
_ROLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# CC V2 固定标识
CC_SPEC = "chara_card_v2"
CC_SPEC_VERSION = "2.0"

# 存档子目录（原件永不删）
_ARCHIVE_DIR = ".archive"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_role_id(role_id: str) -> None:
    """角色 id 白名单校验（防路径穿越）；非法抛 ValueError。

    演化层文件名/角色卡文件名都由 id 直接拼接，这是唯一的穿越防线，
    文件读写前必须先过这里。
    """
    if not _ROLE_ID_RE.match(role_id or ""):
        raise ValueError(f"非法角色 id: {role_id!r}（须匹配 {_ROLE_ID_RE.pattern}）")


def _role_file(roles_dir: Path, role_id: str) -> Path:
    """角色卡文件路径（防路径穿越：id 白名单校验）。"""
    validate_role_id(role_id)
    return roles_dir / f"{role_id}.json"


def validate_role_card(data: dict[str, Any]) -> list[str]:
    """校验角色卡（CC V2 兼容子集），返回错误列表；空列表 = 合法。

    规则：
    - data 必须是对象，必填 name/description 且非空字符串
    - 可选字段只允许白名单键；extensions.sgme_care 只允许关怀策略键
    - 顶层只允许 spec/spec_version/data（单文件即角色）
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["角色卡必须是 JSON 对象"]
    unknown = set(data.keys()) - set(TOP_LEVEL_KEYS)
    if unknown:
        errors.append(f"顶层多余键: {sorted(unknown)}（只允许 {TOP_LEVEL_KEYS}）")
    d = data.get("data")
    if not isinstance(d, dict):
        errors.append("data 必须是对象")
        return errors
    for f in REQUIRED_FIELDS:
        v = d.get(f)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"必填字段缺失或为空: {f}")
    for k, v in d.items():
        if k in REQUIRED_FIELDS or k in OPTIONAL_FIELDS or k in TIMESTAMP_FIELDS:
            if not isinstance(v, (str, dict, list)) and v is not None:
                errors.append(f"字段类型非法: {k}")
        elif k == PRIVACY_FIELD:
            # D-65：白名单只接受非空字符串数组；空白条目等于放行空白身份，一律拒绝
            if (
                not isinstance(v, list)
                or any(not isinstance(a, str) or not a.strip() for a in v)
            ):
                errors.append(f"{PRIVACY_FIELD} 必须是非空字符串数组")
        else:
            errors.append(f"data 多余键: {k}（可选键: {OPTIONAL_FIELDS}）")
    ext = d.get("extensions")
    if ext is not None:
        if not isinstance(ext, dict):
            errors.append("extensions 必须是对象")
        else:
            care = ext.get("sgme_care")
            if care is not None:
                if not isinstance(care, dict):
                    errors.append("extensions.sgme_care 必须是对象")
                else:
                    for k in care:
                        if k not in CARE_EXT_KEYS:
                            errors.append(
                                f"extensions.sgme_care 多余键: {k}"
                                f"（允许: {CARE_EXT_KEYS}）"
                            )
                    level = care.get("care_level")
                    if level is not None and level not in CARE_LEVELS:
                        errors.append(
                            f"extensions.sgme_care.care_level 非法: {level!r}"
                            f"（允许: {CARE_LEVELS}）"
                        )
            profile = ext.get(PROFILE_EXT_KEY)
            if profile is not None:
                errors.extend(_validate_profile(profile))
    return errors


def _validate_profile(profile: Any) -> list[str]:
    """校验 extensions.sgme_profile（基线层五件套）；返回错误列表，空 = 合法。"""
    if not isinstance(profile, dict):
        return [f"extensions.{PROFILE_EXT_KEY} 必须是对象"]
    errs: list[str] = []
    for k in profile:
        if k not in PROFILE_KEYS:
            errs.append(
                f"extensions.{PROFILE_EXT_KEY} 多余键: {k}（允许: {PROFILE_KEYS}）"
            )
    for k in ("biography", "values", "hobbies", "voice"):
        v = profile.get(k)
        if v is not None and not isinstance(v, str):
            errs.append(f"extensions.{PROFILE_EXT_KEY}.{k} 必须是字符串")
    slots = profile.get("personality_slots")
    if slots is not None:
        if not isinstance(slots, dict):
            errs.append(f"extensions.{PROFILE_EXT_KEY}.personality_slots 必须是对象")
        else:
            for k in slots:
                if k not in PERSONALITY_SLOTS:
                    errs.append(
                        f"extensions.{PROFILE_EXT_KEY}.personality_slots 多余槽位: {k}"
                        f"（允许: {PERSONALITY_SLOTS}）"
                    )
    return errs


def normalize_role_card(data: dict[str, Any]) -> dict[str, Any]:
    """规范化角色卡：补 CC V2 标识与默认空字段，返回可落盘结构。"""
    d = dict(data.get("data") or {})
    for f in OPTIONAL_FIELDS:
        d.setdefault(f, None)
    return {
        "spec": CC_SPEC,
        "spec_version": CC_SPEC_VERSION,
        "data": d,
    }


# ---------- 文件 CRUD（roles/ 目录） ----------

def ensure_roles_dir(roles_dir: Path) -> None:
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / _ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)


def get_care_level(card: dict[str, Any] | None) -> str:
    """读取角色卡关怀档位（D-61）。

    缺省/未知值一律回落 default silent——**不猜用户意图**：老卡没有该字段时
    等价于「静默关怀」（只记录不推送），符合「默认不打扰」的拍板。
    """
    data = (card or {}).get("data") or {}
    ext = data.get("extensions") or {}
    care = ext.get("sgme_care") if isinstance(ext, dict) else None
    level = care.get("care_level") if isinstance(care, dict) else None
    return level if level in CARE_LEVELS else CARE_LEVEL_DEFAULT


def list_roles(roles_dir: Path) -> list[dict[str, Any]]:
    """角色卡列表（轻量：role_id/name/description/updated_at/care_level）。"""
    ensure_roles_dir(roles_dir)
    out: list[dict[str, Any]] = []
    for f in sorted(roles_dir.glob("*.json")):
        try:
            card = json.loads(f.read_text(encoding="utf-8"))
            d = card.get("data") or {}
            out.append({
                "role_id": f.stem,
                "name": d.get("name", f.stem),
                "description": d.get("description", ""),
                "updated_at": d.get("updated_at"),
                "care_level": get_care_level(card),
            })
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("角色卡读取失败 %s: %s", f.name, e)
            continue
    return out


def get_role(roles_dir: Path, role_id: str) -> dict[str, Any] | None:
    """单张角色卡全文；不存在返回 None。"""
    fp = _role_file(roles_dir, role_id)
    if not fp.exists():
        return None
    try:
        card = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("角色卡解析失败 %s: %s", role_id, e)
        raise ValueError(f"角色卡解析失败: {role_id}") from e
    card["role_id"] = role_id
    return card


def save_role(roles_dir: Path, role_id: str, data: dict[str, Any]) -> Path:
    """保存角色卡（幂等 upsert：已存在则更新并刷新 updated_at）。

    校验失败抛 ValueError（不落盘）。
    """
    errors = validate_role_card(data)
    if errors:
        raise ValueError("角色卡校验失败: " + "; ".join(errors))
    ensure_roles_dir(roles_dir)
    card = normalize_role_card(data)
    card["data"]["updated_at"] = _now_iso()
    if "created_at" not in card["data"]:
        card["data"]["created_at"] = card["data"]["updated_at"]
    fp = _role_file(roles_dir, role_id)
    fp.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("角色卡已保存: %s（%s）", role_id, card["data"]["name"])
    return fp


def archive_role(roles_dir: Path, role_id: str) -> bool:
    """归档角色卡：移入 .archive/（原件永不删铁律，不物理删除）。

    Returns:
        True=已归档；False=角色不存在。
    """
    ensure_roles_dir(roles_dir)
    fp = _role_file(roles_dir, role_id)
    if not fp.exists():
        return False
    dst = roles_dir / _ARCHIVE_DIR / fp.name
    fp.rename(dst)
    logger.info("角色卡已归档: %s → %s", role_id, dst)
    return True


# ---------- character_book 懒补全读写（D-76 / T-306） ----------

# 归一化：只留中文/字母/数字（去标点、空白、全角符号），用于字面比对
_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)
# 停用字：公共子串若全由这些字组成，视为噪音不参与命中（防「在哪」「的」类误判）
_HIT_STOP_CHARS = frozenset(
    "的了吗呢吧啊呀是在有和与或就都也还又很太不没我你他她它们这那些个一"
    "什么哪怎如何上下里外吗呀哈嘛"
)
# 单条命中阈值：有效公共子串长度 ≥2 才算认出同一段人生（1 个字撑不起判断）
_HIT_MIN_SCORE = 2


def _norm_text(s: str) -> str:
    """归一化文本：去标点与空白，仅保留中文/字母/数字。"""
    return _NORM_RE.sub("", str(s or ""))


def _lcs_str(a: str, b: str) -> str:
    """最长公共子串本体（同长取先出现的）；问题与 key 都很短，O(n*m) 足够。"""
    if not a or not b:
        return ""
    best_len, end = 0, 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len, end = cur[j], i
        prev = cur
    return a[end - best_len:end]


def _has_content_char(s: str) -> bool:
    """子串里至少一个非停用字（否则是「在哪」「的」这类噪音）。"""
    return any(ch not in _HIT_STOP_CHARS for ch in s)


def _hit_score(entry: dict[str, Any], qnorm: str) -> int:
    """单条 character_book 条目的命中分（0 = 不命中）。

    分层（自高到低）：
    - 原问题归一化后完全相同 → 100：同一句话再问，事实必须一模一样
    - 原问题与问句互相包含且都 ≥4 字 → 40：换口气复述同一问题
    - key 归一化后是问句子串 → 10 + len(key)：key 原样被问到
    - 兜底：key 与问句的最长公共子串（≥2 且含实字）→ 分值为子串长度

    修复背景（2026-11-05 生产实测）：原实现只做「key in question」单向字面
    包含，而 LLM 生成的 keys 是概念标签（普通乡镇小学/下雨天/操场泥），用户
    复述时用自然句式（你小学在哪上的）——两者几乎不可能字面相交，命中率≈0，
    表现为同一件事每次重新生成、换个说法（生产 insertion_order 100 → 110）。
    """
    if not qnorm:
        return 0
    score = 0
    for sq in entry.get("source_questions") or []:
        sqn = _norm_text(sq)
        if not sqn:
            continue
        if sqn == qnorm:
            return 100
        if len(sqn) >= 4 and len(qnorm) >= 4 and (sqn in qnorm or qnorm in sqn):
            score = max(score, 40)
    for k in entry.get("keys") or []:
        kn = _norm_text(k)
        if not kn:
            continue
        if kn in qnorm:
            score = max(score, 10 + len(kn))
            continue
        if len(kn) < _HIT_MIN_SCORE:
            continue
        common = _lcs_str(kn, qnorm)
        if len(common) >= _HIT_MIN_SCORE and _has_content_char(common):
            score = max(score, len(common))
    return score


def find_book_entry(card: dict[str, Any], question: str) -> dict[str, Any] | None:
    """D-76 命中判定：给条目打分，取最高分那条（同分取最早写回的）。

    constant 条目不算懒补全命中——它们是恒注入设定（习惯细节），不是被追问的
    人生事实；懒补全只针对「被问才讲」的条目。命中后措辞可变、事实不变（②）。
    """
    qnorm = _norm_text(question)
    if not qnorm:
        return None
    data = card.get("data") or {}
    book = data.get("character_book") or {}
    best: dict[str, Any] | None = None
    best_score = 0
    best_order = 0
    for e in book.get("entries") or []:
        if not isinstance(e, dict) or e.get("constant"):
            continue
        score = _hit_score(e, qnorm)
        if score < _HIT_MIN_SCORE:
            continue
        try:
            order = int(e.get("insertion_order") or 0)
        except (TypeError, ValueError):
            order = 0
        # 同分取最早写回的：事实最原始的那条优先，避免后来的重新生成顶掉第一条
        if best is None or score > best_score or (score == best_score and order < best_order):
            best, best_score, best_order = e, score, order
    return best


def append_book_entry(
    roles_dir: Path,
    role_id: str,
    keys: list[str],
    content: str,
    generated: bool = True,
    question: str | None = None,
) -> dict[str, Any] | None:
    """D-76 写回：向角色卡 character_book 追加条目并落盘。

    角色不存在返回 None；正常返回新条目。落盘走 save_role（校验+刷新
    updated_at），**只写角色卡文件，绝不触碰 memory.db**——角色的人生事实
    是角色档案，不是用户事实（隔离铁律）。

    ``question``：触发这次补全的原始追问，落进 ``source_questions``。
    只靠 LLM 生成的 keys 记不住「同一句话再问」——keys 是概念标签，用户复述
    时的措辞对不上；存原问题才能做到同问必同答（2026-11-05 生产实测修复）。
    """
    card = get_role(roles_dir, role_id)
    if card is None:
        return None
    data = card.get("data") or {}
    book = data.get("character_book")
    if not isinstance(book, dict) or not isinstance(book.get("entries"), list):
        book = {"name": f"{data.get('name', role_id)}的人生", "entries": []}
    entries = book["entries"]
    entry: dict[str, Any] = {
        "keys": [str(k) for k in keys if str(k).strip()],
        "constant": False,
        "insertion_order": max(
            (int(e.get("insertion_order") or 0) for e in entries), default=90,
        ) + 10,
        "content": content,
    }
    if generated:
        entry["generated"] = True
    q = str(question or "").strip()
    if q:
        entry["source_questions"] = [q]
    entries.append(entry)
    data["character_book"] = book
    save_role(roles_dir, role_id, {
        "spec": card.get("spec", CC_SPEC),
        "spec_version": card.get("spec_version", CC_SPEC_VERSION),
        "data": data,
    })
    logger.info("character_book 条目已写回: %s（keys=%s, generated=%s）", role_id, keys, generated)
    return entry


# ---------- 越界记录（D-77 / T-307） ----------

# extensions 命名空间键：越界事件只记在角色卡，永不进 memory.db（与 D-76 同一隔离铁律）
VIOLATION_EXT_KEY = "sgme_violations"
# 原句摘要截断长度（任务清单硬要求：原句截断 60 字）
VIOLATION_SNIPPET_MAX = 60
# 单卡保留上限：越界事件是运行期日志，只留最近 N 条（避免角色卡无限膨胀）
VIOLATION_KEEP_MAX = 100


def load_violations(card: dict[str, Any]) -> list[dict[str, Any]]:
    """读角色卡 extensions.sgme_violations 事件列表（无则空列表）。"""
    ext = (card.get("data") or {}).get("extensions") or {}
    if not isinstance(ext, dict):
        return []
    box = ext.get(VIOLATION_EXT_KEY) or {}
    entries = box.get("entries") if isinstance(box, dict) else None
    return entries if isinstance(entries, list) else []


def append_violation(
    roles_dir: Path,
    role_id: str,
    kind: str,
    snippet: str,
    session_id: str = "",
    now: str | None = None,
) -> dict[str, Any] | None:
    """D-77 第二级「记录」：向角色卡 extensions.sgme_violations 追加一条并落盘。

    条目结构 ``{at, kind, snippet, session_id}``；snippet 截断到 60 字（记摘要
    不记全文）。只写角色卡文件，**绝不写 memory.db**——越界是角色档案事件，
    不是用户事实。角色不存在返回 None。
    """
    card = get_role(roles_dir, role_id)
    if card is None:
        return None
    entries = load_violations(card)
    entry: dict[str, Any] = {
        "at": now or _now_iso(),
        "kind": str(kind),
        "snippet": str(snippet or "")[:VIOLATION_SNIPPET_MAX],
        "session_id": str(session_id or ""),
    }
    entries.append(entry)
    if len(entries) > VIOLATION_KEEP_MAX:
        entries = entries[-VIOLATION_KEEP_MAX:]
    data = dict(card.get("data") or {})
    ext = dict(data.get("extensions") or {})
    ext[VIOLATION_EXT_KEY] = {"entries": entries}
    data["extensions"] = ext
    save_role(roles_dir, role_id, {
        "spec": card.get("spec", CC_SPEC),
        "spec_version": card.get("spec_version", CC_SPEC_VERSION),
        "data": data,
    })
    logger.info("越界记录已写回: %s（kind=%s）", role_id, kind)
    return entry


def count_violations(
    card: dict[str, Any],
    kind: str,
    session_id: str | None = None,
    since_iso: str | None = None,
) -> int:
    """D-77 第三级「硬后果」阈值判定：统计同类型越界次数。

    - session_id 给定 → 只数该会话（≥2 → 附加更正约束）
    - since_iso 给定 → 只数该时间点之后（≥3/天 → 告警）
    """
    n = 0
    for e in load_violations(card):
        if e.get("kind") != kind:
            continue
        if session_id is not None and e.get("session_id") != session_id:
            continue
        if since_iso is not None and str(e.get("at") or "") < since_iso:
            continue
        n += 1
    return n


# ---------- 真实感指标快照（D-74 / T-308） ----------

# extensions 命名空间键：测量快照只记在角色卡，永不进 memory.db（同一隔离铁律）
METRICS_EXT_KEY = "sgme_metrics"
# latest 快照里回复原文的截断长度：区分度脚本按原文算相似度，300 字足够且不撑爆角色卡
METRICS_REPLY_MAX = 300
# 历史 runs 保留上限（只存聚合值不含回复原文，防角色卡无限膨胀）
METRICS_RUNS_KEEP = 20


def load_metrics(card: dict[str, Any]) -> dict[str, Any]:
    """读角色卡 extensions.sgme_metrics（无则空对象）。

    结构：``{baseline, latest, runs, updated_at}``，全部可选；解析权在 operations 层。
    """
    ext = (card.get("data") or {}).get("extensions") or {}
    if not isinstance(ext, dict):
        return {}
    box = ext.get(METRICS_EXT_KEY)
    return box if isinstance(box, dict) else {}


def save_metrics(roles_dir: Path, role_id: str, metrics: dict[str, Any]) -> bool:
    """写回 extensions.sgme_metrics（读-改-写角色卡）。角色不存在返回 False。

    只写角色卡文件，**绝不写 memory.db**——测量快照是角色档案，不是用户事实。
    """
    card = get_role(roles_dir, role_id)
    if card is None:
        return False
    data = dict(card.get("data") or {})
    ext = dict(data.get("extensions") or {})
    ext[METRICS_EXT_KEY] = metrics
    data["extensions"] = ext
    save_role(roles_dir, role_id, {
        "spec": card.get("spec", CC_SPEC),
        "spec_version": card.get("spec_version", CC_SPEC_VERSION),
        "data": data,
    })
    logger.info("真实感指标已写回: %s", role_id)
    return True


# ---------- 演化层（设计 4.2：共同经历/心情/性格演化/三观演化，运行时数据） ----------

# 单卡演化层条目保留上限：与越界记录同一防膨胀思路，超出只留最近 N 条
EVOLUTION_KEEP_MAX = 1000


def _evolution_file(evolution_dir: Path, role_id: str) -> Path:
    """演化层文件路径（复用角色 id 白名单，防路径穿越）。"""
    validate_role_id(role_id)
    return evolution_dir / f"{role_id}.json"


def load_evolution(role_id: str, evolution_dir: Path | None = None) -> dict[str, Any]:
    """读取角色演化层；文件不存在返回空结构（演化层是可选运行数据，不报错）。

    解析失败抛 ValueError（损坏文件必须显式暴露，不能静默清空运行数据）。
    """
    d = evolution_dir or Path("data") / "role_evolution"
    fp = _evolution_file(d, role_id)
    if not fp.exists():
        return {k: list(v) for k, v in EVOLUTION_EMPTY.items()}
    try:
        box = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("演化层解析失败 %s: %s", role_id, e)
        raise ValueError(f"演化层解析失败: {role_id}") from e
    if not isinstance(box, dict):
        raise ValueError(f"演化层结构非法（必须是 JSON 对象）: {role_id}")
    out: dict[str, Any] = {}
    for k in EVOLUTION_KINDS:
        v = box.get(k)
        out[k] = v if isinstance(v, list) else []
    return out


def append_evolution(
    role_id: str,
    kind: str,
    entry: dict[str, Any],
    evolution_dir: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """向演化层追加一条记录并落盘（读-改-写整文件）。

    Args:
        role_id: 角色 id（白名单校验，防路径穿越）。
        kind: 条目类型，必须在 ``EVOLUTION_KINDS`` 白名单内。
        entry: 条目字段（不含 at；落盘前按 kind 白名单校验并自动补 ``at`` 时间戳）。
        evolution_dir: 演化层目录（缺省 ``data/role_evolution/``）。
        now: 覆盖时间戳（测试注入用；生产省略取当前 UTC）。

    Raises:
        ValueError: role_id 非法 / kind 不在白名单 / 必填字段缺失或为空白 /
            字段多余（防各 kind 串味）。拒绝时不落盘。
    """
    validate_role_id(role_id)
    if kind not in EVOLUTION_KINDS:
        raise ValueError(f"演化层 kind 非法: {kind!r}（允许: {EVOLUTION_KINDS}）")
    if not isinstance(entry, dict):
        raise ValueError(f"演化层条目必须是对象: {kind}")
    required, optional = EVOLUTION_FIELDS[kind]
    for f in required:
        v = entry.get(f)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"演化层条目 {kind} 缺必填字段或为空白: {f}")
    extra = set(entry.keys()) - set(required) - set(optional) - {"at"}
    if extra:
        raise ValueError(f"演化层条目 {kind} 多余字段: {sorted(extra)}")
    record: dict[str, Any] = {"at": now or _now_iso()}
    for f in required + optional:
        if entry.get(f) is not None:
            record[f] = entry[f]
    d = evolution_dir or Path("data") / "role_evolution"
    d.mkdir(parents=True, exist_ok=True)
    box = load_evolution(role_id, d)
    box[kind].append(record)
    if len(box[kind]) > EVOLUTION_KEEP_MAX:
        box[kind] = box[kind][-EVOLUTION_KEEP_MAX:]
    fp = _evolution_file(d, role_id)
    fp.write_text(json.dumps(box, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("演化层已追加: %s（kind=%s）", role_id, kind)
    return record


# ---------- persona 物化（唯一物化例外） ----------

PERSONA_PROMPT = """你是 Persona Architect（人格架构师）。请基于给定的用户画像素材，
为用户生成一份「{role_name}」角色视角的沟通画像（persona.md）。角色本身的人设
（性格/语气/场景）已由角色卡定义，你要做的是让角色**知道怎么和这位用户沟通**。

四层深度扫描（TencentDB Persona Architect 方法论）：
- L1 基础锚点：用户是谁（身份/家庭/工作等事实）、可用的破冰话题
- L2 兴趣图谱：用户关心什么（项目/偏好/兴趣），按活跃度分级
- L3 交互协议：沟通习惯、雷区（用户明确不喜欢的）、称谓偏好
- L4 认知内核：决策逻辑、驱动力、价值观（判断何时该提醒/关怀）

约束（硬性）：
- 输出 ≤ {max_chars} 字符
- 禁止推测：内容只准来自给定素材，没有的不写
- 禁止编造用户没有表达过的事实
- 只输出 persona 正文（Markdown），不要解释

用户画像素材：
{profile}
"""


def render_persona_prompt(role_name: str, profile: str, max_chars: int = 2000) -> str:
    """渲染 persona 生成提示词（四层扫描）。"""
    return PERSONA_PROMPT.format(role_name=role_name, profile=profile, max_chars=max_chars)


def save_persona(role_id: str, text: str, persona_dir: Path | None = None) -> Path:
    """persona 物化文件落盘（data/personas/<role_id>.md）。"""
    pd = persona_dir or Path("data") / "personas"
    pd.mkdir(parents=True, exist_ok=True)
    # 备份旧版（保留 3 份，借鉴 TencentDB）
    fp = pd / f"{role_id}.md"
    if fp.exists():
        for i in range(3, 0, -1):
            old = pd / f"{role_id}.md.bak{i}"
            if old.exists():
                old.rename(pd / f"{role_id}.md.bak{i + 1}")
        fp.rename(pd / f"{role_id}.md.bak1")
    fp.write_text(text.strip() + "\n", encoding="utf-8")
    logger.info("persona 已物化: %s（%d 字符）", role_id, len(text))
    return fp


def load_persona(role_id: str, persona_dir: Path | None = None) -> str | None:
    """读取 persona 物化文件；不存在返回 None。"""
    pd = persona_dir or Path("data") / "personas"
    fp = pd / f"{role_id}.md"
    if not fp.exists():
        return None
    return fp.read_text(encoding="utf-8")


# ---------- 当前角色（T-40：用户选择的沟通角色，运行数据） ----------

def _active_role_file(data_dir: Path | None = None) -> Path:
    """当前角色状态文件（运行数据，不入 git）。"""
    d = data_dir or Path("data")
    return d / "care" / "active_role.json"


def get_active_role(data_dir: Path | None = None) -> str | None:
    """读取当前角色 id；未设置返回 None。"""
    fp = _active_role_file(data_dir)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8")).get("role_id")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("active_role 读取失败（按未设置处理）: %s", e)
        return None


def set_active_role(role_id: str, data_dir: Path | None = None) -> Path:
    """设置当前角色（幂等写入；角色 id 白名单校验）。"""
    if not _ROLE_ID_RE.match(role_id):
        raise ValueError(f"非法角色 id: {role_id!r}")
    fp = _active_role_file(data_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(
        json.dumps({"role_id": role_id, "set_at": _now_iso()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("当前角色已设置: %s", role_id)
    return fp


def clear_active_role(data_dir: Path | None = None) -> Path:
    """取消当前角色（写 role_id=None；幂等——重复取消仍成功）。"""
    fp = _active_role_file(data_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(
        json.dumps({"role_id": None, "set_at": _now_iso()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("当前角色已取消（cleared）")
    return fp
