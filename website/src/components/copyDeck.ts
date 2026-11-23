/**
 * SHA-256 / PowerShell 核验命令一键复制与无障碍反馈模块
 * 严格遵循 modern-web-guidance: copy-button & accessibility
 * - 声明式 Invoker Commands (`command="--copy"` + `commandfor`)，不支持时单路径降级 click 委托
 * - 复制中绝不禁用按钮（避免焦点丢失），同时切换图标与可见文字反馈
 * - 单一元素 `.copy-tip[role="status"]` 同时服务视觉用户与屏幕阅读器（防止双重播报），
 *   重复触发时附加 \u00A0 防止 VoiceOver 跳过
 */

interface CommandEventLike extends Event {
  readonly command: string;
  readonly source: Element | null;
}

const copyTimers = new WeakMap<HTMLButtonElement, number>();
let lastTipMessage = '';

function legacyCopy(text: string): boolean {
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.style.cssText = 'position:fixed;inset-block-start:0;opacity:0;pointer-events:none';
  document.body.append(field);
  field.select();
  try {
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    field.remove();
  }
}

export function initCopyDeck(): void {
  const report = (button: HTMLButtonElement, ok: boolean): void => {
    const prevTimer = copyTimers.get(button);
    if (prevTimer !== undefined) {
      window.clearTimeout(prevTimer);
    }

    const tip = button.nextElementSibling as HTMLElement | null;
    const labelSpan = button.querySelector<HTMLElement>('[data-copy-label]');
    const idleLabel = button.dataset.copyIdleLabel || labelSpan?.textContent?.trim() || '复制';
    if (!button.dataset.copyIdleLabel) {
      button.dataset.copyIdleLabel = idleLabel;
    }

    button.removeAttribute('data-copied');
    button.removeAttribute('data-failed');

    let message = ok ? '已复制' : '复制失败';
    if (message === lastTipMessage) {
      message += '\u00A0';
    }
    lastTipMessage = message;

    if (tip) {
      tip.textContent = '';
    }

    // 触发重排以重启动画状态
    void button.offsetWidth;
    button.setAttribute(ok ? 'data-copied' : 'data-failed', '');
    if (labelSpan) {
      labelSpan.textContent = ok ? '已复制' : '重试复制';
    }

    requestAnimationFrame(() => {
      if (tip) {
        tip.textContent = message;
      }
    });

    const timerId = window.setTimeout(() => {
      button.removeAttribute('data-copied');
      button.removeAttribute('data-failed');
      if (labelSpan) {
        labelSpan.textContent = idleLabel;
      }
      if (tip) {
        tip.textContent = '';
      }
    }, 2200);

    copyTimers.set(button, timerId);
  };

  const executeCopy = async (button: HTMLButtonElement, target: HTMLElement): Promise<void> => {
    const text = (target.dataset.copyValue ?? target.innerText).trim();
    if (!text) return;

    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(text);
      } else if (!legacyCopy(text)) {
        throw new Error('Clipboard unavailable');
      }
      report(button, true);
    } catch {
      if (legacyCopy(text)) {
        report(button, true);
      } else {
        report(button, false);
      }
    }
  };

  // 只注册一条路径，确保不会重复触发两次复制
  if ('CommandEvent' in window) {
    document.addEventListener(
      'command',
      (event) => {
        const cmdEvent = event as CommandEventLike;
        if (
          typeof cmdEvent.command === 'string' &&
          cmdEvent.command.startsWith('--copy') &&
          cmdEvent.source instanceof HTMLButtonElement &&
          cmdEvent.target instanceof HTMLElement
        ) {
          void executeCopy(cmdEvent.source, cmdEvent.target);
        }
      },
      true,
    );
  } else {
    document.addEventListener('click', (event) => {
      const targetEl = event.target instanceof Element ? event.target : null;
      const button = targetEl?.closest<HTMLButtonElement>('button[command^="--copy"]');
      if (!button) return;
      const targetId = button.getAttribute('commandfor');
      if (!targetId) return;
      const copyTarget = document.getElementById(targetId);
      if (copyTarget) {
        void executeCopy(button, copyTarget);
      }
    });
  }
}
