# dsh-codegraph

CodeGraph (colbymchenry/codegraph) × DeepSeek Harness 桥接插件。

把本地代码知识图谱 CLI（Rust 内核 + SQLite，100% 本地）暴露为 dsh 工具：

- `codegraph_explore` — 一次调用返回相关符号原文源码 + 调用路径 + 影响范围（主工具，省 token）
- `codegraph_query` — 按名称搜索符号
- `codegraph_node` — 单符号详情或按文件读取
- `codegraph_status` — 索引状态

## 仓库位置（2026-08-16 起）

本插件源码随 **SGME 项目** git 管理（`adapters/dsh/codegraph-bridge/`，与 sgme-bridge 同构），
符合 AGENTS.md「项目产物必须随 git 管理」铁律。旧的 `D:/Projects/dsh-codegraph-bridge`
是历史部署副本，仅作兼容保留，不再维护；修改请改本项目内副本。

## 依赖

- 已安装 `@colbymchenry/codegraph`（npm 全局，npm-shim.js 路径见 cordis.patch.yml）
- 项目目录已执行 `codegraph init`（自动同步已开启；索引库为 `.codegraph/codegraph.db`）

## 挂载

web profile 的 package.json 中（已配置）：

```json
{"dsh-codegraph": "link:D:/Projects/SGME/adapters/dsh/codegraph-bridge"}
```

改动插件代码后：`dsh plugin --profile web install`（重建 junction）并重启 web。

## 已知修复

- **2026-08-16 status 字段 bug**：旧代码误用 `project/files/nodes/edges/dbSize` 解析
  CLI `status -j` 的 `projectPath/fileCount/nodeCount/edgeCount/dbSizeBytes`，导致状态全显 `?`；
  已修正并对齐 v1.5 实际输出（含版本/索引状态/语言/按类型分布）。
