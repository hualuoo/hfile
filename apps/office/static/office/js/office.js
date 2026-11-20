/* OnlyOffice on the file browser and the share pages (office app; apps.files.pages.add_assets loads it after
 * files.js). It registers, through the browser's registries (contract W4):
 *
 *   App.files.previewers  the kind office: opening such a file. With 显示设置's OnlyOffice 服务地址
 *                         (ctx.site.onlyoffice_url) it opens the editor page in a new tab; without one
 *                         it falls back to kkFileView (the preview app's viewer, which itself downloads
 *                         when kkFileView isn't set either), else the download (7.3 change).
 *   App.files.actions     "用 OnlyOffice 打开" for a single Office file, while the address is set.
 *
 * The editor needs a page of its own, /onlyoffice/edit/?storage=<key>&path=<path> (in a share
 * ?share=<key>&path=<path>, read only): the Document Server's api.js runs there, under a CSP that adds
 * its origin, and the browser's pages keep script-src 'self'. The tab is opened from the click itself,
 * so that popup blockers let it through; when one doesn't, a snackbar offers to open it. */
(function () {
  'use strict';

  const EDITOR = '/onlyoffice/edit/';

  const site = (ctx) => (ctx && ctx.site) || {};
  const enabled = (ctx) => Boolean(site(ctx).onlyoffice_url);
  const isFile = (item) => Boolean(item) && !(item.is_dir || item.type === 'folder' || item.type === 'dir');

  function can(ctx, code) {
    const granted = ctx && ctx.permissions;
    if (!granted) return false;
    if (Array.isArray(granted)) return granted.includes(code);
    if (granted instanceof Set) return granted.has(code);
    return Boolean(granted[code]);
  }

  // The item's path relative to the root (inside a share: inside the share), with one leading "/".
  function pathOf(ctx, item) {
    const path = item.path || `${String((ctx && ctx.path) || '').replace(/\/+$/, '')}/${item.name}`;
    return `/${String(path).replace(/^\/+/, '')}`;
  }

  // The share being shown: what the share page gave the browser, else its address (/share/<key>...).
  function shareKey(ctx) {
    const options = (ctx && ctx.browser && ctx.browser.options) || {};
    const given = (ctx && ctx.share && ctx.share.key) || (options.share && options.share.key) || options.shareKey;
    if (given) return String(given);
    const match = /^\/share\/([^/]+)/.exec(location.pathname);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function editorHref(ctx, item) {
    const share = ctx && ctx.mode === 'share';
    const params = share ? { share: shareKey(ctx), path: pathOf(ctx, item) }
      : { storage: (ctx.storage && ctx.storage.key) || '', path: pathOf(ctx, item) };
    return `${EDITOR}?${new URLSearchParams(params).toString()}`;
  }

  function openLink(href) {
    const link = App.h('a', { href, target: '_blank', rel: 'noopener', class: 'sr-only' });
    document.body.append(link);
    link.click();
    link.remove();
  }

  function openEditor(ctx, item) {
    const href = editorHref(ctx, item);
    const tab = window.open(href, '_blank');
    if (tab) {
      try {
        tab.opener = null; // the editor page (and the Document Server's script on it) doesn't get this one
      } catch (e) {
        // a browser that doesn't allow it: the page is this site's own anyway
      }
      return;
    }
    App.snackbar(interpolate(gettext('"%(name)s" opens in a new tab, and the browser blocked it.'), { name: item.name }, true), { action: { label: gettext('Open'), onClick: () => openLink(href) } });
  }

  function download(ctx, item) {
    if (window.App.preview && typeof window.App.preview.download === 'function') {
      return window.App.preview.download(ctx, item, { confirm: true });
    }
    if (typeof ctx.download === 'function') return ctx.download(item);
    if (ctx.browser && typeof ctx.browser.download === 'function') return ctx.browser.download(item);
    return null;
  }

  // Without OnlyOffice: kkFileView when the preview app has it (it downloads when kkFileView isn't set).
  function fallback(ctx, item) {
    const viewers = window.App.preview && window.App.preview.viewers;
    const kk = viewers && viewers.kkfileview;
    if (kk && typeof kk.open === 'function') return kk.open(ctx, item);
    return download(ctx, item);
  }

  const viewer = {
    open(ctx, item) {
      return enabled(ctx) ? openEditor(ctx, item) : fallback(ctx, item);
    },
  };

  const action = {
    key: 'onlyoffice',
    label: gettext('Open with OnlyOffice'),
    icon: 'open_in_new',
    group: 1,
    order: 32,
    when: (ctx, items) => enabled(ctx) && items.length === 1 && isFile(items[0]) && items[0].kind === 'office'
      && !items[0].locked && (can(ctx, 'preview') || can(ctx, 'download')),
    run: (ctx, items) => openEditor(ctx, items[0]),
  };

  // files.js runs first on these pages; should it come later, this attaches once it is there.
  function attach() {
    const files = window.App && window.App.files;
    if (!files || !files.previewers || !files.actions || attach.done) return;
    attach.done = true;
    files.previewers.register('office', viewer);
    files.actions.register(action);
  }
  attach();
  document.addEventListener('DOMContentLoaded', attach);
  window.addEventListener('load', attach);
  document.addEventListener('hfile:files-ready', attach);

  window.App.office = { editorHref, open: (ctx, item) => viewer.open(ctx, item) };
})();
