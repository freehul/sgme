# T-123 分类治理审核清单（uncategorized → 建议）

> 生成：2026-08-30 agnes-2.5-flash 预分类 102 条；白名单校验修正 0 条为待人工定（suggested=null）。应用走写侧 PUT（改源 frontmatter，单一真相源），逐条确认后执行 scripts/t123_apply.py。

| name | 建议 category | 理由 | description（前80字） |
|---|---|---|---|
| apple-notes | software-development | 通过CLI管理Apple备忘录的应用工具 | Manage Apple Notes via memo CLI: create, search, edit. |
| apple-reminders | software-development | 通过CLI管理Apple提醒事项的应用工具 | Apple Reminders via remindctl: add, list, complete. |
| architecture-diagram | design | 生成架构图/云图SVG可视化设计 | Dark-themed SVG architecture/cloud/infra diagrams as HTML. |
| arxiv | research | 搜索学术论文的学术研究工具 | Search arXiv papers by keyword, author, category, or ID. |
| ascii-art | creative | ASCII艺术创作与图像处理工具 | ASCII art: pyfiglet, cowsay, boxes, image-to-ascii. |
| ascii-video | media | 视频音频转ASCII艺术格式处理 | ASCII video: convert video/audio to colored ASCII MP4/GIF. |
| baoyu-infographic | design | 信息图设计与可视化布局生成 | Infographics: 21 layouts x 21 styles (信息图, 可视化). |
| blocked-page-recovery | security | 绕过WAF/付费墙恢复页面访问 | Recover blocked/paywalled/WAF'd pages via fallbacks. |
| blogwatcher | research | 监控博客RSS源的研究跟踪工具 | Monitor blogs and RSS/Atom feeds via blogwatcher-cli tool. |
| box | software-development | 云端文件管理与共享开发工具 | Box manages cloud files, sharing, search, and metadata. |
| browser-annotation | design | 网页视觉标注与交互反馈工具 | Visual feedback on web pages. Use when user says 'annotate'. |
| chengqianbihou-zhibing-jiuren | methodology | 团队错误处理的管理方法论 | 当用户需要处理团队成员的错误时激活，惩前毖后治病救人。 |
| claude-code | software-development | 委托AI代理代码开发的工具 | Delegate coding to Claude Code CLI (features, PRs). |
| codebase-inspection | software-development | 代码库分析与统计的软件开发工具 | Inspect codebases w/ pygount: LOC, languages, ratios. |
| comfyui | media | 基于扩散模型的图像视频音频生成 | Generate images, video, and audio via diffusion workflows. |
| comfyui-cloud-api-polling | media | 云端GPU调用ComfyUI生成媒体内容 | 用户想用云端 GPU 跑 ComfyUI 工作流、本地显存不够想换平台、想做 SaaS 服务、想批量生成不计本地资源、或在 CI/CD 中集成时调用此 skil |
| comfyui-custom-node-skeleton | software-development | 开发ComfyUI自定义节点的编码工具 | 用户想从零写一个 ComfyUI 自定义节点, 或节点报 RETURN_TYPES / IS_CHANGED / NODE_CLASS_MAPPINGS 相关错 |
| comfyui-frontend-extension | software-development | 扩展ComfyUI前端UI的编码工具 | 用户想给自定义节点加 UI / 加侧边栏 Tab / 加快捷键 / 加 Settings / 监听画布事件 / 做 APP mode 表单 / 加 i18n 多 |
| comfyui-install-decision-tree | software-development | ComfyUI安装路径决策的部署工具 | 用户首次安装 ComfyUI、更换设备/GPU、或问"我该用 Desktop/Portable/Manual 哪种方式"时调用,用于按"平台 × GPU × 隔 |
| comfyui-is-changed-cache-strategy | software-development | 自定义节点缓存策略的编码开发 | 当自定义节点出现"输出不更新"、"读了新文件但结果还是旧的"、"随机数节点每次结果一样"、"不确定要不要定义 IS_CHANGED"时调用。不适用于: 想写整个 |
| comfyui-local-api-integration | software-development | 本地API集成与自动化工作流开发 | 用户想用代码调用 ComfyUI、批量生成、做 Web 应用集成、实时显示生成进度、或自动化工作流时调用此 skill。覆盖 POST /prompt 提交 + |
| comfyui-mcp-integration | software-development | AI代理MCP协议集成的开发工具 | 用户想让 AI agent (Claude Code/Cursor/Claude Desktop/Codex) 操作 ComfyUI 生成图像/视频/3D/音频 |
| comfyui-model-architecture-match | software-development | 模型架构匹配与兼容性的开发工具 | 用户在 ComfyUI 模型加载报错 / 生成花图 / ControlNet 无效果 / 不确定 Latent 尺寸 / 混用不同架构模型 (Flux/SD1. |
| comfyui-performance-tuning-flags | software-development | ComfyUI性能调优与参数配置开发 | 用户在 ComfyUI 能正常运行但想优化速度/显存时调用 — 选对启动参数组合 (缓存/精度/预览/注意力)。
trigger 词: "性能调优" / "pe |
| comfyui-registry-publishing | software-development | 发布自定义节点到注册中心的开发 | 用户想把自己写的 ComfyUI 自定义节点发布到 Comfy Registry 让其他人能搜索安装,或想更新节点版本号 (semver),或想理解为什么已发布 |
| comfyui-troubleshoot-bisect | software-development | 定位ComfyUI故障的调试开发工具 | 用户在 ComfyUI 启动崩溃 / 工作流执行报错 / 节点加载失败 / 自定义节点更新后出问题时调用,用于按"自定义节点优先怀疑 + 二分搜索"定位故障源。 |
| comfyui-vram-degradation | ai | Stable Diffusion显存优化 | ComfyUI 启动或生成图像时报 "CUDA out of memory" / "显存不足" / "OOM" 时调用 — 通过渐进降级启动参数 (--lowv |
| comfyui-workflow-dag-design | ai | ComfyUI工作流节点设计 | 用户想自己设计 ComfyUI 工作流 / 不知道该用哪些节点 / 节点连接报错 / 想从 txt2img 改造成 img2img/inpaint/outpai |
| comfyui-workflow-format-conversion | ai | ComfyUI API格式处理 | 用户想用 API 调用 ComfyUI 工作流(本地 Server API 或 Cloud API)、拿到一份工作流 JSON 但不确定是 UI 还是 API  |
| competitor-news-monitor | research | 竞品情报监控与摘要 | Watch named companies for material news; cited digests. |
| content-engine | social-media | 多平台内容分发系统 | 多平台内容系统——X/LinkedIn/YouTube/Newsletter原生适配。 |
| document-to-action-items | data | 文档信息结构化提取 | Extract cited obligations, deadlines, tasks from documents. |
| docx | data | Word文档读写编辑 | Create, read, edit, template, and review Word .docx files. |
| email-inbox-triage | email | 邮件优先级分类与回复 | Triage an inbox: prioritize threads, draft replies safely. |
| excalidraw | design | 手绘风格架构图绘制 | Hand-drawn Excalidraw JSON diagrams (arch, flow, seq). |
| fangxia-baofu-kaidong-jiqi | methodology | 放下包袱的思想工作方法 | 当用户被包袱束缚无法轻装前进时激活，放下包袱开动机器。 |
| firecrawl | research | AI网页爬取与结构化提取 | Firecrawl — AI 原生的网页抓取/搜索/爬取 API + MCP Server。 支持 JS 渲染、结构化提取、浏览器交互、站点爬取、网页监控。 通 |
| frontend-slides | creative | 动画HTML演示文稿创作 | 当用户说'做个PPT/H5演示'时触发。动画HTML演示文稿 |
| gif-search | media | GIF动图搜索下载 | Search/download GIFs from Tenor via curl + jq. |
| github-code-review | github | GitHub PR审查与评论 | Review PRs: diffs, inline comments via gh or REST. |
| grounded-citations | research | 答案来源引用验证 | Ground answers and documents in cited, verifiable sources. |
| home-assistant-camera-setup | devops | IoT摄像头接入配置 | RTSP/ONVIF 摄像头接入/camera HA integration/camera IP probe —— IP摄像头接入Home Assistant： |
| huggingface-hub | ai | HF模型数据集管理 | HuggingFace hf CLI: search/download/upload models, datasets. |
| hyperframes | creative | 视频动画创作系统入口 | Mandatory entry point: read this first for any request to make, create, edit, an |
| hyperframes-animation | creative | 动画运动规则与场景设计 | All animation knowledge for HyperFrames — atomic motion rules, multi-phase scene |
| hyperframes-cli | software-development | HyperFrames开发工具链 | Use the HyperFrames CLI development loop: init, add, catalog, capture, lint, che |
| hyperframes-creative | creative | 视频创意方向与设计规范 | Non-animation creative direction for HyperFrames videos. Use for design spec (fr |
| hyperframes-keyframes | creative | 2D/3D关键帧动画技术 | Use when a HyperFrames composition needs seek-safe 2D/3D keyframes, GSAP timelin |
| imessage | social-media | iMessage/SMS消息收发 | Send and receive iMessages/SMS via the imsg CLI on macOS. |
| jianmiezhan-jizhong-bingli | methodology | 集中优势突破的方法论 | 当用户资源有限面临多个机会威胁时激活，集中优势兵力突破。 |
| llama-cpp | ai | 本地GGUF推理引擎 | llama.cpp local GGUF inference + HF Hub model discovery. |
| macos-computer-use | software-development | macOS桌面自动化控制 | Drive the macOS desktop in the background — screenshots, mouse, keyboard,
scroll |
| manim-video | creative | 数学动画视频制作工具 | Manim CE animations: 3Blue1Brown math/algo videos. |
| maps | data | 地理编码和POI数据服务 | Geocode, POIs, routes, timezones via OpenStreetMap/OSRM. |
| meeting-action-items | software-development | 会议记录转任务追踪流程 | Turn meeting notes into cited decisions, owners, tickets. |
| notion | software-development | Notion笔记与数据库管理 | Notion API + ntn CLI: pages, databases, markdown, Workers. |
| nuwa-skill | ai | AI驱动的技能生成工具 | 女娲造人：输入人名/主题/甚至只是模糊需求，自动深度调研→思维框架提炼→生成可运行的人物Skill。
两种入口：(1)明确人名→直接蒸馏 (2)模糊需求→诊断推 |
| obsidian | software-development | Obsidian笔记库管理工具 | Read, search, create, and edit notes in the Obsidian vault. |
| ocr-and-documents | ai | OCR文字识别与文档提取 | Extract text from PDFs/scans (pymupdf, marker-pdf). |
| official-method-first | methodology | 坚持官方最佳实践的方法论 | 必须使用官方方式，不能因为能跑就用自定义方案 |
| opencode | devops | 自动化代码委托与PR审查 | Delegate coding to OpenCode CLI (features, PR review). |
| openhue | linux | 智能设备控制的CLI工具 | Control Philips Hue lights, scenes, rooms via OpenHue CLI. |
| openhuman-reuse | software-development | OpenHuman代码复用策略规范 | 复用 OpenHuman 代码/吉祥物模块开发/Rive / Ghosty / 口型 / 状态机 —— OpenHuman 源码复用策略——有现成的不重写，直接 |
| p5js | creative | p5.js创意编程与可视化 | p5.js sketches: gen art, shaders, interactive, 3D. |
| partnership-prep | software-development | 合作方案准备方法论 | 合作文档准备——合作方案、电话脚本、对比报告。当用户说'准备个合作方案'、'给投资人pitch'、'合作PPT'时触发。 |
| pdf | software-development | PDF文件的创建与处理工具 | Create, read, merge, fill, and secure PDF files. |
| peer-review-workflow | software-development | 双模型代码审查工作流 | 双模型互检工作流：Hermes设计方案/代码，DeepSeek审查并提出改进建议，Hermes整合后再次审查。
适用于：架构设计、方案评审、代码审查等需要高质量 |
| popular-web-designs | design | 流行网页设计系统参考实现 | build a page that looks like/make it look like stripe —— 54 real design systems  |
| powerpoint | creative | PPT创建与编辑工具 | Create, read, edit .pptx decks with python-pptx. |
| product-price-monitor | data | 商品价格监控与预警服务 | Watch product, flight, or listing prices; alert on target. |
| production-audit | devops | 生产环境上线审计流程 | 生产就绪审计——上线前检查、post-merge验证、风险评估。 |
| project-lifecycle-tracking | methodology | 项目全生命周期管理规范 | 项目立项与追踪体系。触发：立项、开搞、新建项目、需求池、创意池、项目注册表、问题追踪、D:\Projects 整理。 |
| requesting-code-review | software-development | 代码提交前审查工作流 | Pre-commit review: security scan, quality gates, auto-fix. |
| research-paper-writing | research | ML论文写作投稿指导 | Write ML papers for NeurIPS/ICML/ICLR: design→submit. |
| research-workflow | research | 技术调研与知识管理流程 | 调研/搜索/搜集/对比分析/技术选型 —— 调研工作流规范：从搜索到存档的完整流程。确保每份调研产出存到正确的地方（本地+NAS+本地记忆），不遗漏、不重复。适 |
| sdlc-review | methodology | SDLC流程评审与看板规范 | Review Kanban handoffs and route verified outcomes. |
| segment-anything | ai | SAM零样本图像分割模型 | SAM: zero-shot image segmentation via points, boxes, masks. |
| shijian-renshilun | methodology | 实践导向的决策方法论 | 当用户陷入分析瘫痪或盲目行动时激活，强调实践出真知。 |
| shiliuzijue | methodology | 游击战术十六字诀，竞争策略思维框架 | 当用户需要根据竞争对手状态选择行动节奏时激活，游击战十六字诀。 |
| sketch | creative | HTML设计原型制作，多方案视觉对比 | Throwaway HTML mockups: 2-3 design variants to compare. |
| skill-production-pipeline | research | 知识萃取流程，书籍内容提炼为技能 | Use when distilling books into skills or evolving skills. |
| skill-script-packaging | software-development | Python脚本依赖管理与打包部署 | Use when a skill needs Python scripts with external deps. |
| songsee | media | 音频频谱特征提取，Mel/MFCC分析 | Audio spectrograms/features (mel, chroma, MFCC) via CLI. |
| teams-meeting-pipeline | software-development | 企业协作工具集成，会议数据自动化 | Teams meeting summaries, job replay, Graph subscriptions. |
| test-driven-development | software-development | TDD开发范式，先写测试再实现代码 | TDD: enforce RED-GREEN-REFACTOR, tests before code. |
| token-budget-advisor | software-development | 上下文窗口优化，技术成本顾问 | token预算顾问——上下文窗口消耗审计+优化建议。 |
| tongyi-zhanxian-duli | methodology | 统一战线策略，合作中的独立性思维 | 当用户需要在合作中保持独立性时激活，统一战线中的独立自主。 |
| touchdesigner-mcp | media | TouchDesigner创意工具，实时视觉控制 | Control TouchDesigner via twozero MCP. |
| trae-ide-hooks-integration | software-development | IDE自动化钩子，开发工作流定制 | trae hooks/trae ide automation/hooks.json trae —— trae hooks/trae ide automation |
| web-scraping-strategy | devops | 网页数据采集策略，Jina优先方案 | Use when scraping web pages. Jina first, Firecrawl fallback. |
| weekly-review-planning | methodology | 周度复盘规划，工作节奏管理 | Weekly reset: commitments, stalled work, next-week plan. |
| wiki-skills-manager-rules | software-development | 技能库管理系统，分类规范读写 | 处理 wiki 技能库（入库/查询/改分类/更新技能页）时必读。SGME wiki skill 页读写分类规范。 |
| windows-desktop-automation | windows | Windows桌面自动化，Computer Use决策 | Windows 桌面自动化决策框架：Computer Use (cua-driver) vs Windows MCP 的选择指南、
cua-driver Win |
| workbuddy-expert-handoff | methodology | 专家团队协作，工作交接流程规范 | 用户让WorkBuddy专家团干活时使用。验证评审结论、写交接单、划清分工边界，不抢活。 |
| xingxingzhihuo-genjudi | methodology | 星星之火策略，从0到1扩张路径 | 当用户处于早期弱小阶段需要规划从0到1扩张路径时激活。 |
| xlsx | data | Excel工作簿处理，CSV数据读写 | Create, read, edit Excel .xlsx workbooks and CSVs. |
| xurl | social-media | Twitter/X平台API，社交内容管理 | X/Twitter via xurl CLI: raw post search, posting, DM, media. |
| youtube-content | media | YouTube视频转文字，内容提炼摘要 | YouTube transcripts to summaries, threads, blogs. |
| youxiao-goutong-wenti-jiuej | methodology | 有效沟通与问题解决，障碍突破框架 | 当用户遇到沟通障碍或问题解决困难时激活，有效沟通与问题解决。 |
| yuanbao | social-media | 元宝群协作，@提及查询企业社交 | Yuanbao (元宝) groups: @mention users, query info/members. |
| zhangxuefeng-perspective | research | 张雪峰教育战略，思维模型深度调研 | 张雪峰的思维框架与表达方式。基于5本著作、15+篇权威媒体深度采访、
30+条一手语录、11个关键决策记录和完整人生时间线的深度调研，
提炼5个核心心智模型、8 |
| zhudongxing-linghuoxing-jihuaxing | methodology | 团队执行三性，主动灵活计划框架 | 当用户团队执行力出现问题时激活，主动性灵活性计划性。 |
