"""T-16 测试：sgme/operations/template.py 模板管理操作层（契约 §5.8）。

覆盖：
- 列表：内容/分页/坏模板容错/参数校验
- 写入：新建、更新、content 原文优先、结构化字段、原子写、CRLF 行尾保留
- 校验：非法 YAML → 400、维度越界 → 400、token 预算超限 → 400、name 不一致 → 400
- 冲突：重名 → ERR_CONFLICT
- 删除：内置拒绝 → 400、不存在 → ERR_NOT_FOUND、成功删除
- 安全：模板名路径穿越拒绝
- restart_required 语义：实测 load_template 热加载（写盘即生效）

隔离：所有用例把 ``sgme.profile.template.TEMPLATES_DIR`` monkeypatch 到 tmp_path，
真实 ``templates/`` 目录全程只读（仅在 fixture 里复制内置模板做样本）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sgme import config as sgme_config
from sgme.operations import template as tpl_ops
from sgme.operations.errors import ERR_NOT_FOUND, InvalidArgs
from sgme.profile import template as template_mod

# 真实内置模板源目录（只读复制样本用）
REAL_TEMPLATES_DIR = sgme_config.PROJECT_ROOT / "templates"
BUILTIN_NAMES = ("coding", "daily", "full", "work")


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def dimensions() -> list[dict]:
    """已注册维度（供 validate_template 校验 memory_types 已注册）。"""
    return sgme_config.load_config()["dimensions"]


@pytest.fixture
def tpl_dir(tmp_path, monkeypatch) -> Path:
    """隔离的模板目录：复制 4 个内置模板到 tmp_path，并改写 TEMPLATES_DIR 指向它。

    ⚠️ 必须 monkeypatch profile.template.TEMPLATES_DIR（而非 operations 内部常量）——
    operations/template.py::templates_dir() 每次现读该常量，profile 的 load_template
    也用它，二者同源才能保证「写进去的能被 load_template 读出来」这类用例成立。
    """
    d = tmp_path / "templates"
    d.mkdir()
    for n in BUILTIN_NAMES:
        shutil.copyfile(REAL_TEMPLATES_DIR / f"{n}.yaml", d / f"{n}.yaml")
    monkeypatch.setattr(template_mod, "TEMPLATES_DIR", d)
    return d


def _valid_template(name: str = "custom") -> dict:
    """一份结构合法的模板 body（结构化形态，Σ(limit)×30 = 240 ≤ 700）。

    注：projects/tasks 维度已移除（2026-08-18 三池重构），动态段改用
    goals/status（与生产模板 templates/work.yaml 现状同形）。
    """
    return {
        "name": name,
        "display_name": "自定义模式",
        "memory_types": ["identity", "goals", "status"],
        "token_budget": 700,
        "sections": [
            {
                "title": "👤 身份",
                "query": {"dimensions": ["identity"], "priority_min": 70, "limit": 5},
            },
            {
                "title": "🎯 目标与状态",
                "query": {
                    "dimensions": ["goals", "status"],
                    "match": "any",
                    "sort": "updated_at DESC",
                    "limit": 3,
                },
            },
        ],
    }


def _valid_yaml(name: str = "custom", display: str = "自定义模式") -> str:
    """同上内容的 YAML 全文形态（带注释，用于验证 content 原文写盘保留注释）。"""
    return (
        "# 自定义模板（注释应在原文写盘后保留）\n"
        f"name: {name}\n"
        f"display_name: {display}\n"
        "memory_types: [identity, goals, status]\n"
        "token_budget: 700\n"
        "sections:\n"
        '  - title: "👤 身份"\n'
        "    query:\n"
        "      dimensions: [identity]\n"
        "      priority_min: 70\n"
        "      limit: 5\n"
    )


# ---------- 列表 ----------

def test_list_returns_builtin_templates_with_content(tpl_dir, dimensions):
    """§5.8.1：items 含 name/display_name/memory_types/token_budget/sections/content。"""
    res = tpl_ops.list_templates(dimensions=dimensions)
    assert res.ok is True
    data = res.data
    assert data["total"] == 4
    assert data["count"] == 4
    assert data["generated_at"].endswith("Z")

    names = [i["name"] for i in data["items"]]
    assert names == sorted(BUILTIN_NAMES)  # 字典序，确定性分页前提

    daily = next(i for i in data["items"] if i["name"] == "daily")
    assert daily["display_name"] == "日常模式"
    assert daily["token_budget"] == 700
    assert "identity" in daily["memory_types"]
    assert daily["builtin"] is True
    assert daily["valid"] is True
    assert daily["error"] is None
    # content 为原始 YAML 全文（编辑回填用）
    assert daily["content"].startswith("name: daily")
    assert yaml.safe_load(daily["content"])["name"] == "daily"


def test_list_section_view_has_flat_keys_and_raw_query(tpl_dir, dimensions):
    """section 同时给契约扁平键与保真 query 子对象（往返不丢 ttl_filter/time_window）。"""
    res = tpl_ops.list_templates(dimensions=dimensions)
    daily = next(i for i in res.data["items"] if i["name"] == "daily")

    s0 = daily["sections"][0]
    assert s0["title"] == "👤 基本信息"
    assert s0["dimensions"] == ["identity", "family"]
    assert s0["limit"] == 5
    assert s0["priority_min"] == 70
    assert s0["query"] == {"dimensions": ["identity", "family"], "priority_min": 70, "limit": 5}

    s2 = daily["sections"][2]  # 🔥 当前状态：带 ttl_filter + sort + match
    assert s2["match"] == "any"
    assert s2["sort"] == "updated_at DESC"
    assert s2["ttl_filter"] is True


def test_list_pagination_limit_offset(tpl_dir, dimensions):
    """limit/offset 分页：count 为本页条数，total 恒为总数。"""
    page1 = tpl_ops.list_templates(dimensions=dimensions, limit=2, offset=0).data
    page2 = tpl_ops.list_templates(dimensions=dimensions, limit=2, offset=2).data

    assert page1["count"] == 2 and page1["total"] == 4
    assert page2["count"] == 2 and page2["total"] == 4
    assert [i["name"] for i in page1["items"]] == ["coding", "daily"]
    assert [i["name"] for i in page2["items"]] == ["full", "work"]

    beyond = tpl_ops.list_templates(dimensions=dimensions, limit=50, offset=99).data
    assert beyond["count"] == 0 and beyond["total"] == 4


def test_list_invalid_pagination_raises_invalid_args(tpl_dir, dimensions):
    """limit<1 / offset<0 → InvalidArgs（入口层 400）。"""
    with pytest.raises(InvalidArgs):
        tpl_ops.list_templates(dimensions=dimensions, limit=0)
    with pytest.raises(InvalidArgs):
        tpl_ops.list_templates(dimensions=dimensions, offset=-1)


def test_list_tolerates_broken_template(tpl_dir, dimensions):
    """坏模板不拖垮整个列表：该条 valid=false + error，其余照常返回。"""
    (tpl_dir / "broken.yaml").write_text("name: broken\n  bad: [unclosed\n", encoding="utf-8")

    res = tpl_ops.list_templates(dimensions=dimensions)
    assert res.ok is True
    assert res.data["total"] == 5

    broken = next(i for i in res.data["items"] if i["name"] == "broken")
    assert broken["valid"] is False
    assert broken["error"]
    assert broken["content"]  # 仍给原文，编辑器可修复
    assert all(i["valid"] for i in res.data["items"] if i["name"] != "broken")


# ---------- 新建 ----------

def test_create_writes_file_and_reports_restart_not_required(tpl_dir, dimensions):
    """§5.8.3：新建成功 → {created, name, restart_required}，文件真实落盘。"""
    res = tpl_ops.create_template("custom", _valid_template("custom"), dimensions)
    assert res.ok is True
    assert res.data["created"] is True
    assert res.data["name"] == "custom"
    assert res.data["restart_required"] is False

    path = tpl_dir / "custom.yaml"
    assert path.exists()
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert on_disk["name"] == "custom"
    assert on_disk["sections"][0]["query"]["dimensions"] == ["identity"]


def test_create_from_yaml_content_preserves_comments(tpl_dir, dimensions):
    """content（YAML 全文）为写入源时原文落盘，保留注释与排版。"""
    res = tpl_ops.create_template("custom", {"content": _valid_yaml("custom")}, dimensions)
    assert res.ok is True

    text = (tpl_dir / "custom.yaml").read_text(encoding="utf-8")
    assert text.startswith("# 自定义模板")
    assert "memory_types: [identity, goals, status]" in text  # 流式写法未被重排


def test_create_infers_name_from_body_when_not_given(tpl_dir, dimensions):
    """POST 无路径参数：模板名从 body.name 推断。"""
    res = tpl_ops.create_template(None, _valid_template("inferred"), dimensions)
    assert res.ok is True
    assert res.data["name"] == "inferred"
    assert (tpl_dir / "inferred.yaml").exists()


def test_create_duplicate_returns_conflict(tpl_dir, dimensions):
    """§5.8.3：重名 → ERR_CONFLICT（入口层 409），且不覆盖既有文件。"""
    before = (tpl_dir / "daily.yaml").read_bytes()

    res = tpl_ops.create_template("daily", _valid_template("daily"), dimensions)
    assert res.ok is False
    assert res.error_code == tpl_ops.ERR_CONFLICT
    assert "已存在" in res.message
    assert (tpl_dir / "daily.yaml").read_bytes() == before  # 未被改写


# ---------- 更新 ----------

def test_update_overwrites_existing(tpl_dir, dimensions):
    """§5.8.2：更新成功 → {saved: true, restart_required: false}。"""
    body = _valid_template("daily")
    body["display_name"] = "改过的日常"

    res = tpl_ops.update_template("daily", body, dimensions)
    assert res.ok is True
    assert res.data["saved"] is True
    assert res.data["restart_required"] is False

    on_disk = yaml.safe_load((tpl_dir / "daily.yaml").read_text(encoding="utf-8"))
    assert on_disk["display_name"] == "改过的日常"


def test_update_accepts_flat_sections_and_normalizes(tpl_dir, dimensions):
    """扁平 section（契约 §5.8.1 示例形态）写入时归一为嵌套 query，保证可被 load_template 读回。"""
    body = {
        "name": "flat",
        "display_name": "扁平段",
        "memory_types": ["identity"],
        "token_budget": 700,
        "sections": [{"title": "T", "dimensions": ["identity"], "limit": 4}],
    }
    res = tpl_ops.update_template("flat", body, dimensions)
    assert res.ok is True

    on_disk = yaml.safe_load((tpl_dir / "flat.yaml").read_text(encoding="utf-8"))
    assert on_disk["sections"][0]["query"] == {"dimensions": ["identity"], "limit": 4}
    # 关键：能被既有加载器读回
    loaded = template_mod.load_template("flat", dimensions)
    assert loaded["display_name"] == "扁平段"


# ---------- 校验失败（均为 400 ERR_INVALID_ARGS） ----------

def test_invalid_yaml_content_rejected(tpl_dir, dimensions):
    """非法 YAML → InvalidArgs（400），且不落盘。"""
    with pytest.raises(InvalidArgs) as ei:
        tpl_ops.update_template("bad", {"content": "name: bad\n  oops: [unclosed\n"}, dimensions)
    assert "YAML 解析失败" in ei.value.message
    assert not (tpl_dir / "bad.yaml").exists()


def test_yaml_scalar_top_level_rejected(tpl_dir, dimensions):
    """YAML 顶层非字典 → InvalidArgs。"""
    with pytest.raises(InvalidArgs):
        tpl_ops.update_template("bad", {"content": "just a string\n"}, dimensions)


def test_section_dimension_out_of_memory_types_rejected(tpl_dir, dimensions):
    """校验规则：section.dimensions ⊆ memory_types，越界 → 400 且 message 带详情。"""
    body = _valid_template("oob")
    body["memory_types"] = ["identity"]
    # goals 是已注册维度但不在 memory_types → 纯「越界」（非未注册）
    body["sections"] = [{"title": "T", "query": {"dimensions": ["goals"], "limit": 3}}]

    with pytest.raises(InvalidArgs) as ei:
        tpl_ops.update_template("oob", body, dimensions)
    assert "模板校验失败" in ei.value.message
    assert "越界" in ei.value.message
    assert not (tpl_dir / "oob.yaml").exists()


def test_unregistered_dimension_rejected(tpl_dir, dimensions):
    """校验规则：memory_types 必须全部为已注册维度。"""
    body = _valid_template("unreg")
    body["memory_types"] = ["not_a_real_dimension"]
    body["sections"] = [
        {"title": "T", "query": {"dimensions": ["not_a_real_dimension"], "limit": 3}}
    ]

    with pytest.raises(InvalidArgs) as ei:
        tpl_ops.update_template("unreg", body, dimensions)
    assert "未注册维度" in ei.value.message


def test_token_budget_exceeded_rejected(tpl_dir, dimensions):
    """校验规则：Σ(limit) × AVG_ITEM_TOKENS ≤ token_budget。"""
    body = _valid_template("overbudget")
    body["token_budget"] = 100  # 5+3=8 段限额 × 30 = 240 > 100
    with pytest.raises(InvalidArgs) as ei:
        tpl_ops.update_template("overbudget", body, dimensions)
    assert "token 预算超限" in ei.value.message


def test_name_mismatch_rejected(tpl_dir, dimensions):
    """§5.8.2：body.name 必须与路径一致。"""
    with pytest.raises(InvalidArgs) as ei:
        tpl_ops.update_template("path_name", _valid_template("other_name"), dimensions)
    assert "不一致" in ei.value.message


def test_empty_payload_rejected(tpl_dir, dimensions):
    """空 body → 400。"""
    with pytest.raises(InvalidArgs):
        tpl_ops.update_template("empty", {}, dimensions)


@pytest.mark.parametrize(
    "bad_name",
    ["../evil", "a/b", "a\\b", "", "   ", ".hidden", "x" * 65],
)
def test_path_traversal_and_bad_names_rejected(tpl_dir, dimensions, bad_name):
    """安全：模板名白名单拒绝路径穿越与非法字符（写盘目标不可越出 templates/）。"""
    with pytest.raises(InvalidArgs):
        tpl_ops.update_template(bad_name, _valid_template("whatever"), dimensions)
    # 目录内不得多出任何文件
    assert sorted(p.stem for p in tpl_dir.glob("*.yaml")) == sorted(BUILTIN_NAMES)


# ---------- 删除 ----------

@pytest.mark.parametrize("name", BUILTIN_NAMES)
def test_delete_builtin_rejected(tpl_dir, name):
    """§5.8.4：内置 4 模板拒绝删除 → 400，文件仍在。"""
    with pytest.raises(InvalidArgs) as ei:
        tpl_ops.delete_template(name)
    assert "内置模板不可删" in ei.value.message
    assert (tpl_dir / f"{name}.yaml").exists()


def test_delete_missing_returns_not_found(tpl_dir):
    """§5.8.4：不存在 → ERR_NOT_FOUND（入口层 404）。"""
    res = tpl_ops.delete_template("no_such_template")
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND


def test_delete_custom_template_succeeds(tpl_dir, dimensions):
    """§5.8.4：删除自定义模板 → {deleted: true}，文件消失。"""
    tpl_ops.create_template("disposable", _valid_template("disposable"), dimensions)
    assert (tpl_dir / "disposable.yaml").exists()

    res = tpl_ops.delete_template("disposable")
    assert res.ok is True
    assert res.data["deleted"] is True
    assert not (tpl_dir / "disposable.yaml").exists()


# ---------- 写盘特性：原子性 / 行尾 / 热加载 ----------

def test_atomic_write_leaves_no_temp_file(tpl_dir, dimensions):
    """原子写：落盘后目录内不得残留临时文件（.<name>.yaml.tmp）。"""
    tpl_ops.create_template("atomic", _valid_template("atomic"), dimensions)

    leftovers = [p.name for p in tpl_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == []
    assert (tpl_dir / "atomic.yaml").exists()


def test_atomic_write_keeps_old_file_when_validation_fails(tpl_dir, dimensions):
    """校验在写盘之前：非法内容不得留下半写文件，也不得破坏既有模板。"""
    before = (tpl_dir / "daily.yaml").read_bytes()

    bad = _valid_template("daily")
    bad["token_budget"] = 1  # 必然超预算
    with pytest.raises(InvalidArgs):
        tpl_ops.update_template("daily", bad, dimensions)

    assert (tpl_dir / "daily.yaml").read_bytes() == before
    assert [p.name for p in tpl_dir.iterdir() if p.name.startswith(".")] == []


def test_crlf_line_endings_preserved_on_rewrite(tpl_dir, dimensions):
    """行尾保留：CRLF 的既有模板回写后仍是 CRLF（仓库 core.autocrlf=true，避免整文件 diff）。"""
    path = tpl_dir / "daily.yaml"
    path.write_bytes(b"name: daily\r\ndisplay_name: X\r\nmemory_types: [identity]\r\n"
                     b"token_budget: 700\r\nsections:\r\n  - title: T\r\n    query:\r\n"
                     b"      dimensions: [identity]\r\n      limit: 3\r\n")
    assert b"\r\n" in path.read_bytes()

    body = _valid_template("daily")
    res = tpl_ops.update_template("daily", body, dimensions)
    assert res.ok is True

    data = path.read_bytes()
    assert b"\r\n" in data
    # 行尾必须干净：无 \r\r（双写）、无裸 LF（每个 \n 都属于某个 \r\n）
    assert b"\r\r" not in data
    assert data.count(b"\n") == data.count(b"\r\n")


def test_lf_line_endings_preserved_on_rewrite(tpl_dir, dimensions):
    """行尾保留（反向）：LF 文件回写后不得被改成 CRLF。"""
    path = tpl_dir / "lftpl.yaml"
    path.write_bytes(b"name: lftpl\ndisplay_name: X\nmemory_types: [identity]\n"
                     b"token_budget: 700\nsections:\n  - title: T\n    query:\n"
                     b"      dimensions: [identity]\n      limit: 3\n")

    body = _valid_template("lftpl")
    assert tpl_ops.update_template("lftpl", body, dimensions).ok is True
    assert b"\r\n" not in path.read_bytes()


def test_written_template_is_hot_reloaded_by_load_template(tpl_dir, dimensions):
    """restart_required=false 的实测依据：写盘后 load_template 立即读到新内容。"""
    first = _valid_template("hot")
    first["display_name"] = "第一版"
    tpl_ops.create_template("hot", first, dimensions)
    assert template_mod.load_template("hot", dimensions)["display_name"] == "第一版"

    second = _valid_template("hot")
    second["display_name"] = "第二版"
    res = tpl_ops.update_template("hot", second, dimensions)
    assert res.data["restart_required"] is False
    # 同进程内、无重启，直接读到新值 → 证明无缓存，restart_required 填 false 正确
    assert template_mod.load_template("hot", dimensions)["display_name"] == "第二版"


def test_roundtrip_list_then_update_with_returned_content(tpl_dir, dimensions):
    """往返：GET 拿 content → 原样 PUT 回去 → 文件内容不变（编辑回填闭环）。"""
    listed = tpl_ops.list_templates(dimensions=dimensions).data
    daily = next(i for i in listed["items"] if i["name"] == "daily")
    before = (tpl_dir / "daily.yaml").read_bytes()

    res = tpl_ops.update_template("daily", daily, dimensions)
    assert res.ok is True
    assert (tpl_dir / "daily.yaml").read_bytes() == before
