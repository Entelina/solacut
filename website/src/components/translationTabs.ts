/**
 * 六语种 × 60 万语料实时对比台
 * 严格遵循 modern-web-guidance: tabs & search-hidden-content
 * - role="tablist" + <button type="button" role="tab" aria-selected>（不加 aria-controls）
 * - ArrowLeft / ArrowRight / Home / End 键盘导航（selection follows focus）+ roving tabindex
 * - 面板使用 hidden="until-found" + beforematch 事件同步激活（浏览器 Ctrl+F 搜索隐藏语种自动切过去）
 * - scrollIntoView({ block: 'nearest', inline: 'nearest' }) 保持选中 Tab 处于可视区
 * - 不支持 animation-timeline: scroll(self inline) 时以 IntersectionObserver 驱动左右渐隐遮罩
 */

import { TRANSLATION_CASES } from '../data/translationCases';

export function initTranslationTabs(): void {
  const deck = document.getElementById('translation-deck');
  if (!deck) return;

  const tablist = deck.querySelector<HTMLElement>('[role="tablist"]');
  if (!tablist) return;

  const tabs = Array.from(tablist.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
  const panels = Array.from(deck.querySelectorAll<HTMLElement>('[role="tabpanel"]'));
  if (tabs.length === 0 || tabs.length !== panels.length) return;

  // 将结构化语料配置与静态渲染面板 ID 对齐绑定，方便扩展脚本或测试定位
  TRANSLATION_CASES.forEach((item, idx) => {
    const panel = panels[idx];
    if (panel) {
      panel.dataset.langPair = item.id;
    }
  });

  const activateIndex = (targetIndex: number, moveFocus: boolean): void => {
    const normalized = ((targetIndex % tabs.length) + tabs.length) % tabs.length;

    tabs.forEach((tab, idx) => {
      const selected = idx === normalized;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });

    panels.forEach((panel, idx) => {
      const selected = idx === normalized;
      if (selected) {
        panel.removeAttribute('hidden');
        panel.tabIndex = 0;
      } else {
        // 设置 until-found：支持浏览器支持页内 Ctrl+F 搜索，不支持的浏览器自动按原生 boolean hidden 隐藏
        panel.setAttribute('hidden', 'until-found');
        panel.removeAttribute('tabindex');
      }
    });

    const selectedTab = tabs[normalized];
    if (selectedTab) {
      if (moveFocus) {
        selectedTab.focus();
      }
      // 仅水平滚入 tablist 容器，避免引起整页垂直跳动
      const tabLeft = selectedTab.offsetLeft;
      const tabRight = tabLeft + selectedTab.offsetWidth;
      const viewLeft = tablist.scrollLeft;
      const viewRight = viewLeft + tablist.clientWidth;
      const pad = 40;
      if (tabLeft - pad < viewLeft) {
        tablist.scrollTo({ left: Math.max(0, tabLeft - pad), behavior: 'smooth' });
      } else if (tabRight + pad > viewRight) {
        tablist.scrollTo({ left: tabRight + pad - tablist.clientWidth, behavior: 'smooth' });
      }
    }
  };

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => {
      activateIndex(index, false);
    });

    tab.addEventListener('keydown', (event) => {
      switch (event.key) {
        case 'ArrowRight':
          event.preventDefault();
          activateIndex(index + 1, true);
          break;
        case 'ArrowLeft':
          event.preventDefault();
          activateIndex(index - 1, true);
          break;
        case 'Home':
          event.preventDefault();
          activateIndex(0, true);
          break;
        case 'End':
          event.preventDefault();
          activateIndex(tabs.length - 1, true);
          break;
        default:
          break;
      }
    });
  });

  // 监听 beforematch：浏览器 Ctrl+F 命中隐藏面板文本时自动同步切换对应语种 Tab
  deck.addEventListener('beforematch', (event) => {
    const matchedPanel =
      event.target instanceof HTMLElement ? event.target.closest<HTMLElement>('[role="tabpanel"]') : null;
    if (!matchedPanel) return;
    const panelIndex = panels.indexOf(matchedPanel);
    if (panelIndex !== -1) {
      activateIndex(panelIndex, false);
    }
  });

  // 横向滚动边缘渐隐回退：仅当浏览器不支持 scroll-driven animations 时挂载 IntersectionObserver
  if (!CSS.supports('animation-timeline', 'scroll(self inline)')) {
    const firstTab = tabs[0];
    const lastTab = tabs[tabs.length - 1];
    if (firstTab && lastTab) {
      const fadeObserver = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (entry.target === firstTab) {
              tablist.classList.toggle('more-start', entry.intersectionRatio < 1);
            }
            if (entry.target === lastTab) {
              tablist.classList.toggle('more-end', entry.intersectionRatio < 1);
            }
          }
        },
        { root: tablist, threshold: 1, rootMargin: '1px' },
      );
      fadeObserver.observe(firstTab);
      fadeObserver.observe(lastTab);
    }
  }
}
