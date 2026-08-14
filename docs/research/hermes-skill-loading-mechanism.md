# Hermes Skill 加载机制分析

> 日期：2026-08-08
> 背景：讨论 SGME 的 skills-hub 模块设计时，探讨 skill 是否可以完全存入数据库而非文件系统

---

## Skill 的本质

Skill 是一个带 YAML frontmatter 的 Markdown 文件：

```markdown
---
name: video-analysis
description: 分析视频内容...
triggers: [视频分析, 分析这个视频]
---
# 正文（Agent 操作指令）
...
```

和 Wiki 页面的结构几乎相同——标题、标签、正文。区别不在存储格式，在**被消费的方式**。

---

## Hermes 的 Skill 加载链路

```
Hermes 启动 或 /reload-skills
  → 扫描 ~/.hermes/skills/ 目录（硬编码，只认根目录）
  → 解析每个 SKILL.md 的 YAML frontmatter
  → 注册触发器（name/description/triggers 匹配）
  → 匹配命中时将 skill 内容注入 system prompt
  → system prompt 有 LRU 缓存
  → pre_llm_call hook 触发（此时 skill 已在 prompt 中，只能追加 user message）
```

### 关键事实

| 事实 | 影响 |
|------|------|
| Skill loader **只扫描文件系统** | 无法直接替换为数据库读取 |
| Skill 加载在 **prompt 组装之前** | 无法在 context 组装阶段拦截 |
| `pre_llm_call` hook 只能注入 **user message** | 不能修改已组装的 system prompt |
| system prompt 有 **LRU 缓存** | 修改 skill 文件后需 `/reload-skills` 或重启清除缓存 |
| `/reload-skills` 手动触发重新扫描 | 需要人工或定时触发 |

---

## 能否拦截 Skill 加载？

### 不可行的方案

1. **context.engine 槽位拦截**
   - context.engine 是消费端——拿到的已经是包含 skill 的完整 prompt
   - 只能做"已有内容的后处理"，不能替换 skill 的来源

2. **memory.provider 伪装**
   - memory provider 是可插拔的，但 skill 结构（触发器 + 指令体）和记忆（事实 + 标签）不兼容
   - 硬套会失去 skill 的自动触发器匹配能力

3. **pre_llm_call hook**
   - 触发时机太晚（prompt 已组装完毕）
   - 只能追加 user message，不能修改 system prompt
   - 会导致 skill 重复（文件版已经在 prompt 里了，DB 版以 user message 又追加一次）

### 理论可行的方案

**1. 文件系统同步（唯一可靠方案）**

```
wiki.db（skill 内容）
    ↓ 同步 daemon
skills-hub/（Git 仓库，用户编辑本体）
    ↓ map/copy 策略
%LOCALAPPDATA%/hermes/skills/（Hermes 运行时部署副本）
    ↓ Hermes 启动时扫描
system prompt
```

- Windows 上可用 junction / symbolic link 实现 `map` 模式
- NAS/VPS 上可用 `copy` 模式 + 定时同步
- 这是目前唯一不需要修改 Hermes 源码的方案

**2. 给 Hermes 提 PR（长期方案）**

在 Hermes 的 `agent/prompt_builder.py` 中加一个 `skill_provider` 可插拔接口：

```python
# 伪代码
class SkillProvider(ABC):
    def list_skills(self) -> list[SkillDef]: ...
    def load_skill(self, name: str) -> str: ...

class FileSystemSkillProvider(SkillProvider):
    # 现有逻辑

class DatabaseSkillProvider(SkillProvider):
    # 从 SQLite 读取
```

配置项：`skills.provider: filesystem | database`

**3. FUSE 虚拟文件系统（hack）**

在 Linux 上用 FUSE、Windows 上用 WinFsp 在 `~/.hermes/skills/` 上挂一个虚拟文件系统，底层数据从 DB 读取。Hermes 无感知，以为在读普通文件。

- 优点：完全透明，不需要改 Hermes
- 缺点：增加系统复杂度，出问题难排查

---

## 和 Wiki 的关系

Wiki 知识和 Skill 在存储层面可以统一（都在数据库），但消费端不同：

| | Wiki 页面 | Skill |
|------|-----------|-------|
| 消费方式 | SGME search API → 按需查询（pull） | Hermes 文件扫描 → 自动匹配触发（push） |
| 消费者 | SGME 的 data/search | Hermes 的 prompt_builder |
| 可拦截 | ✅ SGME 的 search 是我们自己控制的 | ❌ Hermes 的 skill loader 是闭包的 |

---

## 结论

**Hermes skill 加载机制目前无法被拦截替换。** 和 context 重组不同——context 有 `context.engine` 是设计上预留的扩展点，skill 加载没有对等的设计。

如果想把 skill 统一管理到数据库，可行的路径是：
1. **短期**：DB → 文件同步 daemon（最稳）
2. **长期**：给 Hermes 提 PR 加 `skill_provider` 接口（最彻底）
