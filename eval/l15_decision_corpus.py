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
        "memory_id": "pool-0001",
        "content": "用户的家庭服务器上部署了 SGME 记忆引擎",
        "dimension_ids": ["environment"],
        "memory_type": "fact",
        "priority": 60,
        "updated_day": 61,
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
        "note": "同主体（家庭服务器部署）+ 明确信息增量 → 应覆盖",
        "expect": "update",
        "new_memories": [
            {
                "content": "用户的家庭服务器上部署了 SGME 记忆引擎，并配置了每日自动备份、备份保留 7 份",
                "dimension_ids": ["environment"],
                "memory_type": "fact",
            }
        ],
    },
    {
        "id": "merge_complement",
        "note": "同主体（主力开发机）+ 互补片段 → 应合并",
        "expect": "merge",
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
        "new_memories": [
            {
                "content": r"用户的样例项目目录结构调整为 C:\work\demo 下的 src / tests / docs 三个子目录",
                "dimension_ids": ["environment"],
                "memory_type": "fact",
            }
        ],
    },
]


def make_fillers(n: int, *, dimension: str = "environment") -> list[dict[str, Any]]:
    """生成 n 条**同维度、语义相近**的填充记忆（确定性，不经 LLM）。

    目的：让 prescreen 上限真的卡住 —— 池只有 8 条时，`dimension_top_n=50` 与 `=30`
    都不裁任何东西，B2（50→30）的效果恒为 0，门禁就失去意义。填充条目的
    `updated_at` **比 POOL 旧**，故在「按 updated_at DESC 取 top_n」下**不会**挤掉池条目
    （池条目恒在候选池内，expect 才可能达成）；填充的作用是把总候选数顶到上限之上，
    让截断真的发生。

    ⚠️ 未覆盖的场景（T-298 待补）：**「相关候选恰好落在 30 与 50 之间」的边界场景**——
    那才是 B2 真正的风险面（收紧阈值可能丢掉相关候选）。它需要对
    `updated_at` 排名与 cap 的位置做实测标定，无法凭手写盲配（向量 Top-K 也会并进来），
    故单列为 T-298 的标定项。

    Args:
        n: 填充条数（建议 ≥ 40，使 30 与 50 的截断边界落在池条目之间）。
        dimension: 填充所用维度（须与目标池条目共享，否则挤不掉）。
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


def build_pool(fillers: int = 0) -> list[dict[str, Any]]:
    """池 = POOL + 填充（填充的 updated_at 更新，故在 top_n 截断里优先占据名额）。"""
    return list(POOL) + make_fillers(fillers)
