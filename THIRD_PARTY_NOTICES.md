# Solacut 第三方开源组件与语料署名声明 (Third-Party Notices)

本软件在开发、运行或便携包分发过程中使用了以下优秀的开源项目与开放语料，谨致谢意并严格遵守各自开源许可协议：

## 一、核心依赖组件

| 组件 | 用途 | 许可协议 | 官方主页 |
| :--- | :--- | :--- | :--- |
| **FFmpeg / FFprobe** | 本地音频抽取、无损流切片与视频字幕渲染 | GPLv3（便携包内置 `gyan.dev` 预编译独立 CLI 可执行文件，附 `bin/LICENSE.FFmpeg-GPLv3.txt`；源码详见 https://ffmpeg.org/download.html ） | https://ffmpeg.org |
| **FastAPI / Uvicorn / Starlette** | 本地审片工作台后端服务 | MIT / BSD-3-Clause | https://fastapi.tiangolo.com |
| **pywebview** | 原生 Windows Edge WebView2 无边框桌面容器 | BSD-3-Clause | https://pywebview.flowrl.com |
| **faster-whisper / ctranslate2** | 可选本地离线 GPU 语音识别 | MIT | https://github.com/SYSTRAN/faster-whisper |
| **SQLite / FTS5** | 本地零依赖毫秒级多语种语料检索引擎 | Public Domain | https://sqlite.org |
| **NumPy / httpx / Pydantic** | 音频 FFT 带通分析、HTTP 客户端与数据校验 | BSD-3-Clause / MIT | https://numpy.org |

## 二、商用无限制开源平行语料署名（便携分发包内置）

便携分发包通过 `python src/corpus_builder.py --lang-pair all --commercial-only` 构建，**仅内置允许商用分发的开放许可语料（共计 228,210 条跨 6 大语种对）**：

1. **OPUS Tatoeba (v2026-07-08)** — `CC BY 2.0 FR`  
   来源：Tatoeba Project (https://tatoeba.org) / OPUS (https://opus.nlpl.eu/Tatoeba.php)
2. **Microsoft NTREX-128** — `CC BY-SA 4.0`  
   来源：Microsoft Translator (https://github.com/MicrosoftTranslator/NTREX)
3. **OPUS QED (v2.0a)** — `CC BY-SA 4.0`  
   来源：QCRI Educational Domain Corpus / OPUS (https://opus.nlpl.eu/QED.php)
4. **OPUS WikiMatrix (v1)** — `CC BY-SA 4.0`  
   来源：Facebook AI WikiMatrix / OPUS (https://opus.nlpl.eu/WikiMatrix.php)
5. **OPUS News-Commentary (v16)** — Public Research / News-Commentary  
   来源：WMT / OPUS (https://opus.nlpl.eu/News-Commentary.php)
6. **Solacut 原创六语种游戏/直播专家术语、ASR 误听纠偏与反直译金库** — `MIT`  
   来源：Solacut 原创整理（`data/corpus/*/curated_*.json`）

> 如用户在个人本地点击「扩展 60 万口语库」，软件将直接从 OPUS 官方公开镜像按需拉取 TED2020 (`CC BY-NC-ND 4.0`) 与 OpenSubtitles 影视字幕至用户个人设备本地索引，仅限个人学习与非商业研究用途。完整动态明细见 `data/corpus/SOURCES.md`。
