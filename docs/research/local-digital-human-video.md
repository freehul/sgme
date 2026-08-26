# 本地数字人口播宣传片 · 模型调研（2026-08-26）

> 目标：为 SGME 宣传片补齐「本地数字人」环节。全链路必须本地开源——宣传片本身就是
> 「本地运行、隐私自主」卖点的活证明，禁用云端数字人/云端克隆声音。
> 触发：分析 abouttao《Agent 全自动做数字人视频》后，主人拍板走完整形态。

## 硬件与磁盘基线（本机实测）

| 项 | 值 | 结论 |
|---|---|---|
| GPU | RTX 4080 SUPER 16GB | 口型类模型全部可跑；扩散大模型看单模型需求 |
| C 盘余量 | 488G | HeyGem 要求 C 盘 100G+ ✅ |
| D 盘余量 | 779G | 模型权重放 D:\AI\models ✅ |

## 候选模型对比（数据来自官方仓库 README，2026-08-26 抓取）

| 模型 | Stars | 显存(推理) | 输入→输出 | Windows | 中文口型 | 定位 |
|---|---|---|---|---|---|---|
| **Duix.Avatar (HeyGem)** | 14.9k | 未标注(官方称需 NVIDIA) | 人像视频+音频→全身数字人视频 | ✅ 主推 Windows 客户端 | ✅ 国产、中文社区活跃 | 开源版黑根，最贴近 abouttao 工作流 |
| **MuseTalk 1.5** | 6.5k | 最低实测 4G(RTX3050Ti, fp16, 8s≈5min)；4080S 上快得多 | 人脸视频+音频→换嘴型 | ✅ 有 Windows 直跑命令 | ✅ 明确支持中文 | 实时口型替换，管线里最灵活的嘴型引擎 |
| **LivePortrait** | 19.0k | 轻量(4090 实时级) | 单照片/视频+驱动视频→表情迁移 | ✅ 一键整合包 | 音频驱动非原生(需配音频驱动方案) | 表情生动但主用视频驱动，做口播要绕 |
| **LatentSync 1.6** | 6.0k | **18GB ✗超 16G**；1.5 版 8GB 可跑 | 现成视频+音频→换嘴型 | Linux/Docker 友好，Win 部署摩擦大 | 论文向，中文无官方背书 | 社区评测质量基线，但 1.6 超显存 |

淘汰说明：
- LatentSync 1.6 推理 18G > 16G 直接出局；降级用 1.5 又丢质量优势，不值得引入。
- LivePortrait 强项是表情迁移（驱动视频），纯音频口播需另接口型模型，链路变长，作备选。

## 选型结论

**主力：Duix.Avatar(HeyGem)** —— 与 abouttao 用的黑根同源同形态（素材视频+音频→整段数字人视频），
Windows 原生支持、中文生态最好、开源免费，叙事上还能对标「开源版黑根」。

**备胎：MuseTalk 1.5** —— 若 HeyGem 效果不满意（口型糊/脸崩），用 MuseTalk 做「底板视频+换嘴型」
兜底；它也是未来做实时交互数字人的技术储备。

## 目标管线（全本地）

```
脚本(人写)
 → 声音克隆：NAS AngeVoice(MOSS-TTS-Nano / ZipVoice，重录干净样本重测)
 → 数字人：Duix.Avatar 本地生成口播段
 → 包装：现有 Python+FFmpeg 渲染管线(信息卡/大字/人物让位/布局翻转)
 → 自主优化循环：成片复盘→改流程参数→下轮迭代(借鉴 abouttao)
```

## 素材清单（说服力核心=本地运行实证画面）

1. 终端实跑 `sgme_memory_search` 带溯源结果；
2. 双 Agent 会话命中同一记忆（多智能体共享记忆可视化）；
3. inject 注入瞬间；
4. 断网全功能演示。

## 红线

1. 脚本举例一律中性虚构内容，禁家人健康/私人项目细节入镜；
2. 云端服务零预算；模型权重下载前先查 D:\AI\ 本地库。

## 端到端验证记录（2026-08-26 实测通过）

环境：RTX 4080 SUPER 16G / 内存 31.7G / Docker Desktop(WSL2, 引擎 29.7.2)

| 环节 | 结果 |
|---|---|
| GPU 容器穿透 | ✅ `nvidia/cuda:12.4` 容器内 nvidia-smi 正常识别 |
| 镜像拉取 | ✅ guiji2025/duix.avatar 15.1GB（Docker Hub 直连卡死，给 Docker Desktop 挂本地代理 127.0.0.1:7897 后约 3 分钟拉完；settings-store.json 已备份 .bak-20260826） |
| Lite 服务 | ✅ `docker compose -f docker-compose-lite.yml up -d`，容器 duix-avatar-gen-video 监听 8383，数据盘 D:\duix_avatar_data\face2face |
| 配音链路 | ✅ NAS AngeVoice(Kokoro, 男声 zm_009) → 16k 单声道 WAV（服务端未开 FFmpeg 转码只能要 wav） |
| 合成 | ✅ 10 秒底板视频 + 9.9 秒配音 → 成片 720p H.264+AAC，耗时 **27 秒**（约 0.37× 实时） |

### 关键坑位（必读）

1. **audio_url/video_url 必须传容器内绝对路径**（如 `/code/data/tts_std.wav`）。官方 http_api.http 示例里的裸文件名是坑——代码用 ffprobe 直接探测传入路径，裸名按进程 cwd=/code 解析会报「三次获取音频时长失败」。
2. 成品不在 result/ 在 **temp/**：`/code/data/temp/<code>-r.mp4`。
3. 任务接口：POST `/easy/submit {code, audio_url, video_url}` 异步 → GET `/easy/query?code=`，status 2=成 3=败。
4. Lite 版无 ASR/TTS 容器——声音必须外部生成后以 WAV 喂入（正好契合 AngeVoice 自有链路）。

### 克隆声音对比实测（2026-08-26，小何参考音频 casual 版）

前置改造：飞牛 AngeVoice 容器按商店模板重建（原容器是旧版装的，`ANGEVOICE_ENABLED_MODELS`
只生效了 kokoro；重建后 moss+zipvoice 启用，编排文件备份 `.bak-20260826`）；ZipVoice 权重
6 个文件经 PC 代理中转补齐至 `@appdata/AngeVoice/models/zipvoice/`（NAS 本机代理失效，
HuggingFace 直连不通）。

| 指标 | MOSS-TTS-Nano | ZipVoice |
|---|---|---|
| 合成耗时（热身） | **14.8s** | 91.5s |
| 成品时长 | 10.2s（正常语速） | 39.1s（异常拖长） |
| Whisper 回转写 | 全文正确（仅同音字「和/何」） | 大量乱码复读+幻觉，不可懂 |
| 接入姿势 | `/api/tts` multipart 带 `prompt_audio` | `voice=xiaohe` 档案模式 |
| 结论 | ✅ **主力方案** | ❌ NAS CPU 跑不动，弃用 |

注意点：ZipVoice 引擎优先匹配 `voice` 字段的保存档案，带临时参考音频时必须显式传空 voice；
MOSS 不支持保存档案，每次请求都要附参考音频（管线里固定带上即可）。
默认模型已恢复 kokoro（日常零成本播报），做视频时再切 moss。

### 更强引擎实测（2026-08-26 下午，PC RTX 4080 SUPER）

Nano 版不像后，主人批准放开 PC 显卡。下载前查本地发现现成资源：`D:\GitHubDownloads\MOSS-TTS`
（旗舰 8B 完整权重 32G）、`D:\AI\models\tts\CosyVoice2-0.5B/300M/CosyVoice3-5B`（注意：
CosyVoice3-5B 目录实为 Fun-CosyVoice3-**0.5B**）、MiniCPM-o venv（torch cu128）。

| 引擎 | 合成耗时 | 时长 | 转写 | 响度检测 |
|---|---|---|---|---|
| MOSS-TTS 8B bf16+CPU卸载 | 弃用（1.4s/token，预计 90min） | — | — | — |
| MOSS-TTS 8B 4bit(NF4) 贪心 | 238s | 9.36s | ✅ 全对 | mean -20.1dB |
| MOSS-TTS 8B 4bit 推荐采样参数(1.7/0.8/25) | 185s | 10.72s | ✅ 全对 | mean -20.6dB |
| Fun-CosyVoice3 0.5B（fp32 cuda） | **7s（rtf 0.54）** | 9.48s | ✅ 全对 | mean -23.8dB |

主人初判：MOSS-8B 贪心版「有点像」；三版 A/B 待最终定夺。

**✅ 终局裁决（2026-08-26 主人定）：三版效果都可接受，以效率定输赢——Fun-CosyVoice3 胜出，
为宣传片配音主力引擎**（7 秒出片 vs MOSS-8B 的 185 秒，快约 26 倍；MOSS-8B 作为高表现力备选保留）。

关键经验：
1. MOSS-TTS 8B 的 generate 签名自带 audio_temperature/top_p/top_k（官方推荐值即默认值），
   不要传 do_sample（会 TypeError）；bf16 需 16G 显存放不下，4bit NF4 后占 ~13.6G 可全进卡；
2. 处理器联网拉 Audio-Tokenizer 的解法：`AutoProcessor.from_pretrained(..., codec_path=<本地目录>)`，
   分词器权重(7G)已归档 D:\AI\models\tts\MOSS-Audio-Tokenizer；
3. ⚠️ 大模型加载前必须显存预检（桌面应用常驻占用 6G+），脚本已内置检查（空闲<7G 拒跑）；
4. CosyVoice3 环境：FunSpeech services/cosyvoice 用 uv 管理（venv 无 pip，装包走
   `uv pip install --python .venv/Scripts/python.exe`），torch 换 2.3.1+cu121；
   Matcha-TTS 子模块缺失从 voice-pro 复制补齐；旧记忆「torch≥2.2 输出静音」在新代码库不成立
   （响度/转写双验证通过）。

### 待办余项

1. 客户端 v1.0.6-lite 已装（C:\Program Files\Duix.Avatar），GUI 流程未走通验证；
2. 底板视频需重录主人本人出镜说话素材（现冒烟用的是抖音分析视频片段）；
3. ~~克隆声音对比~~ ✅ MOSS-Nano vs ZipVoice：MOSS 胜出；~~更强引擎~~ ✅ MOSS-8B / CosyVoice3 已实测，待主人三版 A/B 定胜者；
4. 胜者引擎接入数字人管线出完整口播小样；建议重录 30-60s 干净参考音频再提相似度；
5. 模型社区许可协议 PDF 在仓库根目录，商用前过一眼条款。
