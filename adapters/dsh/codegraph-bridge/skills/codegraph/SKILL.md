---
name: codegraph
description: CodeGraph local code knowledge graph for code analysis, symbol search, and call graph traversal. Invoke when user asks to analyze code structure, understand architecture, debug issues, or search for symbols/functions.
license: MIT
---

# CodeGraph 本地代码知识图谱（dsh-codegraph 桥接版）

## 概述

CodeGraph（colbymchenry/codegraph v1.5，Rust 内核 + SQLite，100% 本地）经 dsh-codegraph 插件桥接为 dsh 工具。
本技能描述的是 **dsh 会话内实际可用的 4 个工具**（codegraph_explore/query/node/status）——它们是官方 MCP server 11 个工具的子集。
官方 MCP 才有的 context/trace/search/callers/callees/impact/files 在本环境**不存在**；其中 callers/callees/impact/files/affected/sync 可通过 CLI 直调补齐（见文末「CLI 直调」）。

## 核心原则

- 🔝 **首选 codegraph_explore**：一次调用返回相关符号原文源码（带行号）+ 调用路径 + 影响范围；返回的源码视为已读，无需再用 read 重复读取。
- 索引自动同步已开启（文件变更即增量更新），一般无需手动 sync。
- 所有查询 100% 本地、零网络、零 Python 依赖（子进程调 codegraph CLI）。

## 工具使用层级

### 1️⃣ 第一优先级
- `codegraph_explore` — 探索代码库区域（架构/调用链/影响范围/功能如何工作）
- `codegraph_status` — 检查索引状态

### 2️⃣ 第二优先级
- `codegraph_node` — 单符号源码 + 调用者/被调用者轨迹；或按文件读取带行号源码
- `codegraph_query` — 按名称搜索符号（位置/签名/docstring）

## 详细工具说明（参数与行为均已实测）

### codegraph_explore [最重要]
探索代码库某个区域：相关符号原文源码 + 调用路径 + 影响范围。
- `query` (必需): 自然语言任务/问题描述，或符号名/文件名集合
- `path` (可选): 项目目录（默认插件配置 projectPath = D:\Projects\SGME）
- `maxFiles` (可选): 返回源码的最大文件数

**实测行为**：支持中文自然语言；输出含「Exploration」标题、blast radius（调用方 + ⚠️无测试覆盖提示）、每文件的 verbatim 源码（带行号，视为已读）。跨语言覆盖 Python/TS/Vue。

### codegraph_query
按名称搜索符号。
- `search` (必需): 符号名或名称片段
- `path` (可选)
- `kind` (可选): function/class/method/interface/type/variable/route/component/file 等
- `limit` (可选): 最大结果数（默认插件配置 queryLimit=10）

**实测行为**：输出精简格式 `kind  name` + `filePath:startLine` + 签名，按相关度排序。

### codegraph_node
单符号详情或文件模式读取。
- `symbol` (可选): 符号名（不传则按文件模式）
- `file` (可选): 文件模式读取该文件（配合或替代 symbol）
- `offset` / `limit` (可选): 文件模式起始行/最大行数
- `path` (可选)

**实测行为**：符号模式返回 Location/Signature/docstring/完整源码/Trail（Calls→ 调用链）；文件模式返回带行号源码 + 文件统计（行数/符号数/被谁引用）。文件模式等价 read，无需重复读取。

### codegraph_status
索引状态与统计。
- `path` (可选): 项目目录（默认 projectPath）

**实测输出**（2026-08-16 修复字段映射后）：项目/文件/节点/边/数据库/版本/索引状态/语言/按类型分布。

## 索引检查（每次使用前必须执行）

**索引数据库位置**：`<项目根目录>/.codegraph/codegraph.db`（SQLite，v1.5 命名；旧文档的 graph.db 已过时）

### 检查流程
1. 用 glob 检查 `<项目根>/.codegraph/codegraph.db` 是否存在
2. 不存在 → 自动执行索引（无需询问）：
   ```powershell
   powershell -Command "Set-Location '<项目路径>'; node 'C:/Users/LEO/AppData/Roaming/npm/node_modules/@colbymchenry/codegraph/npm-shim.js' index . 2>&1; Write-Host 'EXIT_CODE:' $LASTEXITCODE"
   ```
   > ⚠️ 必须用 powershell -Command 而非 cmd /c（cmd 会吞输出）；路径用单引号；用 2>&1 + EXIT_CODE 确认退出码
3. 完成标志：输出含 `Indexed N files` / `nodes, edges` 或 `EXIT_CODE: 0`
4. 索引失败但工具可用 → 继续用工具（server 可能自行管理索引）

### 增量同步（代码变更后）
```powershell
powershell -Command "Set-Location '<项目路径>'; node 'C:/Users/LEO/AppData/Roaming/npm/node_modules/@colbymchenry/codegraph/npm-shim.js' sync . 2>&1"
```
自动同步已开启时通常无需手动执行。

## CLI 直调（补齐插件未暴露的能力）

dsh 插件只注册 4 个工具；以下官方 CLI 子命令可通过 pwsh 直调（$shim = npm-shim.js 路径）：
- `callers <symbol> [-j]` — 谁调用了某符号（JSON: {symbol, callers:[{name,kind,filePath,startLine}]}）
- `callees <symbol> [-j]` — 某符号调用了谁
- `impact <symbol> [-d 深度] [-j]` — 修改影响范围（默认深度 2）
- `files [--filter dir] [--format tree|flat|grouped] [-j]` — 项目文件结构
- `affected <files...> [--stdin] [-q]` — 变更影响到的测试文件
- `sync` / `status` / `query <s> -j` 等

统一调用方式：
```powershell
node 'C:/Users/LEO/AppData/Roaming/npm/node_modules/@colbymchenry/codegraph/npm-shim.js' --no-color <subcommand> <args>  # 在项目根目录执行
```

## 已知坑（2026-08-16 实测记录）

1. **status 字段 bug 已修**：旧插件误用 project/files/nodes/edges 解析 CLI 的 projectPath/fileCount/... 导致显示 ?；已修复。
2. **索引命名**：v1.5 生成 `.codegraph/codegraph.db`，不是 graph.db；检查时用 codegraph.db。
3. **索引命令**：用 npm-shim.js（node shim），不是 `py -3.11 -m codegraph_mcp`（那是旧 Python 版）。
4. **explore 的 blast radius 会标注无测试覆盖符号**：`run_batch_scan`、`_step_extract` 等已知无测试，补测试时可从这入手。
5. **受支持语言**：python/javascript/typescript/vue/yaml（SGME 实测 5 种）；C/C++/Go/Rust 等需官方最新支持情况。
6. **索引路径参数**：工具 path 参数会传给 CLI 的 cwd（execFile cwd），CLI 从 cwd 解析 .codegraph 索引。
7. **修改插件代码后需重启 dsh web 生效**（长驻进程）；Junction 链接指向项目内 adapters/dsh/codegraph-bridge。

## 最佳实践

✅ 优先 codegraph_explore（最省 token）
✅ 用具体符号名查询 codegraph_node / codegraph_query
✅ 修改前用 CLI 的 impact 评估风险（插件未暴露，直调）
✅ 文件模式读取用 codegraph_node --file，视为已读
✅ 涉及测试影响用 CLI affected
