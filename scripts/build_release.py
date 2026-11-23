"""Solacut 安全发布构建、商用合规语料装配、Windows 免 Python 便携包打包与零泄露审计器。

严格遵循安全发布铁律：
1. 物理隔离开发目录：绝不打包 `.env`、`.workbench_config.json`、`input/`、`output/`、`.webview_profile`、`data/corpus/raw_cache/`；
2. 商用语料合规打包：自动基于 `--commercial-only` 生成纯商用无限制索引包（MIT / CC BY 2.0 / CC BY-SA 4.0，共 228,542 条），
   严禁将 TED2020 (CC BY-NC-ND) 或 OpenSubtitles 随安装包分发；
3. 自动重置 `user_glossary.json` 为干净空模板，防止开发机隐私词条泄露；
4. 自动拷入静态 `ffmpeg.exe` 与 `ffprobe.exe` 至便携包 `bin/`，小白零依赖双击即用；
5. 自动使用 PyInstaller `--onedir`（防 `--onefile` 杀软误报）编译 `Solacut.exe`（默认自动注入 `assets/brand/app_icon.ico`）；
6. 强制执行 Zero-Leak 安全扫描门禁（0 密钥 `sk-`、0 本机路径、0 敏感文件、0 非商用语料），通过后自动输出 `SHA256SUMS.txt` 与便携 ZIP。

使用示例：
    # 1. 仅执行安全发布舞台装配 + 商用合规语料构建 + 零泄露审计（不跑 PyInstaller，用于快速校验）
    python scripts/build_release.py --stage-only

    # 2. 完整编译 Windows 免 Python 一键便携包（需 .venv 已装 pyinstaller，默认自动注入品牌图标）
    python scripts/build_release.py --version v0.9.0-beta
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.corpus_builder import ALL_LANG_PAIRS, build_corpus_for_lang_pair  # noqa: E402
from src.step1_audio import check_ffmpeg_available, check_ffprobe_available  # noqa: E402

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI/DeepSeek API Key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("Generic Bearer Token", re.compile(r"Bearer\s+sk-[A-Za-z0-9_\-]{12,}", re.IGNORECASE)),
    ("GitHub Personal Access Token", re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("Google API Key (AIza...)", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("Private Key Block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)
_FORBIDDEN_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".workbench_config.json",
        ".desktop_runtime.log",
        ".desktop_crash.log",
        "id_rsa",
        "id_ed25519",
    }
)
_FORBIDDEN_EXTS: frozenset[str] = frozenset({".key", ".p12", ".pfx"})
_ALLOWED_PUBLIC_CERT_PATHS: frozenset[str] = frozenset({"_internal/certifi/cacert.pem"})
_FORBIDDEN_DIRNAMES: frozenset[str] = frozenset(
    {
        "raw_cache",
        ".webview_profile",
        ".venv",
        "__pycache__",
        ".git",
    }
)
_TEXT_SCAN_EXTS: frozenset[str] = frozenset(
    {".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".bat", ".cmd", ".env", ".example", ".yml", ".yaml", ".toml"}
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_security_audit(release_dir: Path) -> list[str]:
    """对发布目录执行严苛零泄露与合规门禁扫描，返回违规列表（空列表代表 100% 通过）。"""
    violations: list[str] = []
    cur_user = getpass.getuser()
    user_path_re = re.compile(
        rf"(?:[A-Za-z]:[/\\]Users[/\\]{re.escape(cur_user)}|/Users/{re.escape(cur_user)}|e:[/\\]Personal-AI)",
        re.IGNORECASE,
    )

    for root, dirs, files in os.walk(release_dir):
        root_p = Path(root)
        for d in dirs:
            if d in _FORBIDDEN_DIRNAMES:
                violations.append(f"发现禁止发布的目录: {(root_p / d).relative_to(release_dir)}")
        for fname in files:
            fpath = root_p / fname
            rel = str(fpath.relative_to(release_dir)).replace("\\", "/")
            if (
                fname in _FORBIDDEN_FILENAMES
                or fpath.suffix.lower() in _FORBIDDEN_EXTS
                or (fpath.suffix.lower() == ".pem" and rel not in _ALLOWED_PUBLIC_CERT_PATHS)
            ):
                violations.append(f"发现禁止发布的敏感文件: {rel}")
                continue
            # 禁止携带开发机上的真实个人录屏或切片（assets/demo/ 除外）
            if fpath.suffix.lower() in {".mp4", ".mkv", ".mov", ".flv", ".wav"} and not rel.startswith("assets/demo/"):
                violations.append(f"发现非示例音视频文件（疑为个人素材）: {rel}")
                continue
            if fpath.suffix.lower() in _TEXT_SCAN_EXTS and fpath.stat().st_size <= 5 * 1024 * 1024:
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for label, pat in _SECRET_PATTERNS:
                    if pat.search(content):
                        violations.append(f"发现疑似密钥泄漏 [{label}]: {rel}")
                # 排除打包器自身源码（若不含脚本）检查本机用户名/绝对开发路径
                if not rel.endswith("build_release.py") and user_path_re.search(content):
                    violations.append(f"发现本机开发路径泄漏: {rel}")

    # 校验商用分发语料库清单：严禁包含 commercial_safe=False 的非商用源
    manifest_p = release_dir / "data" / "corpus" / "corpus_manifest.json"
    if manifest_p.is_file():
        try:
            manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
            for lp, info in manifest.get("lang_pairs", {}).items():
                for src in info.get("sources", []):
                    if not src.get("commercial_safe", False):
                        violations.append(
                            f"商用分发包误含非商用语料源 [{lp}/{src.get('source_id')}]: {src.get('license')}"
                        )
        except Exception as exc:
            violations.append(f"无法校验语料清单 corpus_manifest.json: {exc}")

    return violations


def assemble_commercial_corpus(target_corpus_root: Path) -> dict[str, Any]:
    """在隔离目录中复用本地 raw_cache 生成 100% 商用无限制语料包（零非商用污染）。"""
    target_corpus_root.mkdir(parents=True, exist_ok=True)
    src_corpus_root = PROJECT_ROOT / "data" / "corpus"

    # 1. 复制 MIT 原创专家词库（全部 6 语种）
    for lp in ALL_LANG_PAIRS:
        src_lp = src_corpus_root / lp
        dst_lp = target_corpus_root / lp
        dst_lp.mkdir(parents=True, exist_ok=True)
        for jname in ("curated_gaming_glossary.json", "curated_idioms_slang.json"):
            src_j = src_lp / jname
            if src_j.is_file():
                shutil.copy2(src_j, dst_lp / jname)

    # 2. 写入干净空白的用户专属词库模板（绝不带出开发者私人词条）
    empty_user_glossary = {
        "meta": {"name": "公会与主播专属自定义术语与ASR纠错库", "priority": "highest"},
        "terms": [],
    }
    (target_corpus_root / "user_glossary.json").write_text(
        json.dumps(empty_user_glossary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. 软链接或指向本地 raw_cache 构建纯商用无限制索引，构建后立即移除 raw_cache 引用
    dev_raw_cache = src_corpus_root / "raw_cache"
    tmp_cache = target_corpus_root / "raw_cache"
    created_junction = False
    if dev_raw_cache.is_dir() and not tmp_cache.exists():
        if sys.platform.startswith("win"):
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(tmp_cache), str(dev_raw_cache)],
                check=False,
                capture_output=True,
            )
            created_junction = tmp_cache.exists()
        else:
            tmp_cache.symlink_to(dev_raw_cache)
            created_junction = True

    summary: dict[str, Any] = {}
    try:
        for lp in ALL_LANG_PAIRS:
            info = build_corpus_for_lang_pair(
                lang_pair=lp,
                corpus_root=target_corpus_root,
                commercial_only=True,
                include_opensubtitles=False,
            )
            # 便携包中仅保留高效 FTS5 corpus_index.db，删除大体积冗余 jsonl 省一半空间
            jsonl_file = target_corpus_root / lp / "open_parallel_corpus.jsonl"
            if jsonl_file.is_file():
                jsonl_file.unlink()
            summary[lp] = info["total_pairs"]
    finally:
        if created_junction and tmp_cache.exists():
            if sys.platform.startswith("win"):
                subprocess.run(["cmd", "/c", "rmdir", str(tmp_cache)], check=False, capture_output=True)
            else:
                tmp_cache.unlink()
        elif tmp_cache.is_dir():
            shutil.rmtree(tmp_cache, ignore_errors=True)

    return summary


def stage_portable_assets(release_dir: Path, include_ffmpeg: bool = True) -> None:
    """将合规资源（web、assets/demo、LICENSE、空 input/output、静态 FFmpeg）精准装配至发布目录。"""
    release_dir.mkdir(parents=True, exist_ok=True)

    # 1. 前端静态资源
    dst_web = release_dir / "web"
    if dst_web.exists():
        shutil.rmtree(dst_web)
    shutil.copytree(PROJECT_ROOT / "web", dst_web)

    # 2. 30 秒免版权内置示例工程 + 品牌图标资产
    for asset_sub in ("demo", "brand"):
        src_sub = PROJECT_ROOT / "assets" / asset_sub
        if src_sub.is_dir():
            dst_sub = release_dir / "assets" / asset_sub
            if dst_sub.exists():
                shutil.rmtree(dst_sub)
            shutil.copytree(src_sub, dst_sub)

    # 3. 合规文档与干净配置模板
    for doc in ("LICENSE", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "README.md", ".env.example"):
        p = PROJECT_ROOT / doc
        if p.is_file():
            shutil.copy2(p, release_dir / doc)

    # 4. 干净空工作区目录
    for empty_dir in ("input", "output"):
        d = release_dir / empty_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitkeep").write_text("", encoding="utf-8")

    # 5. 内置 FFmpeg / FFprobe 至 bin/
    if include_ffmpeg:
        bin_dir = release_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for fn in (check_ffmpeg_available, check_ffprobe_available):
            try:
                exe_path = Path(fn())
                shutil.copy2(exe_path, bin_dir / exe_path.name)
                print(f"[内置媒体引擎] 已打入 {exe_path.name} ({exe_path.stat().st_size / 1048576:.1f} MB)")
            except RuntimeError as exc:
                print(f"[警告] 未找到可内置的 FFmpeg 工具：{exc}", file=sys.stderr)


def build_pyinstaller_onedir(release_dir: Path, icon_path: Path | None = None) -> Path:
    """调用 PyInstaller --onedir 模式编译免 Python 桌面主程序 Solacut.exe。"""
    build_tmp = PROJECT_ROOT / "build_release_tmp"
    dist_tmp = build_tmp / "dist"
    work_tmp = build_tmp / "work"
    if build_tmp.exists():
        shutil.rmtree(build_tmp)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "Solacut",
        "--distpath",
        str(dist_tmp),
        "--workpath",
        str(work_tmp),
        "--specpath",
        str(build_tmp),
        "--collect-all",
        "webview",
        "--hidden-import",
        "src.corpus_builder",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
        # 便携包默认走云端 ASR（DeepSeek / OpenAI 兼容），排除重型本地 CUDA/Whisper 依赖以免体积暴涨至 2GB+
        "--exclude-module",
        "nvidia",
        "--exclude-module",
        "faster_whisper",
        "--exclude-module",
        "ctranslate2",
        "--exclude-module",
        "onnxruntime",
        "--exclude-module",
        "av",
        "--exclude-module",
        "yt_dlp",
        "--exclude-module",
        "torch",
        "--exclude-module",
        "tokenizers",
        "--exclude-module",
        "huggingface_hub",
        "--exclude-module",
        "PIL",
        "--exclude-module",
        "pytest",
    ]
    if icon_path and icon_path.is_file():
        cmd.extend(["--icon", str(icon_path.resolve())])
    cmd.append(str(PROJECT_ROOT / "src" / "desktop_app.py"))

    print("[PyInstaller] 开始以 --onedir 安全模式编译 Solacut.exe ...")
    subprocess.run(cmd, check=True)

    built_dir = dist_tmp / "Solacut"
    if not built_dir.is_dir():
        raise RuntimeError(f"PyInstaller 编译产物目录缺失：{built_dir}")

    # 将编译产物（Solacut.exe + _internal/）合入最终便携发布目录
    for item in built_dir.iterdir():
        dst = release_dir / item.name
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(item), str(dst))

    shutil.rmtree(build_tmp, ignore_errors=True)
    return release_dir / "Solacut.exe"


def write_checksums_and_zip(release_dir: Path, zip_path: Path) -> Path:
    """为便携发布包生成 SHA256SUMS.txt 与可分发 ZIP 压缩包。"""
    checksum_lines: list[str] = []
    for f in sorted(release_dir.rglob("*")):
        if f.is_file() and f.name != "SHA256SUMS.txt":
            rel = str(f.relative_to(release_dir)).replace("\\", "/")
            checksum_lines.append(f"{_sha256_file(f)}  {rel}")
    (release_dir / "SHA256SUMS.txt").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(release_dir.rglob("*")):
            if f.is_file():
                arcname = f"{release_dir.name}/{f.relative_to(release_dir).as_posix()}"
                zf.write(f, arcname=arcname)

    zip_sha = _sha256_file(zip_path)
    (zip_path.with_suffix(".zip.sha256")).write_text(f"{zip_sha}  {zip_path.name}\n", encoding="utf-8")
    print(f"[打包完成] {zip_path} ({zip_path.stat().st_size / 1048576:.1f} MB) | SHA256={zip_sha}")
    return zip_path


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Solacut 安全发布与 Windows 免 Python 便携包构建器")
    parser.add_argument("--version", default="v0.9.0-beta", help="发布版本号（默认 v0.9.0-beta）")
    parser.add_argument("--out-root", default=str(PROJECT_ROOT / "release"), help="发布产物输出根目录")
    parser.add_argument(
        "--icon",
        default=str(PROJECT_ROOT / "assets" / "brand" / "app_icon.ico"),
        help="自定义 Windows 图标 .ico 文件路径（默认自动使用 assets/brand/app_icon.ico）",
    )
    parser.add_argument("--stage-only", action="store_true", help="仅装配商用合规语料、资源与执行零泄露安全审计（跳过 PyInstaller 编译）")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="跳过内置 ffmpeg/ffprobe 拷贝")
    parser.add_argument("--skip-zip", action="store_true", help="跳过生成最终 ZIP 归档")
    args = parser.parse_args(argv)

    out_root = Path(args.out_root).resolve()
    release_dir = out_root / f"Solacut-{args.version}-win64-portable"
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Solacut 安全发布流水线 ({args.version}) ===")
    stage_portable_assets(release_dir, include_ffmpeg=not args.skip_ffmpeg)

    print("[语料合规装配] 正在生成纯商用无限制 6 语种 FTS5 索引包 (MIT / CC BY / CC BY-SA)...")
    corpus_summary = assemble_commercial_corpus(release_dir / "data" / "corpus")
    total_comm = sum(corpus_summary.values())
    print(f"[语料合规完成] 6 语种纯商用语料合计 {total_comm:,} 条: {corpus_summary}")

    if not args.stage_only:
        icon_p = Path(args.icon) if args.icon else None
        build_pyinstaller_onedir(release_dir, icon_path=icon_p)

    print("[安全门禁审计] 正在全量扫描发布目录（密钥 / 本机路径 / 个人素材 / 语料许可）...")
    violations = run_security_audit(release_dir)
    if violations:
        print("!!! 安全审计未通过，已中止打包 !!!", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 2
    print("[安全审计通过] 0 密钥泄露 · 0 本机隐私路径 · 0 非商用语料违规！")

    if not args.skip_zip:
        zip_path = out_root / f"Solacut-{args.version}-win64-portable.zip"
        write_checksums_and_zip(release_dir, zip_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
