# Solacut — 本地 NLE 游戏录屏高光切片与六语种母语精翻工作台

> 🌐 **官方网站**：[**https://solacut.com**](https://solacut.com)（便携包下载与 SHA-256 核验）｜📖 **新手必看**：[**《Solacut 操作手册 (USER_MANUAL.md)》**](./USER_MANUAL.md)（软件内按 `F1` 随时打开）｜🔒 [`SECURITY.md`](./SECURITY.md)｜📄 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)

一个纯本地起手的专业桌面级游戏录屏高光自动切片与类达芬奇审片工作台，将数小时直播通过「OBS 多音轨人声带通探测 + 多引擎语音识别 + 6 语种 22.8 万商用语料（可扩展至 60 万研究口语库）RAG 与三道零漏翻闭环 + 原生桌面 NLE 审片工作台（含媒体池虚拟文件夹分类）+ FFmpeg 无损/竖屏双语渲染」变成可交付的短视频成片与剪辑工程。

## 最快捷使用：启动原生桌面审片工作台

直接运行内置桌面启动脚本，或在终端运行：

```powershell
# 启动原生无边框桌面工作台（推荐）
.\.venv\Scripts\pythonw.exe src/desktop_app.py

# 或以浏览器调试模式启动
.\.venv\Scripts\python.exe src/desktop_app.py --browser
```

启动后将打开原生暗房工作台：
- 首次启动无 Key 可一键点击「体验 30 秒示例」零门槛体验完整审片、双语字幕修改与导出闭环；
- 支持自动探测 OBS 多音轨并智能推荐纯人声麦轨，默认开启 300~3400Hz 人声带通防低频爆炸误报；
- 支持任意源语种与译文语种自由搭配（中/英/日/韩/俄/西），内置媒体池虚拟文件夹自由分类、J/K/L/I/O 与 100 步原子撤销栈；
- 一键导出公会无损包（附带 CMX 3600 EDL 与 DaVinci CSV 标记表）或 1080×1920 双语竖屏爆款成片。

## 模块化架构与命令行流水线

0. **`src/config.py` / `src/pipeline.py` / `src/web_app.py`（已上线）**：统一三层接口路由配置（`ASR_*` 语音识别 + `SCREEN_LLM_*` 粗筛 + `POLISH_LLM_*` 双语精修）、全阶段参数指纹感知断点续跑流水线与 FastAPI 图形化工作台服务。
1. **`src/step1_audio.py`（已完成）**：支持 `FFprobe` OBS 多音轨自动探测与指定选轨提取 16kHz 全频段 WAV（无损保留清辅音），并在测算高能峰值时用纯 `numpy` FFT（Parseval 定理）执行 300~3400Hz 核心人声频段带通，滤除低频爆炸/枪炮与 BGM 干扰。
2. **`src/asr_providers.py` / `src/step2_asr.py`（已完成）**：独立语音识别引擎层，默认走 **DeepSeek 云端 ASR（`POST /v1/tools/asr`）**（配合内存级轻声响度归一化 + 敏感 Silero VAD 精确切句，既捞回小声吐槽又彻底消灭僵尸长字幕），并可随时一键切换为 **本地 RTX 增强版 `faster-whisper`（默认 `large-v3-turbo` + 调优 VAD + 热词表）** 或 **通用 OpenAI 兼容 `/audio/transcriptions`（Groq / OpenAI / 硅基流动）**。
3. **`src/llm_client.py` / `src/step3_llm.py`（已完成）**：生产级 LLM 调度层（连接池复用、429/5xx 指数退避、思考开关与推理参数自适应、Token 账单统计）；默认采用 **`deepseek-flash`**（1M 上下文）按直播时长自适应精选高光，并并发执行「整句意群语义重组 + 听写纠错 + 字幕组地道本土化意译」。
4. **`src/step4_cut.py`（已完成）**：调用 `FFmpeg` 切片，支持 `lossless` 原画无损快切（含长 GOP 关键帧偏移补偿对齐 SRT）、达芬奇/PR 时间线标记文件（CSV + EDL）、以及 `vertical`（9:16 模糊背景 + 顶部标题条 + ASS 分层双语字幕）/`landscape` 成片渲染。

## 为什么锁定 Python 3.11？

系统里即使有更新的 Python（如 3.14），`faster-whisper`、`ctranslate2`、音视频与 CUDA 生态包通常在新版本刚发布时缺少预编译 wheel，直接安装极易报错编译失败。因此本项目通过 `uv` 显式锁定 **Python 3.11** 独立虚拟环境（`.venv`）。

## 环境激活与使用（Windows PowerShell）

进入项目根目录后，按需激活虚拟环境（也可以直接使用 `.venv\Scripts\python.exe` 运行）：

```powershell
.\.venv\Scripts\Activate.ps1
```

## 运行 Step 1：分析游戏录屏音频高光

将你的测试录屏放到 `input/` 目录，或直接传入视频绝对路径：

```powershell
.\.venv\Scripts\python.exe src/step1_audio.py input/你的游戏录屏.mp4

# 先查看 OBS 多音轨视频包含哪些音轨（自动推荐纯人声麦轨）
.\.venv\Scripts\python.exe src/step1_audio.py input/obs_multitrack.mkv --list-streams

# 指定提取第 1 号纯人声麦轨进行分析（默认音轨 0，默认开启 300~3400Hz 人声带通）
.\.venv\Scripts\python.exe src/step1_audio.py input/obs_multitrack.mkv --audio-stream 1
```

常用可选参数：
- `--output-dir`：输出目录（默认 `output`）
- `--list-streams`：仅探测并打印视频包含的全部音频轨道信息
- `--audio-stream`：指定抽取视频的第几条音轨（`0` 起算，适合 OBS 多音轨单独选人声麦轨）
- `--no-vocal-bandpass`：关闭默认的 `300~3400Hz` 人声带通滤波，改用全频段音量测算峰值
- `--window-sec`：统计窗口秒数（默认 `1.0` 秒）
- `--top-percent`：选取音量排名前百分之多少的片段作为高光候选（默认 `15.0`）
- `--min-rms`：最低 RMS 音量硬底线，防止静音视频误报（默认 `0.01`）
- `--merge-gap`：两个高音量区间间隔小于等于多少秒时自动合并（默认 `2.0`）
- `--pad`：每个高光区间前后额外扩展缓冲秒数（默认 `1.0`）

运行后会在 `output/` 下生成：
- `<视频名>_16k.wav`：提取出的 16kHz 单声道音频（可直接给 Step 2 复用）
- `<视频名>_audio_peaks.json`：高音量时间段列表与阈值详情

## 运行 Step 2：提取带高音量标记的字幕稿（独立 ASR 引擎）

```powershell
# 1. 默认模式：走 DeepSeek 官方云端 ASR（/v1/tools/asr，配合轻声响度归一化 + 敏感 Silero VAD 精确切句）
.\.venv\Scripts\python.exe src/step2_asr.py output/demo_stream_audio_peaks.json

# 2. 切换为免费离线本地 RTX 增强版 Whisper（默认 large-v3-turbo，附带主播/游戏热词表）
.\.venv\Scripts\python.exe src/step2_asr.py output/demo_stream_audio_peaks.json --asr-provider local --asr-model large-v3-turbo --hotwords "Where Winds Meet, Elden Ring, KEKW"

# 3. 切换为 OpenAI 兼容云端 ASR（如 Groq / OpenAI / 硅基流动），或整场 full 全量转写
.\.venv\Scripts\python.exe src/step2_asr.py output/demo_stream_audio_peaks.json --asr-provider openai_compatible --mode full
```

运行后会在 `output/` 下生成：
- `<视频名>_transcript.json`：包含候选窗口、时间戳字幕、云端计费时长及 `is_high_volume` / `peak_rms` 标记的结构化结果
- `<视频名>_transcript.txt`：可直接阅读和交给 Step 3 大模型分析的带标签台词稿

## 运行 Step 3：大模型自适应粗筛与生成中文审片清单

1. 先复制 `.env.example` 为 `.env`，填入你的 DeepSeek / OpenAI 兼容密钥（或直接在工作台右上角「设置与外观」保存；单厂商 DeepSeek 只需填 1 个 Key 即可同时驱动云端 ASR + `deepseek-flash`）：

```powershell
Copy-Item .env.example .env
```

2. 运行 Step 3（默认使用 `deepseek-flash`，支持通过 `--polish-model` 独立指定双语精修模型）：

```powershell
.\.venv\Scripts\python.exe src/step3_llm.py output/demo_stream_transcript.json

# 仅对已有的 *_highlights.json 重新按整句意群与地道意译刷新双语字幕（保留既有勾选状态与起止时间）
.\.venv\Scripts\python.exe src/step3_llm.py output/demo_stream_transcript.json --translate-only --hotwords "Where Winds Meet, Elden Ring, KEKW"
```

如需手动覆盖条数或时长区间：

```powershell
.\.venv\Scripts\python.exe src/step3_llm.py output/demo_stream_transcript.json --top-k 12 --min-clip-sec 20 --max-clip-sec 60
```

运行后会在 `output/` 下生成：
- `<视频名>_highlights.json`：供 Step 4 切片使用的结构化方案（每条默认 `"selected": true`，审片时把不要的改成 `false` 即可跳过）
- `<视频名>_review.md`：给剪辑师看的中文速览审片清单（含中英标题、一句话中文梗概、入选理由及原文台词摘录）

## 运行 Step 4：导出无损切片 / 时间线标记 / 竖屏烧录成片

```powershell
# 1. 公会无损工作流（默认）：原画快切 + 同名 .srt 字幕 + 达芬奇/PR 标记表（CSV + EDL）
.\.venv\Scripts\python.exe src/step4_cut.py output/demo_stream_highlights.json

# 2. 先试切前 2 条快速验货
.\.venv\Scripts\python.exe src/step4_cut.py output/demo_stream_highlights.json --mode lossless --limit 2

# 3. 个人博主一键竖屏成片（1080x1920 高斯模糊背景 + 顶部标题条 + 双语 ASS 硬字幕），仅切第 2 名
.\.venv\Scripts\python.exe src/step4_cut.py output/demo_stream_highlights.json --mode vertical --ranks 2 --subtitle-lang bilingual
```

支持的核心模式与参数：
- `--mode lossless` / `vertical` / `landscape`：无损原画快切 / 9:16 模糊背景竖屏 / 横屏重编码
- `--subtitle-lang bilingual` / `bilingual_rev` / `target` / `source`：译上原下双语 / 原上译下双语 / 仅译文 / 仅原文
- `--no-title-banner`：关闭竖屏顶部的爆款标题条
- `--ranks 1,2,5` 与 `--limit N`：按排名精选或限制导出数量
- `--video-path`：如果分析的是低画质代理视频，可在这里无缝替换为 4K 原片输出
- `--no-burn-subtitles`：关闭竖屏/横屏重编码时的画面硬字幕烧录，仅保留外挂 `.srt`

## 一键命令行全流程（批处理）

```powershell
.\.venv\Scripts\python.exe src/pipeline.py input/demo_stream.mp4 --output-dir output

# 指定使用 OBS 第 1 号麦轨，或附加 --no-vocal-bandpass 改用全频段峰值检测
.\.venv\Scripts\python.exe src/pipeline.py input/ --output-dir output --audio-stream 1
```

## 本地多语种专家语料库与零漏翻翻译闭环（6 大语种 60 万级）

系统内置 **60 万条**经严格去噪与口语亲和度精选的开源平行语料 SQLite FTS5 索引（覆盖 **`en_zh` 英→中、`zh_en` 中→母语英、`ja_zh` 日→中、`ko_zh` 韩→中、`ru_zh` 俄→中、`es_zh` 西→中** 六大核心语种，每语种对等拉齐 **100,000 条**，对无空格 CJK 语种采用原生 `trigram` 分词），并配套原创 MIT 商用无风险游戏/电竞/VTuber 专家术语库、ASR 同音误听纠偏库与母语反直译红线金库（完整来源与许可清单见 `data/corpus/SOURCES.md`）。

Step 3 双语精修内置三道防线：毫秒级多语种 RAG 专家参考卡注入 + 源语种感知严苛漏翻质检（堵死未译英文、日语假名残留、韩语谚文残留、俄语西里尔残留及时间轴跳句黑洞） + 手术刀级定点补翻闭环，确保出品贴近真人母语字幕组且 0 漏翻。

如需重建、更新或按严格商用无限制许可（`--commercial-only`）重新构建语料：

```powershell
# 一键拉取/刷新全部 6 大语种（英/中/日/韩/俄/西）并拉齐至 100,000 条/语种
.\.venv\Scripts\python.exe src/corpus_builder.py --lang-pair all

# 严格仅收录商用无限制许可（MIT / CC BY 2.0 / CC BY-SA 4.0）数据源
.\.venv\Scripts\python.exe src/corpus_builder.py --lang-pair all --commercial-only
```

在 Web 工作台右上角「设置」->「专家双语语料库与公会专属词库」中，可随时添加公会/主播专属译名与 ASR 误听映射，保存后下一次翻译立即生效。

## 运行自动化测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
