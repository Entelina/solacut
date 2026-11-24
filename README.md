# Solacut — Open-Source Local VOD Assistant Editor & 6-Language Localization Workbench
**开源纯本地长录屏助理剪辑与六语种审校工作台（MIT License）**

> 🌐 **Official Website / 官方网站**：[**https://solacut.com**](https://solacut.com)（Win64 免 Python 便携包下载与 SHA-256 核验）｜📖 **操作手册**：[**`USER_MANUAL.md`**](./USER_MANUAL.md)（软件内按 `F1` 随时打开）｜🔒 [`SECURITY.md`](./SECURITY.md)｜📄 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)

Solacut 不是“黑盒无人值守的 AI 垃圾切片工厂（AI Slop Generator）”，而是一款遵循 **Human-in-the-Loop（人类掌舵）** 理念的纯本地桌面级 **助理剪辑师（Assistant Editor）与多语种字幕审校工作台**。

它将数小时游戏录屏或跨语种直播回放中最枯燥的前期体力活——**OBS 多音轨人声带通探测、内嵌 OBS 章节标记读取、Twitch/B站/YouTube 弹幕刷屏峰值雷达、全片逐字稿搜词定位、以及基于 22.8 万商用语料的 6×6 母语级双语初译**——全部留在你的本机完成，最后以 **`< 1s` 零转码导出原生 `FCPXML 1.10`（DaVinci Resolve / Final Cut Pro）与 `Premiere Pro XML` 时间线**，或经过人工 `Reviewed` 审校门禁后无损直切交付。

---

## 核心特性（Utility, Not Replacement）

- **全 6 语种原生暗房界面（默认 English）**：支持 `English / 简体中文 / 日本語 / 한국어 / Русский / Español` 一键无刷新切换；采用 `pywebview` (Edge WebView2) 无边框桌面窗体，支持 `J/K/L` 8x 梭巡、`I/O` 设点、媒体池虚拟文件夹（Bin）与 5 色标签、100 步原子撤销栈及 43 项可检索快捷键。
- **音轨 + OBS 章节 + 弹幕三轨高光雷达（零 API 消耗先行体检）**：
  - 自动探测 OBS 多音轨并推荐纯人声麦轨，以纯 NumPy FFT 执行 `300~3400Hz` 人声带通滤波防爆炸枪声误报，同步标出 `>=15s` 的挂机死寂空白段（审片可开启 `Skip Air` 自动跳空，绝不破坏正常说话气口）；
  - 原生提取 MP4/MKV 内嵌 **OBS Chapter Markers（章节标记）** 并一键带入 Premiere / DaVinci XML；
  - 支持导入 **TwitchDownloader JSON / B站 XML / YouTube Live Chat** 弹幕文件，基于本地 SQLite FTS5 绘制弹幕热度曲线与跨语种梗词（`OMEGALUL` / `草` / `ㅋㅋ` 等）峰值，精准捕捉无声神操作与冷面反讽。
- **全片逐字稿搜词剪辑（Full Transcript Search）与 ±30s 上下文核查**：
  - 即使完全不使用 AI 选片，也可在 `Transcript` 面板直接搜词跳播、勾选任意连续台词一键建片段（纯免费本地文本剪辑工作流）；
  - 审校任意高光时可一键展开 **`±30s Source Context`（前后 30 秒完整原片上下文）**，杜绝断章取义与反讽误译。
- **6×6 母语级字幕系统与 `Draft → Reviewed` 人工品控门禁**：
  - 中/英/日/韩/俄/西任意源语与目标语自由互译，内置 **228,210 条纯商用无限制平行语料**（可按需本地扩展至 60 万口语研究库）+ 三道零漏翻闭环；监视器 `CC` 菜单支持译上原下、原上译下、单语、关闭 5 种实时预览；
  - AI 初稿默认标为 `Draft` 并自动标注高黑话密度/回退复核提示，人工核验后按 `R` 标为绿色 `✓ Reviewed`，交付页默认开启 **`Only Reviewed`（仅导出已人工审校片段）** 护栏。
- **专业 NLE 优先交付（不绑架主力剪辑软件）**：
  - 默认 **`NLE XML` 极速模式（`< 1s` 零转码）**：直接生成严格符合 DTD 规范的 **`FCPXML 1.10` 双序列工程**（DaVinci Resolve / FCP）与 **`Premiere Pro FCP7 XML (xmeml v5)`**（完整保留双轨立体声、`masterclipid` 与全部 OBS 章节标记），以及 CMX 3600 EDL、DaVinci CSV 与对齐 `.srt`；
  - 同时支持 **FFmpeg 原画无损直切**（自动补偿长 GOP 关键帧偏移防外挂字幕抢跑，支持 4K 原片代切）与可选的 9:16 竖屏 / 16:9 横屏双语 ASS 渲染（按语种自动匹配零缺字系统字体）。

---

## 最快捷上手

### 方式一：Windows 64-bit 免 Python 便携包（推荐）
前往 [**https://solacut.com**](https://solacut.com) 或 [GitHub Releases](https://github.com/Entelina/solacut/releases) 下载最新便携包，解压双击 **`Solacut.exe`** 即可运行。首次无 API Key 可直接在引导弹窗或顶栏 **`Help → Load 30s Demo Project`** 一键离线体验全流程。

### 方式二：源码运行（Windows PowerShell · Python 3.11 + uv）

> 为保证 `faster-whisper`、`ctranslate2` 与音视频/CUDA 预编译轮子稳定可用，工程锁定 **Python 3.11** 独立虚拟环境。

```powershell
# 启动原生无边框桌面工作台（推荐）
.\.venv\Scripts\pythonw.exe src/desktop_app.py

# 或以浏览器调试模式启动
.\.venv\Scripts\python.exe src/desktop_app.py --browser
```

---

## 模块化命令行流水线（供高级批处理与脚本集成）

1. **Step 1 音频带通峰值与死寂段探测（`src/step1_audio.py`）**
   ```powershell
   # 查看 OBS 多音轨列表并自动推荐纯人声麦轨
   .\.venv\Scripts\python.exe src/step1_audio.py input/obs_multitrack.mkv --list-streams

   # 指定第 1 号人声麦轨执行 300~3400Hz 带通峰值与 >=15s 死寂段检测
   .\.venv\Scripts\python.exe src/step1_audio.py input/obs_multitrack.mkv --audio-stream 1
   ```
2. **Step 2 多引擎语音转写（`src/step2_asr.py`）**
   ```powershell
   # 默认云端 DeepSeek ASR（含轻声局部响度归一化 + Silero VAD 精确切句）
   .\.venv\Scripts\python.exe src/step2_asr.py output/obs_multitrack_audio_peaks.json

   # 或切换本地 RTX CUDA 离线 faster-whisper (large-v3-turbo)
   .\.venv\Scripts\python.exe src/step2_asr.py output/obs_multitrack_audio_peaks.json --asr-provider local --asr-model large-v3-turbo
   ```
3. **Step 3 高光候选初筛与 6×6 双语初译（`src/step3_llm.py`）**
   ```powershell
   Copy-Item .env.example .env  # 填入 API Key（或在桌面工作台 Settings 面板保存）
   .\.venv\Scripts\python.exe src/step3_llm.py output/obs_multitrack_transcript.json
   ```
4. **Step 4 导出 NLE 时间线工程 / 无损切片 / 多语种字幕（`src/step4_cut.py`）**
   ```powershell
   # 1. 极速零转码导出 DaVinci FCPXML 1.10 + Premiere XML + EDL + CSV + SRT（仅放行已人工审校片段）
   .\.venv\Scripts\python.exe src/step4_cut.py output/obs_multitrack_highlights.json --mode nle_only --only-reviewed

   # 2. 原画无损快切（含长 GOP 关键帧偏移补偿 + 同步生成 FCPXML / Premiere XML）
   .\.venv\Scripts\python.exe src/step4_cut.py output/obs_multitrack_highlights.json --mode lossless --only-reviewed

   # 3. 可选 9:16 竖屏双语硬字幕成片（默认不加顶部标题横幅，保持画面克制干净）
   .\.venv\Scripts\python.exe src/step4_cut.py output/obs_multitrack_highlights.json --mode vertical --subtitle-lang bilingual --only-reviewed
   ```
5. **批处理全流程（`src/pipeline.py`，仅自动生成审片清单与可选 NLE XML 工程，杜绝无人值守粗制滥造出片）**
   ```powershell
   .\.venv\Scripts\python.exe src/pipeline.py input/demo_highlight_30s.mp4 --output-dir output --export-nle-xml
   ```

---

## 本地六语种语料库与版权合规分级

便携发行包默认内置 **228,210 条**严格限定商用无限制许可（`MIT / CC BY 2.0 / CC BY-SA 4.0`）的六语种 SQLite FTS5 平行语料（无空格 CJK 语种采用原生 `trigram` 分词）及原创电竞/直播反直译词库；个人学习与非商业研究可在工作台「Glossary / 词库」页或通过命令行按需从官方镜像拉取扩展至 **600,000 条**口语索引（详见 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)）：

```powershell
# 严格仅构建商用无限制许可语料（打包默认模式）
.\.venv\Scripts\python.exe src/corpus_builder.py --lang-pair all --commercial-only

# 本地按需构建全量 60 万条个人研究口语索引
.\.venv\Scripts\python.exe src/corpus_builder.py --lang-pair all
```

---

## 运行自动化测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
