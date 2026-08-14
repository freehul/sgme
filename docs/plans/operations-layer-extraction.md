# SGME Operations 层抽取计划

> 日期：2026-08-08
> 状态：设计阶段，待实施

## 问题

当前 HTTP 路由层（`server/routes_*.py`）和 MCP 工具层（`mcp_server.py`）各自直接调 engine，参数校验、异常分类、序列化各写了一遍。入口之间互相独立，修改一个操作需要改两处。

## 方案

在 engine 和入口层之间加一层 `sgme/operations/`，作为**唯一的操作入口**。

### 架构

```
HTTP /v1/* (9910) ──→ ┐
MCP (9913) ──────────→ ├──→ operations/ ──→ engine/storage/search/profile
适配层 (HTTP) ───────→ ┘
```

### operations 模块结构

```
sgme/operations/
├── __init__.py
├── append.py          # L0 捕获（校验 → pipeline.append_l0 → 标准化返回）
├── inject.py          # 记忆注入（校验 → profile.inject → 标准化返回）
├── search.py          # 混合检索（校验 → search_memories → 标准化返回）
├── memory.py          # 单条记忆操作（get / reject / unreject）
├── refine.py          # 提炼触发（trigger / trigger_async）
├── stats.py           # 统计查询
├── health.py          # 健康检查
├── config.py          # 配置读写
└── errors.py          # OperationError / InvalidArgs / 标准化错误码
```

### 每个操作的统一模式

```python
# 每个 operation 返回 OperationResult(ok=True, data=...) 或抛标准化异常
class OperationResult:
    ok: bool
    data: dict | None
    error_code: str | None

class InvalidArgs(Exception):   # 参数校验失败 → 400
class OperationError(Exception):  # 操作执行失败 → 500/503
    error_code: str              # RAW_FILE_MISSING / LLM_UNAVAILABLE / INTERNAL
```

### 入口层变成薄包装

HTTP 路由：`operations.xxx(**params)` → 成功转 JSONResponse，异常转 api_error
MCP 工具：`operations.xxx(**params)` → 成功转 json.dumps，异常转 error JSON

## 不变的内容

- engine/storage/profile/search 零改动
- 适配层（adapters/hermes、adapters/reasonix）零改动——它们调 HTTP，不感知内部
- API 契约（JSON schema）不变

## 改动范围

- 新增：`sgme/operations/`（~10 个文件）
- 修改：`sgme/server/routes_*.py`（7 个文件，每处改 5~10 行）
- 修改：`sgme/mcp_server.py`（7 个工具函数，每处改 5~10 行）
- 修改：测试文件（import 路径调整）

## 时机

等 wiki/log/cursor/skills-hub 模块设计完成后，在下一轮编码阶段一并实施。
