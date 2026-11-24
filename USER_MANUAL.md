# Solacut 操作手册（User Manual）

欢迎使用 **Solacut** — 纯本地优先、MIT 开源的 **长录屏助理剪辑师与六语种审校工作台（Local VOD Assistant Editor & 6-Language Localization Workbench）**。

> Solacut 默认以 **English (`EN`)** 界面启动，点击工作台右上角地球图标 `🌐 EN ▾` 可 1 秒无刷新切换为 **简体中文 / English / 日本語 / 한국어 / Русский / Español**；按 **`F1`** 可随时在软件内呼出交互式手册。

---

## 目录

1. [3 分钟快速上手（零配置 Demo + 界面切换）](#1-3-分钟快速上手)
2. [三层 API 路由与本地 RTX 离线配置](#2-三层-api-路由与本地-rtx-离线配置)
3. [媒体池分类、OBS 章节标记、人声带通与弹幕雷达](#3-媒体池分类obs-章节标记人声带通与弹幕雷达)
4. [全片逐字稿搜词剪辑与 ±30s 上下文核查](#4-全片逐字稿搜词剪辑与-30s-上下文核查)
5. [6×6 六语种母语精翻、CC 实时叠字与专属词库](#5-66-六语种母语精翻cc-实时叠字与专属词库)
6. [暗房审片、`Draft → Reviewed` 人工审校与微调快捷键](#6-暗房审片draft--reviewed-人工审校与微调快捷键)
7. [专业交付：NLE XML 极速回迁、无损直切与竖屏导出](#7-专业交付nle-xml-极速回迁无损直切与竖屏导出)
8. [常见问题与本地安全隔离说明](#8-常见问题与本地安全隔离说明)

---

## 1. 3 分钟快速上手

### 方式 A：Windows 64-bit 免 Python 便携包（推荐）
1. 前往官网 [https://solacut.com](https://solacut.com) 或 [GitHub Releases](https://github.com/Entelina/solacut/releases) 下载 `Solacut-<版本>-win64-portable.zip`；
2. 解压至本地任意常规目录，双击 **`Solacut.exe`** 启动，在顶栏右侧 `🌐 EN ▾` 可切换为 `简体中文` 或其他语种；
3. 首次启动若出现 Windows SmartScreen 蓝色提示，可在 PowerShell 执行 `Get-FileHash .\Solacut-*.zip -Algorithm SHA256` 校验哈希一致后，点击「更多信息 → 仍要运行」。

### 方式 B：源码运行（Python 3.11 + uv）
```powershell
git clone https://github.com/Entelina/solacut.git
cd solacut
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\pythonw.exe src/desktop_app.py
```

### 零门槛离线试玩 30 秒示例工程
- 还没配置 API Key？直接在首次引导弹窗或顶栏 **`Help → Load 30s Demo Project`（帮助 → 加载 30 秒体验示例工程）** 一键载入示例；
- 完全离线、零费用即可体验时间线波形、逐字稿搜词、±30s 上下文、双语字幕修改与 `NLE XML` / 无损导出。

---

## 2. 三层 API 路由与本地 RTX 离线配置

按 `Ctrl+,` 打开 **Settings（设置与外观）**：
- **极简单 Key 模式**：仅填写 1 个 **DeepSeek API Key**，即可驱动「云端 ASR + `deepseek-flash` 初筛 + 六语种精修」全流程，核显轻薄本也能流畅使用；
- **高级三层独立混搭**：
  1. **ASR 语音转写层**：支持 `DeepSeek 云端 ASR`、**本地 RTX CUDA 离线 `faster-whisper (large-v3-turbo)`** 或 `OpenAI 兼容接口 (Groq / OpenAI / 硅基流动)`；
  2. **初筛模型层**：选用长上下文高性价比模型；
  3. **双语精修层**：独立指定擅长母语意译的高阶模型。
- **纯本地隐私存储**：密钥仅采用原子替换保存在本机 `.env` 中，界面仅显示掩码，原片 0 字节上传云端。

---

## 3. 媒体池分类、OBS 章节标记、人声带通与弹幕雷达

- **虚拟文件夹（Bin）与 5 色标签**：在媒体池空白处右键新建虚拟文件夹（6 色可选），支持多选视频拖拽归类与 5 色达芬奇标签标记；纯逻辑归类，**绝不移动或改动磁盘原片**。
- **零费用音频体检 & OBS 多音轨带通**：点媒体池底部 `Scan / 体检`（0 API 消耗），在检查器 `Analyze` 页查看场次爆点密度与推荐参数；自动探测 OBS 多音轨推荐纯人声麦轨，并以 `300~3400Hz` 带通滤除爆炸与枪炮声误报。
- **OBS 内嵌章节标记（Chapter Markers）**：加载 MP4 / MKV 后自动读取 OBS 录制时打下的章节标记，在时间线标尺渲染为翡翠绿菱形旗标；支持单击跳播、一键将章节转为高光片段，并在导出 Premiere XML / DaVinci FCPXML 时完整携带（彻底解决 Premiere 原生读不出 MP4 章节标记的痛点）。
- **弹幕刷屏雷达（Chat Radar）**：点时间线工具栏右侧 `Chat` 按钮，可导入 **TwitchDownloader JSON / B站 XML / YouTube Live Chat** 弹幕文件，在时间线上叠加弹幕热度曲线与跨语种梗词（`OMEGALUL` / `草` / `666` 等）峰值，支持按任意关键词检索弹幕高潮并一键建片段。
- **死寂空白段检测（Dead Air Map）与 `Skip Air`**：时间线自动将 `>=15s` 的离席/加载纯静音段标为暗灰斜纹；点亮监视器播控栏左下角的 `Skip Air` 图标，审片播放将自动跳过长死寂段（手动拖入静音段查看时自动豁免不弹飞，且绝不触碰 0.5~5 秒正常说话气口）。

---

## 4. 全片逐字稿搜词剪辑与 ±30s 上下文核查

- **Full Transcript（全片逐字稿搜词剪辑）**：切换到右下角 `Transcript` 标签页，输入任意关键词即可实时高亮定位全片台词；单击任意行秒跳监视器画面，勾选连续几句台词点击 `Create Clip`（或点行尾 `+ Clip`）即可直接创建人工片段——**即使完全不调 AI 初筛，也能把 Solacut 当作免费的本地文本剪辑器使用**。
- **±30s Source Context（防断章取义核查）**：在检查器 `Clip` 页或字幕面板点击 `±30s Context`，立刻弹窗查看当前片段前后 30 秒的完整原片逐字稿（琥珀色标出当前选段），1 秒核实反讽、铺垫与双关语境。

---

## 5. 6×6 六语种母语精翻、CC 实时叠字与专属词库

- **6×6 任意语种互译**：完整支持 **英语 (`EN`) / 简体中文 (`ZH`) / 日语 (`JA`) / 韩语 (`KO`) / 俄语 (`RU`) / 西班牙语 (`ES`)** 任意源语 × 目标语组合及同语种润色。
- **监视器 `CC` 弹出菜单**：在监视器右下角点击 `CC` 按钮，可实时切换 **双语（译上原下）/ 双语（原上译下）/ 仅译文 / 仅原文 / 关闭** 5 种画面叠字预览，也可直接在菜单下部一键切换 6 种目标译文语种。
- **228,210 条商用语料 + 主播专属词库**：便携包内置 22.8 万条纯商用无限制平行语料（可在 `Glossary / 词库` 页扩展至 60 万研究库），支持自定义主播黑话与 ASR 误听纠偏映射。

---

## 6. 暗房审片、`Draft → Reviewed` 人工审校与微调快捷键

- **`Draft → Reviewed` 审校品控流**：AI 生成的候选片段默认标为琥珀色 `Draft`（若存在高俚语密度、回退译文或超长时长会额外显示 `!` 复核提示）；人工确认台词与切点无误后，点击徽标或按快捷键 **`R`** 即可切换为翡翠绿 **`✓ Reviewed`**。
- **核心高频快捷键（软件内按 `?` 可实时检索全部 43 项快捷键）**：
  - `Space` 播放/暂停 ｜ `J` / `K` / `L` 倒放/暂停/最高 8x 快进梭巡（`K+L` 0.5x 慢放精听）
  - `I` / `O` 设当前片段入点/出点 ｜ `Alt+X` 重置初始边界
  - `R` 切换当前片段 `Draft / Reviewed` 审校状态 ｜ `S` 勾选/取消纳入导出
  - `Delete` 删除选中高光片段 ｜ `Ctrl+Shift+\` 在播放头处将片段一分为二
  - `V` 选择工具 ｜ `B` 剃刀拆分字幕工具 ｜ `C` 拆分当前字幕行 ｜ `N` 磁吸开关
  - `Ctrl+F` 聚焦当前面板检索（逐字稿搜词 / 字幕查找替换）
  - `Ctrl+Z` / `Ctrl+Shift+Z` 100 步原子撤销与重做 ｜ `Ctrl+E` 执行当前交付任务 ｜ `F1` 打开内置手册

---

## 7. 专业交付：NLE XML 极速回迁、无损直切与竖屏导出

切换到右侧检查器 **`Deliver（交付）`** 面板（默认勾选 **`Only Reviewed` 仅导出已人工审校片段**，严防未审机翻外流）：

1. **`NLE XML` 极速工程模式（默认推荐，`< 1s` 零转码）**
   - 不做任何视频重编码，瞬间输出全套行业标准工程文件：
     - **`*_timeline.fcpxml`（FCPXML 1.10 双序列）**：原生导入 **DaVinci Resolve / Final Cut Pro**，同时包含「精选片段精剪序列」与「带全量 OBS 章节及高光标记的完整原片序列」；
     - **`*_premiere.xml`（Premiere FCP7 XML `xmeml v5`）**：原生导入 **Adobe Premiere Pro**，完整保留双轨立体声、`masterclipid` 素材归并与全部 OBS 章节标记；
     - 同步附带 **`*_markers.edl`（CMX 3600）**、**`*_markers.csv`（DaVinci CSV）** 与独立 `.srt` 字幕包。
2. **`Lossless` 原画无损直切**
   - FFmpeg 流拷贝毫秒直切原画 `.mp4`，自动探测并补偿长 GOP 关键帧前导偏移以重对齐 `.srt` 字幕（防字幕抢跑），支持通过 `Source File` 挂载 4K 原片代切，并同步附带上述全套 FCPXML / Premiere XML / EDL / CSV。
3. **`9:16` 竖屏 / `16:9` 横屏重编码成片**
   - 按语种自动匹配零缺字系统字体烧录双语分层 ASS 字幕（优先使用 NVIDIA NVENC 硬件编码，无独显自动回退 CPU `libx264`）；竖屏顶部标题条默认关闭以保持克制专业的画面观感，需要时可手动勾选开启。

---

## 8. 常见问题与本地安全隔离说明

- **部分 MKV / HEVC 视频在内置监视器画面黑屏？**：受限于系统 WebView2 浏览器内核解码器许可，少数特殊封装无法网页硬解预览，但底层独立 FFmpeg 的**音频体检、OBS 章节提取、转写、精翻、`NLE XML` 与无损导出均 100% 正常工作**。
- **本地安全架构**：后端仅绑定 `127.0.0.1` 回环地址，每次启动生成 256-bit 随机进程令牌并严格校验 `Host` / `Origin` / `Sec-Fetch-Site` 防御跨站与 DNS Rebinding；所有文件读写严格限定于授权工作区并防御 XXE、CSV 公式注入与路径穿越。
