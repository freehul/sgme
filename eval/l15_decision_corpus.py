"""L1.5 决策级评测语料（ST-58/T-296，G3-c 门禁用）。

为什么需要它（背景）：
- 既有评测框架（`eval/runner.py` / `metrics.py`）只度量**检索层** `recall@k`；
  而 L1.5 的作用是**裁决**（store/skip/update/merge）——审查意见 K7 与实测 N6 都指出：
  裁决错误（重复残留、冲突漏判、该 update 却 store）**不会显著改变 recall**，
  所以 recall 无法为 L1.5 的质量把关。
- 本语料为 L1.5 提供**人工标注期望动作**的固定输入，从而支持：
  ① 逐条决策**一致率**（A/B 两臂对比，方案 §4.2 的 G3-c）；
  ② 逐条决策**准确率**（对 `expect` 的命中率，绝对质量而非相对）。

设计原则：
1. **输入完全确定性**：池与新记忆均是手写 fixture（不经 LLM 抽取），
   否则 L1 抽取本身的随机性会淹没 L1.5 的差异（这是方法论要点，不是偷懒）。
2. **覆盖 L1.5 的完整动作空间** + 两个专项风险场景：
   - 长记忆「关键差异在尾部」（B1' 若截断正文会误判）
   - 含 Windows 风格路径（JSON 转义 / 就地修复链路）
3. **可调填充池**：`make_fillers()` 生成同维度、语义相近的填充记忆，
   使 prescreen 上限（`vector_top_k` / `dimension_top_n`）**真的会卡住**——
   池太小时 50/50 与 30/30 无差异，B2 的效果就测不出来。

数据卫生：内容全部为通用假值（无真实路径/主机/姓名）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

#: 固定时间基准（确定性：所有 fixture 的 created/updated 都相对它取值）。
BASE_TS = "2026-03-01T00:00:00Z"

#: ⚠️ 时间线设计（2026-10-17 修正，首轮实跑踩坑）：
#: 池条目的 `updated_at` **必须比填充条目更新**。
#: 因为候选池按 `updated_at DESC` 取 top-n 截断——首版让填充比池更新，结果
#: `pool-0001` / `pool-0008` 被 60 条填充挤出候选池（environment 维度共 63 条），
#: 目标候选根本没送到模型眼前，`skip_subsumed` / `escape_path` 两条 expect
#: **在物理上不可达成**（模型只好判 store，理由也写得很对）。那是语料缺陷，
#: 不是模型能力问题——绝对准确率因此被严重低估（实测 50%）。
#: 现排布：池 = day 61..68（最新），填充 = day 1..n（更旧）→ 池条目恒在候选池内，
#: 而总候选数仍 > 上限（63 > 50 > 30），故 prescreen 上限照旧会卡住。
_POOL_DAY_BASE = 60

#: 时间线基准日；`updated_day` 是**相对基准日的天数偏移**（不是月内日号）。
#: ⚠️ 首版直接把它当「月内日号」拼字符串，偏移 >31 时生成 `2026-03-61` 这类
#: **非法日期**——字典序排序虽然还能用，但任何 `fromisoformat` 解析都会失败。
#: 改为真日历偏移，保证产出的是合法 ISO 8601。
_EPOCH = date(2026, 3, 1)


def _ts(day_offset: int, hour: int = 0) -> str:
    d = _EPOCH + timedelta(days=day_offset)
    return f"{d.isoformat()}T{hour:02d}:00:00Z"


# ---------- 池（既有记忆 = L1.5 的候选来源） ----------
# 每条：(memory_id 固定便于跨臂比对, content, dimension_ids, memory_type, priority, updated_day)
POOL: list[dict[str, Any]] = [
    {
        # ⚠️ 独占约束（T-298）：每条池条目只被**一条**批当作目标。
        # 因为 L1.5 会**落库改池**（update/merge 会把目标候选归档）——若两条批共用同一
        # 池条目，批序就会互相破坏（先跑的那条把候选归档掉，后跑的就找不到目标）。
        "memory_id": "pool-0001",
        "content": "用户的家庭服务器上部署了 SGME 记忆引擎",
        "dimension_ids": ["environment"],
        "memory_type": "fact",
        "priority": 60,
        "updated_day": 61,
    },
    {
        "memory_id": "pool-update-increment",
        "content": "用户的家庭服务器上部署了反向代理服务",
        "dimension_ids": ["environment"],
        "memory_type": "fact",
        "priority": 55,
        "updated_day": 69,
    },
    {
        "memory_id": "pool-0002",
        "content": "用户用 ModelScope 作为模型下载源",
        "dimension_ids": ["habits"],
        "memory_type": "fact",
        "priority": 50,
        "updated_day": 62,
    },
    {
        "memory_id": "pool-0003",
        "content": "用户的主力开发机是 Win11，显卡 16GB 显存",
        "dimension_ids": ["tech_stack"],
        "memory_type": "fact",
        "priority": 60,
        "updated_day": 63,
    },
    {
        "memory_id": "pool-0004",
        "content": "用户计划在第四季度完成记忆引擎的评测基线",
        "dimension_ids": ["goals"],
        "memory_type": "fact",
        "priority": 70,
        "updated_day": 64,
    },
    {
        "memory_id": "pool-0005",
        "content": "用户的家庭服务器是纯 CPU 推理，没有独立显卡",
        "dimension_ids": ["environment"],
        "memory_type": "fact",
        "priority": 50,
        "updated_day": 65,
    },
    {
        "memory_id": "pool-0006",
        "content": "用户偏好先调研后动手，破坏性操作前先列清单确认",
        "dimension_ids": ["preferences"],
        "memory_type": "persona",
        "priority": 80,
        "updated_day": 66,
    },
    {
        # 长记忆：关键信息在**尾部**（B1' 截断正文的专项风险场景）
        "memory_id": "pool-0007",
        "content": (
            "用户的模型资源仓库按用途划分为多个子目录：图像生成、本地推理、"
            "共享权重、语音合成与语音识别各自独立；图像生成工具通过 extra_model_paths "
            "配置文件挂载该仓库作为模型来源；模型目录已从系统盘迁移到外置数据盘"
        ),
        "dimension_ids": ["tech_stack"],
        "memory_type": "fact",
        "priority": 55,
        "updated_day": 67,
    },
    {
        # 含 Windows 风格路径：JSON 转义 / 就地修复专项场景
        "memory_id": "pool-0008",
        "content": r"用户的样例项目目录结构是 C:\work\demo 下的 src 与 tests",
        "dimension_ids": ["environment"],
        "memory_type": "fact",
        "priority": 45,
        "updated_day": 68,
    },
]


# ---------- 新记忆批（待 L1.5 裁决；expect 为人工标注的期望动作） ----------
BATCHES: list[dict[str, Any]] = [
    {
        "id": "store_new_subject",
        "note": "全新主体（池中无 family 维度记忆）→ 应新增",
        "expect": "store",
        "target": None,
        "new_memories": [
            {
                "content": "用户家里有一位叫小满的家庭成员",
                "dimension_ids": ["family"],
                "memory_type": "persona",
            }
        ],
    },
    {
        "id": "skip_subsumed",
        "note": "同主体但新记忆更模糊、无信息增量 → 应忽略",
        "expect": "skip",
        "target": "pool-0001",
        "new_memories": [
            {
                "content": "用户在一台服务器上部署了软件",
                "dimension_ids": ["environment"],
                "memory_type": "fact",
            }
        ],
    },
    {
        "id": "update_increment",
        "note": "同主体（家庭服务器上的反向代理部署）+ 明确信息增量 → 应覆盖",
        "expect": "update",
        "target": "pool-update-increment",
        "new_memories": [
            {
                "content": "用户的家庭服务器上部署了反向代理服务，并配置了每日自动备份、备份保留 7 份",
                "dimension_ids": ["environment"],
                "memory_type": "fact",
            }
        ],
    },
    {
        "id": "merge_complement",
        "note": "同主体（主力开发机）+ 互补片段 → 应合并",
        "expect": "merge",
        "target": "pool-0003",
        "new_memories": [
            {
                "content": "用户的主力开发机内存是 32GB，外接双 4K 显示器",
                "dimension_ids": ["tech_stack"],
                "memory_type": "fact",
            }
        ],
    },
    {
        "id": "update_long_tail",
        "note": "同主体，关键差异在**尾部**（新增子目录 + 迁移目标变化）→ 应覆盖",
        "expect": "update",
        "target": "pool-0007",
        "new_memories": [
            {
                "content": (
                    "用户的模型资源仓库按用途划分为多个子目录：图像生成、本地推理、"
                    "共享权重、语音合成与语音识别各自独立；图像生成工具通过 extra_model_paths "
                    "配置文件挂载该仓库作为模型来源；模型目录已迁移到独立数据盘，"
                    "并新增了语音识别子目录用于本地转写"
                ),
                "dimension_ids": ["tech_stack"],
                "memory_type": "fact",
            }
        ],
    },
    {
        "id": "escape_path",
        "note": "含 Windows 风格路径：不仅要知道动作，还要确保不因 JSON 转义失败而降级 store",
        "expect": "update",
        "target": "pool-0008",
        "new_memories": [
            {
                "content": r"用户的样例项目目录结构调整为 C:\work\demo 下的 src / tests / docs 三个子目录",
                "dimension_ids": ["environment"],
                "memory_type": "fact",
            }
        ],
    },
]


# ---------- 扩容族（T-298：每类动作 ≥10 条，使一致率/准确率有统计意义） ----------
#
# 设计约束（都是踩坑换来的）：
# 1. **同族共享主体、跨族主题互不相同** —— 池候选按主体相似度命中，若两族主题撞车，
#    A 族的新记忆会去命中 B 族的池条目，expect 就不再成立。
# 2. 池条目的 `updated_at` 一律**比填充新**（见 `_POOL_DAY_BASE` 注释）→ 恒在候选池内。
# 3. `store` 族不需要池条目（主体全新）；`skip/update/merge` 族必须各带一条池条目。
#
# 元组：(batch_id, dimension, expect, 池内容 or None 表示 store, 新记忆内容)

_FAMILIES: list[tuple[str, str, str, str | None, str]] = [
    # ---------- skip：池有具体版，新记忆是更模糊的泛化版（无信息增量） ----------
    ("skip_morning_run", "habits", "skip",
     "用户每天早上 6:30 出门晨跑 5 公里", "用户每天都运动"),
    ("skip_diet_control", "habits", "skip",
     "用户晚餐主食是杂粮饭，控糖", "用户注意饮食健康"),
    ("skip_privacy_stance", "values", "skip",
     "用户重视数据隐私，个人数据不上传云端", "用户在意隐私"),
    ("skip_driving", "skills", "skip",
     "用户会开车，有 8 年驾龄", "用户有驾驶能力"),
    ("skip_coffee_habit", "preferences", "skip",
     "用户喝黑咖啡，不加糖不加奶", "用户喝咖啡"),
    ("skip_journal", "habits", "skip",
     "用户每晚睡前写 5 分钟日记", "用户有写日记的习惯"),
    ("skip_podcast_idea", "ideas", "skip",
     "用户想做一档讲本地模型部署的播客", "用户想做内容创作"),
    ("skip_reading_goal", "goals", "skip",
     "用户计划今年读完 24 本书", "用户想多读书"),
    ("skip_community", "social", "skip",
     "用户加入了一个本地 AI 爱好者小组，每周三晚线上交流", "用户参与了一些社群活动"),

    # ---------- update：同主体 + 明确信息增量 ----------
    ("update_strength_training", "habits", "update",
     "用户每周去两次健身房做力量训练",
     "用户每周去三次健身房做力量训练，深蹲已经能到 100 公斤"),
    ("update_spanish", "skills", "update",
     "用户在学西班牙语",
     "用户在学西班牙语，已过 A2，每周上一次外教口语课"),
    ("update_bike_commute", "preferences", "update",
     "用户骑公路车通勤",
     "用户骑公路车通勤，单程 12 公里，雨天改乘地铁"),
    ("update_bread", "habits", "update",
     "用户会做面包",
     "用户会做欧包，用天然酵种，基本每周烤一次"),
    ("update_notes", "ideas", "update",
     "用户用双链笔记软件记笔记",
     "用户用双链笔记软件记笔记，已积累 3000 条并建了主题索引页"),
    ("update_travel_plan", "goals", "update",
     "用户每年安排一次长途旅行",
     "用户每年安排一次长途旅行，偏好淡季出行并提前三个月订票"),
    ("update_photo", "social", "update",
     "用户用微单拍照",
     "用户用全画幅微单拍照，主要拍城市夜景，常用 35mm 定焦"),

    # ---------- merge：同主体 + 互补片段（各自不完整） ----------
    ("merge_disk_encryption", "values", "merge",
     "用户的工作电脑和数据盘做了全盘加密",
     "用户的手机也开了生物识别锁，并关掉了云同步"),
    ("merge_sleep_hygiene", "habits", "merge",
     "用户睡前一小时不看手机",
     "用户卧室装了遮光窗帘，温度常年在 20 度左右"),
    ("merge_leave_status", "status", "merge",
     "用户最近在休假，八月中旬才恢复工作",
     "用户休假期间不处理工作消息，手机常年免打扰"),
    ("merge_display", "tech_stack", "merge",
     "用户的外接显示器是双 4K 27 寸，一台 144Hz、一台标准刷新率",
     "用户的显示器用双屏气动支架，桌面走线走理线槽收纳"),
    ("merge_backup", "environment", "merge",
     "用户的家庭服务器上部署了备份服务，每日快照保留 30 天",
     "备份同时落一份到外置硬盘，每周做一次离线校验"),
    ("merge_file_mgmt", "habits", "merge",
     "用户用 Everything 做本地文件搜索",
     "用户同时用标签化的目录命名规则管理文档"),
    ("merge_programming", "skills", "merge",
     "用户能用 Python 和 TypeScript 写工程代码",
     "用户也在学 Rust，主要用来写桌面小工具"),
    ("merge_business_trip", "preferences", "merge",
     "用户出差偏好住交通便利的酒店",
     "用户出差习惯把行程排松，留半天自由时间"),
    ("merge_meeting", "habits", "merge",
     "用户开会前会把议题提前发到群里",
     "用户开会时会当场记录待办并同步给相关人"),

    # ---------- store：全新主体（无池条目） ----------
    ("store_family_child", "family", "store", None,
     "用户家里有一个上小学的女儿"),
    ("store_identity_alias", "identity", "store", None,
     "用户在技术社区里常用的笔名是「山雾」"),
    ("store_skills_swim", "skills", "store", None,
     "用户会游泳，自由泳能连续游 1500 米"),
    ("store_ideas_newsletter", "ideas", "store", None,
     "用户想办一份讲本地模型部署的邮件通讯"),
    ("store_goals_trip", "goals", "store", None,
     "用户计划明年去冰岛自驾环岛一圈"),
    ("store_status_relocate", "status", "store", None,
     "用户下个月要搬到新的住处"),
    ("store_values_punctual", "values", "store", None,
     "用户非常看重守时，约定时间从不迟到"),
    ("store_pref_tea", "preferences", "store", None,
     "用户喜欢喝凤凰单丛，偏好蜜兰香"),
    ("store_focus_reading", "focus", "store", None,
     "用户近来在关注边缘推理设备的功耗表现"),
]


def _build_families() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """由 `_FAMILIES` 生成（额外的池条目, 额外的批）。

    池条目的 `updated_day` 从 `_POOL_DAY_BASE + 10` 起递增，**保证比填充（day 1..60）新**
    → 在 `updated_at DESC` 下恒在候选池内（否则 expect 不可达成，见 `_POOL_DAY_BASE` 注释）。
    """
    pool: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    for idx, (bid, dim, expect, pool_content, new_content) in enumerate(_FAMILIES):
        if pool_content is not None:
            pool.append({
                "memory_id": f"pool-{bid}",
                "content": pool_content,
                "dimension_ids": [dim],
                "memory_type": "fact",
                "priority": 50,
                "updated_day": _POOL_DAY_BASE + 10 + idx,
            })
        batches.append({
            "id": bid,
            "note": f"扩容族：expect={expect}（主体见内容）",
            "expect": expect,
            # 目标池条目（供排名探针定位「相关候选排第几」；store 族为 None）
            "target": (f"pool-{bid}" if pool_content is not None else None),
            "new_memories": [{
                "content": new_content,
                "dimension_ids": [dim],
                "memory_type": "fact",
            }],
        })
    return pool, batches


# ---------- 边界场景（T-298：B2 真正的风险面） ----------
#
# 目标：让「**相关候选**」的排名恰好落在 30 与 50 之间 —— 此时 50/50 能看到它、
# 30/30 看不到，从而实测「收紧阈值是否会导致漏判」（store 而非 update）。
#
# 构造手法（靠 `--probe-ranks` 实测迭代，不盲配）：
# - 维度侧按 `updated_at DESC` 取 top-n → 注入 n 条**比目标更新**的填充即可把目标挤到第 n+1 位；
# - 但填充若太像「同一主体」，模型即使看不到目标也能对填充判 update，风险就演示不出来
#   → 故边界填充刻意做成「**术语高度相似、主体不同**」（谈的是别的研究报告的指标，
#   不是用户自己的设备），语义近（能挤掉向量 Top-K 名额）但不是合法的 update 目标。

BOUNDARY_BATCH_ID = "boundary_hidden_by_cap"
BOUNDARY_TARGET_ID = "pool-boundary-target"

_POOL_BOUNDARY = {
    "memory_id": BOUNDARY_TARGET_ID,
    "content": "用户近来在关注边缘推理设备的功耗表现",
    "dimension_ids": ["focus"],
    "memory_type": "fact",
    "priority": 50,
    # 比边界填充旧（填充 200+）→ 维度侧排在填充之后，排名 ≈ 填充条数 + 1
    "updated_day": 100,
}
_BATCH_BOUNDARY = {
    "id": BOUNDARY_BATCH_ID,
    "note": "边界场景：同主体+信息增量，但目标候选被 cap 挤到 31..50 之间——"
            "50/50 可见（应判 update），30/30 不可见（将漏判为 store）",
    "expect": "update",
    "target": BOUNDARY_TARGET_ID,
    "new_memories": [{
        "content": "用户近来在关注边缘推理设备的功耗表现，重点看 7B 级模型的每瓦吞吐",
        "dimension_ids": ["focus"],
        "memory_type": "fact",
    }],
}

POOL.append(_POOL_BOUNDARY)
BATCHES.append(_BATCH_BOUNDARY)


def make_boundary_fillers(n: int) -> list[dict[str, Any]]:
    """边界填充：**术语高度相似、主体不同**（谈别人的报告，不是用户自己的设备）。

    双重作用：①`updated_at` 比目标新 → 维度侧 `updated_at DESC` 把它挤到第 n+1 位；
    ②语义近 → 也占掉向量 Top-K 的名额。二者合力把目标推出 cap=30、留在 cap=50。
    """
    return [{
        "memory_id": f"boundary-filler-{i:04d}",
        "content": (f"某份行业研究报告讨论了边缘推理设备的功耗表现与每瓦吞吐指标"
                    f"（样本 {i}，非用户自有设备）"),
        "dimension_ids": ["focus"],
        "memory_type": "fact",
        "priority": 20,
        "updated_day": 200 + i,
    } for i in range(n)]


POOL.extend(_build_families()[0])
BATCHES.extend(_build_families()[1])


def make_fillers(n: int, *, dimension: str = "environment") -> list[dict[str, Any]]:
    """生成 n 条**同维度、语义相近**的填充记忆（确定性，不经 LLM）。

    目的：把总候选数顶到 prescreen 上限之上，让截断真的发生（池太小时
    `dimension_top_n=50` 与 `=30` 都不裁任何东西，B2 的效果恒为 0，门禁失去意义）。

    填充条目的 `updated_at` **比池条目旧**，故在「按 updated_at DESC 取 top_n」下
    **不会**挤掉池条目（池条目恒在候选池内，expect 才可能达成）。

    Args:
        n: 填充条数（建议 ≥ 40）。
        dimension: 填充所用维度（与目标池条目共享候选池）。
    """
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append({
            "memory_id": f"filler-{i:04d}",
            # 语义上贴近「服务器 / 部署 / 目录」主题，使向量 Top-K 也容易挤进前列
            "content": (
                f"用户的第 {i} 台测试服务器上部署了一套示例服务，"
                f"日志目录位于示例路径示例子目录 {i}"
            ),
            "dimension_ids": [dimension],
            "memory_type": "fact",
            "priority": 20,
            # **比池条目更旧**（day 1..n 严格递增）→ 池条目在 updated_at DESC 下恒在前列、
            # 必然进候选池；填充只负责把总候选数顶到上限之上（截断落在填充区）
            "updated_day": 1 + (i % 60),
        })
    return out


def build_pool(fillers: int = 0, boundary_fillers: int = 0) -> list[dict[str, Any]]:
    """池 = POOL + 环境填充 + 边界填充。

    Args:
        fillers: 环境填充条数（`environment` 维度，把总候选数顶到上限之上）。
        boundary_fillers: 边界填充条数（`focus` 维度，把边界目标挤到 31..50 之间）。
    """
    return list(POOL) + make_fillers(fillers) + make_boundary_fillers(boundary_fillers)

