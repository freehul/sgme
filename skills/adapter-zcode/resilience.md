# SGME 韧性自救卡（任何 Agent 通用）

> 目标：SGME 同时管**记忆**与**技能库**。它不可用时，任何接入的 agent 都能照本文件**发现 → 修复 → 降级 → 补位**。
> 本文件是纯命令清单，不依赖脚本、不依赖 agent 特有工具——只要能执行 shell 就能用。
> 占位符：`<SGME_BASE_URL>`（如 `http://<NAS_IP>:9910`）、`<NAS_HOST>`、`<NAS_USER>`、`<CONTAINER>`（默认 `sgme`）。

## 0. 什么时候看本文件

- 会话第一问后**没有记忆/画像注入**
- 任何 SGME 工具报错、超时、返回空
- 技能检索无结果，但确信库里该有

只在以上三处检测，**不要**每轮探测（注入本身就是探针）。

## 1. 判断（只读，按序）

```bash
# L0 健康（1.5 秒超时；绕过系统代理）
curl -s -m 2 --noproxy '*' "<SGME_BASE_URL>/v1/health"
```

判读：

| 返回 | 结论 |
|---|---|
| 200 + `llm.available=true`、`vector.available=true`、`refinement.stalled=false` | 正常 |
| 200 但某字段 false | 降级（可用但能力缺失） |
| 非 200 / 连不上 | 继续 L1 |

```bash
# L1 端口
nc -z -w 3 <NAS_IP> 9910 && echo "port ok" || echo "port down"

# L2 容器
ssh -o BatchMode=yes -o ConnectTimeout=8 <NAS_USER>@<NAS_HOST> \
  "docker ps -a --filter name=^<CONTAINER>$ --format '{{.State}}|{{.Status}}'"

# L3 宿主 Docker
ssh -o BatchMode=yes -o ConnectTimeout=8 <NAS_USER>@<NAS_HOST> "systemctl is-active docker"
```

## 2. 修复（每级成功即停，绝不越级）

| 现象 | 动作 |
|---|---|
| L0 偶发失败 | 重试一次（瞬时抖动） |
| 端口不通 | **不动手**，报告网络/主机问题 |
| 容器 stopped / created | `docker start <CONTAINER>` → 等 12 秒 → 重跑 L0 |
| Docker 服务 inactive | `sudo systemctl start docker.service` → 再起容器 → 重跑 L0 |
| 容器 running 但仍不可用 | 抓 `docker logs --tail 30 <CONTAINER>` + `df -h`，**交人工**；**严禁自动清库/重建** |

允许自动执行的只有两条：`docker start <CONTAINER>`、`sudo systemctl start docker.service`。
其它一律先报告。

## 3. 降级（SGME 离线时怎么继续干活）

1. 每次回复**首行标注** `[SGME 离线 · 降级模式]`——不静默降级
2. 记忆：只能用会话历史 + agent 自带的记忆文件；不要假装记得
3. 技能：按下面第 4 节拿
4. 产出先落本地文件；SGME 恢复后补记

## 4. 补位（技能从哪来）

按优先级：

1. **本地技能目录**（若该 agent 有）：直接读 `<skills_root>/<name>/SKILL.md` 与其 `references/`
2. **直取技能包**（无需 SGME 在线检索接口也能拿单个技能）：
   `GET <SGME_BASE_URL>/v1/skills/<name>/package`（agent key）→ tar.gz + `MANIFEST.sha256` 校验 → 落地解压
3. **注册表临时安装**（Hermes 示例，其它 agent 用各自的安装方式）：
   `hermes skills search <kw>` → `hermes skills inspect <name>` → `hermes skills install <name>`
4. 装/取完**记一笔**（名字、来源、用途、日期），用完不自动删除；SGME 恢复后评估是否入真源

## 5. 恢复后

1. 重跑第 1 节，确认 `online`
2. 把降级期间的产出/临时技能清单补记进 SGME
3. 若恢复靠的是手动动作，建议在 SGME 侧留痕（append 一条），便于复盘

## 6. 已知坑

- `/v1/health` 不能走系统代理（代理 502 会被误判成 SGME 挂了）→ 用 `--noproxy '*'`
- **不要**用 `docker restart` 代替 `start`：容器不存在时 restart 报错，会掩盖真实原因
- SSH 必须 `BatchMode=yes`，否则断网时会卡在密码提示
- 「检索无结果」≠「库里没有」：先按第 1 节判断，再降级

## 7. Hermes 专用加速器（可选）

Hermes 侧有对应技能与脚本，做同样的分级判断与修复：

```bash
python <skills_root>/sgme-resilience/scripts/sgme_doctor.py          # 只读
python <skills_root>/sgme-resilience/scripts/sgme_doctor.py --fix    # 探测 + 修复
```

其它 agent 没有脚本也能照第 1～5 节手工完成同样的处置。
