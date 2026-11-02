## 部署与升级踩坑

**① `tar --exclude=data` 会排掉代码包**

- **现象**：升级包解开、容器重启后起不来，日志 `ModuleNotFoundError: No module named 'sgme.data'`（或其它 `sgme.*` 缺失）。
- **判断**：`--exclude=data` 按名字匹配**任意层级**的 `data` 目录，把 `sgme/data/` 这个代码包一起排掉了。包体不完整，不是依赖问题。
- **处置**：改用 `--exclude=./data`，只排归档根下的用户数据目录；打包后必须核对代码包在不在：

```bash
tar --exclude=./data -czf sgme-2.0.tar.gz -C <project-root> .
tar -tzf sgme-2.0.tar.gz | grep -c '^\./sgme/data/'   # 必须 >0
```

  已经装坏就重打重传，不要靠补 `__init__.py` 或手工拷目录糊过去（会漏模块）。

**② 技能 lint 不能拿库里的 `content` 直接调 `lint_skill`**

- **现象**：全库扫描报「缺少 frontmatter」，几百条全红。
- **判断**：入库时 frontmatter 已拆成 `name`/`description` 等列，`skills.content` 只剩正文；对库内数据要求 `---` 围栏必然全量误报，不是技能真的坏了。
- **处置**：走真实入口，别自己拼参数：

```bash
python scripts/lint_all_skills.py --db <home>/data/skills.db --json
python scripts/lint_all_skills.py --source-dir <技能目录>          # 目录模式
```

  只有**写入口**（用户提交 SKILL.md 原文）才该 `require_frontmatter=True`。
