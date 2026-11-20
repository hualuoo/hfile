/* kkFileView (inventory 7.4): files of the kind kkfileview, and "kkFileView 预览" in the menus for any
 * single file with the 预览 permission. Only while 显示设置 has a kkFileView 服务地址; without one a click
 * downloads the file (7.4 change). The server makes the address (POST /api/v1/preview/kkfileview/ with
 * the viewer's signed URL): <kkFileView>/onlinePreview?url=<base64 of an absolute signed URL of this site
 * + fullfilename=<random>.<suffix>>, which the kkFileView server downloads. It opens in a dialog with a
 * frame (内嵌; the page's CSP lets only kkFileView's origin be framed) or in a new tab (新标签页). */
(function () {
  'use strict';

  const P = App.preview;
  const { h } = App;
  const ACTION_ICONS = { kkfileview: 'preview' }; // the menus draw it with App.icon()

  // A link in a new tab that doesn't get this page as its opener, from a click (a snackbar's 打开).
  function openTab(url) {
    const link = h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', class: 'sr-only' });
    document.body.append(link);
    link.click();
    link.remove();
  }

  async function show(ctx, item) {
    const newTab = P.kk(ctx).mode === 'new_tab';
    // A tab opened by the click itself (popup blockers allow that), pointed at kkFileView once the
    // address is known; it doesn't get this page as its opener.
    const tab = newTab ? window.open('', '_blank') : null;
    if (tab) tab.opener = null;
    let box = null;
    let frameView = null;
    if (!newTab) {
      box = h('div', { class: 'kk-box' });
      frameView = P.frame({ title: item.name, className: 'preview-kk', body: box, actions: [P.downloadAction(ctx, item)] });
      P.message(box, { busy: true, text: gettext('Loading the preview, please wait…') });
    }
    try {
      const inline = await ctx.inlineUrl(item);
      const data = await App.api('POST', P.config.urls.kkfileview, { url: App.pageUrl(inline) });
      // On this site's own origin kkFileView would show stored files as this site (the server refuses it too).
      if (new URL(data.url, location.href).origin === location.origin) throw new Error(gettext("kkFileView can't be at the same address as the site."));
      if (newTab) {
        if (tab && !tab.closed) tab.location.replace(data.url);
        else App.snackbar(gettext('The kkFileView preview is ready (the browser blocked the new tab).'), { action: { label: gettext('Open'), onClick: () => openTab(data.url) } });
        return;
      }
      if (!frameView.dialog.open) return;
      const loading = h('div', { class: 'kk-loading' });
      P.message(loading, { busy: true, text: gettext('Loading the preview, please wait…') });
      const iframe = h('iframe', {
        class: 'kk-frame', src: data.url, title: interpolate(gettext('kkFileView preview: %(name)s'), { name: item.name }, true), referrerpolicy: 'no-referrer',
        // kkFileView is another site: it may run its scripts in its own origin, nothing on this page.
        sandbox: 'allow-scripts allow-same-origin allow-popups allow-forms allow-downloads allow-modals',
      });
      iframe.addEventListener('load', () => loading.remove());
      box.replaceChildren(iframe, loading);
    } catch (err) {
      if (tab) tab.close();
      const text = interpolate(gettext('Loading the preview failed: %(message)s'), { message: err.message }, true);
      if (box) P.message(box, { icon: 'error', text, actions: [P.downloadButton(ctx, item)].filter(Boolean) });
      else App.toast(text, 'error');
    }
  }

  function open(ctx, item) {
    if (!P.kk(ctx).enabled) return P.download(ctx, item, { confirm: true });
    return show(ctx, item);
  }

  P.register(['kkfileview'], { open }, 'kkfileview');
  P.action({
    // After files' 预览 (30) and before 下载 (50).
    key: 'kkfileview', label: gettext('kkFileView preview'), icon: ACTION_ICONS.kkfileview, group: 1, order: 40,
    when: (ctx, items) => P.kk(ctx).enabled && items.length === 1 && P.isFile(items[0]) && P.can(ctx, 'preview'),
    run: (ctx, items) => show(ctx, items[0]),
  });
})();
