/**
 * 顶部导航栏 & 移动端原生 <dialog> 滑动导航抽屉
 * 遵循 modern-web-guidance: navigation-drawer & accessibility
 */

export function initNavbar(): void {
  const drawer = document.getElementById('mobile-drawer') as HTMLDialogElement | null;
  const openBtn = document.getElementById('mobile-menu-open') as HTMLButtonElement | null;
  const closeBtn = document.getElementById('mobile-menu-close') as HTMLButtonElement | null;
  if (!drawer || !openBtn || !closeBtn) return;

  const scroller = drawer.querySelector<HTMLElement>('.mobile-drawer-scroller');
  const sheet = drawer.querySelector<HTMLElement>('.mobile-drawer-sheet');
  if (!scroller || !sheet) return;

  const supportsScrollTimeline = CSS.supports('animation-timeline: scroll()');
  const supportsInitialTarget = CSS.supports('scroll-initial-target', 'nearest');

  let isClosing = false;

  const updateBackdropFallback = (): void => {
    const sheetWidth = sheet.offsetWidth || 1;
    const ratio = Math.max(0, Math.min(1, scroller.scrollLeft / sheetWidth));
    drawer.style.setProperty('--drawer-backdrop', ratio.toFixed(3));
  };

  if (!supportsScrollTimeline) {
    scroller.addEventListener('scroll', updateBackdropFallback, { passive: true });
  }

  const openDrawer = async (): Promise<void> => {
    if (drawer.open) return;
    isClosing = false;
    drawer.showModal();
    openBtn.setAttribute('aria-expanded', 'true');

    if (!supportsInitialTarget) {
      scroller.scrollTo({ left: 0, behavior: 'instant' as ScrollBehavior });
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      });
    }

    if (!supportsScrollTimeline) {
      updateBackdropFallback();
    }

    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    scroller.scrollTo({
      left: sheet.offsetWidth,
      behavior: prefersReduced ? ('instant' as ScrollBehavior) : 'auto',
    });
  };

  const closeDrawer = (): void => {
    if (!drawer.open || isClosing) return;
    isClosing = true;
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (prefersReduced) {
      drawer.close();
      openBtn.setAttribute('aria-expanded', 'false');
      isClosing = false;
      return;
    }
    scroller.scrollTo({ left: 0, behavior: 'auto' });
  };

  const visibleThreshold = Math.max(0.01, 1 / Math.max(window.innerWidth, 320));
  const observer = new IntersectionObserver(
    (entries) => {
      if (!drawer.open) return;
      const entry = entries.at(-1);
      if (!entry) return;
      if (entry.intersectionRatio < visibleThreshold) {
        drawer.close();
        openBtn.setAttribute('aria-expanded', 'false');
        isClosing = false;
      } else if (entry.intersectionRatio >= 0.98) {
        openBtn.setAttribute('aria-expanded', 'true');
        isClosing = false;
      }
    },
    { root: drawer, threshold: [visibleThreshold, 0.98] },
  );
  observer.observe(sheet);

  openBtn.addEventListener('click', () => {
    void openDrawer();
  });

  closeBtn.addEventListener('click', closeDrawer);

  // 点击抽屉遮罩空白处轻关闭
  drawer.addEventListener('click', (event) => {
    if (event.target instanceof Node && !sheet.contains(event.target)) {
      closeDrawer();
    }
  });

  // 拦截 Escape 的瞬间关闭，改走平滑收起
  drawer.addEventListener('cancel', (event) => {
    if (event.cancelable) {
      event.preventDefault();
      closeDrawer();
    }
  });

  // 点击抽屉内导航链接时关闭抽屉
  const drawerLinks = sheet.querySelectorAll<HTMLAnchorElement>('a[href]');
  drawerLinks.forEach((link) => {
    link.addEventListener('click', () => {
      if (drawer.open) {
        drawer.close();
        openBtn.setAttribute('aria-expanded', 'false');
        isClosing = false;
      }
    });
  });

  // 窗口拉宽至桌面端时自动关闭移动端抽屉
  const wideQuery = window.matchMedia('(min-width: 861px)');
  wideQuery.addEventListener('change', (e) => {
    if (e.matches && drawer.open) {
      drawer.close();
      openBtn.setAttribute('aria-expanded', 'false');
      isClosing = false;
    }
  });
}
