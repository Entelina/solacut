/**
 * Solacut 官网集中式发版配置文件
 * -----------------------------------------------------------------------------
 * 每次正式发版时，只需在本文件中完成 3 步即可全站自动生效：
 * 1. 将 `RELEASE_PUBLISHED`（及 `REPO_PUBLISHED`）改为 `true`；
 * 2. 更新 `VERSION`、`RELEASE_DATE`、`DOWNLOAD_LINKS.fileSize` 与下载地址；
 * 3. 在 PowerShell 运行 `Get-FileHash .\Solacut-<VERSION>-win64-portable.zip -Algorithm SHA256`
 *    并将真实 64 位十六进制哈希写入 `DOWNLOAD_LINKS.checksumSha256`。
 *
 * 在 `RELEASE_PUBLISHED = false`（公测预发阶段）时：
 * - 所有下载与未上线仓库链接不会跳转至无效 404 / NXDOMAIN，而是平滑呼出内置
 *   暗房交付说明与合规署名弹窗，保持零死链与 100% 透明体验；
 * - 校验控制台自动切换为「待发版固化校验槽 + 可一键复制的 PowerShell 核验命令」，
 *   绝不展示虚假或空文件哈希。
 */

/** 品牌主名称 */
export const BRAND_NAME = 'Solacut';

/** 规划主域名（备选：solacut.cn / solacut.com.cn） */
export const DOMAIN = 'solacut.com';

/** 站点规范 URL */
export const SITE_URL = `https://${DOMAIN}`;

/** 当前公开发布版本号 */
export const VERSION = 'v0.9.0-beta';

/** 顶栏紧凑展示版本短标 */
export const VERSION_SHORT = 'v0.9 Beta';

/** 当前版本发布日期（ISO 格式 YYYY-MM-DD） */
export const RELEASE_DATE = '2026-11-23';

/**
 * 是否已上传真实公测便携包直链并填入真实 SHA-256 哈希。
 * 发版上传 Release 资产后改为 true 即可 1 秒切换为直链下载模式。
 */
export const RELEASE_PUBLISHED = true;

/**
 * 开源代码仓库是否已公开上线。
 * 仓库公开后改为 true，导航栏与页脚链接将直接外链至 GitHub。
 */
export const REPO_PUBLISHED = true;

/** 开源代码仓库地址（正式 GitHub 公开仓库） */
export const REPO_URL = 'https://github.com/Entelina/solacut';

/** 第三方组件与语料署名声明链接 */
export const NOTICES_URL = `${REPO_URL}/blob/main/THIRD_PARTY_NOTICES.md`;

/** 开源许可协议链接 */
export const LICENSE_URL = `${REPO_URL}/blob/main/LICENSE`;

/** 问题反馈与公测讨论区链接 */
export const ISSUES_URL = `${REPO_URL}/issues`;

/**
 * Windows 64-bit 便携版交付清单
 */
export const DOWNLOAD_LINKS = {
  /** GitHub Releases 主下载直链（RELEASE_PUBLISHED = true 时激活） */
  githubReleases: `${REPO_URL}/releases/download/${VERSION}/Solacut-${VERSION}-win64-portable.zip`,
  /** 备选发行页 / 国内镜像入口（当前直达 GitHub Releases 校验资产页，接入独立镜像域后替换直链） */
  cnMirror: `${REPO_URL}/releases/tag/${VERSION}`,
  /** 便携包文件名（用于核验命令与交付规格展示） */
  fileName: `Solacut-${VERSION}-win64-portable.zip`,
  /**
   * 正式发版包真实 64 位 SHA-256 哈希
   */
  checksumSha256: 'ddce1b2c3db6fc5a9ed1d56c073fda9895a0cae462a481377c2ad5e9d8f64a51',
  /** 便携包体积说明（内含静态 FFmpeg/FFprobe + WebView2 壳 + 228,210 条商用无限制语料索引） */
  fileSize: '247.4 MB',
  /** 目标系统要求 */
  platform: 'Windows 10 / 11 (x64) · 免 Python / 解压即用',
} as const;

/** PowerShell 本地 SHA-256 核验命令 */
export const VERIFY_COMMAND = `Get-FileHash .\\${DOWNLOAD_LINKS.fileName} -Algorithm SHA256`;

export const SITE_CONFIG = {
  BRAND_NAME,
  DOMAIN,
  SITE_URL,
  VERSION,
  VERSION_SHORT,
  RELEASE_DATE,
  RELEASE_PUBLISHED,
  REPO_PUBLISHED,
  REPO_URL,
  NOTICES_URL,
  LICENSE_URL,
  ISSUES_URL,
  DOWNLOAD_LINKS,
  VERIFY_COMMAND,
} as const;
