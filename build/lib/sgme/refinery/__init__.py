"""refinery/：知识提炼引擎（v0.7 §9）。

定位：SGME 内部模块，仅服务 wiki 知识提炼。从蒸馏套装（仓颉/沈括）吸取
共性能力（输入解析、LLM 提取、结果校验），但不替代蒸馏套装；与 engine/
（会话→记忆管线）互不依赖。

模块结构：
- __init__.py  refine(source) → RefineryResult 统一入口
- ingest.py    输入处理（文本透传 / 本地文件 / URL）
- extract.py   LLM 提取（调模型 → 解析 JSON → 校验 schema → 失败重试）
- validate.py  质量门框架（可注册自定义验证步骤）
- output.py    统一产出格式（RefineryResult → wiki_pages 行）

用法：
    from sgme.refinery import refine
    result = refine("...文本或文件路径或URL...")
    if result.ok:
        row = result.to_wiki_page(...)  # 见 output.to_wiki_page
"""

from __future__ import annotations

import logging
from typing import Any

from sgme.refinery.extract import ExtractError, extract
from sgme.refinery.ingest import IngestError, ingest
from sgme.refinery.output import RefineryResult
from sgme.refinery.validate import ValidationReport, validate

logger = logging.getLogger("sgme.refinery")

# 默认提炼 prompt：不绑定具体领域，产出通用 wiki 知识页（§9.4 不绑定 schema）
DEFAULT_PROMPT = (
    "你是 SGME 知识提炼引擎。请阅读下面的材料，提取为结构化 wiki 知识页。\n"
    "要求：\n"
    "1. 只输出一个 JSON 对象，不要输出任何其他内容；\n"
    "2. JSON 字段：title（页面标题，字符串）、content（页面正文，Markdown 格式，字符串）、\n"
    "   tags（标签数组，字符串列表）、category（分类，字符串，可省略）。\n\n"
    "材料：\n{content}"
)

# 默认输出 schema：category 可空，其余必填
DEFAULT_SCHEMA: dict[str, Any] = {
    "title": str,
    "content": str,
    "tags": list,
    "category": (str, type(None)),
}


def _render_prompt(prompt: str, content: str) -> str:
    """把材料内容渲染进 prompt（支持 {content} 占位符，否则直接拼接）。"""
    if "{content}" in prompt:
        return prompt.format(content=content)
    return f"{prompt}\n\n材料：\n{content}"


def _default_model_cfg() -> dict | None:
    """读取 llm.yaml 作为默认模型配置；读取失败返回 None（调用方转失败结果）。"""
    try:
        from sgme.llm.chain import load_config

        return load_config()
    except Exception as e:  # noqa: BLE001 —— 配置缺失不阻断管线，转失败结果
        logger.warning("加载 llm.yaml 失败: %s", e)
        return None


def refine(
    source: str,
    prompt: str | None = None,
    output_schema: dict | None = None,
    model_cfg: dict | None = None,
    rules: list[Any] | None = None,
    client: Any = None,
) -> RefineryResult:
    """知识提炼统一入口：ingest → extract → validate → RefineryResult。

    Args:
        source: 输入材料——纯文本、本地文件路径（md/txt）或 URL。
        prompt: 提取提示词；缺省用 DEFAULT_PROMPT（含 {content} 占位符）。
        output_schema: 输出 schema；缺省 DEFAULT_SCHEMA。
        model_cfg: llm.yaml 配置；缺省自动加载。
        rules: 质量门规则列表；缺省 validate 默认规则（非空 + 最小长度）。
        client: 可复用的 httpx.Client（透传给降级链，可选）。

    Returns:
        RefineryResult：任一环节失败时 ok=False 且携带 error，不抛异常。

    Raises:
        NotImplementedError: 输入为 pdf/docx/图片/视频等暂不支持类型时透传。
    """
    # ---- ingest：输入处理 ----
    try:
        text, metadata = ingest(source)
    except IngestError as e:
        logger.warning("refine ingest 失败: %s", e)
        return RefineryResult.failure("unknown", str(e))
    source_type = metadata["source_type"]

    # ---- extract：LLM 提取 ----
    if model_cfg is None:
        model_cfg = _default_model_cfg()
        if model_cfg is None:
            return RefineryResult.failure(source_type, "无法加载 LLM 配置 (config/llm.yaml)")
    try:
        data = extract(
            _render_prompt(prompt or DEFAULT_PROMPT, text),
            output_schema or DEFAULT_SCHEMA,
            model_cfg,
            client=client,
        )
    except ExtractError as e:
        logger.warning("refine extract 失败: %s", e)
        return RefineryResult.failure(source_type, str(e))

    # ---- validate：质量门 ----
    content = data.get("content") or ""
    report: ValidationReport = validate(content, rules=rules)
    if not report.ok:
        logger.warning("refine validate 失败: %s", report.summary())
        return RefineryResult.failure(source_type, f"质量校验未通过: {report.summary()}")

    # ---- output：组装结果 ----
    return RefineryResult(
        ok=True,
        source_type=source_type,
        title=data.get("title"),
        content=content,
        tags=[str(t) for t in (data.get("tags") or [])],
        category=data.get("category"),
    )


__all__ = [
    "RefineryResult",
    "DEFAULT_PROMPT",
    "DEFAULT_SCHEMA",
    "refine",
]
