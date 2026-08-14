# 一次性调试脚本归档

本目录存放已完成历史使命的一次性调试/验证脚本，保留仅作历史参考。

## 历史问题（2026-08-14，F-9）

曾归档 6 个硬编码本机路径读取密钥的调试脚本（check_usage / deepseek_usage / test_dsv4 系列等），
已从仓库移除——它们硬编码 `<用户目录>/AppData/Local/hermes/.env` 读取 DEEPSEEK_API_KEY，违反：
- AGENTS.md 架构约束 #10「密钥不落盘」
- 跨机器/跨用户不可用
- 泄露开发环境信息

如需复用类似脚本，请从 `os.environ.get("DEEPSEEK_API_KEY")` 读取环境变量。

## 现存脚本

| 脚本 | 用途 |
|---|---|
| `T53_check_*.py` | dsh 适配器（ST-26/T-53）入库验证 |
| `fix_wiki_tags_double_encoded.py` | wiki 标签双重编码修复（一次性） |
