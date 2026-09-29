---
name: sgme-adapter-development
description: SGME 六个官方 Agent 适配器的开发、发布和对账：Hermes、DSH、豆包、MiMo、WorkBuddy、ZCode。修改 adapters/*、导入历史或适配器分发包时使用。
category: sgme-development
tags: [skill, sgme, adapter, bridge, mcp, hooks, incremental, dedup]
version: 1.0.0
pattern: auto
uses:
  - coding-workflow
  - sgme-development
---

# SGME 适配器开发

## 真源与分发

- 真源：`adapters/<host>/`，按宿主实现捕获、注入、工具和安装逻辑。
- 分发：`skills/adapter-<host>/`，由 `scripts/publish_adapter_skills.py` 生成；禁止直接编辑分发副本。
- 六个平级宿主：`hermes`、`dsh`、`doubao`、`mimo`、`workbuddy`、`zcode`。

## 共同契约

- 适配器只负责宿主生命周期与 HTTP/MCP 调用，提炼、检索和数据库逻辑留在 SGME Gateway。
- L0 写入必须幂等；重复会话、工具回执和历史导入不能制造重复消息。
- 只读宿主原始数据，只写 SGME L0；不删除原件。
- HTTP 客户端访问本机/内网服务使用 `trust_env=False`；密钥只从环境变量或部署配置读取。
- 新增 MCP 工具必须同步评估六个适配器，不能只修改一个宿主。

## 开发与验收

1. 先用 CodeGraph/项目测试确认目标适配器的调用链和现有契约。
2. 先写重复写入、失败重试、空响应和权限边界测试，再实现改动。
3. 运行适配器自己的测试、主仓 `tests/test_adapter_parity.py` 和：

   ```powershell
   python scripts/publish_adapter_skills.py --check
   python scripts/adapter_parity.py --strict
   ```

4. 需要更新分发包时运行发布脚本，检查 `SKILL.md` 的 `name: adapter-<host>` 与随附文件完整性。
