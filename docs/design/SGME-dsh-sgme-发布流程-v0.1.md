# dsh-sgme npm 发布流程

> 日期：2026-08-17 ｜ 来源：dsh-sgme 0.2.0 发布实操（含踩坑）｜ 状态：可复用手册

## 一、前置条件

1. 版本号递增：`adapters/dsh/sgme-bridge/package.json` 的 `version`（新功能 minor，如 0.1.2 → 0.2.0）
2. `pnpm run verify` 全绿（typecheck + 103 测试 + build，lib/index.js 已重新生成）
3. 确认 `lib/` 是最新 build（含本次代码改动）

## 二、npm 认证（关键：granular token 三要素缺一不可）

> npm 政策已收紧（2026-08）：automation 类型已撤，只剩 granular 一种；granular 生成时权限定死，事后网页改不了，必须重新生成。

1. npmjs.com → 头像 → Access Tokens → Generate New Token → 类型选 **Granular**
2. 三个区块都要填：
   - **Token name**：随意
   - **IP allowlist**：`43.255.156.6/32`（VPS 出口 IP）
   - **Packages and scopes**：Add Packages → 选 `dsh-sgme` 或 All packages → 勾 **Read and write** ← 最容易漏
3. 生成后复制新 token（只显示一次）→ 更新 `D:/Projects/SGME/.env` 的 `NPM_KEY=` 整行
4. 校验：新 token 前缀一定 ≠ 旧值（`Get-Content .env | Select-String NPM_KEY` 看前缀变化）

## 三、发布命令（必须走 7897 代理）

> 出口 IP 必须匹配 token 的 IP 白名单（VPS 43.255.156.6）。直连出口是家里 IP → 404；走 Clash 7897 代理 → 出口 VPS → 匹配。

```powershell
cd D:/Projects/SGME/adapters/dsh/sgme-bridge
$token = (Get-Content "D:/Projects/SGME/.env" | Where-Object { $_ -match '^NPM_KEY=' } | ForEach-Object { ($_ -split '=',2)[1] }).Trim()
Set-Content -Path ".npmrc" -Value ("//registry.npmjs.org/:_authToken=" + $token) -NoNewline -Encoding utf8
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
$env:HTTP_PROXY   = "http://127.0.0.1:7897"
npm publish --ignore-scripts    # 跳过 prepublishOnly（verify 已在发布前跑过）
Remove-Item ".npmrc" -ErrorAction SilentlyContinue
```

成功标志：`+ dsh-sgme@<版本>`；`--ignore-scripts` 用于避免 vitest 内存不足重崩（若机器内存充足可去掉）。

## 四、验证 + 收尾

1. `npm view dsh-sgme version` 返回新版本、`dist-tags.latest` 指向它
2. git 提交 `package.json`（version 变更）并 push gitee + GitHub
3. 用户侧 `dsh plugin add dsh-sgme` 即拿到新版

## 五、RC 兼容验证（DSH 新 RC 必做，2026-08-20 定）

> DSH 处于 RC 快速迭代期（官方明示 "THERE WILL BE COMPATIBILITY-BREAKING CHANGES"）。
> peer 声明 `^0.1.0-rc.6` 会自动解析到最新 RC——**新 RC 发布后必须先验证再承诺兼容**。

**验证方法**（实测 rc.7/rc.8 用，零 LLM 成本）：
1. 临时目录建 package.json：devDependencies 锁目标 RC（`@deepseek-ai/dsh-tools` / `dsh-commands` 精确到 `0.1.0-rc.N`；dsh-llm/dsh-session/dsh-system-prompt 用 `^0.1.0-rc.N` 避免传递冲突）
2. 拷 `src/` `tests/` `vitest.config.ts` `tsconfig.json` 进临时目录
3. `npx tsc --noEmit` 必须 exit 0；`npx vitest run` 必须全绿
4. 通过后更新 README 支持矩阵 + 本插件 peer 声明基线

**已验证矩阵**：rc.6（开发基线）/ rc.7 / rc.8 全部 typecheck + 136 测试通过（2026-08-20）。
dsh-tools rc.6→rc.8 类型零差异；dsh-commands 仅新增图片附件字段（向后兼容）。

## 六、踩坑记录（只增不改）

- **404 权限不足**：granular token 没给 dsh-sgme 包授权（Read and write），或出口 IP 不在白名单。排查：`npm view dsh-sgme maintainers` 确认 owner 是 freehul；用 Python `httpx.get('/-/whoami', proxy='http://127.0.0.1:7897')` 若 200=token 有效且代理出口对，若 403=IP 不匹配。
- **403 2FA required**：`npm login`（网页登录）的 session 不吃 granular 的 IP 白名单，发布必强制 OTP（而本机无手机认证器）→ 只能走 granular token 路线。
- **automation 类型已撤**：npm 收紧 2FA-bypass GAT（GitHub Discussion #201329），生成页不再有类型可选。
- **.env 没真更新**：网页授权 ≠ 写入本地文件；更新后必须确认 .env 前缀变化、修改时间刷新。
- **内存不足**：publish 触发 prepublishOnly→verify 时 esbuild 可能 `cannot allocate memory`，用 `--ignore-scripts` 跳过（verify 提前跑过即可）。
- **NODE_ENV=production 跳过 devDependencies**（2026-08-20 实锤）：用户级环境变量 `NODE_ENV=production` 时 pnpm/npm install 全部跳过 devDependencies（报 "devDependencies: skipped because NODE_ENV is set to production"）——临时校验环境装不上 typescript/vitest 的根因。处理：脚本内 `$env:NODE_ENV = ''` 显式清空（每次 pwsh 新进程都要设）或 install 加 `--include=dev`。长期建议排查该环境变量是否故意设置。
- **`^0.2.0` 不升 0.3.0**（2026-08-20）：npm 0.x 系列 `^` 锁 minor——profile 依赖 `"dsh-sgme": "^0.2.0"` 时 pnpm install 不会自动升 0.3.x，必须显式改 `^0.3.0` 再 install；升级主版本号后记得同步更新各 profile 的依赖声明。