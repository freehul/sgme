## 待审队列（2.0 / D-30）

- **现象**：WebUI 待审页有积压；某条刚提炼的记忆在 `inject`/`search` 里查不到。
- **判断**：L2 低置信度产物被打了 `needs_review=1`。这类记忆不进主注入块、不进人格证据，只在待审队列可见，等人工放行或拒绝。
- **查队列**（Agent Key；`limit` 1–200 默认 50，`offset` ≥0，`dimension` 走 memory_tags 精确过滤）：

```bash
curl -s -H "X-API-Key: $SGME_AGENT_KEY" \
  "$SGME_URL/v1/admin/review/pending?limit=50&offset=0&dimension=<dimension_id>"
# → {"items":[...], "total": N}；items 是完整记忆行
```

- **放行**（幂等；`note` 可选，会带时间戳追加进 notes，不覆盖既有备注）：

```bash
curl -s -X POST -H "X-API-Key: $SGME_AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{"note":"用户确认可用"}' \
  "$SGME_URL/v1/admin/review/<memory_id>/approve"
```

- **拒绝**（软删：`status='rejected'` + `reject_reason` + `rejected_at`，可 `memory_unreject` 恢复）：

```bash
curl -s -X POST -H "X-API-Key: $SGME_AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{"reason":"用户否认的过期偏好"}' \
  "$SGME_URL/v1/admin/review/<memory_id>/reject"
```

  `reason` 为空/空白直接 400 `ERR_INVALID_ARGS`；拒绝是幂等的，已拒绝的行不会被二次理由覆盖。

- **批量消化超期项**（慎用）：默认 7 天未处理按「保守直存」处理——放行进库但 notes 追加 `未经人工确认（超期 N 天保守直存）`，是**未经人工裁决**的记忆，后续只能按该标记批量复查。

```bash
curl -s -X POST -H "X-API-Key: $SGME_AGENT_KEY" -H 'Content-Type: application/json' \
  -d '{"days":7}' "$SGME_URL/v1/admin/review/auto-resolve"
# → {"resolved": N}；N 是本次实际消化条数
```

- **坑**：`approve` 只把 `needs_review` 置 0，**不会**把 `status='rejected'` 拉回 active；对已拒绝的行调 approve 等于没救。要恢复先 `memory_unreject`。
- **坑**：待审列表只列 `status='active'` 的行，拒绝后从列表消失是预期，不是丢数据。
