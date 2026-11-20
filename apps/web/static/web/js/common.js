/* What every page shares, as the global App (plain script, no build):
 *
 *   App.api(method, url, body)   the REST API: JSON or FormData in, JSON out, with the CSRF token. An
 *                                absolute URL the API gave (a next page) is cut to its path (spec §6:
 *                                behind a proxy it may say http:// on an https page). Errors are thrown
 *                                as App.ApiError with the server's Chinese message; one that nobody
 *                                catches is shown as an error snackbar.
 *   App.h(tag, attrs, ...children), App.icon(name)       building the page (text only as text nodes)
 *   App.snackbar(message, options), App.toast(message, kind)
 *   App.openDialog, App.confirmDialog, App.editDialog    native <dialog>s
 *   App.applyRequires(boxes)     checkboxes offered only once the one they need is ticked
 *   App.menu(button)             menus, also from the keyboard (set up for every [aria-haspopup="menu"])
 *   App.tabs(tablist)            tabs, with the arrow keys
 *   App.fmtSize, App.fmtDateTime, App.fmtDate, App.fmtRelative, App.fmtNum
 *   App.theme                    the 主题 choice: get() and set('system' | 'light' | 'dark')
 *
 * Tooltips: any element with data-tooltip="..." gets an M3 plain tooltip on hover and keyboard focus.
 * The press ripple is here too. User-provided text is only ever inserted as text, never as HTML. */
(function () {
  'use strict';

  // ------------------------------------------------------------------ the API

  // Names of fields in the API's errors, for "名称：不能为空" when the message doesn't say it. Apps add theirs.
  const fieldLabels = {
    username: gettext('Username'), password: gettext('Password'), old_password: gettext('Current password'), new_password: gettext('New password'), nickname: gettext('Nickname'),
    step: gettext('Log in'), site_name: gettext('Site name'), page: gettext('Page'), page_size: gettext('Page size'), search: gettext('Search'), ordering: gettext('Sort'),
  };

  const loggedIn = document.body.dataset.auth === '1';
  const pageUser = document.body.dataset.user || ''; // the id of who the page was made for, '' for nobody
  const siteName = (document.querySelector('meta[name="site-name"]') || {}).content || '';

  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.data = data;
    }
  }

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  // Logging in or out in another tab gives the browser a new CSRF token, and this page's one stops
  // working: ask for the current one. Returns whether the session still belongs to the page's user.
  async function refreshCsrf() {
    const res = await fetch('/api/v1/session/', { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
    const data = await res.json();
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && data.csrf_token) meta.content = data.csrf_token;
    return (data.user ? String(data.user.id) : '') === pageUser;
  }
  const STALE_PAGE = gettext('Your login state has changed (you may have logged in or out in another tab). Please reload the page and try again.');

  // DRF's error body as readable text. Errors for items of a list come keyed by position,
  // {"items": {"2": {"name": [...]}}}, and read as "第 3 项（名称：…）".
  function errorText(data, status, separator = '\n') {
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      if (typeof data.detail === 'string') return data.detail;
      const parts = [];
      for (const [key, value] of Object.entries(data)) {
        const msg = fieldMessage(value, status);
        // "code": "csrf_failed" says what kind of error it is; a field called code has a list of messages.
        if (!msg || (key === 'code' && typeof value === 'string')) continue;
        if (key === 'non_field_errors') parts.push(msg);
        else if (/^\d+$/.test(key)) parts.push(interpolate(gettext('Item %(n)s (%(msg)s)'), { n: Number(key) + 1, msg }, true));
        else {
          // Most messages name their field already ("用户名不能为空"); don't say it twice.
          const label = key in fieldLabels ? fieldLabels[key] : key;
          parts.push(!label || msg.startsWith(label) ? msg : interpolate(gettext('%(label)s: %(msg)s'), { label, msg }, true));
        }
      }
      if (parts.length) return parts.join(separator);
    }
    if (status === 401) return gettext('Please log in first.');
    if (status === 403) return gettext("You don't have permission to do this.");
    if (status === 404) return gettext('Not found.');
    if (status === 413) return gettext('What was sent is too large.');
    if (status === 429) return gettext('Too many requests. Please try again later.');
    if (status >= 500) return interpolate(gettext('The server ran into an error (HTTP %(status)s). Please try again later.'), { status }, true);
    return interpolate(gettext('Request failed (HTTP %(status)s)'), { status }, true);
  }

  function fieldMessage(value, status) {
    if (Array.isArray(value)) {
      return value.map((item, i) => {
        if (item && typeof item === 'object') {
          return Object.keys(item).length ? interpolate(gettext('Item %(n)s (%(msg)s)'), { n: i + 1, msg: errorText(item, status, pgettext('between clauses', ', ')) }, true) : '';
        }
        return String(item);
      }).filter(Boolean).join(gettext('; '));
    }
    if (value && typeof value === 'object') return errorText(value, status, pgettext('between clauses', ', '));
    return String(value);
  }

  // A link (one the API gives, or a path) as a path on this site, with its query. Behind a reverse
  // proxy that ends https without saying so (X-Forwarded-Proto) the API writes http://, which a page
  // opened over https isn't allowed to fetch; and a "//elsewhere/" would be another site.
  function pageUrl(url) {
    if (!url) return null;
    const link = new URL(url, location.href);
    return `/${link.pathname.replace(/^\/+/, '')}${link.search}`;
  }

  async function api(method, url, body, { retried = false } = {}) {
    const headers = { Accept: 'application/json' };
    const init = { method, headers, credentials: 'same-origin' };
    const writes = method !== 'GET' && method !== 'HEAD';
    if (writes) headers['X-CSRFToken'] = csrfToken();
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(pageUrl(url), init);
    } catch (e) {
      throw new ApiError(gettext('Network error. Please check your connection and try again.'), 0, null);
    }
    let data = null;
    if (res.status !== 204) {
      const type = res.headers.get('Content-Type') || '';
      data = type.includes('json') ? await res.json().catch(() => null) : await res.text();
    }
    // A CSRF failure is answered before anything is done, so sending it again with the new token is safe.
    if (writes && !retried && res.status === 403 && data && data.code === 'csrf_failed') {
      let same = false;
      try {
        same = await refreshCsrf();
      } catch (e) {
        // the request below fails the same way, with its own message
      }
      if (!same) throw new ApiError(STALE_PAGE, 403, data);
      return api(method, url, body, { retried: true });
    }
    if (!res.ok) {
      let message = errorText(data, res.status);
      // The session ended (logged out in another tab, expired, or the password changed).
      if (res.status === 401 && loggedIn) message = gettext('Your login has expired. Please reload the page and log in again.');
      throw new ApiError(message, res.status, data);
    }
    // A page instead of the API's JSON (a Wi-Fi login page, a proxy's maintenance page): an error,
    // not data that breaks whatever reads it.
    if (res.status !== 204 && (data === null || typeof data !== 'object')) {
      throw new ApiError(interpolate(gettext("The server's answer couldn't be understood (HTTP %(status)s). Please try again later."), { status: res.status }, true), res.status, data);
    }
    return data;
  }

  // An API error that nobody caught: the snackbar says it.
  window.addEventListener('unhandledrejection', (e) => {
    if (e.reason instanceof ApiError) {
      e.preventDefault();
      snackbar(e.reason.message, { kind: 'error' });
    }
  });

  // ------------------------------------------------------------------ building the page

  // The DOM's append()/replaceChildren() turn null into the text "null"; drop empty slots first.
  const nodes = (...items) => items.flat().filter((x) => x !== null && x !== undefined && x !== false);

  // h('a', {href: '/x', class: 'btn', onclick: fn}, 'text', childNode)
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') el.className = value;
        else if (key === 'text') el.textContent = value;
        else if (key === 'dataset') Object.assign(el.dataset, value);
        else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
        else if (key === 'style') throw new Error('no style attributes: the CSP forbids them');
        else el.setAttribute(key, value === true ? '' : String(value));
      }
    }
    for (const child of nodes(...children)) {
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  // An icon of the sprite web/icons.svg (Material Symbols), as the {% icon %} template tag makes it.
  // Its URL, with the file's fingerprint, comes from <meta name="icons"> in base.html.
  const ICONS = (document.querySelector('meta[name="icons"]') || {}).content || '/static/web/icons.svg';
  const iconHref = (name) => `${ICONS}#i-${name}`;
  function icon(name, extraClass) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'icon' + (extraClass ? ' ' + extraClass : ''));
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', iconHref(name));
    svg.append(use);
    return svg;
  }

  // M3's circular progress indicator: without `value` it spins; with one (0 to 1) it shows it.
  function spinner({ value = null, small = false, label = gettext('Loading') } = {}) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'circular-progress' + (small ? ' small' : ''));
    svg.setAttribute('viewBox', '0 0 48 48');
    svg.setAttribute('role', 'progressbar');
    svg.setAttribute('aria-label', label);
    const track = document.createElementNS(ns, 'circle');
    const bar = document.createElementNS(ns, 'circle');
    for (const circle of [track, bar]) {
      circle.setAttribute('cx', '24');
      circle.setAttribute('cy', '24');
      circle.setAttribute('r', '20');
    }
    track.setAttribute('class', 'track');
    bar.setAttribute('class', 'bar');
    svg.append(track, bar);
    if (value !== null) setProgress(svg, value);
    return svg;
  }
  // A determinate circular indicator's value, 0 to 1.
  function setProgress(svg, value) {
    const length = 2 * Math.PI * 20;
    const done = Math.max(0, Math.min(1, value));
    svg.setAttribute('aria-valuenow', String(Math.round(done * 100)));
    svg.setAttribute('aria-valuemin', '0');
    svg.setAttribute('aria-valuemax', '100');
    const bar = svg.querySelector('.bar');
    bar.style.strokeDasharray = `${length}`;
    bar.style.strokeDashoffset = `${length * (1 - done)}`;
  }

  const setTitle = (text) => { document.title = text ? `${text} · ${siteName}` : siteName; };

  // ------------------------------------------------------------------ formatting

  const numberFormat = new Intl.NumberFormat('zh-CN');
  const fmtNum = (n) => numberFormat.format(n || 0);
  // 0 B, 1023 B, 1.0 KB, 12.3 MB, 4.56 GB: binary units, as file managers show them.
  function fmtSize(bytes) {
    if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) return '—';
    let n = Number(bytes);
    if (n < 1024) return `${n} B`;
    const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
    let unit = -1;
    do {
      n /= 1024;
      unit += 1;
    } while (n >= 1024 && unit < units.length - 1);
    return `${n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)} ${units[unit]}`;
  }
  const pad = (n) => String(n).padStart(2, '0');
  // 2026-11-07 23:05 (the browser's own time zone), with seconds when asked.
  function fmtDateTime(value, { seconds = false } = {}) {
    if (!value) return '';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return '';
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? `:${pad(d.getSeconds())}` : ''}`;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`;
  }
  function fmtDate(value) {
    return fmtDateTime(value).slice(0, 10);
  }
  // 刚刚, 5 分钟前, 3 小时前, 2 天前, then the date.
  function fmtRelative(value, now = Date.now()) {
    if (!value) return '';
    const seconds = (now - new Date(value).getTime()) / 1000;
    if (Number.isNaN(seconds)) return '';
    if (seconds < 60) return gettext('just now');
    if (seconds < 3600) return interpolate(ngettext('%(n)s minute ago', '%(n)s minutes ago', Math.floor(seconds / 60)), { n: Math.floor(seconds / 60) }, true);
    if (seconds < 86400) return interpolate(ngettext('%(n)s hour ago', '%(n)s hours ago', Math.floor(seconds / 3600)), { n: Math.floor(seconds / 3600) }, true);
    if (seconds < 30 * 86400) return interpolate(ngettext('%(n)s day ago', '%(n)s days ago', Math.floor(seconds / 86400)), { n: Math.floor(seconds / 86400) }, true);
    return fmtDate(value);
  }

  // Only same-site paths are allowed as redirect targets. Checked on the parsed URL, not the text:
  // the URL parser drops tabs and line breaks, so "/\t/evil.com" would become "//evil.com". It also
  // resolves "." and "..", so "/.//evil.com" has the path "//evil.com", which a browser reads as
  // another site: the result always starts with a single "/".
  function safeNext(value) {
    if (!value || !value.startsWith('/')) return '/';
    try {
      const url = new URL(value, location.origin);
      if (url.origin === location.origin) return '/' + url.pathname.replace(/^\/+/, '') + url.search + url.hash;
    } catch (err) {
      // not a URL at all
    }
    return '/';
  }

  // The server's clean-up of names (apps/core/text.py), for live counters and previews.
  const oneLine = (text) => text.normalize('NFC').split(/\s+/).filter(Boolean).join(' ');
  const charCount = (text) => [...text].length; // code points, like Python's len()

  // <span data-count-for="input-id" data-max="40"> shows "12 / 40" for that input.
  function updateCounter(counter) {
    const input = document.getElementById(counter.dataset.countFor);
    if (!input) return;
    const n = charCount(input.tagName === 'TEXTAREA' ? input.value.trim() : oneLine(input.value));
    const max = Number(counter.dataset.max);
    counter.textContent = `${n} / ${max}`;
    counter.classList.toggle('over', n > max);
  }
  const updateCounters = (root = document) => root.querySelectorAll('[data-count-for]').forEach(updateCounter);
  document.addEventListener('input', (e) => {
    if (!e.target.id) return;
    document.querySelectorAll(`[data-count-for="${CSS.escape(e.target.id)}"]`).forEach(updateCounter);
  });

  // ------------------------------------------------------------------ snackbar

  // The snackbar is a popover, so it sits in the top layer: showing it (again) puts it above an open
  // dialog, whose backdrop would otherwise dim it. Without the Popover API it is [hidden] toggled.
  let snackbarTimer = null;
  function snackbar(message, { kind = '', action = null, timeout = null } = {}) {
    const el = document.getElementById('snackbar');
    if (!el) return;
    const popover = typeof el.showPopover === 'function';
    const close = () => {
      clearTimeout(snackbarTimer);
      if (popover && el.matches(':popover-open')) el.hidePopover();
      el.hidden = true;
    };
    el.className = 'snackbar' + (kind === 'error' ? ' snackbar-error' : '');
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.replaceChildren(...nodes(
      h('span', { class: 'snackbar-text', text: message }),
      action ? h('button', { type: 'button', class: 'btn btn-text', text: action.label, onclick: () => { close(); action.onClick(); } }) : null,
      h('button', { type: 'button', class: 'icon-button small', 'aria-label': gettext('Close'), onclick: close }, icon('close'))));
    el.hidden = false;
    if (popover) {
      if (el.matches(':popover-open')) el.hidePopover();
      el.showPopover();
    }
    clearTimeout(snackbarTimer);
    snackbarTimer = setTimeout(close, timeout || (kind === 'error' || action ? 8000 : 4000));
  }
  const toast = (message, kind) => snackbar(message, { kind });

  // A snackbar for the next page, when this one is about to navigate away: flash('已退出登录').
  const FLASH_KEY = 'hfile-flash';
  function flash(message) {
    try { sessionStorage.setItem(FLASH_KEY, message); } catch (e) { /* storage blocked: no snackbar, nothing else lost */ }
  }
  function showFlash() {
    let message = null;
    try {
      message = sessionStorage.getItem(FLASH_KEY);
      sessionStorage.removeItem(FLASH_KEY);
    } catch (e) { /* storage blocked */ }
    if (message) toast(message);
  }

  // ------------------------------------------------------------------ dialogs

  // Native <dialog>: showModal() keeps focus inside and Esc closes it. This adds: [data-open="id"]
  // opens one, [data-close] closes its dialog, a click on the backdrop closes it, and focus goes back
  // where it was when the dialog closes.
  function openDialog(dialog) {
    if (!dialog.open) {
      const before = document.activeElement;
      dialog.showModal();
      dialog.addEventListener('close', () => {
        if (before && before.isConnected && typeof before.focus === 'function') before.focus();
      }, { once: true });
    }
    const first = [...dialog.querySelectorAll('[data-autofocus]')].find((el) => !el.closest('[hidden]'));
    if (first) first.focus();
  }

  // Closing with [data-close] or the backdrop fires "cancel" first, like Esc does, so a dialog
  // with something filled in can call preventDefault() and ask.
  function requestClose(dialog) {
    if (dialog.dispatchEvent(new Event('cancel', { cancelable: true }))) dialog.close();
  }

  let pressedOn = null;
  document.addEventListener('pointerdown', (e) => { pressedOn = e.target; });
  document.addEventListener('click', (e) => {
    const target = e.target;
    if (target instanceof HTMLDialogElement && target.open && pressedOn === target) {
      // A dialog with something typed into it (data-dirty, editDialog) stays: a click beside it would
      // lose the typing. Esc, 取消 and ✕ still close it.
      if (!('dirty' in target.dataset)) requestClose(target);
      return;
    }
    const closer = target.closest('[data-close]');
    if (closer && closer.closest('dialog')) {
      requestClose(closer.closest('dialog'));
      return;
    }
    const opener = target.closest('[data-open]');
    const dialog = opener && document.getElementById(opener.dataset.open);
    if (dialog) openDialog(dialog);
  });

  let dialogSeq = 0;
  // Resolves to true when the user confirms.
  function confirmDialog({ title, message = '', confirmText = gettext('OK'), cancelText = gettext('Cancel'), danger = false }) {
    return new Promise((resolve) => {
      let confirmed = false;
      const titleId = `confirm-title-${++dialogSeq}`;
      const cancel = h('button', { type: 'button', class: 'btn btn-text', text: cancelText });
      const ok = h('button', { type: 'button', class: 'btn ' + (danger ? 'btn-danger' : 'btn-filled'), text: confirmText });
      const dialog = h('dialog', { class: 'dialog dialog-small', role: 'alertdialog', 'aria-labelledby': titleId },
        h('div', { class: 'dialog-box' },
          h('div', { class: 'dialog-body' },
            h('h2', { class: 'confirm-title', id: titleId, text: title }),
            message ? h('p', { class: 'confirm-text', text: message }) : null,
            h('div', { class: 'dialog-foot' }, cancel, ok))));
      cancel.addEventListener('click', () => dialog.close());
      ok.addEventListener('click', () => { confirmed = true; dialog.close(); });
      dialog.addEventListener('close', () => { dialog.remove(); resolve(confirmed); });
      document.body.append(dialog);
      openDialog(dialog);
      (danger ? cancel : ok).focus();
    });
  }

  // A dialog that edits something and saves it: { title, body, save, submitText, danger, full }.
  // On submit `save()` runs; what it returns is what the dialog resolves to, and it closes. If it
  // throws, its message shows in the dialog. Returning undefined keeps it open (a field says what is
  // wrong itself). Closing without saving resolves to null.
  function editDialog({ title, body, save, submitText = gettext('Save'), submitIcon = 'save', danger = false, full = false, wide = false }) {
    return new Promise((resolve) => {
      const titleId = `edit-title-${++dialogSeq}`;
      let saved = null;
      let saving = null; // the save under way: the dialog waits for its answer, which may have changed things
      const error = h('div', { class: 'form-error', role: 'alert', hidden: true });
      const saveButton = h('button', { type: 'submit', class: danger ? 'btn btn-danger' : 'btn btn-filled' }, icon(submitIcon), submitText);
      const form = h('form', { class: 'dialog-body', novalidate: true },
        body,
        error,
        h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') }), saveButton));
      const dialog = h('dialog', { class: `dialog${full ? ' dialog-full' : ''}${wide ? ' dialog-wide' : ''}`, 'aria-labelledby': titleId },
        h('div', { class: 'dialog-box' },
          h('header', { class: 'dialog-head' },
            h('h2', { id: titleId, text: title }),
            h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
          form));
      const closers = () => dialog.querySelectorAll('[data-close]');
      form.addEventListener('input', () => { dialog.dataset.dirty = ''; });
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        if (saving) return;
        error.hidden = true;
        saveButton.disabled = true;
        closers().forEach((button) => { button.disabled = true; });
        saving = (async () => {
          // save() runs once `saving` is set: one that throws straight away (a save that isn't async) would
          // otherwise get to the finally below first, and the dialog would stay "saving" for good.
          await null;
          try {
            const result = await save(form.elements);
            if (result !== undefined) {
              saved = result;
              if (dialog.open) dialog.close();
            }
          } catch (err) {
            error.textContent = err.message;
            error.hidden = false;
          } finally {
            saveButton.disabled = false;
            closers().forEach((button) => { button.disabled = false; });
            saving = null;
          }
        })();
      });
      // Esc while saving: not yet. A browser may still close it (a second Esc): then the answer is
      // waited for, so the page shows what was saved.
      dialog.addEventListener('cancel', (e) => { if (saving) e.preventDefault(); });
      dialog.addEventListener('close', () => {
        const finish = () => {
          dialog.remove();
          resolve(saved);
        };
        if (saving) saving.then(finish);
        else finish();
      });
      document.body.append(dialog);
      updateCounters(dialog);
      openDialog(dialog);
    });
  }

  // Shows `message` in the .form-error box of a form (or of the dialog around it); '' hides it.
  function formError(form, message) {
    const box = form.querySelector('.form-error') || (form.closest('dialog') || form).querySelector('.form-error');
    if (!box) return;
    box.textContent = message || '';
    box.hidden = !message;
  }

  // Runs `handler(form.elements)` on submit with the button disabled and errors shown in the form.
  // If the handler returns true (it is navigating away, say), the button stays disabled.
  function handleSubmit(form, handler) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const button = form.querySelector('[type="submit"]');
      formError(form, '');
      if (button) button.disabled = true;
      let keepDisabled = false;
      try {
        keepDisabled = (await handler(form.elements)) === true;
      } catch (err) {
        formError(form, err.message);
      } finally {
        if (button && !keepDisabled) button.disabled = false;
      }
    });
  }

  // Checkboxes that need another one first: data-requires="a b" means one of the boxes of the same list
  // with the value a or b (a user's permissions, apps/accounts/permissions.py REQUIRES). Such a box is
  // offered only while one of them is ticked. The last one unticked takes its tick away and marks it
  // data-held-back; ticking one again gives the tick back. Call it after every change of ticks: with
  // `fresh: true` after a script set them (a user's data, 全选, 反选), which forgets what was held back.
  // `editable: false` disables every box.
  function applyRequires(boxes, { editable = true, fresh = false } = {}) {
    if (fresh) boxes.forEach((box) => { delete box.dataset.heldBack; });
    const ticked = (value) => boxes.some((box) => box.value === value && box.checked);
    let changed = true;
    while (changed) { // a box can need one that needs another
      changed = false;
      for (const box of boxes) {
        const needs = (box.dataset.requires || '').split(' ').filter(Boolean);
        const met = !needs.length || needs.some(ticked);
        if (!met && box.checked) {
          box.checked = false;
          box.dataset.heldBack = '';
          changed = true;
        } else if (met && !box.checked && 'heldBack' in box.dataset) {
          box.checked = true;
          changed = true;
        }
        if (box.checked) delete box.dataset.heldBack;
        box.disabled = !editable || !met;
      }
    }
  }

  // Puts `text` on the clipboard; whether it worked. navigator.clipboard only exists on https (and
  // localhost): on a site opened over plain http, a selected hidden text box and the old copy command.
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (err) {
        // fall back below
      }
    }
    const box = h('textarea', { readonly: true, class: 'copy-buffer', 'aria-hidden': 'true' });
    box.value = text;
    document.body.append(box);
    box.select();
    let done = false;
    try {
      done = document.execCommand('copy');
    } catch (err) {
      done = false;
    }
    box.remove();
    return done;
  }

  // ------------------------------------------------------------------ menus

  // <div class="menu-wrap"><button aria-haspopup="menu" aria-controls="m" aria-expanded="false">
  // <div class="menu" id="m" role="menu" hidden><button role="menuitem" class="menu-item">…
  // Opens on click, Enter, Space or the arrow keys (focusing the first or last item); the arrow keys,
  // Home and End move among the items; Esc closes it and goes back to the button; so do Tab, a click
  // outside, and choosing an item (unless it has data-keep-open).
  const openMenus = new Set();
  function menu(button) {
    if (button.dataset.menuReady) return null;
    button.dataset.menuReady = '1';
    const list = document.getElementById(button.getAttribute('aria-controls'));
    if (!list) return null;
    const items = () => [...list.querySelectorAll('[role^="menuitem"]')].filter((item) => !item.closest('[hidden]') && item.getAttribute('aria-disabled') !== 'true' && !item.disabled);
    const focusAt = (index) => {
      const all = items();
      if (all.length) all[(index + all.length) % all.length].focus();
    };
    const control = {
      open(focus = null) {
        for (const other of openMenus) if (other !== control) other.close(false);
        list.hidden = false;
        button.setAttribute('aria-expanded', 'true');
        openMenus.add(control);
        if (focus === 'first') focusAt(0);
        else if (focus === 'last') focusAt(-1);
      },
      close(refocus = true) {
        if (list.hidden) return;
        list.hidden = true;
        button.setAttribute('aria-expanded', 'false');
        openMenus.delete(control);
        if (refocus) button.focus();
      },
      get isOpen() { return !list.hidden; },
      wrap: button.closest('.menu-wrap') || button.parentElement,
    };
    button.addEventListener('click', (e) => {
      if (control.isOpen) control.close(false);
      else control.open(e.detail === 0 ? 'first' : null); // detail 0: from the keyboard
    });
    button.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); control.open('first'); }
      if (e.key === 'ArrowUp') { e.preventDefault(); control.open('last'); }
    });
    list.addEventListener('keydown', (e) => {
      const all = items();
      const index = all.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); focusAt(index + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); focusAt(index - 1); }
      else if (e.key === 'Home') { e.preventDefault(); focusAt(0); }
      else if (e.key === 'End') { e.preventDefault(); focusAt(-1); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); control.close(); }
      else if (e.key === 'Tab') control.close(false);
    });
    list.addEventListener('click', (e) => {
      const item = e.target.closest('[role^="menuitem"]');
      if (item && !item.hasAttribute('data-keep-open')) control.close();
    });
    return control;
  }
  // A click outside an open menu's own .menu-wrap closes it.
  document.addEventListener('click', (e) => {
    for (const control of [...openMenus]) if (!control.wrap.contains(e.target)) control.close(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') for (const control of [...openMenus]) control.close();
  });

  // ------------------------------------------------------------------ tabs

  // role="tab" buttons with data-tab and aria-controls; panels are shown with [hidden]. The arrow
  // keys, Home and End move between tabs (and select them), as WAI-ARIA's tabs pattern has it.
  function tabs(tablist, onChange) {
    const buttons = [...tablist.querySelectorAll('[role="tab"]')];
    let current = (buttons.find((b) => b.getAttribute('aria-selected') === 'true') || buttons[0]).dataset.tab;
    function select(name, { focus = false, silent = false } = {}) {
      if (!buttons.some((b) => b.dataset.tab === name)) return;
      current = name;
      for (const b of buttons) {
        const on = b.dataset.tab === name;
        b.setAttribute('aria-selected', String(on));
        b.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(b.getAttribute('aria-controls'));
        if (panel) panel.hidden = !on;
        if (on && focus) b.focus();
      }
      if (!silent && onChange) onChange(name);
    }
    tablist.addEventListener('click', (e) => {
      const b = e.target.closest('[role="tab"]');
      if (b && b.dataset.tab !== current) select(b.dataset.tab);
    });
    tablist.addEventListener('keydown', (e) => {
      const i = buttons.findIndex((b) => b.dataset.tab === current);
      let next = null;
      if (e.key === 'ArrowRight') next = (i + 1) % buttons.length;
      else if (e.key === 'ArrowLeft') next = (i + buttons.length - 1) % buttons.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = buttons.length - 1;
      if (next === null) return;
      e.preventDefault();
      select(buttons[next].dataset.tab, { focus: true });
    });
    select(current, { silent: true });
    return { select, get current() { return current; } };
  }

  // ------------------------------------------------------------------ tooltips

  // [data-tooltip="复制链接"]: shown after a moment of hovering, at once on keyboard focus; hidden on
  // leaving, Esc and scrolling. One element for the whole page, in the top layer when it can be.
  const tip = h('div', { class: 'tooltip', role: 'tooltip', id: 'tooltip', popover: 'manual', hidden: true });
  document.body.append(tip);
  const tipPopover = typeof tip.showPopover === 'function';
  let tipTimer = null;
  let tipFor = null;
  function showTip(target) {
    const text = target.dataset.tooltip;
    if (!text) return;
    tipFor = target;
    tip.textContent = text;
    tip.hidden = false;
    if (tipPopover && !tip.matches(':popover-open')) tip.showPopover();
    else tip.classList.add('shown');
    const box = target.getBoundingClientRect();
    const own = tip.getBoundingClientRect();
    let top = box.bottom + 4;
    if (top + own.height > window.innerHeight - 4) top = box.top - own.height - 4;
    const left = Math.max(8, Math.min(window.innerWidth - own.width - 8, box.left + box.width / 2 - own.width / 2));
    tip.style.top = `${Math.max(4, top)}px`;
    tip.style.left = `${left}px`;
  }
  function hideTip() {
    clearTimeout(tipTimer);
    tipFor = null;
    if (tipPopover && tip.matches(':popover-open')) tip.hidePopover();
    tip.classList.remove('shown');
    tip.hidden = true;
  }
  document.addEventListener('pointerover', (e) => {
    const target = e.target.closest('[data-tooltip]');
    if (!target || target === tipFor) return;
    hideTip();
    if (e.pointerType === 'touch') return;
    tipTimer = setTimeout(() => showTip(target), 500);
  });
  document.addEventListener('pointerout', (e) => {
    const target = e.target.closest('[data-tooltip]');
    if (target && !target.contains(e.relatedTarget)) hideTip();
  });
  document.addEventListener('focusin', (e) => {
    const target = e.target.closest && e.target.closest('[data-tooltip]');
    if (target && target.matches(':focus-visible')) showTip(target);
  });
  document.addEventListener('focusout', hideTip);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideTip(); });
  window.addEventListener('scroll', hideTip, { passive: true, capture: true });
  document.addEventListener('pointerdown', hideTip);

  // ------------------------------------------------------------------ ripple

  // M3's press feedback: a circle of the content color grows from where a component was pressed
  // and fades once it is let go (.ripple in app.css). Hover and focus are the CSS state layers.
  const RIPPLE_HOSTS = [
    '.btn', '.icon-button', '.fab', '.nav-link', '.menu-item', '.tab-button', '.segment', '.chip', '.chip-remove',
    '.list-item.interactive', '.manage-link', '.breadcrumb', '.user-button', '.card.interactive',
  ].join(', ');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  document.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || reducedMotion.matches) return;
    let host = e.target.closest(RIPPLE_HOSTS);
    if (!host || host.disabled || host.getAttribute('aria-disabled') === 'true' || host.getAttribute('aria-current') === 'page' && host.classList.contains('breadcrumb')) return;
    if (host.classList.contains('nav-link')) host = host.querySelector('.nav-indicator') || host;
    const rect = host.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const radius = Math.hypot(Math.max(x, rect.width - x), Math.max(y, rect.height - y)); // to the farthest corner
    const wave = document.createElement('span');
    wave.className = 'ripple';
    wave.setAttribute('aria-hidden', 'true');
    wave.style.left = `${x - radius}px`;
    wave.style.top = `${y - radius}px`;
    wave.style.width = wave.style.height = `${radius * 2}px`;
    host.append(wave);
    const release = () => {
      window.removeEventListener('pointerup', release);
      window.removeEventListener('pointercancel', release);
      wave.classList.add('out');
      setTimeout(() => wave.remove(), 400);
    };
    window.addEventListener('pointerup', release);
    window.addEventListener('pointercancel', release);
  });

  // ------------------------------------------------------------------ theme

  // 跟随系统, 浅色 or 深色 (theme.js applies it before the page is drawn). Every storage access in a
  // try: a private window or a sandbox may refuse it, and the page still works with the system's theme.
  const THEME_KEY = 'hfile-theme';
  const theme = {
    get() {
      const set = document.documentElement.getAttribute('data-theme');
      return set === 'light' || set === 'dark' ? set : 'system';
    },
    set(value) {
      const root = document.documentElement;
      if (value === 'light' || value === 'dark') root.setAttribute('data-theme', value);
      else root.removeAttribute('data-theme');
      try {
        if (value === 'light' || value === 'dark') localStorage.setItem(THEME_KEY, value);
        else localStorage.removeItem(THEME_KEY);
      } catch (e) {
        // not kept: it lasts for this page
      }
      document.dispatchEvent(new CustomEvent('theme:change', { detail: theme.get() }));
    },
  };

  // ------------------------------------------------------------------ start

  updateCounters();
  showFlash();

  window.App = {
    api, ApiError, pageUrl, errorText, fieldLabels, h, nodes, icon, iconHref, spinner, setProgress,
    snackbar, toast, flash, setTitle, fmtNum, fmtSize, fmtDateTime, fmtDate, fmtRelative, safeNext,
    oneLine, charCount, updateCounters, openDialog, requestClose, confirmDialog, editDialog, formError,
    handleSubmit, applyRequires, copyText, menu, tabs, theme, loggedIn, siteName,
  };

  // Every menu button of the page (more come from scripts: they call App.menu themselves).
  document.querySelectorAll('[aria-haspopup="menu"][aria-controls]').forEach(menu);
})();
