# adapters/_shared — 八适配器共用逻辑（D-59 / T-406）

本目录是**仓库内真源**，不随任何适配器分发：适配器安装后是独立目录（Hermes 插件
副本、各家 `scripts/` 目录），无法反向 import 仓库路径。故共用代码以**源码副本**
形式随各自分发包携带，由脚本同步、由测试守一致性。

```
adapters/_shared/skill_cache.py          # 唯一可编辑处（纯标准库单文件）
        │  python scripts/sync_shared_skill_cache.py
        ├─ adapters/hermes/skill_cache.py
        ├─ adapters/doubao/scripts/skill_cache.py
        ├─ adapters/mimo/scripts/skill_cache.py
        ├─ adapters/workbuddy/scripts/skill_cache.py
        └─ adapters/zcode/scripts/skill_cache.py
adapters/dsh/sgme-bridge/src/skill-cache.ts   # TS 同语义移植（语言不同，不能逐字节共用）
```

一致性门禁：`tests/test_skill_cache_sync.py`（副本 == 真源；TS 与 Python 的协议
常量同值；dsh 产物含 `skill-cache` region）。

## 接口契约

```python
SkillCache(transport, state_dir=None)
    .ensure_skill(name, dest_dir) -> Path   # 命中零包传输；未命中下载校验后原子替换
    .refresh_catalog() -> bool              # True = 目录版本有变化
```

- 两级失效：`GET /v1/skills/catalog-version` 目录级短路 → `GET /v1/skills/{name}/manifest`
  的 `package_sha256` 与本地 `.sgme-manifest.json` 的 `content_hash` 比对；都命中才零传输。
- 本地清单：`{name, version, content_hash, files: [{path, sha256}], fetched_at, catalog_version}`，
  放在技能目录内（随目录一起原子替换）；文件被外部改动/删掉 → 下次自动重拉。
  `catalog_version` 记录落盘时见到的目录版本——只有版本仍相同才允许一级短路，
  防止「上次下载失败但目录版本已记下」把旧副本误判成最新。
- 未命中才 `GET /v1/skills/{name}/package`：包内 `MANIFEST.sha256` + 文件集双向对照 +
  `X-Skill-Sha256/X-Skill-Files` 对账 → 临时目录组装 → `rename` 原子替换（失败回滚）。
- 降级：catalog/manifest 端点不可用（旧服务端 404/405）→ 视为「不可比」直接拉包；
  只有包下载/校验失败才抛错。

## 八适配器盘点与改造状态（T-406）

| 适配器 | 改造前技能获取方式 | 本次接入点 | 状态 |
|---|---|---|---|
| hermes | 常驻插件；`sgme_skill_materialize` 只写**服务端**固定根（跨机拿不到文件） | 该工具改走包通道落**本地** `$HERMES_HOME/plugin-data/sgme/skills/`；服务端无包通道时回退旧 materialize | ✅ 已接 |
| dsh | TS 桥；`skill_materialize` 只写服务端 | `src/skill-cache.ts` 移植 + 工具本地落盘；404/405 回退服务端物化 | ✅ 已接 |
| doubao | 已有 `fetch` 包通道（下载 → 校验 → 解包），**无缓存** | `fetch` 先两级比对，命中零下载 | ✅ 已接 |
| mimo | 同 doubao（同构客户端） | 同上 | ✅ 已接（同构，顺带完成） |
| workbuddy | 同 doubao（同构客户端） | 同上 | ✅ 已接（同构，顺带完成） |
| zcode | 同 doubao（同构客户端） | 同上 | ✅ 已接 |
| codex | MCP stdio 动态代理；工具面 `skill_materialize` 是服务端语义；技能文件由 agent 侧 `skill_fetch.py` 获取 | 代理进程内没有技能落盘路径 | ⏳ T-406b |
| claude-code | 插件市场只发 SKILL.md + hooks，无技能库通道 | 需在插件内加技能索引 + 接口拉取 | ⏳ T-406b |

### T-406b 待办（codex / claude-code）

- **codex**：`skill_materialize` 路径语义改本地落盘 + manifest（D-60 codex 行），
  工具描述与 README 同步改（避免再出现「写了文件却找不到」）。
- **claude-code**：插件内加技能索引 → 接口拉取；marketplace 包只含引导器 + 索引，
  不再随包携带全部技能正文（D-60 claude-code 行）。
- **为什么没在本批硬改**：两者都是**动态 MCP 代理**（`mcp_proxy.py` 只做 tools/list 与
  tools/call 透传，parity 以 `remote.list_tools()` / `remote.call_tool(` 为锚点）。
  代理进程不接触技能文件系统，硬塞落盘逻辑会改变「原样透传」语义，需要单独设计
  落点（代理侧缓存目录 / 宿主侧 hook）与验收口径，不宜在统一缓存层这批里顺手改。
