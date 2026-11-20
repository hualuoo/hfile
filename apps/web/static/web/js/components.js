/* /components/ (DEBUG): makes the demos of apps/web/templates/web/_components.html do something. */
(function () {
  'use strict';
  const { snackbar, confirmDialog, tabs, spinner } = window.App;

  document.querySelectorAll('[data-demo-tabs]').forEach((tablist) => tabs(tablist));
  document.querySelectorAll('[data-demo-spinners]').forEach((row) => {
    row.append(spinner(), spinner({ small: true }), spinner({ value: 0.7, label: '70%' }));
  });
  document.addEventListener('click', async (e) => {
    const toggle = e.target.closest('[data-demo-toggle]');
    if (toggle) toggle.setAttribute('aria-pressed', String(toggle.getAttribute('aria-pressed') !== 'true'));
    const segment = e.target.closest('[data-demo-segment]');
    if (segment) {
      segment.parentElement.querySelectorAll('[data-demo-segment]').forEach((other) => {
        other.setAttribute('aria-pressed', String(other === segment));
      });
    }
    const say = e.target.closest('[data-demo-snackbar]');
    if (say) {
      const kind = say.dataset.demoSnackbar;
      if (kind === 'error') snackbar('连不上服务器，请稍后再试。', { kind: 'error' });
      else if (kind === 'action') snackbar('已删除 3 个文件。', { action: { label: '撤销', onClick: () => snackbar('已撤销。') } });
      else snackbar('链接已经复制。');
    }
    if (e.target.closest('[data-demo-confirm]')) {
      const ok = await confirmDialog({ title: '删除这 3 个文件？', message: '删除以后不能恢复。', confirmText: '删除', danger: true });
      snackbar(ok ? '点了删除。' : '取消了。');
    }
  });
})();
