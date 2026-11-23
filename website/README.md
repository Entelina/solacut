# Solacut 官方网站 (`website/`)

Solacut 官方发布页前端源码，采用 **Vite + TypeScript + 原生现代 CSS** 零冗余架构，专为 Vercel 生产部署与国内直连秒开优化（0 国外字体外链、极小首屏产物体积）。

## 一、本地开发与构建验证

```powershell
cd website

# 安装依赖
npm install

# 启动本地开发预览服务器（默认 http://localhost:5173）
npm run dev

# 执行严格 TypeScript 类型检查 + Vite 生产打包（输出至 dist/）
npm run build

# 预览生产构建产物
npm run preview
```

## 二、发版 1 秒更新配置

所有版本号、发布日期、下载直链、国内镜像、体积与 SHA-256 校验值均集中在：

- [`src/config/site.ts`](./src/config/site.ts)

每次构建新的便携包（例如 `Solacut-v0.9.1-beta-win64-portable.zip`）后：

1. 在 PowerShell 计算真实 SHA-256：
   ```powershell
   Get-FileHash .\Solacut-v0.9.1-beta-win64-portable.zip -Algorithm SHA256
   ```
2. 修改 `src/config/site.ts` 中的 `VERSION`、`VERSION_SHORT`、`RELEASE_DATE` 与 `DOWNLOAD_LINKS`（含 `githubReleases`、`cnMirror`、`checksumSha256`、`fileSize`）。
3. 推送到 Git 仓库后，Vercel 将自动触发生产构建并全站同步更新。

## 三、Vercel 一键上线步骤

1. 将仓库推送到 GitHub / GitLab / Bitbucket；
2. 登录 [Vercel Dashboard](https://vercel.com/new) 导入该仓库；
3. 在 **Configure Project** 页面设置：
   - **Framework Preset**: `Vite`
   - **Root Directory**: `website`（**关键**：点击 Edit 选择 `website` 目录）
   - **Build Command**: `npm run build`
   - **Output Directory**: `dist`
4. 点击 **Deploy** 等待约 15~30 秒上线；
5. 在 Vercel 项目 **Settings → Domains** 绑定主域名 `solacut.com`（及备选 `solacut.cn` / `solacut.com.cn`）。

## 四、生产缓存与安全响应头

已内置生产级 [`vercel.json`](./vercel.json)：

- `cleanUrls: true`：无后缀干净路径；
- `/assets/*`：`Cache-Control: public, max-age=31536000, immutable`（哈希产物长期不可变缓存）；
- 全站安全响应头：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: strict-origin-when-cross-origin`、`Permissions-Policy`。
