# 设计说明：ZCode 官方通道桥（official_bridge）

## 1. 为什么需要它

- ZCode PC 端的赠送额度（Start Plan / ZCODE GLOBAL BUILD）**不是普通 API Key**：端点 `https://zcode.z.ai/api/v1/zcode-plan/anthropic` 要求账户 JWT + **V4 客户端请求签名**，并且会校验请求体是否被代理改写；第三方网关（magpie 等）改 body 后会被风控 405/3012。
- 官方 CLI（`zcode.cjs`）在进程内完成 OAuth/JWT、签名与请求构造，是唯一合规且可自动化的通道。
- SGME 的提炼/重建只支持 OpenAI 兼容供应商，因此需要一层“官方 CLI ↔ OpenAI”桥。

## 2. 架构

```
SGME  ──HTTP──▶  bridge.py (127.0.0.1:8899/v1/chat/completions)
                     │  每个请求：prompt 写入临时文件
                     ▼
         node zcode.cjs -p <短指令> --attach prompt.txt
              --cwd <隔离目录> --mode plan --surface terminal
                     │  官方 CLI 内部完成签名/风控合规
                     ▼
           ZCode 官方端点（Start Plan / Coding Plan）
```

- 桥不保存凭据、不读取 CLI 的 `config.json`；身份完全由官方 CLI 的登录态决定。
- 并发用信号量（默认 2）；单次调用超时、429/3012/MCP 错误分别归类并映射为 HTTP 429/502。

## 3. 身份模型（关键）

| 套餐 | 凭据形态 | 能否直连 | 桥怎么用 |
|---|---|---|---|
| ZCode Coding Plan | API Key（`id.secret`） | 可以（标准 Anthropic 端点） | 可直接用 SGME 的 openai_compat，不需要本桥 |
| ZCode Start Plan（赠送额度） | 账户 JWT + V4 签名 | 不可以 | 必须让 CLI 以**同一账号**登录（`zcode login bigmodel` / `zai`），由 CLI 签名 |

实践要点：
- 登录用错账号（例如登成另一个只有 coding-plan 的账号）会得到 `429/1113 无资源包`；
- 用错套餐/端点会得到 `400 no active limited quota`；
- 账号被风控时所有客户端（含官方 App）都会 405/3012，只有 ZCode 支持可解封。

## 4. 已知问题与待办

1. **注入 App 身份的测试失败**：把 App localStorage 里的 bigmodel token 写入 CLI 凭据后，CLI 启动卡在 MCP 版本协商（`ERA_NEGOTIATION_FAILED`）。推测与 CLI 启动时连接 `~/.zcode/cli/config.json` 中的 MCP servers 有关。
   - 待验证：临时禁用 MCP 配置（备份后改为空 `mcp.servers`）再试；
   - 该路线涉及从 App 存储提取 token，**需用户明确授权**，并在测试后立即还原凭据文件。
2. **性能**：官方 CLI 有 agent 回合开销，单次 8–30s；重建前必须先 1 文件试点，再压测并发上限。
3. **输出格式**：headless 输出可能随版本变化；`extract_text` 已覆盖 OpenAI/消息数组/纯文本三种形态，仍需真实账号回归。
4. **凭据安全**：测试时不要打印 `~/.zcode/cli/config.json`（含第三方密钥明文）；桥进程本身不读它。

## 5. 验证清单

- [x] pytest（假 CLI，13 例）：解析、429/3012/MCP/超时、HTTP 映射、模型列表；
- [x] `bridge.py --check` 只读诊断（CLI 路径/登录时间/模型选择）；
- [x] `bridge.py --selftest` 真实调用入口；
- [ ] 有额度账号登录后的真实 200 调用；
- [ ] `tmp/full_rebuild.py --limit 1` 试点产出 ≥1 条记忆；
- [ ] 并发 2/4 压测（延迟、429 率、额度消耗）。
