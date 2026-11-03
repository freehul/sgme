# ZCode 官方通道桥（official_bridge）

把 **ZCode 官方 CLI** 桥接成 OpenAI 兼容的本地服务，供 SGME 的提炼/重建使用。
官方 CLI 自己完成 OAuth/JWT 与 V4 请求签名，因此不经过任何第三方代理改写请求体。

## 前置条件

1. 本机有 ZCode 安装（官方 CLI 位于 `resources/glm/zcode.cjs`），Node 可执行；
2. **ZCode CLI 已登录一个有额度的账号**（Start Plan / Coding Plan）；
   可用下面命令验证一次（能返回正文即通道可用）：

   ```bash
   node <ZCode>/resources/glm/zcode.cjs -p "只回复：可用" --cwd <临时目录> --mode plan --surface terminal
   ```

3. 账号当前**未被 ZCode 风控**（否则 CLI 会返回 405/3012 或 429/1113，桥会原样映射为 HTTP 错误）。

## 身份与额度（最重要）

- 本桥**不保存任何凭据**；它复用官方 CLI 的登录身份（`~/.zcode/v2/credentials.json` + `provider_config.json`）。
- 要使用 ZCode PC 端赠送的 Start Plan 额度，必须让 CLI 登录**与 PC 端同一个账号**（`zcode login bigmodel` / `zcode login zai`），CLI 会自己完成签名；把 token 抠出来直连是行不通的（Start Plan 要求 V4 客户端签名）。
- 用 `--check` 可只读查看 CLI 路径、登录时间与当前模型选择；用 `--login bigmodel|zai` 可启动官方登录；用 `--selftest` 做一次真实调用验证额度。
- 想隔离测试、不动本机登录态：设置 `ZCODE_DATA_BASE_DIR=<临时目录>`，CLI 会读写该目录下的 `.zcode/v2` 凭据。

## 启动

```bash
python adapters/zcode/official_bridge/bridge.py --port 8899 --concurrency 2
# 可选：--cjs <路径> --node <node路径> --timeout 600 --model zcode/GLM-5.3-Flash-Trial
# 只读诊断（查看 CLI 路径 / 登录时间 / 当前模型选择）：
python adapters/zcode/official_bridge/bridge.py --check
# 自检（真实调用一次，可能消耗少量额度）：
python adapters/zcode/official_bridge/bridge.py --selftest
```

启动后：

| 端点 | 用途 |
|---|---|
| `GET /health` | 存活与调用统计 |
| `GET /v1/models` | 模型列表 |
| `POST /v1/chat/completions` | OpenAI 兼容补全 |

## 接到 SGME（重建/提炼）

把 LLM 链指向桥（示例）：

```yaml
chains:
  refinement:
    - provider: zcode-official
      model: zcode/GLM-5.3-Flash-Trial
      base_url: http://127.0.0.1:8899/v1
      provider_type: openai_compat
      max_tokens: 32768
      context_window: 200000
      sampling:
        temperature: 0
        top_p: 1.0
    - provider: rule
      rule: drop_batch
```

> 注意：桥按「一次请求 = 一次官方 CLI 调用」工作，官方 CLI 是重量级进程，
> 并发建议 2–4；一个重建请求可能耗时 10–30 秒，全量重建前先跑 `tmp/full_rebuild.py --limit 1` 试点。

## 安全注意

- 桥**不读取** ZCode CLI 的 `config.json`（其中含 MCP/第三方密钥明文）；排障时也不要把该文件内容贴到对话或日志。
- 桥不落盘任何凭据；凭据始终在官方 CLI 的 `~/.zcode` 内。
- 若需临时换身份做测试，先备份 `~/.zcode/v2`，测试完立即还原。

## 已知限制

- 官方 CLI 的 headless 输出格式可能随版本变化：桥用多形态解析兜底（OpenAI 风格 / 消息数组 / 纯文本尾部）；
- 官方 CLI 有自己的 agent 回合开销（思考/规划），单次延迟高于直连 API；
- 账号额度与风控由 ZCode 侧决定，桥不绕过风控；
- 桥不保存任何凭据，凭据由官方 CLI 管理（`~/.zcode`）。

## 测试

```bash
pytest adapters/zcode/official_bridge/tests -q
```

测试用假 CLI（`tests/fake_zcode_cli.py`）覆盖 JSON/纯文本/429/3012/超时五种形态，不消耗额度。
