/**
 * 暗房原生 <dialog> 交付与合规声明弹窗
 * 在 RELEASE_PUBLISHED / REPO_PUBLISHED 尚未上线前，接管未公开链接防止用户遭遇 404 / NXDOMAIN，
 * 以透明、克制、工程级的方式呈现便携包构建说明、本地启动命令与语料版权明细。
 */

import { RELEASE_PUBLISHED, REPO_PUBLISHED } from '../config/site';

function wireDialogDismiss(dialog: HTMLDialogElement): void {
  dialog.querySelectorAll<HTMLButtonElement>('[data-dialog-close]').forEach((btn) => {
    btn.addEventListener('click', () => dialog.close());
  });

  // 点击对话框外部遮罩关闭
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) {
      dialog.close();
    }
  });
}

export function initInfoDialogs(): void {
  const deliveryDialog = document.getElementById('beta-delivery-dialog') as HTMLDialogElement | null;
  const noticesDialog = document.getElementById('legal-notices-dialog') as HTMLDialogElement | null;

  if (deliveryDialog) wireDialogDismiss(deliveryDialog);
  if (noticesDialog) wireDialogDismiss(noticesDialog);

  const openDialog = (dialog: HTMLDialogElement | null): void => {
    if (!dialog || dialog.open) return;
    dialog.showModal();
  };

  // 预发态接管尚未上传资产的下载按钮，避免跳出 404 / NXDOMAIN
  if (!RELEASE_PUBLISHED && deliveryDialog) {
    document
      .querySelectorAll<HTMLAnchorElement>('[data-site-download-github], [data-site-download-cn]')
      .forEach((link) => {
        link.addEventListener('click', (event) => {
          event.preventDefault();
          openDialog(deliveryDialog);
        });
      });
  }

  // 仓库未公开前，点击署名/协议打开内嵌透明声明弹窗；点击仓库/Issues 打开公测交付弹窗
  if (!REPO_PUBLISHED) {
    if (noticesDialog) {
      document
        .querySelectorAll<HTMLAnchorElement>('[data-site-notices], [data-site-license]')
        .forEach((link) => {
          link.addEventListener('click', (event) => {
            event.preventDefault();
            openDialog(noticesDialog);
          });
        });
    }

    if (deliveryDialog) {
      document
        .querySelectorAll<HTMLAnchorElement>('[data-site-repo], [data-site-issues]')
        .forEach((link) => {
          link.addEventListener('click', (event) => {
            event.preventDefault();
            openDialog(deliveryDialog);
          });
        });
    }
  }
}
