## 角色运维

**三层存储，别混：**

| 层 | 位置 | 内容 |
|---|---|---|
| 角色卡 | `<home>/roles/*.json`（容器内 `/data/roles`，持久化卷） | Character Card V2 兼容子集：`name`/`description` 必填，其余可选 |
| 基线层 | 卡内 `extensions.sgme_profile` | 五键白名单：`biography`、`values`、`hobbies`、`voice`、`personality_slots`；多余键拒绝 |
| 演化层 | `data/role_evolution/<role_id>.json` | 四类 kind：`timeline`、`feelings`、`personality_changes`、`values_changes` |

- **端点**：`GET/POST/DELETE /v1/admin/roles/{role_id}`（DELETE 是归档到 `.archive/`，原件永不物理删）、`GET/POST /v1/admin/roles/{role_id}/persona`、`GET/POST /v1/admin/roles/{role_id}/evolution`。
- **`role_id` 白名单**：`^[a-z0-9][a-z0-9_-]{0,63}$`。大写、中文、`/`、`..` 一律拒绝（防路径穿越），报错可能是笼统的 `ERR_INTERNAL`，先查 id 形态。
- **persona 是唯一物化例外**：落 `data/personas/<role_id>.md`，属运行数据，不入 git；演化层同理。
- **血泪坑（切库/升级最容易踩）**：镜像内置卡**只在首次启动时物化**——entrypoint 判断 `$SGME_HOME/roles` 目录不存在才 `cp /app/roles/*.json`。目录已存在时**新镜像不会覆盖已有卡**，所以「升级后内置人设没变」不是 bug。必须直接往卷里传卡：

```bash
docker cp roles/butler.json sgme:/data/roles/butler.json   # 覆盖单张
docker exec sgme ls /data/roles                            # 核对落盘
docker restart sgme
```

  裸机部署同理：直接改 `<home>/roles/<role_id>.json`，不要指望重装程序覆盖。
