# progressive-skill 卸载交接（ST-36 M5 收官）

> 2026-08-26 定案：SGME Skills 管理模块上线并完成生产迁移（B106）后，
> progressive-skill 插件停用卸载。本文档是唯一交接依据。

## 一、背景与分工边界

| 阶段 | 载体 | 状态 |
|------|------|------|
| 过渡期 | progressive-skill v3.2.0（本地文件树 + audit R1-R5 + budget） | **待卸载** |
| 终局 | SGME skills 模块（git 真源 + 四级披露 + 门禁写侧） | ✅ 已上线（v1.0.2） |

设计裁决：两线思想同源、载体升级；SGME 上线后 progressive-skill 直接卸载
（不做自动探测休眠等花活）。progressive-skill 架构红线=永不引入数据库。

## 二、SGME 已接管的能力对照

| progressive-skill 能力 | SGME 对应 | 备注 |
|------------------------|-----------|------|
| 技能索引压缩（L0/L1） | `GET /v1/skills`（budget 截断）/ `/{name}/digest` | MCP: skill_search/skill_digest |
| 全文按需加载 | `GET /v1/skills/{name}?section=` | MCP: skill_get |
| 物化到工作区 | `POST /v1/skills/{name}/materialize` | 字节保真+遥测 |
| 冷启动包 | `GET /v1/skills/coldstart` | 索引+热集+手册一次拉取 |
| audit R2 frontmatter 门禁 | gates.py 六规则（含 pattern 枚举/scripts 实体判据） | 写侧 PUT 前置拦截 |
| pre-receive 观察模式审计 | 同一钩子转执法（见下节） | NAS 侧操作 |

## 三、卸载步骤

### 1. NAS pre-receive 转执法（先做，防绕过 API 直推）

```bash
ssh LEO@192.168.10.10
cd /vol1/1000/git/skills-hub.git/hooks/
cp audit.env audit.env.bak-warn-only   # 原件保留
# 删除 AUDIT_WARN_ONLY=1 行 → 审计 FAIL 开始真实拒绝推送
sed -i '/^AUDIT_WARN_ONLY=1/d' audit.env
```

验证：向 hub 推一个带门禁违规的提交，应被拒绝（exit non-zero）。
回滚：恢复备份的 audit.env.bak-warn-only 即回到观察模式。

### 2. Hermes 侧插件停用

- config.yaml 移除 progressive-skill 插件条目（或 plugins/ 下移除安装副本）
- 重启 Hermes 会话生效；`~/.agents/skills/` 本地技能树**保留不删**
  （历史资产，已由 SGME 索引接管，本地目录退化为缓存）

### 3. 项目仓库处置

- D:\Projects\progressive-skill 归档保留（不删）；README 加一行
  「已被 SGME Skills 管理（ST-36/B106）取代，归档」即可

## 四、回退方案

若 SGME skills 模块异常需要临时回退：

1. config 的 skills.enabled=false（模块整体禁用，核心零影响）
2. 恢复 progressive-skill 插件条目 + audit.env 备份（观察模式）
3. 技能内容双源兜底：git 裸仓（真源）∪ wiki superseded 页（原件未删，可批量复活）

## 五、关联文档

- 设计：docs/design/SGME-Skills管理模块设计-v0.2.md（v0.2.1）
- 变更记录：docs/design/SGME-实施变更记录-v0.9.md B105/B106
- 运维：skills-hub.git hooks/audit_gate.py（执法后 FAIL=拒绝）
