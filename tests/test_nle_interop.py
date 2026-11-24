"""专业 NLE 互操作引擎（src/nle_export.py）与 Step 4 升级能力自动化测试套件。

覆盖验证：
1. FFmpeg 真实生成带 2 个 OBS 章节元数据（中/英文标题）的 MP4，验证 `extract_video_chapters`
   精准提取及异常/无章节/畸形输出/超长控制字符容错；
2. `export_fcpxml`（FCPXML 1.10 双序列）与 `export_premiere_xml`（xmeml v5 双序列）：
   - `ET.fromstring` 严格 XML 合法性；
   - Windows 含中文与空格路径的正确 URL 编码；
   - `& < > "` 特殊字符自动转义与非法控制字符清洗；
   - NTSC 29.97fps (`1001/30000s` / `timebase=30, ntsc=TRUE`) 等精准帧率时间码；
   - OBS 章节标记与双语字幕注释完整写入；
3. `render_highlight_clips(..., mode="nle_only")` 极速（零转码、不生成 MP4）产出全套
   FCPXML / Premiere XML / EDL / CSV / SRT / Manifest，以及 `lossless` 模式同步附带 NLE 工程；
4. `only_reviewed=True` 严格拦截未校对 `draft` 片段、精准放行 `reviewed` 片段，
   以及 `show_title_banner` 新克制默认 `False` 与显式开启兼容。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.nle_export import (  # noqa: E402
    export_fcpxml,
    export_premiere_xml,
    extract_video_chapters,
    path_to_file_url,
    resolve_fps_rational,
)
from src.step1_audio import check_ffmpeg_available, run_media_subprocess  # noqa: E402
from src.step4_cut import render_highlight_clips  # noqa: E402


def _create_mp4_with_obs_chapters(
    mp4_path: Path,
    chapters: list[tuple[int, int, str]],
    *,
    width: int = 640,
    height: int = 360,
    fps: int = 30,
    duration_sec: float = 12.0,
) -> None:
    """使用内置 FFmpeg + FFMETADATA1 真实生成内嵌 OBS 章节元数据的 MP4 视频。"""
    ffmpeg_bin = check_ffmpeg_available()
    meta_path = mp4_path.parent / f".{mp4_path.stem}_ffmeta.txt"

    lines = [";FFMETADATA1"]
    for start_ms, end_ms, title in chapters:
        escaped_title = (
            title.replace("\\", "\\\\")
            .replace("=", "\\=")
            .replace(";", "\\;")
            .replace("#", "\\#")
            .replace("\n", " ")
        )
        lines.extend(
            [
                "",
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start_ms}",
                f"END={end_ms}",
                f"title={escaped_title}",
            ]
        )
    meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate={fps}:duration={duration_sec}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration_sec}",
            "-i",
            str(meta_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(mp4_path),
        ]
        proc = run_media_subprocess(cmd, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"带章节测试视频生成失败：\n{proc.stderr}")
    finally:
        meta_path.unlink(missing_ok=True)


class TestNleInterop(unittest.TestCase):
    """专业 NLE 互操作与 Step 4 NLE-only / 审校门禁测试套件。"""

    def test_extract_video_chapters_from_real_obs_style_mp4_and_resilience(self) -> None:
        """测试 1：真实 FFmpeg 合成带中/英文 OBS 章节 MP4 并验证精准提取与畸形输出容错。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            obs_mp4 = tmp / "obs_recording_with_chapters.mp4"
            # MP4 容器 QuickTime 章节轨按规范连续覆盖（第 1 章从 0ms 起，第 2 章从 6000ms 起）
            _create_mp4_with_obs_chapters(
                obs_mp4,
                chapters=[
                    (0, 6000, "Boss Kill & Clutch"),
                    (6000, 11000, "团战绝杀·中文章节名"),
                ],
                duration_sec=12.0,
            )

            chapters = extract_video_chapters(obs_mp4)
            self.assertEqual(len(chapters), 2)

            self.assertEqual(chapters[0]["index"], 1)
            self.assertAlmostEqual(chapters[0]["start_sec"], 0.0, places=2)
            self.assertAlmostEqual(chapters[0]["end_sec"], 6.0, places=2)
            self.assertEqual(chapters[0]["title"], "Boss Kill & Clutch")
            self.assertEqual(chapters[0]["source"], "obs_chapter")

            self.assertEqual(chapters[1]["index"], 2)
            self.assertAlmostEqual(chapters[1]["start_sec"], 6.0, places=2)
            self.assertAlmostEqual(chapters[1]["end_sec"], 11.0, places=2)
            self.assertEqual(chapters[1]["title"], "团战绝杀·中文章节名")
            self.assertEqual(chapters[1]["source"], "obs_chapter")

            # 同步验证 OBS MKV 任意非零起点章节的精准提取
            obs_mkv = tmp / "obs_recording_with_chapters.mkv"
            _create_mp4_with_obs_chapters(
                obs_mkv,
                chapters=[
                    (1500, 5500, "MKV Mid Start"),
                    (7250, 10500, "MKV 精准中文章节"),
                ],
                duration_sec=12.0,
            )
            mkv_chapters = extract_video_chapters(obs_mkv)
            self.assertEqual(len(mkv_chapters), 2)
            self.assertAlmostEqual(mkv_chapters[0]["start_sec"], 1.5, places=2)
            self.assertAlmostEqual(mkv_chapters[0]["end_sec"], 5.5, places=2)
            self.assertEqual(mkv_chapters[1]["title"], "MKV 精准中文章节")

            # 无章节 MP4 应安全返回空列表
            plain_mp4 = tmp / "plain_no_chapters.mp4"
            _create_mp4_with_obs_chapters(plain_mp4, chapters=[], duration_sec=4.0)
            self.assertEqual(extract_video_chapters(plain_mp4), [])

            # 不存在路径应安全返回 []
            self.assertEqual(extract_video_chapters(tmp / "nonexistent.mp4"), [])

            # 模拟畸形 JSON 输出应安全返回 []
            bad_proc = mock.Mock(returncode=0, stdout="{invalid json", stderr="")
            with mock.patch("src.nle_export.run_media_subprocess", return_value=bad_proc):
                self.assertEqual(extract_video_chapters(obs_mp4), [])

            # 模拟超长标题与控制字符应清洗截断，不崩溃
            dirty_payload = {
                "chapters": [
                    {
                        "start_time": "2.25",
                        "end_time": "9.75",
                        "tags": {"title": "Dirty\x00\x07\nTitle " + ("X" * 200)},
                    }
                ]
            }
            dirty_proc = mock.Mock(returncode=0, stdout=json.dumps(dirty_payload), stderr="")
            with mock.patch("src.nle_export.run_media_subprocess", return_value=dirty_proc):
                cleaned = extract_video_chapters(obs_mp4)
            self.assertEqual(len(cleaned), 1)
            self.assertNotIn("\x00", cleaned[0]["title"])
            self.assertNotIn("\x07", cleaned[0]["title"])
            self.assertNotIn("\n", cleaned[0]["title"])
            self.assertLessEqual(len(cleaned[0]["title"]), 120)

    def test_export_fcpxml_and_premiere_xml_validity_escaping_urls_and_fps(self) -> None:
        """测试 2：验证 FCPXML 1.10 与 Premiere xmeml v5 的 XML 合法性、URL 编码、特殊字符转义、帧率有理数与章节写入。"""
        # 验证核心广播帧率有理数映射
        self.assertEqual(resolve_fps_rational(23.976), (1001, 24000, 24, True))
        self.assertEqual(resolve_fps_rational(29.97), (1001, 30000, 30, True))
        self.assertEqual(resolve_fps_rational(59.94), (1001, 60000, 60, True))
        self.assertEqual(resolve_fps_rational(60.0), (1, 60, 60, False))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video_dir = tmp / "OBS 录屏 目录"
            video_dir.mkdir(parents=True, exist_ok=True)
            special_video = video_dir / "高能 录像 clip & test.mp4"
            special_video.write_bytes(b"")  # 仅占位路径，验证无实体流也能稳健导出合法 XML

            highlights = [
                {
                    "rank": 1,
                    "selected": True,
                    "review_status": "reviewed",
                    "start_sec": 10.0,
                    "end_sec": 25.0,
                    "score": 9.6,
                    "title_tgt": '极限 1v3 & "反杀" <神操作>\x00',
                    "title_en": 'Clutch 1v3 & "Comeback" <Insane>',
                    "summary_zh": '残血 <10HP> 反打 & 队友喊 "Nice!"',
                    "reason": "节奏拉满 <A&B>",
                    "subtitles": [
                        {
                            "start_sec": 11.0,
                            "end_sec": 13.5,
                            "text_src": 'Wait <what> & "no way"!',
                            "text_tgt": '不是哥们 <真的假的> & "离谱"！',
                        }
                    ],
                },
                {
                    "rank": 2,
                    "selected": True,
                    "review_status": "draft",
                    "start_sec": 40.0,
                    "end_sec": 50.0,
                    "score": 8.4,
                    "title_zh": "第二段笑点",
                    "summary_zh": "语音嘴瓢笑场",
                },
            ]
            chapters = [
                {"index": 1, "start_sec": 12.5, "end_sec": 20.0, "title": 'OBS "Boss" & <P2>', "source": "obs_chapter"},
                {"index": 2, "start_sec": 60.0, "end_sec": 95.0, "title": "终局结算中文章节", "source": "obs_chapter"},
            ]

            fcpxml_out = tmp / "out.fcpxml"
            premiere_out = tmp / "out_premiere.xml"

            export_fcpxml(
                highlights=highlights,
                video_path=special_video,
                output_path=fcpxml_out,
                fps=29.97,
                total_duration_sec=120.0,
                chapters=chapters,
                source_language="en",
                target_language="zh",
            )
            export_premiere_xml(
                highlights=highlights,
                video_path=special_video,
                output_path=premiere_out,
                fps=29.97,
                total_duration_sec=120.0,
                chapters=chapters,
                source_language="en",
                target_language="zh",
            )

            # 1. 校验 FCPXML 1.10
            fcp_raw = fcpxml_out.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE fcpxml>", fcp_raw)
            self.assertNotIn("\x00", fcp_raw)
            fcp_root = ET.fromstring(fcp_raw)
            self.assertEqual(fcp_root.tag, "fcpxml")
            self.assertEqual(fcp_root.attrib.get("version"), "1.10")

            fmt_el = fcp_root.find("./resources/format[@id='r1']")
            self.assertIsNotNone(fmt_el)
            assert fmt_el is not None
            self.assertEqual(fmt_el.attrib.get("frameDuration"), "1001/30000s")

            asset_el = fcp_root.find("./resources/asset[@id='r2']")
            self.assertIsNotNone(asset_el)
            assert asset_el is not None
            # FCPXML 1.10 严格 DTD 合规：已废弃的 asset@src 属性必须彻底移除，URL 仅位于 media-rep
            self.assertNotIn("src", asset_el.attrib)
            media_rep_el = asset_el.find("./media-rep[@kind='original-media']")
            self.assertIsNotNone(media_rep_el)
            assert media_rep_el is not None
            asset_src = media_rep_el.attrib.get("src", "")
            self.assertTrue(asset_src.startswith("file:///"))
            self.assertNotIn(" ", asset_src)
            self.assertIn("%20", asset_src)
            self.assertIn("OBS 录屏 目录", unquote(asset_src))
            self.assertIn("高能 录像 clip & test.mp4", unquote(asset_src))
            self.assertEqual(asset_el.attrib.get("hasVideo"), "1")
            self.assertEqual(asset_el.attrib.get("hasAudio"), "1")

            projects = fcp_root.findall("./library/event/project")
            self.assertEqual(len(projects), 2)
            # 序列 1：2 条精选高光片段 + 注释（默认 include_subtitle_markers=False 不污染普通 marker）+ 落入片段内的 OBS 章节标记
            clips_seq1 = projects[0].findall("./sequence/spine/asset-clip")
            self.assertEqual(len(clips_seq1), 2)
            self.assertEqual(clips_seq1[0].attrib.get("offset"), "0s")
            # 10s @ 29.97fps -> round(10 * 30000 / 1001) = 300 帧 -> 300300/30000s
            self.assertEqual(clips_seq1[0].attrib.get("start"), "300300/30000s")
            markers_clip1 = clips_seq1[0].findall("./marker")
            self.assertEqual(len(markers_clip1), 1)  # 默认仅 1 条高光标记，绝不塞入 [SUB] 字幕污染
            self.assertIn("reviewed", markers_clip1[0].attrib.get("value", ""))
            self.assertIn('极限 1v3 & "反杀" <神操作>', markers_clip1[0].attrib.get("value", ""))
            self.assertFalse(any("[SUB]" in m.attrib.get("value", "") for m in markers_clip1))
            # 落入第 1 段高光 [10s, 25s] 的 OBS 章节（12.5s）应挂为 chapter-marker
            ch_in_clip1 = clips_seq1[0].findall("./chapter-marker")
            self.assertEqual(len(ch_in_clip1), 1)
            self.assertIn('OBS "Boss" & <P2>', ch_in_clip1[0].attrib.get("value", ""))

            # 序列 2：OBS Chapters & Full Timeline 应包含全部 2 个 OBS chapter-marker + 2 个高光 marker
            self.assertEqual(projects[1].attrib.get("name"), "OBS Chapters & Full Timeline")
            full_clip = projects[1].find("./sequence/spine/asset-clip")
            self.assertIsNotNone(full_clip)
            assert full_clip is not None
            full_ch_markers = full_clip.findall("./chapter-marker")
            full_hl_markers = full_clip.findall("./marker")
            self.assertEqual(len(full_ch_markers), 2)
            self.assertEqual(len(full_hl_markers), 2)
            self.assertEqual(full_ch_markers[1].attrib.get("value"), "终局结算中文章节")

            # 2. 校验 Premiere Pro xmeml v5（主素材 masterclip-1 + 立体声双轨 exploded 结构）
            pr_raw = premiere_out.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE xmeml>", pr_raw)
            self.assertNotIn("\x00", pr_raw)
            pr_root = ET.fromstring(pr_raw)
            self.assertEqual(pr_root.tag, "xmeml")
            self.assertEqual(pr_root.attrib.get("version"), "5")

            # 顶层主素材定义 <clip id="masterclip-1"> 包含完整 <file id="file-1"><pathurl>
            master_clip = pr_root.find("./project/children/clip[@id='masterclip-1']")
            self.assertIsNotNone(master_clip)
            assert master_clip is not None
            pathurl = master_clip.findtext("./media/video/track/clipitem/file[@id='file-1']/pathurl") or ""
            self.assertTrue(pathurl.startswith("file://localhost/"))
            self.assertNotIn(" ", pathurl)
            self.assertIn("OBS 录屏 目录", unquote(pathurl))
            self.assertIn("高能 录像 clip & test.mp4", unquote(pathurl))

            seqs = pr_root.findall("./project/children/sequence")
            self.assertEqual(len(seqs), 2)
            self.assertEqual(seqs[0].findtext("./rate/timebase"), "30")
            self.assertEqual(seqs[0].findtext("./rate/ntsc"), "TRUE")

            v_clips = seqs[0].findall("./media/video/track/clipitem")
            a_tracks = seqs[0].findall("./media/audio/track")
            a_clips = seqs[0].findall("./media/audio/track/clipitem")
            self.assertEqual(len(v_clips), 2)
            # 默认立体声 2 声道：双轨 exploded Stereo，每轨 2 个 clipitem（共 4 个）
            self.assertEqual(len(a_tracks), 2)
            self.assertEqual(len(a_clips), 4)
            self.assertEqual(a_tracks[0].attrib.get("premiereTrackType"), "Stereo")
            self.assertEqual(a_tracks[0].attrib.get("currentExplodedTrackIndex"), "0")
            self.assertEqual(a_tracks[0].attrib.get("totalExplodedTrackCount"), "2")
            self.assertEqual(a_tracks[1].attrib.get("currentExplodedTrackIndex"), "1")
            self.assertEqual(a_clips[0].attrib.get("premiereChannelType"), "stereo")
            self.assertEqual(a_clips[0].findtext("./sourcetrack/trackindex"), "1")
            self.assertEqual(a_clips[2].findtext("./sourcetrack/trackindex"), "2")
            # 所有 clipitem 均关联 masterclip-1 并仅以 <file id="file-1"/> 引用不重复写 pathurl
            for ci in v_clips + a_clips:
                self.assertEqual(ci.findtext("./masterclipid"), "masterclip-1")
                self.assertIsNotNone(ci.find("./file[@id='file-1']"))
                self.assertIsNone(ci.find("./file[@id='file-1']/pathurl"))
            # 双向 <link> 含 groupindex=1（视频 + 音频左 + 音频右共 3 个 link）
            links_v1 = v_clips[0].findall("./link")
            self.assertEqual(len(links_v1), 3)
            self.assertTrue(any(lk.findtext("./groupindex") == "1" for lk in links_v1))
            # 精确帧边界：start=0, in=300, out=749 (25s @ 29.97fps -> round(25*30000/1001)=749), end=449
            self.assertEqual(v_clips[0].findtext("./start"), "0")
            self.assertEqual(v_clips[0].findtext("./in"), "300")
            self.assertEqual(v_clips[0].findtext("./out"), "749")
            self.assertEqual(v_clips[0].findtext("./end"), "449")
            self.assertEqual(v_clips[1].findtext("./start"), "449")

            # 序列 2：OBS 章节与完整原片序列应含 2 个 OBS 章节标记 + 2 个高光标记
            seq2_markers = seqs[1].findall("./marker")
            self.assertEqual(len(seq2_markers), 4)
            marker_names = [m.findtext("name") or "" for m in seq2_markers]
            self.assertTrue(any('OBS "Boss" & <P2>' in name for name in marker_names))
            self.assertTrue(any("终局结算中文章节" in name for name in marker_names))

            # 单独校验 path_to_file_url 对 FCPXML 与 Premiere 的一致性
            url_fcp = path_to_file_url(special_video, premiere_localhost=False)
            url_pr = path_to_file_url(special_video, premiere_localhost=True)
            self.assertTrue(url_fcp.startswith("file:///"))
            self.assertTrue(url_pr.startswith("file://localhost/"))

    def test_render_nle_only_subsecond_package_without_mp4_and_lossless_includes_nle(self) -> None:
        """测试 3：验证 mode='nle_only' 零转码极速交付全套 NLE 工程 + SRT（零 MP4），且 lossless 同步产出 FCPXML/Premiere XML。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src_mp4 = tmp / "obs_session.mp4"
            transcript_json = tmp / "obs_session_transcript.json"
            highlights_json = tmp / "obs_session_highlights.json"
            nle_out_dir = tmp / "nle_pack"
            lossless_out_dir = tmp / "lossless_pack"

            _create_mp4_with_obs_chapters(
                src_mp4,
                chapters=[
                    (1000, 4000, "Warmup Chapter"),
                    (5000, 9000, "决胜波次章节"),
                ],
                duration_sec=10.0,
            )
            transcript_json.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"start_sec": 1.5, "end_sec": 4.2, "text": "Let's go huge clutch!"},
                            {"start_sec": 6.0, "end_sec": 8.5, "text": "GG well played!"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            highlights_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "transcript_json_path": str(transcript_json),
                        "source_language": "en",
                        "target_language": "zh",
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "review_status": "reviewed",
                                "start_sec": 1.0,
                                "end_sec": 5.0,
                                "score": 9.4,
                                "title_tgt": "开局神级反杀",
                                "title_zh": "开局神级反杀",
                                "title_en": "Opening Clutch",
                                "summary_zh": "丝血反杀对面两人",
                                "subtitles": [
                                    {
                                        "start_sec": 1.5,
                                        "end_sec": 4.2,
                                        "text_src": "Let's go huge clutch!",
                                        "text_tgt": "起飞！极限反杀！",
                                    }
                                ],
                            },
                            {
                                "rank": 2,
                                "selected": False,
                                "review_status": "draft",
                                "start_sec": 5.5,
                                "end_sec": 9.0,
                                "score": 7.0,
                                "title_zh": "未勾选片段",
                                "summary_zh": "跳过",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # 3A. nle_only 模式：不得调用 cut_single_clip、零 MP4 产物、全套工程文件 + SRT 极速落盘
            t0 = time.perf_counter()
            with mock.patch("src.step4_cut.cut_single_clip", side_effect=AssertionError("nle_only 不应触发视频转码")) as mock_cut:
                nle_res = render_highlight_clips(
                    highlights_json_path=highlights_json,
                    output_dir=nle_out_dir,
                    mode="nle_only",
                    subtitle_lang="bilingual",
                )
            elapsed = time.perf_counter() - t0
            mock_cut.assert_not_called()
            self.assertLess(elapsed, 1.0, f"nle_only 耗时超过 1 秒红线：{elapsed:.2f}s")

            # 零 MP4 文件
            self.assertEqual(list(nle_out_dir.glob("*.mp4")), [])
            self.assertEqual(nle_res["mode"], "nle_only")
            self.assertEqual(nle_res["exported_count"], 1)
            self.assertEqual(nle_res["chapters_count"], 2)
            self.assertEqual(nle_res["clips"][0]["mp4_path"], "")
            self.assertEqual(nle_res["clips"][0]["encoder"], "nle_only")
            self.assertTrue(Path(nle_res["clips"][0]["srt_path"]).is_file())
            self.assertIn("起飞！极限反杀！", Path(nle_res["clips"][0]["srt_path"]).read_text(encoding="utf-8"))

            for key in (
                "fcpxml_path",
                "premiere_xml_path",
                "markers_edl_path",
                "markers_csv_path",
                "manifest_json_path",
            ):
                p = Path(nle_res[key])
                self.assertTrue(p.is_file(), f"缺少交付产物 {key}: {p}")

            # 校验 manifest 中完整记录了全部 NLE 路径与章节数
            manifest_data = json.loads(Path(nle_res["manifest_json_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest_data["chapters_count"], 2)
            self.assertEqual(manifest_data["fcpxml_path"], nle_res["fcpxml_path"])
            self.assertEqual(manifest_data["premiere_xml_path"], nle_res["premiere_xml_path"])

            # 校验生成的 FCPXML 与 Premiere XML 均含真实提取的 2 个 OBS 章节且 XML 合法
            fcp_tree = ET.fromstring(Path(nle_res["fcpxml_path"]).read_text(encoding="utf-8"))
            pr_tree = ET.fromstring(Path(nle_res["premiere_xml_path"]).read_text(encoding="utf-8"))
            self.assertIn("决胜波次章节", ET.tostring(fcp_tree, encoding="unicode"))
            self.assertIn("Warmup Chapter", ET.tostring(pr_tree, encoding="unicode"))

            # 3B. lossless 模式也同步产出 FCPXML 与 Premiere XML
            ll_res = render_highlight_clips(
                highlights_json_path=highlights_json,
                output_dir=lossless_out_dir,
                mode="lossless",
            )
            self.assertTrue(Path(ll_res["fcpxml_path"]).is_file())
            self.assertTrue(Path(ll_res["premiere_xml_path"]).is_file())
            self.assertEqual(ll_res["chapters_count"], 2)
            self.assertTrue(Path(ll_res["clips"][0]["mp4_path"]).is_file())

    def test_only_reviewed_gate_and_default_no_title_banner(self) -> None:
        """测试 4：验证 only_reviewed 门禁严格拦截 draft、放行 reviewed，且 show_title_banner 默认克制为 False。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src_mp4 = tmp / "review_gate.mp4"
            _create_mp4_with_obs_chapters(src_mp4, chapters=[], duration_sec=10.0)

            # 4A. 全部为 draft 时，only_reviewed=True 必须给出精准中文拦截提示
            all_draft_json = tmp / "all_draft_highlights.json"
            all_draft_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "review_status": "draft",
                                "start_sec": 1.0,
                                "end_sec": 4.0,
                                "score": 9.0,
                                "title_zh": "未审校草稿",
                            },
                            {
                                "rank": 2,
                                "selected": True,
                                "start_sec": 5.0,
                                "end_sec": 8.0,
                                "score": 8.5,
                                "title_zh": "默认缺省草稿",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                render_highlight_clips(
                    highlights_json_path=all_draft_json,
                    output_dir=tmp / "blocked_out",
                    mode="nle_only",
                    only_reviewed=True,
                )
            self.assertIn("当前没有已标记为「已人工校对」的高光片段", str(ctx.exception))

            # 4B. 混合状态：rank1(selected+draft), rank2(selected+reviewed), rank3(unselected+reviewed)
            mixed_json = tmp / "mixed_highlights.json"
            mixed_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "review_status": "draft",
                                "start_sec": 0.5,
                                "end_sec": 3.0,
                                "score": 8.8,
                                "title_zh": "草稿高光",
                            },
                            {
                                "rank": 2,
                                "selected": True,
                                "review_status": "reviewed",
                                "start_sec": 3.5,
                                "end_sec": 6.5,
                                "score": 9.5,
                                "title_zh": "已校对精选",
                            },
                            {
                                "rank": 3,
                                "selected": False,
                                "review_status": "reviewed",
                                "start_sec": 7.0,
                                "end_sec": 9.5,
                                "score": 7.9,
                                "title_zh": "未勾选但已校对",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            reviewed_res = render_highlight_clips(
                highlights_json_path=mixed_json,
                output_dir=tmp / "reviewed_out",
                mode="nle_only",
                only_reviewed=True,
            )
            self.assertEqual(reviewed_res["exported_count"], 1)
            self.assertEqual(reviewed_res["clips"][0]["rank"], 2)
            self.assertTrue(reviewed_res["only_reviewed"])
            # 确认默认 show_title_banner 为 False
            self.assertFalse(reviewed_res["show_title_banner"])

            # 红队 P0-8：即使显式指定 ranks=[1]（草稿），只要 only_reviewed=True 必须严格拦截，绝不允许绕过审校门禁！
            with self.assertRaises(ValueError):
                render_highlight_clips(
                    highlights_json_path=mixed_json,
                    output_dir=tmp / "single_rank_blocked_out",
                    mode="nle_only",
                    ranks=[1],
                    only_reviewed=True,
                )

            # 已校对的 ranks=[2] 在 only_reviewed=True 下正常放行；草稿 ranks=[1] 仅在 only_reviewed=False 时放行
            reviewed_rank_res = render_highlight_clips(
                highlights_json_path=mixed_json,
                output_dir=tmp / "single_rank_reviewed_out",
                mode="nle_only",
                ranks=[2],
                only_reviewed=True,
            )
            self.assertEqual(reviewed_rank_res["exported_count"], 1)
            self.assertEqual(reviewed_rank_res["clips"][0]["rank"], 2)

            draft_rank_res = render_highlight_clips(
                highlights_json_path=mixed_json,
                output_dir=tmp / "single_rank_draft_out",
                mode="nle_only",
                ranks=[1],
                only_reviewed=False,
            )
            self.assertEqual(draft_rank_res["exported_count"], 1)
            self.assertEqual(draft_rank_res["clips"][0]["rank"], 1)

            # 4C. 验证竖屏模式默认不渲染标题条（title_banner=None），显式传 True 时正常传递标题条
            captured_banners: list[str | None] = []
            real_gen_ass = __import__("src.step4_cut", fromlist=["generate_clip_ass"]).generate_clip_ass

            def _spy_gen_ass(*args, **kwargs):
                captured_banners.append(kwargs.get("title_banner"))
                return real_gen_ass(*args, **kwargs)

            fake_cut_res = {
                "mp4_path": str((tmp / "dummy.mp4").resolve()),
                "encoder": "libx264",
                "burned_subtitles": True,
                "lead_in_offset_sec": 0.0,
                "elapsed_sec": 0.1,
            }

            def _fake_cut_single(*args, **kwargs):
                out_p = Path(kwargs["out_mp4"])
                out_p.write_bytes(b"fake_mp4")
                res = dict(fake_cut_res)
                res["mp4_path"] = str(out_p.resolve())
                return res

            with mock.patch("src.step4_cut.generate_clip_ass", side_effect=_spy_gen_ass), mock.patch(
                "src.step4_cut.cut_single_clip", side_effect=_fake_cut_single
            ):
                render_highlight_clips(
                    highlights_json_path=mixed_json,
                    output_dir=tmp / "vert_default_out",
                    mode="vertical",
                    ranks=[2],
                )
                render_highlight_clips(
                    highlights_json_path=mixed_json,
                    output_dir=tmp / "vert_banner_out",
                    mode="vertical",
                    ranks=[2],
                    show_title_banner=True,
                )

            self.assertEqual(len(captured_banners), 2)
            self.assertIsNone(captured_banners[0])
            self.assertEqual(captured_banners[1], "已校对精选")

    def test_nle_dtd_compliance_vfr_timecode_audio_layout_and_empty_guards(self) -> None:
        """测试 5：红队 NLE DTD 合规全面回归——VFR 贴靠、非标分辨率、音频白名单/声道布局、真实起始时间码、字幕开关与空列表防呆。"""
        # 1. VFR（如 59.83 / 29.88 / 23.95 / 119.9 / 120.05）自动贴靠标准广播 NLE 帧率，绝不输出 100/5983s
        self.assertEqual(resolve_fps_rational(59.83), (1001, 60000, 60, True))
        self.assertEqual(resolve_fps_rational(29.88), (1001, 30000, 30, True))
        self.assertEqual(resolve_fps_rational(23.95), (1001, 24000, 24, True))
        self.assertEqual(resolve_fps_rational(119.9), (1001, 120000, 120, True))
        self.assertEqual(resolve_fps_rational(120.05), (1, 120, 120, False))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src_video = tmp / "ultrawide_tc.mp4"
            src_video.write_bytes(b"")

            # 模拟带鱼屏 3440x1440、非标 16000Hz 音频、6 声道环绕、起始时间码 01:00:00:00 (3600s) 的 probe 结果
            fake_meta = {
                "duration_sec": 60.0,
                "width": 3440,
                "height": 1440,
                "fps": 59.83,
                "has_video": True,
                "has_audio": True,
                "audio_channels": 6,
                "audio_rate": 16000,
                "start_timecode": "01:00:00:00",
                "start_timecode_sec": 3600.0,
            }
            manual_hl = [
                {
                    "rank": 1,
                    "selected": True,
                    "review_status": "reviewed",
                    "start_sec": 5.0,
                    "end_sec": 15.0,
                    "score": None,  # 手动片段无 AI 评分
                    "title_tgt": "手动带鱼屏片段",
                    "subtitles": [
                        {"start_sec": 6.0, "end_sec": 8.0, "text_src": "Hello", "text_tgt": "你好"}
                    ],
                }
            ]

            with mock.patch("src.nle_export.probe_media_metadata", return_value=fake_meta):
                fcp_path = tmp / "ultrawide.fcpxml"
                export_fcpxml(
                    highlights=manual_hl,
                    video_path=src_video,
                    output_path=fcp_path,
                    fps=59.83,
                    include_subtitle_markers=True,
                    subtitle_lang="bilingual",
                )
                fcp_root = ET.fromstring(fcp_path.read_text(encoding="utf-8"))
                fmt_el = fcp_root.find("./resources/format[@id='r1']")
                assert fmt_el is not None
                # 非标宽高 3440x1440 省略 format@name，绝不伪造 FFVideoFormat3440x1440p...
                self.assertNotIn("name", fmt_el.attrib)
                self.assertEqual(fmt_el.attrib.get("frameDuration"), "1001/60000s")

                # 非法采样率 16000 回落白名单默认 "48k"，6 声道 -> "surround"
                seq_el = fcp_root.find("./library/event/project/sequence")
                assert seq_el is not None
                self.assertEqual(seq_el.attrib.get("audioRate"), "48k")
                self.assertEqual(seq_el.attrib.get("audioLayout"), "surround")

                # 起始时间码 3600s 叠加到 asset@start、asset-clip@start 与 marker@start
                asset_el = fcp_root.find("./resources/asset[@id='r2']")
                clip_el = fcp_root.find("./library/event/project/sequence/spine/asset-clip")
                assert asset_el is not None and clip_el is not None
                self.assertNotEqual(asset_el.attrib.get("start"), "0s")
                self.assertNotEqual(clip_el.attrib.get("start"), "0s")
                # 显式 include_subtitle_markers=True 时输出 [SUB] 标记，且 score=None 显示 Manual
                markers = clip_el.findall("./marker")
                self.assertIn("Manual", markers[0].attrib.get("value", ""))
                self.assertTrue(any("[SUB]" in m.attrib.get("value", "") for m in markers))

                # subtitle_lang="none" 即使传 include_subtitle_markers=True 也强制不输出 [SUB]
                fcp_nosub = tmp / "ultrawide_nosub.fcpxml"
                export_fcpxml(
                    highlights=manual_hl,
                    video_path=src_video,
                    output_path=fcp_nosub,
                    include_subtitle_markers=True,
                    subtitle_lang="none",
                )
                nosub_root = ET.fromstring(fcp_nosub.read_text(encoding="utf-8"))
                nosub_markers = nosub_root.findall("./library/event/project/sequence/spine/asset-clip/marker")
                self.assertFalse(any("[SUB]" in m.attrib.get("value", "") for m in nosub_markers))

                # 校验 Premiere 起始时间码 + 点标记 out=-1
                pr_path = tmp / "ultrawide_pr.xml"
                export_premiere_xml(
                    highlights=manual_hl,
                    video_path=src_video,
                    output_path=pr_path,
                    fps=30.0,
                    chapters=[{"index": 1, "start_sec": 10.0, "end_sec": 10.0, "title": "Zero Width Chapter"}],
                )
                pr_root = ET.fromstring(pr_path.read_text(encoding="utf-8"))
                tc_el = pr_root.find("./project/children/clip[@id='masterclip-1']/media/video/track/clipitem/file[@id='file-1']/timecode")
                assert tc_el is not None
                self.assertEqual(tc_el.findtext("./frame"), "108000")
                self.assertEqual(tc_el.findtext("./string"), "01:00:00:00")
                # 0 宽度章节标记 out_frame <= in_frame 必须强设 -1（点标记）
                ch_markers = pr_root.findall("./project/children/sequence[2]/marker")
                zero_ch = next(m for m in ch_markers if "Zero Width Chapter" in (m.findtext("name") or ""))
                self.assertEqual(zero_ch.findtext("out"), "-1")

            # 2. 单声道 44.1kHz 校验：FCPXML audioLayout=mono & audioRate=44.1k；Premiere 仅 1 条音频轨
            mono_meta = {
                "duration_sec": 30.0,
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
                "has_video": True,
                "has_audio": True,
                "audio_channels": 1,
                "audio_rate": 44100,
                "start_timecode": "",
                "start_timecode_sec": 0.0,
            }
            with mock.patch("src.nle_export.probe_media_metadata", return_value=mono_meta):
                fcp_mono = tmp / "mono.fcpxml"
                pr_mono = tmp / "mono_pr.xml"
                export_fcpxml(manual_hl, src_video, fcp_mono)
                export_premiere_xml(manual_hl, src_video, pr_mono)
                fcp_m_root = ET.fromstring(fcp_mono.read_text(encoding="utf-8"))
                seq_m = fcp_m_root.find("./library/event/project/sequence")
                assert seq_m is not None
                self.assertEqual(seq_m.attrib.get("audioRate"), "44.1k")
                self.assertEqual(seq_m.attrib.get("audioLayout"), "mono")
                pr_m_root = ET.fromstring(pr_mono.read_text(encoding="utf-8"))
                self.assertEqual(len(pr_m_root.findall("./project/children/sequence[1]/media/audio/track")), 1)

            # 3. 空 highlights 绝不产出空 0s <spine/> 或空精选序列
            with mock.patch("src.nle_export.probe_media_metadata", return_value=mono_meta):
                fcp_empty = tmp / "empty.fcpxml"
                pr_empty = tmp / "empty_pr.xml"
                export_fcpxml([], src_video, fcp_empty, chapters=[{"index": 1, "start_sec": 2.0, "end_sec": 5.0, "title": "Ch1"}])
                export_premiere_xml([], src_video, pr_empty, chapters=[{"index": 1, "start_sec": 2.0, "end_sec": 5.0, "title": "Ch1"}])
                empty_fcp_root = ET.fromstring(fcp_empty.read_text(encoding="utf-8"))
                fcp_projs = empty_fcp_root.findall("./library/event/project")
                self.assertEqual(len(fcp_projs), 1)
                self.assertEqual(fcp_projs[0].attrib.get("name"), "OBS Chapters & Full Timeline")
                empty_pr_root = ET.fromstring(pr_empty.read_text(encoding="utf-8"))
                self.assertEqual(len(empty_pr_root.findall("./project/children/sequence")), 1)

    def test_step4_cut_redteam_hardening_ranks_empty_csv_injection_timecode_and_manifest(self) -> None:
        """测试 6：红队 Step 4 加固回归——ranks=[] 拦截、空产物拒写、CSV 公式注入中和、29.97fps 零漂移时间码与 manifest 字段。"""
        import csv
        import src.step4_cut as step4_mod
        from src.nle_export import seconds_to_frames
        from src.step1_audio import probe_media_info

        # 1. 已彻底删除 step4_cut 中重复的私有 ffprobe 探测函数
        self.assertFalse(hasattr(step4_mod, "probe_video_fps"))
        self.assertFalse(hasattr(step4_mod, "probe_video_duration"))
        self.assertFalse(hasattr(step4_mod, "probe_video_dimensions"))

        # 2. 29.97fps 下 1 小时精确帧计数对齐 nle_export.seconds_to_frames，消灭 10.8 秒漂移
        frames_1h = seconds_to_frames(3600.0, 29.97)  # round(3600 * 30000 / 1001) = 107892
        tc_1h = step4_mod.format_timecode_fps(3600.0, 29.97)
        expected_hh = frames_1h // (30 * 3600)
        expected_mm = (frames_1h % (30 * 3600)) // (30 * 60)
        expected_ss = (frames_1h % (30 * 60)) // 30
        expected_ff = frames_1h % 30
        self.assertEqual(tc_1h, f"{expected_hh:02d}:{expected_mm:02d}:{expected_ss:02d}:{expected_ff:02d}")
        self.assertNotEqual(tc_1h, "01:00:00:00")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src_mp4 = tmp / "formula_test.mp4"
            _create_mp4_with_obs_chapters(src_mp4, chapters=[], duration_sec=8.0)

            # 权威 probe_media_info 应返回音频声道、采样率与时间码新字段
            media_info = probe_media_info(src_mp4)
            self.assertIn("audio_channels", media_info)
            self.assertIn("audio_rate", media_info)
            self.assertIn("start_timecode", media_info)
            self.assertIn("start_timecode_sec", media_info)
            self.assertEqual(media_info["audio_rate"], 48000)

            hl_json = tmp / "formula_test_highlights.json"
            hl_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "source_language": "en",
                        "target_language": "zh",
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "review_status": "reviewed",
                                "start_sec": 1.0,
                                "end_sec": 5.0,
                                "score": None,  # 手动无评分片段
                                "title_tgt": "=CMD|' /C calc'!A0",
                                "summary_zh": "+SUM(1,2)",
                                "reason": "@HYPERLINK(\"http://evil\")",
                                "tags": ["-evil_tag"],
                                "subtitles": [
                                    {"start_sec": 1.5, "end_sec": 3.5, "text_src": "Test", "text_tgt": "测试"}
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # 3. ranks=[] 显式报错，绝不退化为全量导出
            with self.assertRaises(ValueError):
                render_highlight_clips(hl_json, output_dir=tmp / "empty_ranks_out", mode="nle_only", ranks=[])

            # 4. 空 prepared_items（如所有选中片段 start >= end）直接报错不写空壳产物
            invalid_hl_json = tmp / "invalid_highlights.json"
            invalid_hl_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "highlights": [
                            {"rank": 1, "selected": True, "review_status": "reviewed", "start_sec": 5.0, "end_sec": 2.0}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bad_out = tmp / "invalid_out"
            with self.assertRaises(ValueError):
                render_highlight_clips(invalid_hl_json, output_dir=bad_out, mode="nle_only")
            self.assertFalse((bad_out / "export_manifest.json").exists())

            # 5. CSV 公式注入中和 + subtitle_lang 归一化 + exported_clip_count/nle_item_count 区分 + score=None 兼容
            nle_out = tmp / "csv_safe_out"
            res = render_highlight_clips(
                hl_json,
                output_dir=nle_out,
                mode="nle_only",
                subtitle_lang="zh_only",
                only_reviewed=True,
            )
            self.assertEqual(res["subtitle_lang"], "target_only")
            self.assertEqual(res["exported_clip_count"], 0)
            self.assertEqual(res["nle_item_count"], 1)
            self.assertIsNone(res["clips"][0]["score"])
            # 确认默认传 include_subtitle_markers=False，FCPXML 不含 [SUB] 污染
            self.assertNotIn("[SUB]", Path(res["fcpxml_path"]).read_text(encoding="utf-8"))

            with Path(res["markers_csv_path"]).open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["Name"].startswith("'="), f"未中和公式注入：{rows[0]['Name']}")
            self.assertTrue(rows[0]["Notes"].startswith("'+"), f"未中和公式注入：{rows[0]['Notes']}")
            self.assertEqual(rows[0]["Score"], "")
            for payload in ("-1+1", "@HYPERLINK(\"http://evil\")", "  =SUM(A1)", "\t+1"):
                self.assertTrue(step4_mod._sanitize_csv_cell(payload).startswith("'"))


if __name__ == "__main__":
    unittest.main()
