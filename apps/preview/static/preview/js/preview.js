/* The file viewers (preview app), on the file browser and the share pages. They register with the
 * browser's registries (contract W4 of briefs/wave2-files.md): App.files.previewers.register(kinds,
 * {open(ctx, item, siblings)}) for what opening a file of a kind (W3) does, and
 * App.files.actions.register({...}) for "kkFileView 预览" in the menus. They use nothing but
 *
 *   ctx       mode ('browse' or 'share'), storage {key, name, type}, path (the folder), permissions
 *             ({code: true}), prefs (the browser's: previewMode 'full' or 'only' = 双击预览模式), site (the
 *             site's public settings: kkFileView, the text limit, 下载前先确认), toast(), downloadUrl(item),
 *             inlineUrl(item, {fresh}) (a Promise of the item's signed same-origin URL: the same one while it
 *             lasts, a new one a minute before it ends, or with fresh: true after it failed)
 *   item      name, size, modified, kind, url (its download URL)
 *   siblings  the items of its folder in the browser's order (playlists, subtitles, m3u8 segments)
 *
 * item.url goes into <img>, <video> and <audio>; what is read with JS (text, Markdown, PDF, playlists,
 * subtitles) comes from ctx.inlineUrl(item), so no storage needs CORS for a preview. Names and text go in
 * as text (App.h); the only HTML is the server's Markdown (apps.core.markdown) and highlight.js's spans, both
 * parsed in a document of their own (inert()) and checked first. Shares get the same viewers, read only.
 *
 * The pages load these scripts through apps.files.pages.add_assets (apps/preview/apps.py). App.preview is what the
 * viewer scripts (image.js, video.js, ...) share:
 *   register(kinds, viewer), action(spec)   to App.files once it is there (now, or when the page has loaded)
 *   viewers                 the viewers by name (markdown.js opens text.js for 查看源文件)
 *   config                  where the vendored libraries and the endpoints are
 *   limits(ctx), kk(ctx)    the size guards and kkFileView, from the site's settings (ctx.site)
 *   expiresAt(url)          when a signed link ends (its e=), in ms; Infinity when it doesn't say
 *   frame({...})            a viewer's dialog: {dialog, body, head, close(), setTitle()}
 *   can(ctx, permission), isShare(ctx), isFile(item), ofKind(siblings, kinds), pathOf(ctx, item),
 *   suffix(name), sameOrigin(url), absolute(url)
 *   download(ctx, item, {confirm})          the file's download (下载前先确认 when confirm)
 *   lib(name)               a vendored library, loaded on first use: 'hls', 'hljs', 'pdf'
 *   fetchBytes(url, {limit, onProgress}), TooLarge, decodeText(bytes, encoding), guessEncoding(bytes)
 *   inert(html) -> a <body> of a document of its own, adopt(body) -> its nodes for the page
 *   highlight(hljs, text, language)         highlight.js's result as checked nodes, or null
 *   message(box, {icon, text, actions}), tooLarge(ctx, item, limit), store.get(key, fallback)/set(key, value)
 */
(function () {
  'use strict';

  const { h } = App;

  // The vendored libraries sit next to this script under /static/, in folders named with their versions
  // (a new version is a new address): /static/preview/js/preview.js -> /static/preview/vendor/....
  const script = document.currentScript;
  const STATIC = script && script.src ? new URL('../../', script.src).pathname : '/static/';
  const VENDOR = `${STATIC}preview/vendor/`;
  const HLS = `${VENDOR}hls.js-1.7.3/`;
  const PDF = `${VENDOR}pdf.js-6.4.299/`;
  const HIGHLIGHT = `${VENDOR}highlight.js-11.12.0/`;
  const config = {
    libs: {
      hls: `${HLS}hls.min.js`,
      hlsWorker: `${HLS}hls.worker.js`,
      pdf: `${PDF}pdf.min.mjs`,
      pdfWorker: `${PDF}pdf.worker.min.mjs`,
      pdfAssets: PDF, // cMaps, standard fonts, wasm decoders: pdf.js asks for files by name under it
      hljs: `${HIGHLIGHT}highlight.min.js`, // the 36 common languages; these few register after it:
      hljsLanguages: ['dos', 'properties', 'dockerfile', 'nginx', 'powershell'].map((name) => `${HIGHLIGHT}languages/${name}.min.js`),
    },
    urls: {
      markdown: '/api/v1/preview/markdown/',
      kkfileview: '/api/v1/preview/kkfileview/',
      save: '/api/v1/uploads/text/', // the uploads app's text save
    },
  };

  // The size guards (7.Z.8): previewed up to the site's 文本预览的最大文件, edited up to 2 MB; Markdown is
  // rendered on the server up to 2 MB of it (apps/preview/content.py MARKDOWN_MAX).
  const EDIT_MAX = 2 * 1024 * 1024;
  const MARKDOWN_MAX = 2 * 1024 * 1024;
  function limits(ctx) {
    const kb = Number(ctx && ctx.site && ctx.site.text_preview_max_kb) || 512;
    const text = kb * 1024;
    return { text, edit: EDIT_MAX, markdown: Math.min(text, MARKDOWN_MAX) };
  }
  // kkFileView: on while 显示设置 has its address (the server builds the addresses).
  function kk(ctx) {
    const site = (ctx && ctx.site) || {};
    return { enabled: Boolean(site.kkfileview_url), mode: site.kkfileview_open_mode === 'new_tab' ? 'new_tab' : 'iframe' };
  }
  // When a signed link ends: files' /pd/ links say it in e= (Unix seconds).
  function expiresAt(url) {
    try {
      const e = Number(new URL(url, location.href).searchParams.get('e'));
      return e > 0 ? e * 1000 : Infinity;
    } catch (err) {
      return Infinity;
    }
  }

  // ------------------------------------------------------------------ the browser's registries

  const pending = { previewers: [], actions: [] };
  const viewers = {};

  function attach() {
    const files = window.App && window.App.files;
    if (!files || !files.previewers || !files.actions) return false;
    for (const [kinds, viewer] of pending.previewers.splice(0)) files.previewers.register(kinds, viewer);
    for (const spec of pending.actions.splice(0)) files.actions.register(spec);
    return true;
  }
  function register(kinds, viewer, name) {
    if (name) viewers[name] = viewer;
    pending.previewers.push([kinds, viewer]);
    attach();
  }
  function action(spec) {
    pending.actions.push(spec);
    attach();
  }
  // The browser's script may come after these: they attach when it is there.
  document.addEventListener('DOMContentLoaded', attach);
  window.addEventListener('load', attach);
  document.addEventListener('hfile:files-ready', attach);

  // ------------------------------------------------------------------ what ctx and items say

  function can(ctx, code) {
    const granted = ctx && ctx.permissions;
    if (!granted) return false;
    if (Array.isArray(granted)) return granted.includes(code);
    if (granted instanceof Set) return granted.has(code);
    return Boolean(granted[code]);
  }
  const isShare = (ctx) => Boolean(ctx && ctx.mode === 'share');
  const isFile = (item) => Boolean(item) && !(item.is_dir || item.type === 'folder' || item.type === 'dir');
  const ofKind = (siblings, kinds) => (siblings || []).filter((item) => isFile(item) && kinds.includes(item.kind));
  // The file's path relative to the root, always with one leading "/" ("docs/a.md" and "/docs/a.md" alike).
  function pathOf(ctx, item) {
    const path = item.path || `${String((ctx && ctx.path) || '').replace(/\/+$/, '')}/${item.name}`;
    return `/${path.replace(/^\/+/, '')}`;
  }
  function suffix(name) {
    const base = String(name || '').split('/').pop();
    const dot = base.lastIndexOf('.');
    return dot < 0 ? '' : base.slice(dot + 1).toLowerCase();
  }
  function sameOrigin(url) {
    try {
      return new URL(url, location.href).origin === location.origin;
    } catch (e) {
      return false;
    }
  }
  const absolute = (url) => new URL(url, location.href).href;
  const sameItem = (a, b) => a === b || (a && b && a.name === b.name && (a.path || '') === (b.path || ''));

  // ------------------------------------------------------------------ storage of small choices

  const store = {
    get(key, fallback) {
      try {
        const value = localStorage.getItem(`hfile-preview-${key}`);
        return value === null ? fallback : JSON.parse(value);
      } catch (e) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(`hfile-preview-${key}`, JSON.stringify(value));
      } catch (e) {
        // not kept: it lasts for this page
      }
    },
  };

  // ------------------------------------------------------------------ the dialog of a viewer

  let frameSeq = 0;
  // A viewer in an M3 dialog, full screen on phones: the file's name as its title, the viewer's own
  // buttons, 关闭. onClose runs once it is closed (stop the video, free the PDF).
  function frame({ title, className = '', actions = [], body = null, onClose = null, label = '' }) {
    const titleId = `preview-title-${++frameSeq}`;
    const heading = h('h2', { id: titleId, text: title });
    const head = h('header', { class: 'dialog-head preview-head' },
      heading,
      h('div', { class: 'preview-actions' }, ...actions),
      h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, App.icon('close')));
    const content = h('div', { class: 'preview-body' }, body);
    const dialog = h('dialog', {
      class: `dialog dialog-full preview-dialog ${className}`.trim(),
      'aria-labelledby': titleId,
      'aria-description': label || null,
    }, h('div', { class: 'dialog-box' }, head, content));
    dialog.addEventListener('close', () => {
      dialog.remove();
      if (onClose) onClose();
    });
    document.body.append(dialog);
    App.openDialog(dialog);
    return {
      dialog,
      head,
      body: content,
      close: () => { if (dialog.open) dialog.close(); },
      setTitle(text) { heading.textContent = text; },
    };
  }

  // An icon button for a viewer's head or toolbar: button(App.icon('zoom_in'), '放大', onClick).
  function button(iconNode, label, onClick, extra = {}) {
    return h('button', { type: 'button', class: 'icon-button', 'aria-label': label || null, 'data-tooltip': label || null, onclick: onClick, ...extra },
      iconNode);
  }

  // A state in a viewer's body: a spinner, an error with what one can do instead.
  function message(box, { icon = null, text = '', detail = '', actions = [], busy = false }) {
    box.replaceChildren(h('div', { class: 'preview-message', role: busy ? 'status' : 'alert' },
      busy ? App.spinner({ label: text || gettext('Loading') }) : (icon ? App.icon(icon) : null),
      text ? h('p', { class: 'preview-message-text', text }) : null,
      detail ? h('p', { class: 'preview-message-detail muted', text: detail }) : null,
      actions.length ? h('div', { class: 'btn-row' }, ...actions) : null));
  }

  function downloadButton(ctx, item, label = gettext('Download')) {
    if (!can(ctx, 'download') || !item.url) return null;
    return h('button', { type: 'button', class: 'btn btn-tonal', onclick: () => download(ctx, item) }, App.icon('download'), label);
  }
  function downloadAction(ctx, item) {
    if (!can(ctx, 'download') || !item.url) return null;
    return button(App.icon('download'), gettext('Download'), () => download(ctx, item));
  }

  // Too big for the page: what the size guards say (limits() above).
  function tooLarge(box, ctx, item, limit, extra = []) {
    message(box, {
      icon: 'description',
      text: interpolate(gettext('The file is too large (%(size)s); the page previews at most %(limit)s.'), { size: App.fmtSize(item.size), limit: App.fmtSize(limit) }, true),
      detail: can(ctx, 'download') ? gettext('You can download it and open it on your device.') : '',
      actions: [...extra, downloadButton(ctx, item)].filter(Boolean),
    });
  }

  // ------------------------------------------------------------------ downloads

  // The file's download: the browser's own (with its 下载前先确认) when ctx has it, else the item's URL.
  async function download(ctx, item, { confirm = false } = {}) {
    if (confirm && typeof ctx.download === 'function') return ctx.download(item);
    if (confirm && !(ctx.site && ctx.site.download_confirm === false)) {
      const ok = await App.confirmDialog({ title: gettext('Download'), message: interpolate(gettext('Download the file %(name)s?'), { name: item.name }, true), confirmText: gettext('Download') });
      if (!ok) return null;
    }
    let url = null;
    try {
      url = typeof ctx.downloadUrl === 'function' ? await ctx.downloadUrl(item) : item.url;
    } catch (err) {
      App.toast(err.message || gettext("Couldn't get a download address."), 'error');
      return null;
    }
    if (!url) {
      App.toast(gettext("You don't have permission to download this file."), 'error');
      return null;
    }
    // A 下载 button: the site's /pd/ link says so (dl=1), for the download log (files.js clickedUrl).
    if (App.files && typeof App.files.clickedUrl === 'function') url = App.files.clickedUrl(url);
    // As the file list's 下载 (files.js download()): a link of this site is saved, also from a storage whose links
    // open in the browser (下载链接强制下载 off, which replaced the page by the picture); another site's opens in
    // a tab of its own. A share's /share/<key>/download opens in a tab too: when the share's cookie has ended it
    // answers with the share's page (or an error page), which is to be seen, not saved under the file's name.
    const save = sameOrigin(url) && !isShare(ctx);
    const link = h('a', { href: url, class: 'sr-only', rel: 'noopener noreferrer', target: save ? null : '_blank' });
    if (save) link.setAttribute('download', item.name);
    document.body.append(link);
    link.click();
    link.remove();
    return url;
  }

  // ------------------------------------------------------------------ the vendored libraries

  const scripts = new Map();
  function loadScript(url) {
    if (!scripts.has(url)) {
      scripts.set(url, new Promise((resolve, reject) => {
        const tag = h('script', { src: url });
        tag.addEventListener('load', resolve);
        tag.addEventListener('error', () => {
          scripts.delete(url);
          tag.remove();
          reject(new Error(gettext("A script didn't load. Please reload the page and try again.")));
        });
        document.head.append(tag);
      }));
    }
    return scripts.get(url);
  }

  const libs = new Map();
  function lib(name) {
    if (!libs.has(name)) {
      let loading;
      const urls = config.libs;
      if (name === 'hls') {
        loading = loadScript(urls.hls).then(() => window.Hls);
      } else if (name === 'hljs') {
        // The common languages are in highlight.min.js; the few others register themselves after it.
        loading = loadScript(urls.hljs)
          .then(() => Promise.all((urls.hljsLanguages || []).map(loadScript)))
          .then(() => window.hljs);
      } else if (name === 'pdf') {
        loading = import(urls.pdf).then((pdfjs) => {
          pdfjs.GlobalWorkerOptions.workerSrc = urls.pdfWorker;
          return pdfjs;
        });
      } else {
        loading = Promise.reject(new Error(`no library ${name}`));
      }
      libs.set(name, loading.catch((err) => {
        libs.delete(name);
        throw err;
      }));
    }
    return libs.get(name);
  }

  // ------------------------------------------------------------------ reading files

  class TooLarge extends Error {
    constructor(size, limit) {
      super(interpolate(gettext('The file is too large (%(size)s); the page previews at most %(limit)s.'), { size: App.fmtSize(size), limit: App.fmtSize(limit) }, true));
      this.name = 'TooLarge';
      this.size = size;
      this.limit = limit;
    }
  }

  // The bytes at a same-origin URL, at most `limit` of them (TooLarge beyond, stopped as soon as it is
  // known): the size guards hold also when the listing didn't know the size. fresh: not from the cache.
  async function fetchBytes(url, { limit = Infinity, onProgress = null, signal = null, fresh = false } = {}) {
    let res;
    try {
      res = await fetch(url, { credentials: 'same-origin', signal, cache: fresh ? 'no-store' : 'default' });
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      throw new Error(gettext('Network error. Please check your connection and try again.'));
    }
    if (!res.ok) {
      let detail = '';
      try {
        const data = await res.json();
        detail = data && typeof data.detail === 'string' ? data.detail : '';
      } catch (e) {
        // not JSON
      }
      throw new Error(detail || App.errorText(null, res.status));
    }
    const total = Number(res.headers.get('Content-Length')) || 0;
    if (total > limit) {
      res.body && res.body.cancel().catch(() => {});
      throw new TooLarge(total, limit);
    }
    if (!res.body || !res.body.getReader) {
      const buffer = new Uint8Array(await res.arrayBuffer());
      if (buffer.length > limit) throw new TooLarge(buffer.length, limit);
      return buffer;
    }
    const reader = res.body.getReader();
    const parts = [];
    let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.length;
      if (received > limit) {
        reader.cancel().catch(() => {});
        throw new TooLarge(received, limit);
      }
      parts.push(value);
      if (onProgress) onProgress(received, total);
    }
    const bytes = new Uint8Array(received);
    let at = 0;
    for (const part of parts) {
      bytes.set(part, at);
      at += part.length;
    }
    return bytes;
  }

  // The guess of 7.Z.5: a byte-order mark says it; else UTF-8 when the bytes are valid UTF-8, else GB18030.
  function guessEncoding(bytes) {
    if (bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) return { encoding: 'utf-8', bom: true };
    if (bytes[0] === 0xFF && bytes[1] === 0xFE) return { encoding: 'utf-16le', bom: true };
    if (bytes[0] === 0xFE && bytes[1] === 0xFF) return { encoding: 'utf-16be', bom: true };
    try {
      new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      return { encoding: 'utf-8', bom: false };
    } catch (e) {
      return { encoding: 'gb18030', bom: false };
    }
  }
  function decodeText(bytes, encoding) {
    try {
      return new TextDecoder(encoding).decode(bytes); // a BOM of that encoding is dropped
    } catch (e) {
      return new TextDecoder('utf-8').decode(bytes);
    }
  }

  // HTML (the server's Markdown, highlight.js's spans) parsed in a document of its own, where nothing
  // runs or loads: the <body> to check or rewrite there. adopt() then moves it into the page, where
  // pictures start loading (so only once their addresses are the right ones).
  const inert = (html) => new DOMParser().parseFromString(`<!doctype html><body>${html}`, 'text/html').body;
  function adopt(container) {
    const fragment = document.createDocumentFragment();
    fragment.append(...container.childNodes);
    return fragment;
  }

  // highlight.js's HTML as checked nodes: it escapes the text and adds only <span class="hljs-…">.
  // Anything else (a bug, a hostile grammar) and the caller shows plain text.
  const HLJS_CLASS = /^(?:hljs-[\w-]+|language-[\w-]+|[\w-]+_+)$/;
  function highlight(hljs, text, language) {
    let html;
    try {
      html = hljs.highlight(text, { language, ignoreIllegals: true }).value;
    } catch (e) {
      return null;
    }
    const parsed = inert(html);
    for (const el of parsed.querySelectorAll('*')) {
      if (el.tagName !== 'SPAN' || el.attributes.length > 1 || (el.attributes.length === 1 && !el.hasAttribute('class'))) return null;
      if (![...el.classList].every((name) => HLJS_CLASS.test(name))) return null;
    }
    return adopt(parsed);
  }

  App.preview = {
    config, limits, kk, expiresAt, register, action, attach, viewers, can, isShare, isFile, ofKind, pathOf, suffix, sameOrigin,
    absolute, sameItem, store, frame, button, message, downloadButton, downloadAction, tooLarge, download,
    loadScript, lib, TooLarge, fetchBytes, guessEncoding, decodeText, inert, adopt, highlight,
  };
})();
