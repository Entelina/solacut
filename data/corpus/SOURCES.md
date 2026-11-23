# ClipStudio 多语种顶级双语语料与母语专家词库清单

本目录汇总了六大核心语种对（英→中、中→英、日→中、韩→中、俄→中、西→中）的高品质平行语料及原创游戏/直播专家词典，驱动本地零依赖毫秒级翻译记忆（TM）与动态 Few-Shot 母语意译检索（`src/corpus_rag.py`）。

## 一、内建原创商业级多语种专家词库（MIT 许可，100% 商用无风险）

- `en_zh/`：欧美游戏/电竞/Twitch 直播术语 + ASR 误听纠错 + 英语习语反直译红线
- `zh_en/`：国产大作与中文直播黑话出海术语 + 中文热梗去 Chinglish 反直译红线
- `ja_zh/`：日 V / Hololive / 彩虹社 / 日系二游动作术语 + 日语 ASR 误听 + 日语口语反直译红线
- `ko_zh/`：LCK / 韩服电竞 / SOOP·CHZZK 直播术语 + 韩语 ASR 误听 + 韩语半语敬语反直译红线
- `ru_zh/`：CS2 / Dota2 / 塔科夫俄区电竞术语 + 俄语口语与情绪词自然意译红线
- `es_zh/`：Twitch 西语/拉美顶流主播与电竞术语 + 板鸭/拉美口语反直译红线
- `user_glossary.json`：公会与主播专属自定义词库（最高优先级，支持在工作台实时热更新）

## 二、各语种平行语料规模总览

| 语种对 | 语言方向 | 总精选句对 | 商用无限制许可 | FTS5 分词器 |
| :---: | :--- | :---: | :---: | :--- |
| `en_zh` | 英语 → 简体中文 | **100,000** | 55,043 | `porter unicode61 remove_diacritics 2` |
| `zh_en` | 简体中文 → 母语英语 | **100,000** | 54,297 | `trigram` |
| `ja_zh` | 日语 → 简体中文 | **100,000** | 33,377 | `trigram` |
| `ko_zh` | 韩语 → 简体中文 | **100,000** | 18,556 | `trigram` |
| `ru_zh` | 俄语 → 简体中文 | **100,000** | 31,891 | `unicode61 remove_diacritics 2` |
| `es_zh` | 西班牙语 → 简体中文 | **100,000** | 35,046 | `unicode61 remove_diacritics 2` |
| **合计** | **全 6 大语种方向** | **600,000** | **228,210** | — |

## 三、开源社区平行语料接入明细与许可协议

| 语种对 | 数据源 | 领域 | 精选清洗条数 | 许可协议 | 商用友好 | 官方主页 |
| :--- | :--- | :---: | :---: | :--- | :---: | :--- |
| `en_zh` | **Microsoft NTREX-128 (EN-ZH)** | `expert_ref` | 1,121 | `CC BY-SA 4.0` | 是 | https://github.com/MicrosoftTranslator/NTREX |
| `en_zh` | **OPUS Tatoeba EN-ZH (v2026-07-08)** | `spoken` | 23,500 | `CC BY 2.0 FR` | 是 | https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php |
| `en_zh` | **OPUS QED EN-ZH (v2.0a)** | `subtitle` | 2,426 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/QED.php |
| `en_zh` | **OPUS WikiMatrix EN-ZH (v1)** | `encyclopedia` | 17,996 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/WikiMatrix.php |
| `en_zh` | **OPUS News-Commentary EN-ZH (v16)** | `expert_ref` | 10,000 | `Public Research / News-Commentary` | 是 | https://opus.nlpl.eu/News-Commentary.php |
| `en_zh` | **OPUS TED2020 EN-ZH (v1)** | `subtitle` | 2,985 | `CC BY-NC-ND 4.0 (TED Talks)` | 仅研究/选配 | https://opus.nlpl.eu/TED2020.php |
| `en_zh` | **OPUS OpenSubtitles EN-ZH (v2018)** | `subtitle` | 41,972 | `Community Subtitles (OpenSubtitles)` | 仅研究/选配 | https://opus.nlpl.eu/OpenSubtitles.php |
| `es_zh` | **Microsoft NTREX-128 (ES-ZH)** | `expert_ref` | 1,035 | `CC BY-SA 4.0` | 是 | https://github.com/MicrosoftTranslator/NTREX |
| `es_zh` | **OPUS Tatoeba ES-ZH (v2026-07-08)** | `spoken` | 4,173 | `CC BY 2.0 FR` | 是 | https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php |
| `es_zh` | **OPUS QED ES-ZH (v2.0a)** | `subtitle` | 1,628 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/QED.php |
| `es_zh` | **OPUS WikiMatrix ES-ZH (v1)** | `encyclopedia` | 20,000 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/WikiMatrix.php |
| `es_zh` | **OPUS News-Commentary ES-ZH (v16)** | `expert_ref` | 8,210 | `Public Research / News-Commentary` | 是 | https://opus.nlpl.eu/News-Commentary.php |
| `es_zh` | **OPUS TED2020 ES-ZH (v1)** | `subtitle` | 13,001 | `CC BY-NC-ND 4.0 (TED Talks)` | 仅研究/选配 | https://opus.nlpl.eu/TED2020.php |
| `es_zh` | **OPUS OpenSubtitles ES-ZH (v2018)** | `subtitle` | 51,953 | `Community Subtitles (OpenSubtitles)` | 仅研究/选配 | https://opus.nlpl.eu/OpenSubtitles.php |
| `ja_zh` | **Microsoft NTREX-128 (JA-ZH)** | `expert_ref` | 1,152 | `CC BY-SA 4.0` | 是 | https://github.com/MicrosoftTranslator/NTREX |
| `ja_zh` | **OPUS Tatoeba JA-ZH (v2026-07-08)** | `spoken` | 3,242 | `CC BY 2.0 FR` | 是 | https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php |
| `ja_zh` | **OPUS QED JA-ZH (v2.0a)** | `subtitle` | 2,984 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/QED.php |
| `ja_zh` | **OPUS WikiMatrix JA-ZH (v1)** | `encyclopedia` | 25,999 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/WikiMatrix.php |
| `ja_zh` | **OPUS TED2020 JA-ZH (v1)** | `subtitle` | 11,674 | `CC BY-NC-ND 4.0 (TED Talks)` | 仅研究/选配 | https://opus.nlpl.eu/TED2020.php |
| `ja_zh` | **OPUS OpenSubtitles JA-ZH (v2024)** | `subtitle` | 54,949 | `Community Subtitles (OpenSubtitles)` | 仅研究/选配 | https://opus.nlpl.eu/OpenSubtitles.php |
| `ko_zh` | **Microsoft NTREX-128 (KO-ZH)** | `expert_ref` | 1,113 | `CC BY-SA 4.0` | 是 | https://github.com/MicrosoftTranslator/NTREX |
| `ko_zh` | **OPUS Tatoeba KO-ZH (v2026-07-08)** | `spoken` | 302 | `CC BY 2.0 FR` | 是 | https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php |
| `ko_zh` | **OPUS QED KO-ZH (v2.0a)** | `subtitle` | 3,539 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/QED.php |
| `ko_zh` | **OPUS WikiMatrix KO-ZH (v1)** | `encyclopedia` | 13,602 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/WikiMatrix.php |
| `ko_zh` | **OPUS TED2020 KO-ZH (v1)** | `subtitle` | 19,507 | `CC BY-NC-ND 4.0 (TED Talks)` | 仅研究/选配 | https://opus.nlpl.eu/TED2020.php |
| `ko_zh` | **OPUS OpenSubtitles KO-ZH (v2024)** | `subtitle` | 61,937 | `Community Subtitles (OpenSubtitles)` | 仅研究/选配 | https://opus.nlpl.eu/OpenSubtitles.php |
| `ru_zh` | **Microsoft NTREX-128 (RU-ZH)** | `expert_ref` | 1,064 | `CC BY-SA 4.0` | 是 | https://github.com/MicrosoftTranslator/NTREX |
| `ru_zh` | **OPUS Tatoeba RU-ZH (v2026-07-08)** | `spoken` | 3,284 | `CC BY 2.0 FR` | 是 | https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php |
| `ru_zh` | **OPUS QED RU-ZH (v2.0a)** | `subtitle` | 1,052 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/QED.php |
| `ru_zh` | **OPUS WikiMatrix RU-ZH (v1)** | `encyclopedia` | 19,998 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/WikiMatrix.php |
| `ru_zh` | **OPUS News-Commentary RU-ZH (v16)** | `expert_ref` | 6,493 | `Public Research / News-Commentary` | 是 | https://opus.nlpl.eu/News-Commentary.php |
| `ru_zh` | **OPUS TED2020 RU-ZH (v1)** | `subtitle` | 16,285 | `CC BY-NC-ND 4.0 (TED Talks)` | 仅研究/选配 | https://opus.nlpl.eu/TED2020.php |
| `ru_zh` | **OPUS OpenSubtitles RU-ZH (v2018)** | `subtitle` | 51,824 | `Community Subtitles (OpenSubtitles)` | 仅研究/选配 | https://opus.nlpl.eu/OpenSubtitles.php |
| `zh_en` | **Microsoft NTREX-128 (ZH-EN)** | `expert_ref` | 1,117 | `CC BY-SA 4.0` | 是 | https://github.com/MicrosoftTranslator/NTREX |
| `zh_en` | **OPUS Tatoeba ZH-EN (v2026-07-08)** | `spoken` | 22,751 | `CC BY 2.0 FR` | 是 | https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php |
| `zh_en` | **OPUS QED ZH-EN (v2.0a)** | `subtitle` | 2,431 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/QED.php |
| `zh_en` | **OPUS WikiMatrix ZH-EN (v1)** | `encyclopedia` | 17,998 | `CC BY-SA 4.0` | 是 | https://opus.nlpl.eu/WikiMatrix.php |
| `zh_en` | **OPUS News-Commentary ZH-EN (v16)** | `expert_ref` | 10,000 | `Public Research / News-Commentary` | 是 | https://opus.nlpl.eu/News-Commentary.php |
| `zh_en` | **OPUS WMT-News ZH-EN (v2019)** | `expert_ref` | 13 | `Public Research Benchmark (WMT)` | 仅研究/选配 | https://opus.nlpl.eu/WMT-News.php |
| `zh_en` | **OPUS TED2020 ZH-EN (v1)** | `subtitle` | 3,714 | `CC BY-NC-ND 4.0 (TED Talks)` | 仅研究/选配 | https://opus.nlpl.eu/TED2020.php |
| `zh_en` | **OPUS OpenSubtitles ZH-EN (v2018)** | `subtitle` | 41,976 | `Community Subtitles (OpenSubtitles)` | 仅研究/选配 | https://opus.nlpl.eu/OpenSubtitles.php |

> **商业化合规提示**：若用于对外商业分发且需严格限定仅使用商用无限制开源许可（MIT / CC BY 2.0 / CC BY-SA 4.0），可在构建时附加 `--commercial-only` 或在环境变量中设置 `CORPUS_COMMERCIAL_ONLY=1`，系统将自动仅检索标注为“商用友好=是”的语料。
