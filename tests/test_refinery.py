"""refinery 知识提炼引擎测试（v0.7 §9）。

覆盖：
- ingest：文本透传 / 文件读取 / URL mock / 异常（不存在文件 → IngestError）
- extract：成功 / JSON 解析失败重试 / 重试耗尽抛 ExtractError / schema 校验失败
- validate：规则通过 / 失败 / 自定义规则
- output：RefineryResult → wiki_page 映射
- refine()：端到端（mock extract）

LLM 调用全部 mock sgme.llm.chain.call_with_fallback，不实际联网；
URL 拉取 mock httpx.get。
"""

from __future__ import annotations

import importlib
import json

import pytest

from sgme.refinery import DEFAULT_SCHEMA, refine
from sgme.refinery.extract import ExtractError, extract, parse_json_output, validate_schema
from sgme.refinery.ingest import IngestError, ingest
from sgme.refinery.output import RefineryResult, to_wiki_page
from sgme.refinery.validate import ValidationReport, max_length, min_length, non_empty, register_rule, validate

# 子模块对象（包 __init__ 的函数同名导出会遮蔽包属性，必须经 sys.modules 取模块）
ingest_mod = importlib.import_module("sgme.refinery.ingest")
validate_mod = importlib.import_module("sgme.refinery.validate")
refinery_pkg = importlib.import_module("sgme.refinery")

# 固定模型配置（mock 下不真实联网）
FAKE_MODEL_CFG = {"chains": {"refinement": []}, "rules": {}}

SCHEMA = {"title": str, "content": str, "tags": list, "category": str}


def _valid_json() -> str:
    """合法的提取结果 JSON 串。"""
    return json.dumps(
        {"title": "测试标题", "content": "这是一段足够长的正文内容，用于通过质量门校验。", "tags": ["测试"], "category": "知识"},
        ensure_ascii=False,
    )


# ==================== ingest ====================


class TestIngest:
    """输入处理：文本透传 / 文件 / URL / 异常。"""

    def test_text_passthrough(self):
        """纯文本直接透传，元数据 source_type=text。"""
        text, meta = ingest("你好，世界")
        assert text == "你好，世界"
        assert meta["source_type"] == "text"
        assert meta["source_file"] is None and meta["source_url"] is None

    def test_file_md(self, tmp_path):
        """读取本地 md 文件。"""
        p = tmp_path / "note.md"
        p.write_text("# 标题\n\n正文内容", encoding="utf-8")
        text, meta = ingest(str(p))
        assert text == "# 标题\n\n正文内容"
        assert meta["source_type"] == "file"
        assert meta["title"] == "note"
        assert meta["source_file"] == str(p)

    def test_file_txt(self, tmp_path):
        """读取本地 txt 文件。"""
        p = tmp_path / "note.txt"
        p.write_text("纯文本文件", encoding="utf-8")
        text, meta = ingest(str(p))
        assert text == "纯文本文件"
        assert meta["source_type"] == "file"

    def test_file_not_found(self):
        """不存在的文件路径 → IngestError。"""
        with pytest.raises(IngestError, match="文件不存在"):
            ingest("C:/definitely/not/exist.md")

    def test_pdf_not_implemented(self, tmp_path):
        """pdf 预留接口 → NotImplementedError 标注后续。"""
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4")
        with pytest.raises(NotImplementedError, match="PDF"):
            ingest(str(p))

    def test_unsupported_extension(self, tmp_path):
        """未知扩展名 → IngestError。"""
        p = tmp_path / "data.xyz"
        p.write_text("x", encoding="utf-8")
        with pytest.raises(IngestError, match="不支持的文件类型"):
            ingest(str(p))

    def test_url_success(self, monkeypatch):
        """URL 拉取：mock httpx.get 返回 HTML → markdown + 元数据。"""
        html = (
            "<html><head><title>测试页面</title></head><body>"
            "<h1>大标题</h1><p>第一段</p><ul><li>条目一</li><li>条目二</li></ul>"
            "<a href='https://example.com/x'>链接</a></body></html>"
        )

        class FakeResp:
            status_code = 200
            text = html

        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            assert kwargs.get("trust_env") is False  # 项目铁律：禁系统代理劫持
            return FakeResp()

        monkeypatch.setattr(ingest_mod.httpx, "get", fake_get)
        text, meta = ingest("https://example.com/page")
        assert meta["source_type"] == "url"
        assert meta["title"] == "测试页面"
        assert meta["source_url"] == "https://example.com/page"
        assert "# 大标题" in text
        assert "第一段" in text
        assert "- 条目一" in text
        assert "[链接](https://example.com/x)" in text
        assert calls[0][0] == "https://example.com/page"

    def test_url_http_error(self, monkeypatch):
        """URL 非 2xx → IngestError。"""

        class FakeResp:
            status_code = 404
            text = "not found"

        monkeypatch.setattr(ingest_mod.httpx, "get", lambda url, **kwargs: FakeResp())
        with pytest.raises(IngestError, match="404"):
            ingest("https://example.com/missing")

    def test_url_network_error(self, monkeypatch):
        """URL 网络异常 → IngestError。"""
        import httpx

        def boom(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(ingest_mod.httpx, "get", boom)
        with pytest.raises(IngestError, match="URL 拉取失败"):
            ingest("https://example.com/down")


# ==================== extract ====================


class TestExtract:
    """LLM 提取：成功 / 重试 / 重试耗尽 / schema 校验。"""

    def _patch_chain(self, monkeypatch, responses):
        """mock call_with_fallback 依次返回 responses 中的文本；记录调用次数。"""
        calls = {"n": 0}

        def fake_call(cfg, prompt, chain_name="refinement", client=None):
            calls["n"] += 1
            idx = min(calls["n"] - 1, len(responses) - 1)
            return responses[idx], "mock", {}

        monkeypatch.setattr("sgme.llm.chain.call_with_fallback", fake_call)
        return calls

    def test_success(self, monkeypatch):
        """正常返回 → 解析 JSON + schema 校验通过。"""
        calls = self._patch_chain(monkeypatch, [_valid_json()])
        data = extract("提取", SCHEMA, FAKE_MODEL_CFG)
        assert data["title"] == "测试标题"
        assert data["tags"] == ["测试"]
        assert calls["n"] == 1

    def test_json_fence(self, monkeypatch):
        """带 ```json 代码围栏的输出也能解析。"""
        self._patch_chain(monkeypatch, ["```json\n" + _valid_json() + "\n```"])
        data = extract("提取", SCHEMA, FAKE_MODEL_CFG)
        assert data["title"] == "测试标题"

    def test_parse_retry_success(self, monkeypatch):
        """首次 JSON 解析失败 → 自动重试 → 第二次成功。"""
        calls = self._patch_chain(monkeypatch, ["不是JSON", _valid_json()])
        data = extract("提取", SCHEMA, FAKE_MODEL_CFG)
        assert data["title"] == "测试标题"
        assert calls["n"] == 2

    def test_retry_exhausted_raises(self, monkeypatch):
        """重试耗尽仍解析失败 → ExtractError。"""
        calls = self._patch_chain(monkeypatch, ["坏输出1", "坏输出2", "坏输出3"])
        with pytest.raises(ExtractError, match="重试 3 次"):
            extract("提取", SCHEMA, FAKE_MODEL_CFG)
        assert calls["n"] == 3  # 恰好 3 次，不超限

    def test_schema_missing_key_retry(self, monkeypatch):
        """缺键 → 重试；第二次补全 → 成功。"""
        bad = json.dumps({"title": "只有标题"}, ensure_ascii=False)
        calls = self._patch_chain(monkeypatch, [bad, _valid_json()])
        data = extract("提取", SCHEMA, FAKE_MODEL_CFG)
        assert data["content"]
        assert calls["n"] == 2

    def test_schema_type_error_exhausted(self, monkeypatch):
        """类型不符且重试耗尽 → ExtractError（带 schema 错误信息）。"""
        wrong = json.dumps({"title": 123, "content": "x" * 100, "tags": "not-list", "category": "c"}, ensure_ascii=False)
        calls = self._patch_chain(monkeypatch, [wrong, wrong, wrong])
        with pytest.raises(ExtractError) as ei:
            extract("提取", SCHEMA, FAKE_MODEL_CFG)
        assert "title" in str(ei.value)
        assert calls["n"] == 3

    def test_parse_json_output_plain(self):
        """parse_json_output：纯 JSON 文本。"""
        assert parse_json_output('{"a": 1}') == {"a": 1}

    def test_parse_json_output_fenced(self):
        """parse_json_output：代码围栏包裹。"""
        assert parse_json_output('```json\n{"a": 1}\n```') == {"a": 1}

    def test_parse_json_output_invalid(self):
        """parse_json_output：无效文本 → JSONDecodeError。"""
        import json as _json

        with pytest.raises(_json.JSONDecodeError):
            parse_json_output("随便说点什么")

    def test_validate_schema_ok(self):
        """validate_schema：通过返回空列表。"""
        assert validate_schema({"title": "t", "content": "c", "tags": [], "category": "知识"}, SCHEMA) == []

    def test_validate_schema_errors(self):
        """validate_schema：缺键与类型错都报出来。"""
        errors = validate_schema({"title": 1}, SCHEMA)
        assert any("缺失字段: content" in e for e in errors)
        assert any("title" in e and "类型不符" in e for e in errors)

    def test_validate_schema_nullable(self):
        """validate_schema：可空字段 (str, None) 接受 None。"""
        schema = {"category": (str, type(None))}
        assert validate_schema({"category": None}, schema) == []
        assert validate_schema({"category": "知识"}, schema) == []


# ==================== validate ====================


class TestValidate:
    """质量门：内置规则 / 失败 / 自定义规则。"""

    def test_default_rules_pass(self):
        """默认规则（非空 + 最小长度）通过。"""
        report = validate("这是一段足够长的内容。" * 10)
        assert report.ok
        assert report.passed and not report.failed

    def test_empty_fails(self):
        """空内容 → 非空规则失败。"""
        report = validate("   ")
        assert not report.ok
        assert any(f["rule"] == "non_empty" for f in report.failed)

    def test_short_fails(self):
        """过短内容 → min_length 失败并带原因。"""
        report = validate("短")
        assert not report.ok
        assert any(f["rule"] == "min_length_50" for f in report.failed)

    def test_custom_rule_inline(self):
        """显式传入自定义规则（含关键词）。"""
        def must_mention_keyword(content):
            return "SGME" in content or (False, "缺少关键词 SGME")

        report = validate("SGME 记忆引擎正文内容", rules=[non_empty, must_mention_keyword])
        assert report.ok
        assert "must_mention_keyword" in report.passed

        bad = validate("没有关键词的正文内容", rules=[non_empty, must_mention_keyword])
        assert not bad.ok
        assert bad.failed[0]["rule"] == "must_mention_keyword"
        assert "SGME" in bad.failed[0]["reason"]

    def test_custom_rule_registry(self, monkeypatch):
        """全局注册自定义规则 → validate 自动合并执行。"""
        monkeypatch.setattr(validate_mod, "_CUSTOM_RULES", {})
        register_rule("has_markdown", lambda c: "#" in c or (False, "缺少 Markdown 标题"))

        report = validate("# 标题\n正文内容", rules=[non_empty, min_length(5)])
        assert report.ok
        assert "has_markdown" in report.passed

        bad = validate("正文内容无标题", rules=[non_empty, min_length(5)])
        assert not bad.ok
        assert any(f["rule"] == "has_markdown" for f in bad.failed)
        assert any(f["rule"] == "has_markdown" and "Markdown" in f["reason"] for f in bad.failed)

    def test_rule_exception_counts_as_fail(self):
        """规则内部抛异常 → 记为失败而非崩溃。"""

        def broken(content):
            raise RuntimeError("规则炸了")

        report = validate("正常内容" * 10, rules=[broken])
        assert not report.ok
        assert "规则执行异常" in report.failed[0]["reason"]

    def test_factories(self):
        """min_length / max_length 工厂。"""
        assert validate("a" * 10, rules=[min_length(5)]).ok
        assert not validate("a" * 10, rules=[min_length(20)]).ok
        assert validate("a" * 10, rules=[max_length(20)]).ok
        assert not validate("a" * 10, rules=[max_length(5)]).ok

    def test_report_shape(self):
        """ValidationReport 结构：passed/failed/ok/summary。"""
        report = validate("", rules=[non_empty])
        assert isinstance(report, ValidationReport)
        assert report.failed[0]["rule"] == "non_empty"
        assert "校验失败" in report.summary()


# ==================== output ====================


class TestOutput:
    """统一产出格式：RefineryResult → wiki_pages 行。"""

    def _ok_result(self) -> RefineryResult:
        return RefineryResult(
            ok=True,
            source_type="url",
            title="SGME 架构",
            content="正文内容，" * 30,
            tags=["架构", "v0.7"],
            category="技术",
        )

    def test_to_wiki_page_mapping(self):
        """字段对齐 wiki_pages 表。"""
        row = to_wiki_page(self._ok_result(), source_url="https://example.com/sgme", source_file=None)
        assert row["title"] == "SGME 架构"
        assert row["category"] == "技术"
        assert json.loads(row["tags"]) == ["架构", "v0.7"]  # tags 为 JSON 数组串
        assert row["source_type"] == "url"
        assert row["source_url"] == "https://example.com/sgme"
        assert row["source_file"] is None
        assert row["content_seg"] is None
        assert row["ingested_at"] and row["updated_at"] == row["ingested_at"]
        assert row["page_id"].startswith("sgme-架构-")

    def test_to_wiki_page_custom_id_and_time(self):
        """自定义 page_id 与 ingested_at 生效。"""
        row = to_wiki_page(
            self._ok_result(),
            page_id="custom-123",
            source_file="raw/text/abc.md",
            ingested_at="2026-08-08T00:00:00+00:00",
        )
        assert row["page_id"] == "custom-123"
        assert row["source_file"] == "raw/text/abc.md"
        assert row["ingested_at"] == "2026-08-08T00:00:00+00:00"

    def test_to_wiki_page_failure_raises(self):
        """失败结果不能产出 wiki 页 → ValueError。"""
        with pytest.raises(ValueError, match="失败结果"):
            to_wiki_page(RefineryResult.failure("url", "提取失败"))

    def test_failure_helper(self):
        """failure 构造器：ok=False + error。"""
        r = RefineryResult.failure("file", "文件不存在")
        assert not r.ok
        assert r.error == "文件不存在"
        assert r.source_type == "file"


# ==================== refine 端到端 ====================


class TestRefine:
    """refine() 统一入口：mock extract 层，全链路不联网。"""

    def test_end_to_end_text(self, monkeypatch):
        """文本 → 提炼成功 → RefineryResult 字段齐全。"""
        monkeypatch.setattr(
            refinery_pkg, "extract",
            lambda prompt, schema, cfg, client=None: {
                "title": "端到端标题",
                "content": "端到端正文，" * 20,
                "tags": ["e2e"],
                "category": None,
            },
        )
        result = refine("某段原始材料文本")
        assert result.ok
        assert result.source_type == "text"
        assert result.title == "端到端标题"
        assert result.tags == ["e2e"]
        assert result.error is None
        # 产物可直接转 wiki_pages 行
        assert to_wiki_page(result)["page_id"].startswith("端到端标题-")

    def test_ingest_error_becomes_failure(self):
        """文件不存在 → ok=False 携带错误，不抛异常。"""
        result = refine("C:/definitely/not/exist.md")
        assert not result.ok
        assert "文件不存在" in result.error

    def test_extract_error_becomes_failure(self, monkeypatch):
        """extract 抛 ExtractError → ok=False。"""
        def boom(prompt, schema, cfg, client=None):
            raise ExtractError("模型输出不可用")

        monkeypatch.setattr(refinery_pkg, "extract", boom)
        result = refine("材料文本")
        assert not result.ok
        assert "模型输出不可用" in result.error

    def test_validate_failure_becomes_failure(self, monkeypatch):
        """内容过短未过质量门 → ok=False。"""
        monkeypatch.setattr(
            refinery_pkg, "extract",
            lambda prompt, schema, cfg, client=None: {
                "title": "短标题",
                "content": "太短",
                "tags": [],
                "category": None,
            },
        )
        result = refine("材料文本")
        assert not result.ok
        assert "质量校验未通过" in result.error

    def test_custom_schema_and_prompt(self, monkeypatch):
        """自定义 prompt/schema 透传给 extract。"""
        captured = {}

        def fake_extract(prompt, schema, cfg, client=None):
            captured["prompt"] = prompt
            captured["schema"] = schema
            return {"title": "t", "content": "正文内容" * 30, "tags": [], "category": None}

        monkeypatch.setattr(refinery_pkg, "extract", fake_extract)
        refine(
            "材料",
            prompt="自定义提示：{content}",
            output_schema={"title": str, "content": str, "tags": list},
            model_cfg=FAKE_MODEL_CFG,
        )
        assert "自定义提示：材料" in captured["prompt"]  # {content} 占位符已渲染
        assert captured["schema"] == {"title": str, "content": str, "tags": list}

    def test_default_prompt_contains_material(self, monkeypatch):
        """缺省 prompt 会渲染 {content} 占位符。"""
        captured = {}

        def fake_extract(prompt, schema, cfg, client=None):
            captured["prompt"] = prompt
            return {"title": "t", "content": "正文内容" * 30, "tags": [], "category": None}

        monkeypatch.setattr(refinery_pkg, "extract", fake_extract)
        refine("原始材料AB", model_cfg=FAKE_MODEL_CFG)
        assert "原始材料AB" in captured["prompt"]
        assert captured["prompt"].count("{content}") == 0  # 占位符已被替换

    def test_default_schema(self):
        """DEFAULT_SCHEMA：category 可空。"""
        assert DEFAULT_SCHEMA["category"] == (str, type(None))
