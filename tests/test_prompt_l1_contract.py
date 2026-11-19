"""L1 提炼提示词 2.0 结构契约（防「结构硬约束」再次丢失）。

背景（2026-11-07 缺陷）：v014 给 2.0 提示词加了「结构是硬性的」防回退约束——模型若
输出 1.x 平铺数组，`engine/l2_extract.py` 会静默委托旧解析，2.0 字段（facts/causes/
trait/dimensions/route/subject…）全部作废且不报错。但 v015 误从 v013 分叉，v014 的这
段内容在 v015~v019 一路缺失，登录版本 v019 也缺。

本测试对「v020 及以后」的每一个 l1_extraction 版本文件做契约断言：谁再丢掉这段约束，
测试立即红。历史版本（v001~v019）不在断言范围内，不改动。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "sgme" / "resources" / "prompts"
MANIFEST = PROMPTS_ROOT / "manifest.yaml"

# 结构硬约束的三个锚点（缺任一条即视为约束丢失）
HARD_ANCHORS = (
    "结构是硬性的，不是建议",
    "禁止**输出平铺数组",
    # 只锁「自检要确认这三个结构键」这件事，**不锁键的总数**：v024 起单元多出
    # `route_confidence`（分流置信度契约），正文如写"四个键"，写死"三个键"就会
    # 把「内容已正确演进」误判成「锚点丢失」。用前缀锚 + 下方独立断言共同约束。
    "自检：输出前逐个元素确认有 `main_event` / `dimensions` / `route`",
)
# v024 起分流置信度契约必填字段：定稿版本必须带上（换成更实质的检查，不是纯放宽）
REQUIRED_CONTRACT_FIELDS = ("route_confidence", "route_reason")
# 输出要求末尾的第 8 条
TAIL_ANCHOR = "禁止平铺数组——结构错了，2.0 字段全部作废"
# 过期状态行（v019 及更早的草案残留；v020 起禁止出现）
STALE_STATUS = "状态：草案，未登记 manifest"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


# T-411（2026-11-18，用户拍板「暂不定稿」）：manifest 版本条目可带 status 字段。
#   缺省（无该字段）/ "approved" = 已批准：参与一切契约断言。
#   "candidate" = 已登记未批准：只参与「登记与 sha 一致性」，不参与 active 判定与结构硬约束强制。
# 任何其它取值 → 判失败（防拼错把候选偷偷当批准）。
_ALLOWED_STATUS = (None, "approved", "candidate")


def _approved(items: list[dict]) -> list[dict]:
    """已批准版本 = 不带 candidate 标记者。"""
    return [i for i in items if i.get("status") != "candidate"]


def _assert_statuses_legal(items: list[dict]) -> None:
    for item in items:
        assert item.get("status") in _ALLOWED_STATUS, (
            f"{item['version']} 的 status={item.get('status')!r} 非法"
            f"（只允许缺省/approved/candidate）"
        )


def _versions_at_least(minimum: int) -> list[dict]:
    """返回版本号 >= minimum 的 l1_extraction 版本条目。"""
    versions = _manifest()["versions"]["l1_extraction"]
    out = []
    for item in versions:
        tag = item["version"]
        assert tag.startswith("v") and tag[1:].isdigit(), f"非法版本号 {tag}"
        if int(tag[1:]) >= minimum:
            out.append(item)
    return out


def test_v020_and_later_keep_structural_hard_constraint():
    """v020 起每个**已批准**版本都必须含 v014 的结构硬约束（正文 + 输出要求）。

    T-411 口径（2026-11-18）：`status: candidate` 的版本是「已登记未批准」的候选，
    允许在定稿前暂缺锚点句；一旦去掉 candidate（或把 candidate 设成 active）即恢复强制。
    —— 这不是放宽，而是把「未定稿」变成可判定数据：谁把候选当批准用，下面
    test_working_copy_matches_active 与两条 active 断言会立刻红。
    """
    all_versions = _versions_at_least(20)
    assert all_versions, "未找到 v020 及以后的 l1_extraction 版本"
    _assert_statuses_legal(all_versions)
    versions = _approved(all_versions)
    assert versions, "v020 及以后没有任何已批准版本"
    for item in versions:
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        for anchor in HARD_ANCHORS:
            assert anchor in text, f"{item['version']} 丢失结构硬约束锚点：{anchor}"
        assert TAIL_ANCHOR in text, f"{item['version']} 丢失输出要求第 8 条"
        # v024 起：分流置信度契约的两个必填字段必须在（定稿前必须有，未定稿也得有）
        if int(item["version"][1:]) >= 24:
            for field in REQUIRED_CONTRACT_FIELDS:
                assert field in text, (
                    f"{item['version']} 缺分流置信度契约字段 {field}——"
                    f"该版本宣称走 v024 契约却未要求模型输出它"
                )


def test_v020_and_later_no_stale_draft_status():
    """v020 起**已批准**版本不得再出现「草案、未激活」这类过期状态行。"""
    for item in _approved(_versions_at_least(20)):
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        assert STALE_STATUS not in text, f"{item['version']} 仍含过期状态行"


def test_v020_and_later_version_chain_mentions_v014():
    """头部版本链必须能溯源到 v014（分叉丢失的起点）——已批准版本强制。"""
    for item in _approved(_versions_at_least(20)):
        text = (PROMPTS_ROOT / item["file"]).read_text(encoding="utf-8")
        assert "# v014（2026-10-31）" in text, f"{item['version']} 版本链缺 v014"


def test_manifest_sha256_matches_v020_file():
    """manifest 登记的 v020 sha256 必须与文件一致（防手改文件后忘记重登记）。

    口径与 `PromptStore._lazy_verify_sha` 对齐：read_text 归一换行后按 UTF-8 计算文本 sha，
    避免 Windows autocrlf 把工作区转成 CRLF 时字节 sha 漂移造成误报。

    T-411：**候选版本同样要 sha 一致**（登记了就得是那份文件，只是暂不批准生效）。
    """
    for item in _versions_at_least(20):
        path = PROMPTS_ROOT / item["file"]
        digest = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        assert digest == item["sha256"], (
            f"{item['version']} sha256 不一致：manifest={item['sha256'][:12]} 实际={digest[:12]}"
        )


def test_working_copy_matches_active():
    """工作副本必须与 **active** 版本逐字节一致（防 @working 回退成旧提示词）。

    T-411（2026-11-18）口径变更：原断言写「工作副本 == 最新登记版本」，在「登记了新版本但
    用户决定暂不定稿」时会误红——那时工作副本应当等于**仍在服役的 active 版本**，而新版本
    只是 `status: candidate` 的候选。判定依据从「谁最新」改成「谁在服役」，强度不减：
    工作副本与 active 漂移仍然立刻红，且下面额外拦「active 竟然是候选」。
    """
    active = _manifest()["stages"]["l1_extraction"]["active"]
    versions = {v["version"]: v for v in _versions_at_least(1)}
    if active.startswith("@"):  # @working 形态：工作副本即真源，无可比对象
        return
    assert active in versions, f"active={active} 未在 manifest 登记"
    chosen = versions[active]
    # active 永不得是 candidate —— 否则「未批准」的提示词已经在生产服役
    assert chosen.get("status") != "candidate", (
        f"active={active} 被标为 candidate：未批准的版本不得服役（先定稿去标，或把 active 切回已批准版本）"
    )
    assert int(active[1:]) >= 20, (
        f"active={active} 低于 v020：v020 起才有结构硬约束契约，切回旧版需显式留痕"
    )
    active_text = (PROMPTS_ROOT / chosen["file"]).read_text(encoding="utf-8")
    working_text = (PROMPTS_ROOT / "l1_extraction.txt").read_text(encoding="utf-8")
    assert working_text == active_text, f"工作副本与 active（{active}）不一致"
