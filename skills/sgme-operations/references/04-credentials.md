## 凭证管理

6 个端点（注意读侧是 `/v1/credentials`，不在 `/v1/admin` 下）：

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/v1/credentials` | agent | 列表：键名 + 元数据，**不含值**，默认过滤已删 |
| GET | `/v1/credentials/{key}` | agent | 取单值（记 read 审计），不存在/已删 → 404 |
| GET | `/v1/credentials/export` | agent | 批量导出明文；`only=` 与 `prefix=` 互斥，`only` 缺键 → 404；记领域审计 + SecurityLog `credential_export` |
| POST | `/v1/credentials` | admin | 写入/更新；已软删键名 → 409，需先 restore，不自动复活 |
| DELETE | `/v1/credentials/{key}` | admin | 软删（幂等） |
| POST | `/v1/credentials/{key}/restore` | admin | 恢复（幂等，清 `deleted_at`） |

- **铁律：值永不明文展示。** 列表、日志、聊天、工单、截图、排障回执都只出现键名/描述/指纹，不回显值。核对是否已配置看「键名是否存在 + 描述 + 审计记录」，不要把值打印出来对照。
- **导出会显式列出跳过项（D-37 v1.1）**：全量/prefix 导出返回 `{credentials, count, skipped:[{key_name, reason}]}`（reason=scope 无权 / expired 已过期）；`only` 命中不可读或已过期的键 → 403，不做静默跳过。客户端 `sgme-cred export/run` 会把跳过项告警到 stderr。
- **导出是唯一批量明文出口，必须二次确认**：先向用户确认目标范围（`only` 还是 `prefix`，含哪些键），明确点头后才执行；导出内容落到受控文件或直接管道给消费方，不回贴对话。事后可按 SecurityLog 的 `credential_export` 事件核对 `target`（`all` / `only:...` / `prefix:...`）。

```bash
# 只列键名，确认真实存在哪些
curl -s -H "X-API-Key: $SGME_AGENT_KEY" "$SGME_URL/v1/credentials"
# 二次确认后按前缀导出，输出重定向到受控文件（不要 cat 到终端）
curl -s -H "X-API-Key: $SGME_AGENT_KEY" \
  "$SGME_URL/v1/credentials/export?prefix=DEEPSEEK_" > <受控路径>/creds.json
```

- **现象**：写入/更新/导出报「主密钥不可用」（`state=missing|invalid_format|probe_failed`）。
- **判断**：`SGME_CRED_KEY` 缺失或不是合法的 44 字符 Fernet key；`probe_failed` 表示库内已有密文但当前 key 解不开（换过 key）。缺 key 时**绝不**明文兜底，写路径直接拒绝。
- **处置**：从部署配置注入正确的 `SGME_CRED_KEY` 后重启。不要随手重新生成 key——既有密文会永久不可读；确需轮换，先解出全部值再统一重写。
- **坑**：`export` 必须先于 `/{key}` 注册。若前置代理/重写规则把 `export` 当键名转发，会命中详情端点导致 404 或取错值——先直连服务验证。
- **坑**：写/删/恢复/导出四处都写 admin_op 审计；排障时以审计为准，不以客户端缓存为准。
