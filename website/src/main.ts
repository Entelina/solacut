import './styles/tokens.css';
import './styles/base.css';
import './styles/layout.css';
import './styles/nle.css';
import './styles/tabs.css';

import {
  BRAND_NAME,
  DOMAIN,
  DOWNLOAD_LINKS,
  ISSUES_URL,
  LICENSE_URL,
  NOTICES_URL,
  RELEASE_DATE,
  RELEASE_PUBLISHED,
  REPO_URL,
  VERIFY_COMMAND,
  VERSION,
  VERSION_SHORT,
} from './config/site';
import { initCopyDeck } from './components/copyDeck';
import { initInfoDialogs } from './components/infoDialogs';
import { initNavbar } from './components/navbar';
import { initNleWorkbench } from './components/nleWorkbench';
import { initTranslationTabs } from './components/translationTabs';

/** 将集中式发版配置 site.ts 同步到所有 DOM 占位点，保证修改一处即更新全站 */
function hydrateSiteConfig(): void {
  const setText = (selector: string, value: string): void => {
    document.querySelectorAll<HTMLElement>(selector).forEach((el) => {
      el.textContent = value;
    });
  };

  const setHref = (selector: string, url: string): void => {
    document.querySelectorAll<HTMLAnchorElement>(selector).forEach((el) => {
      el.href = url;
    });
  };

  setText('[data-site-brand]', BRAND_NAME);
  setText('[data-site-domain]', DOMAIN);
  setText('[data-site-version]', VERSION);
  setText('[data-site-version-short]', VERSION_SHORT);
  setText('[data-site-release-date]', RELEASE_DATE);
  setText('[data-site-file-name]', DOWNLOAD_LINKS.fileName);
  setText('[data-site-file-size]', DOWNLOAD_LINKS.fileSize);
  setText('[data-site-platform]', DOWNLOAD_LINKS.platform);
  const verifyCmdEl = document.getElementById('verify-cmd-code');
  if (verifyCmdEl) {
    verifyCmdEl.dataset.copyValue = VERIFY_COMMAND;
  }

  setHref('[data-site-download-github]', DOWNLOAD_LINKS.githubReleases);
  setHref('[data-site-download-cn]', DOWNLOAD_LINKS.cnMirror);
  setHref('[data-site-repo]', REPO_URL);
  setHref('[data-site-notices]', NOTICES_URL);
  setHref('[data-site-license]', LICENSE_URL);
  setHref('[data-site-issues]', ISSUES_URL);

  // 根据 RELEASE_PUBLISHED 精确同步校验块与发版状态提示，杜绝虚假/空文件哈希
  const hasRealHash = RELEASE_PUBLISHED && /^[a-f0-9]{64}$/i.test(DOWNLOAD_LINKS.checksumSha256);
  const shaCodeEl = document.getElementById('sha256-code');
  const copyBtn = document.querySelector<HTMLButtonElement>('[data-site-copy-btn]');
  const copyLabelEl = copyBtn?.querySelector<HTMLElement>('[data-copy-label]');
  const channelDot = document.querySelector<HTMLElement>('[data-site-channel-dot]');
  const channelText = document.querySelector<HTMLElement>('[data-site-channel-text]');

  if (shaCodeEl) {
    if (hasRealHash) {
      shaCodeEl.textContent = DOWNLOAD_LINKS.checksumSha256;
      shaCodeEl.removeAttribute('data-pending');
    } else {
      shaCodeEl.textContent = 'PENDING-RELEASE · 正式便携包封板后自动固化真实 64 位 SHA-256';
      shaCodeEl.setAttribute('data-pending', '');
    }
  }

  if (copyBtn && copyLabelEl) {
    if (hasRealHash) {
      copyBtn.setAttribute('commandfor', 'sha256-code');
      copyBtn.dataset.copyIdleLabel = '复制 SHA-256';
      copyLabelEl.textContent = '复制 SHA-256';
    } else {
      copyBtn.setAttribute('commandfor', 'verify-cmd-code');
      copyBtn.dataset.copyIdleLabel = '复制核验命令';
      copyLabelEl.textContent = '复制核验命令';
    }
  }

  if (channelDot && channelText) {
    if (RELEASE_PUBLISHED) {
      channelDot.classList.add('deck-channel-dot--live');
      channelText.textContent = '正式公测分流节点在线 · 点击直接下载便携包';
    } else {
      channelDot.classList.remove('deck-channel-dot--live');
      channelText.textContent = '公测预发封板中 · 点击按钮查看便携包交付与本地运行指引（零 404）';
    }
  }
}

/** 集中式非视觉状态播报器（页面唯一 polite 区域 + VoiceOver 防吞字处理） */
function createAnnouncer(): (message: string) => void {
  const region = document.getElementById('sr-announcer');
  let lastAnnouncement = '';
  let clearTimer: number | undefined;

  return (message: string): void => {
    if (!region || !message) return;
    if (clearTimer !== undefined) {
      window.clearTimeout(clearTimer);
    }

    let payload = message;
    if (payload === lastAnnouncement) {
      payload += '\u00A0';
    }
    lastAnnouncement = payload;

    region.textContent = '';
    requestAnimationFrame(() => {
      region.textContent = payload;
    });

    clearTimer = window.setTimeout(() => {
      region.textContent = '';
    }, 3500);
  };
}

function bootstrap(): void {
  hydrateSiteConfig();
  const announce = createAnnouncer();
  initNavbar();
  initNleWorkbench(announce);
  initTranslationTabs();
  initCopyDeck();
  initInfoDialogs();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
} else {
  bootstrap();
}
