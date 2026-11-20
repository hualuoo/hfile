/* The file browser's 获取直链, 生成短链 and 同时获取 (contract W4: actions of group 2, for files only), and
 * their dialogs (inventory 9.2, 6U.4):
 *
 *   生成短链   the lifetime, one of the site's 短链有效期 ("<value> <unit>" or 永久), the first chosen;
 *   直链       the result. One file: its name and size, each link (a click copies it, 复制成功) and a QR code
 *              the server drew (segno, SVG in an <img>; downloadable as <name>-qrcode.svg or .png).
 *              Several: a table with a copy button per column and 导出, a CSV file (UTF-8 with a BOM).
 *
 * The links come from the API (POST /api/v1/links/<storage key>/path/ and short/): every file is checked
 * there as opening the link will check it, and the addresses are absolute ([site] url). A folder password
 * the files need is asked for by the browser (withPasswords) and the request sent again. Names are only
 * ever inserted as text. */
(function () {
  'use strict';
  const App = window.App;
  if (!App || !App.files) return;
  const { api, h, icon, toast, fmtSize, fmtDateTime, copyText, openDialog } = App;

  Object.assign(App.fieldLabels, { paths: gettext('File'), expires: gettext('Short link lifetime') });

  const API = '/api/v1/links/';
  const UNITS = {
    s: (n) => ngettext('%(n)s second', '%(n)s seconds', n), m: (n) => ngettext('%(n)s minute', '%(n)s minutes', n),
    h: (n) => ngettext('%(n)s hour', '%(n)s hours', n), d: (n) => ngettext('%(n)s day', '%(n)s days', n),
    w: (n) => ngettext('%(n)s week', '%(n)s weeks', n), M: (n) => ngettext('%(n)s month', '%(n)s months', n),
    y: (n) => ngettext('%(n)s year', '%(n)s years', n),
  };
  const lifetimeLabel = (item) => (item.unit === 'forever' || item.seconds === -1 ? gettext('Forever')
    : UNITS[item.unit] ? interpolate(UNITS[item.unit](item.value), { n: item.value }, true) : `${item.value} ${item.unit}`);

  const onlyFiles = (items) => items.length > 0 && items.every((item) => item.type === 'file');
  const browsing = (ctx) => ctx.mode === 'browse' && !!ctx.storage;
  const pathLinks = (ctx) => !!(ctx.site.show_path_link && ctx.permissions.generate_link);
  const shortLinks = (ctx) => !!(ctx.site.show_short_link && ctx.permissions.generate_short_link);
  const dialogId = (name) => `links-${name}-${Math.random().toString(36).slice(2)}`;

  App.files.actions.register({
    key: 'path_link', label: gettext('Get direct links'), icon: 'link', group: 2, order: 10,
    when: (ctx, items) => browsing(ctx) && onlyFiles(items) && pathLinks(ctx),
    run: (ctx, items) => makeLinks(ctx, items, { path: true, short: false }),
  });
  App.files.actions.register({
    key: 'short_link', label: gettext('Create short links'), icon: 'add_link', group: 2, order: 20,
    when: (ctx, items) => browsing(ctx) && onlyFiles(items) && shortLinks(ctx),
    run: (ctx, items) => makeLinks(ctx, items, { path: false, short: true }),
  });
  App.files.actions.register({
    key: 'both_links', label: gettext('Get both links'), icon: 'dataset_linked', group: 2, order: 30,
    when: (ctx, items) => browsing(ctx) && onlyFiles(items) && pathLinks(ctx) && shortLinks(ctx),
    run: (ctx, items) => makeLinks(ctx, items, { path: true, short: true }),
  });

  async function makeLinks(ctx, items, { path, short }) {
    if (!items.length) { toast(gettext('Please select at least one file'), 'error'); return; }
    let expires = null;
    if (short) {
      expires = await chooseLifetime(ctx, items);
      if (expires === null) return;
    }
    const base = `${API}${encodeURIComponent(ctx.storage.key)}/`;
    const paths = items.map((item) => item.path);
    const one = items.length === 1;
    let answer;
    try {
      // A folder password the files need: the browser asks for it, unlocks, and this runs again.
      answer = await ctx.browser.withPasswords(async () => ({
        path: path ? await api('POST', `${base}path/`, { paths, qr: one && !short }) : null,
        short: short ? await api('POST', `${base}short/`, { paths, expires, qr: one }) : null,
      }));
    } catch (err) {
      toast(err.message, 'error');
      return;
    }
    if (answer) showLinks(items, answer, ctx);
  }

  // ------------------------------------------------------------------ 生成短链: the lifetime

  function chooseLifetime(ctx, items) {
    const choices = (ctx.site.short_link_expiries || []).filter((item) => item && Number.isInteger(item.seconds));
    if (!choices.length) { toast(gettext('The site has no short link lifetimes set. Please contact the administrator.'), 'error'); return Promise.resolve(null); }
    return new Promise((resolve) => {
      let chosen = null;
      const select = h('select', { name: 'expires', id: dialogId('expires'), 'data-autofocus': '' },
        ...choices.map((item, index) => h('option', { value: String(item.seconds), selected: index === 0, text: lifetimeLabel(item) })));
      const titleId = dialogId('title');
      const form = h('form', { class: 'dialog-body', novalidate: true },
        h('div', { class: 'field links-chosen' }, h('span', { class: 'label', text: gettext('Selected files') }),
          h('p', { class: 'links-chosen-text', text: items.length === 1 ? items[0].name : interpolate(ngettext('%(count)s file', '%(count)s files', items.length), { count: items.length }, true) })),
        h('div', { class: 'field' }, h('label', { for: select.id, text: gettext('Short link lifetime') }), select),
        h('div', { class: 'dialog-foot' },
          h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') }),
          h('button', { type: 'submit', class: 'btn btn-filled', text: gettext('OK') })));
      const dialog = h('dialog', { class: 'dialog dialog-small links-lifetime', 'aria-labelledby': titleId },
        h('div', { class: 'dialog-box' },
          h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: gettext('Create short links') }),
            h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
          form));
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        chosen = Number(select.value);
        dialog.close();
      });
      dialog.addEventListener('close', () => { dialog.remove(); resolve(chosen); });
      document.body.append(dialog);
      openDialog(dialog);
    });
  }

  // ------------------------------------------------------------------ 直链: the result

  async function copy(text, done = gettext('Copied')) {
    if (await copyText(text)) toast(done);
    else toast(gettext('Copying failed. Please copy it by hand.'), 'error');
  }

  // A link as a button: shown whole (it wraps), a click copies it.
  // What each kind of link says, whole sentences: the kind put into one sentence doesn't translate.
  const KINDS = {
    path: {
      label: gettext('Direct link'), copy: gettext('Copy direct link'), copyAll: gettext('Copy all direct links'), toClipboard: gettext('Copy all direct links to the clipboard'),
      copied: (n) => ngettext('Copied %(count)s direct link', 'Copied %(count)s direct links', n),
    },
    short: {
      label: gettext('Short link'), copy: gettext('Copy short link'), copyAll: gettext('Copy all short links'), toClipboard: gettext('Copy all short links to the clipboard'),
      copied: (n) => ngettext('Copied %(count)s short link', 'Copied %(count)s short links', n),
    },
  };

  function linkButton(url, kind) {
    return h('button', { type: 'button', class: 'links-url', 'aria-label': KINDS[kind].copy, 'data-tooltip': gettext('Click to copy'), onclick: () => copy(url) },
      h('span', { class: 'links-url-text', text: url }), icon('content_copy'));
  }

  function expiryText(link) {
    return link.expires_at ? interpolate(gettext('Valid until %(expires_at)s'), { expires_at: fmtDateTime(link.expires_at) }, true) : gettext('Never expires');
  }

  function shortNote(count, link) {
    if (!link.expires_at) return interpolate(ngettext('%(count)s file; the short link never expires.', '%(count)s files; the short links never expire.', count), { count }, true);
    return interpolate(ngettext('%(count)s file; the short link is valid until %(expires_at)s.', '%(count)s files; the short links are valid until %(expires_at)s.', count),
      { count, expires_at: fmtDateTime(link.expires_at) }, true);
  }

  function showLinks(items, answer, ctx) {
    const pathList = answer.path ? answer.path.links : null;
    const shortList = answer.short ? answer.short.links : null;
    const titleId = dialogId('title');
    const body = items.length === 1
      ? oneFile(items[0], pathList && pathList[0], shortList && shortList[0], (answer.short || answer.path || {}).qr)
      : severalFiles(items, pathList, shortList, ctx);
    const dialog = h('dialog', { class: `dialog ${items.length === 1 ? 'dialog-small links-one' : 'dialog-wide links-many'}`, 'aria-labelledby': titleId },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: gettext('Direct link') }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        h('div', { class: 'dialog-body' }, body.content,
          h('div', { class: 'dialog-foot' }, ...(body.actions || []), h('button', { type: 'button', class: 'btn btn-filled', 'data-close': '', text: gettext('Close') })))));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    openDialog(dialog);
  }

  function oneFile(item, pathLink, shortLink, qr) {
    const rows = [];
    if (pathLink) {
      rows.push(h('div', { class: 'links-row' },
        h('span', { class: 'label', text: gettext('Direct link'), 'data-tooltip': gettext("The direct link, with the file's full path.") }),
        linkButton(pathLink.url, 'path')));
    }
    if (shortLink) {
      rows.push(h('div', { class: 'links-row' },
        h('span', { class: 'label', text: gettext('Short link'), 'data-tooltip': gettext('A shorter link, easy to copy and pass on.') }),
        linkButton(shortLink.url, 'short'),
        h('span', { class: 'hint', text: expiryText(shortLink) })));
    }
    const base = `${item.name}-qrcode`;
    const code = qr
      ? h('figure', { class: 'links-qr' },
        h('img', { src: qr.svg, alt: gettext('QR code of the file'), width: '200', height: '200' }),
        h('figcaption', { class: 'links-qr-actions' },
          h('a', { class: 'btn btn-text btn-small', href: qr.svg, download: `${base}.svg` }, icon('download'), 'SVG'),
          h('a', { class: 'btn btn-text btn-small', href: qr.png, download: `${base}.png` }, icon('download'), 'PNG')))
      : h('p', { class: 'hint links-qr-none', text: gettext('The link is too long for a QR code.') });
    return {
      content: h('div', { class: 'links-result' },
        code,
        h('dl', { class: 'links-facts' },
          h('dt', { text: gettext('File name') }), h('dd', { class: 'links-name', text: item.name }),
          h('dt', { text: gettext('Size') }), h('dd', { text: item.size === null || item.size === undefined ? gettext('Unknown') : fmtSize(item.size) }),
          item.modified ? h('dt', { text: gettext('Modified') }) : null, item.modified ? h('dd', { text: fmtDateTime(item.modified) }) : null),
        ...rows),
    };
  }

  function severalFiles(items, pathList, shortList, ctx) {
    const columns = [];
    if (pathList) columns.push({ kind: 'path', label: KINDS.path.label, links: pathList });
    if (shortList) columns.push({ kind: 'short', label: KINDS.short.label, links: shortList });
    const head = h('tr', null, h('th', { text: gettext('File name') }), ...columns.map((column) => h('th', null,
      h('span', { class: 'links-th' }, column.label,
        h('button', { type: 'button', class: 'icon-button small', 'aria-label': KINDS[column.kind].toClipboard, 'data-tooltip': gettext('Copy all to the clipboard'),
          onclick: () => copy(column.links.map((link) => link.url).join('\n'), interpolate(KINDS[column.kind].copied(column.links.length), { count: column.links.length }, true)) }, icon('content_copy'))))));
    const rows = items.map((item, index) => h('tr', null,
      h('td', { class: 'links-name', 'data-label': gettext('File name'), text: item.name }),
      ...columns.map((column) => h('td', { 'data-label': column.label }, linkButton(column.links[index].url, column.kind)))));
    const exporter = h('button', { type: 'button', class: 'btn btn-text' }, icon('download'), gettext('Export'));
    exporter.addEventListener('click', () => exportCsv(items, columns, ctx));
    // On phones the table's head is hidden (its rows are stacked): the column buttons are here instead.
    const copyAll = columns.map((column) => h('button', { type: 'button', class: 'btn btn-text links-phone-only',
      onclick: () => copy(column.links.map((link) => link.url).join('\n'), interpolate(KINDS[column.kind].copied(column.links.length), { count: column.links.length }, true)) },
    icon('content_copy'), KINDS[column.kind].copyAll));
    return {
      content: h('div', { class: 'links-result' },
        h('p', { class: 'muted', text: shortList && shortList[0] ? shortNote(items.length, shortList[0]) : interpolate(ngettext('%(count)s file.', '%(count)s files.', items.length), { count: items.length }, true) }),
        h('div', { class: 'table-wrap' }, h('table', { class: 'data-table stack-table links-table' }, h('thead', null, head), h('tbody', null, ...rows)))),
      actions: [...copyAll, exporter],
    };
  }

  // A cell a spreadsheet won't run: text starting with = + - @ (a formula) gets a ' in front.
  function csvCell(value) {
    let text = String(value === null || value === undefined ? '' : value);
    if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function exportCsv(items, columns, ctx) {
    const lines = [[gettext('File name'), gettext('Path'), ...columns.map((column) => column.label)]];
    items.forEach((item, index) => lines.push([item.name, item.path, ...columns.map((column) => column.links[index].url)]));
    const text = '\ufeff' + lines.map((line) => line.map(csvCell).join(',')).join('\r\n') + '\r\n';
    const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const link = h('a', { href: url, download: interpolate(gettext('%(site)s direct links %(stamp)s.csv'), { site: ctx.site.site_name || 'HFile', stamp }, true), hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
})();
