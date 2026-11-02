## 运维看板（2.0）

两个端点都是 Admin Key、只读、`days` 取值 1–365（越界或非整数静默回退 30）。

- **TTL 处置**：`GET /v1/admin/ops/ttl-disposition?days=30`
  返回按天分组的五档计数（固定枚举，顺序即看板顺序）：`valid_expired`（有效过期）、`renewed`（续期）、`anchor`（锚定）、`sleeping`（休眠）、`archived`（归档），另给 `total` 汇总。
- **LLM 用量**：`GET /v1/admin/ops/llm-cost?days=30`
  返回按 `(day, provider, model, stage)` 的 `prompt_tokens`/`completion_tokens`/`calls` 明细与 `totals` 汇总。

```bash
curl -s -H "X-API-Key: $SGME_ADMIN_KEY" "$SGME_URL/v1/admin/ops/ttl-disposition?days=30"
curl -s -H "X-API-Key: $SGME_ADMIN_KEY" "$SGME_URL/v1/admin/ops/llm-cost?days=30"
```

- **现象**：看板全 0，响应带 `"note": "审计表尚未创建（无处置记录）"` 或 `"用量账本尚未创建（无记账）"`。
- **判断**：`ttl_disposition` / `llm_usage_daily` 是懒建表——首次有处置/记账才创建。HTTP 200 + 空集 + note 是**正常空态，不是 404/500 故障**；先确认最近是否真的没有提炼/召回老化任务跑过。
- **处置**：note 长期存在且业务明显在跑 → 查提炼/召回任务是否真的执行、`SGME_HOME` 是否指错（表建到了另一个库）；不要手工建表绕过。
