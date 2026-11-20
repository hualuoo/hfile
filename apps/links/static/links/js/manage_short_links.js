/* /manage/#short-links (短链管理): the short links (GET /api/v1/manage/short-links/) with their filters and
 * order; deleting one, the ticked ones (POST …/delete/ {"ids"}) or the expired ones with their download logs
 * (POST …/delete-expired/); 导出, a CSV of every link the filters find (…/export/); and 统计信息, the rate
 * limit's live windows (GET /api/v1/manage/link-limits/). */
(function () {
  'use strict';
  const { api, h, icon, toast, fmtNum, fmtDateTime, confirmDialog, copyText, openDialog } = window.App;

  const filters = document.getElementById('short-links-filters');
  if (!filters) return;
  const PAGE_SIZE = 50;
  const FILTERS = ['storage', 'path', 'key', 'since', 'until'];
  const rows = document.getElementById('short-links-rows');
  const summary = document.getElementById('short-links-summary');
  const pager = document.getElementById('short-links-pager');
  const checkPage = document.getElementById('short-links-check-page');
  const deleteSelected = document.getElementById('short-links-delete-selected');
  const deleteExpired = document.getElementById('short-links-delete-expired');
  const exportButton = document.getElementById('short-links-export');
  const refresh = document.getElementById('short-links-refresh');
  const limitsButton = document.getElementById('short-links-limits');

  let page = 1;
  let count = 0;
  let generation = 0;
  let loaded = false;
  let shown = [];
  const selected = new Set();

  function query({ ordering = true } = {}) {
    const params = new URLSearchParams();
    for (const name of FILTERS) {
      const value = filters.elements[name].value.trim();
      if (value) params.set(name, value);
    }
    if (ordering && filters.elements.ordering.value) params.set('ordering', filters.elements.ordering.value);
    return params;
  }
  const filtered = () => [...query({ ordering: false }).keys()].length > 0;

  function updateButtons() {
    deleteSelected.disabled = !selected.size;
    deleteSelected.replaceChildren(icon('delete'), selected.size ? interpolate(ngettext('Delete %(count)s selected', 'Delete %(count)s selected', selected.size), { count: fmtNum(selected.size) }, true) : gettext('Delete selected'));
    exportButton.disabled = !count;
    checkPage.checked = shown.length > 0 && shown.every((id) => selected.has(id));
    checkPage.indeterminate = !checkPage.checked && shown.some((id) => selected.has(id));
  }

  async function copy(text) {
    if (await copyText(text)) toast(gettext('Copied'));
    else toast(gettext('Copying failed. Please copy it by hand.'), 'error');
  }

  // A moment on two lines (the day, then the time): the table has many columns.
  function moment(value) {
    const text = fmtDateTime(value, { seconds: true });
    return h('span', { class: 'when' }, text.slice(0, 10), h('span', { class: 'when-time', text: text.slice(11) }));
  }

  function row(link) {
    const box = h('input', { type: 'checkbox', 'aria-label': interpolate(gettext('Select short link %(key)s'), { key: link.key }, true), checked: selected.has(link.id) });
    box.addEventListener('change', () => {
      if (box.checked) selected.add(link.id);
      else selected.delete(link.id);
      updateButtons();
    });
    const who = link.created_by;
    const remove = h('button', { type: 'button', class: 'icon-button small', 'aria-label': interpolate(gettext('Delete short link %(key)s'), { key: link.key }, true), 'data-tooltip': gettext('Delete') }, icon('delete'));
    remove.addEventListener('click', () => removeOne(link));
    return h('tr', null,
      h('td', { class: 'cell-check' }, box),
      h('td', { class: 'cell-storage', 'data-label': gettext('Storage source') }, h('span', { text: link.storage.name }), h('span', { class: 'muted small storage-type', text: link.storage.type_label })),
      h('td', { 'data-label': gettext('Short link key') },
        h('button', { type: 'button', class: 'btn btn-text btn-small link-key', 'data-tooltip': interpolate(gettext('Copy %(url)s'), { url: link.url }, true), text: link.key, onclick: () => copy(link.url) })),
      h('td', { class: 'cell-path', 'data-label': gettext('File path'), text: link.path }),
      h('td', { 'data-label': gettext('Created by'), text: who.nickname && who.nickname !== who.username ? interpolate(gettext('%(nickname)s (%(username)s)'), { nickname: who.nickname, username: who.username }, true) : who.username }),
      h('td', { 'data-label': gettext('Created at') }, moment(link.created_at)),
      h('td', { class: link.expired ? 'expired' : '', 'data-label': gettext('Expires at') },
        link.expires_at ? moment(link.expires_at) : gettext('Forever'),
        link.expired ? h('span', { class: 'badge badge-error', text: gettext('Expired') }) : null),
      h('td', { class: 'row-actions' },
        h('a', { class: 'icon-button small', href: link.url, target: '_blank', rel: 'noopener noreferrer', 'aria-label': interpolate(gettext('Open short link %(key)s'), { key: link.key }, true), 'data-tooltip': gettext('Open') }, icon('open_in_new')),
        remove));
  }

  const messageRow = (text, error = false) => h('tr', { class: 'message-row' },
    h('td', { colspan: '8' }, h('span', { class: error ? 'field-error' : '', text })));

  function renderPager() {
    const pages = Math.max(1, Math.ceil(count / PAGE_SIZE));
    pager.hidden = pages <= 1;
    if (pages <= 1) { pager.replaceChildren(); return; }
    pager.replaceChildren(
      h('button', { type: 'button', class: 'btn btn-small', disabled: page <= 1, onclick: () => go(page - 1) }, icon('chevron_left'), gettext('Previous page')),
      h('span', { class: 'pager-text', text: interpolate(ngettext('Page %(page)s of %(pages)s', 'Page %(page)s of %(pages)s', pages), { page, pages }, true) }),
      h('button', { type: 'button', class: 'btn btn-small pager-next', disabled: page >= pages, onclick: () => go(page + 1) }, gettext('Next page'), icon('chevron_right')));
  }

  function go(number) {
    page = number;
    selected.clear();
    load();
  }

  async function storages() {
    const select = filters.elements.storage;
    try {
      const list = await api('GET', '/api/v1/manage/storages/');
      select.append(...list.map((storage) => h('option', { value: String(storage.id), text: storage.name })));
    } catch (err) {
      // the filter just offers no storages
    }
  }

  async function load() {
    if (!loaded) storages();
    loaded = true;
    const mine = ++generation;
    const params = query();
    params.set('page', page);
    params.set('page_size', PAGE_SIZE);
    summary.textContent = gettext('Loading…');
    refresh.disabled = true;
    try {
      const data = await api('GET', `/api/v1/manage/short-links/?${params}`);
      if (mine !== generation) return;
      count = data.count;
      shown = data.results.map((link) => link.id);
      summary.textContent = interpolate(filtered() ? ngettext('%(count)s found', '%(count)s found', count) : ngettext('%(count)s entry', '%(count)s entries', count), { count: fmtNum(count) }, true);
      rows.replaceChildren(...(data.results.length ? data.results.map(row)
        : [messageRow(filtered() ? gettext('No short links match.') : gettext('No short links yet.'))]));
      renderPager();
    } catch (err) {
      if (mine !== generation) return;
      if (err.status === 404 && page > 1) { go(1); return; }
      count = 0;
      shown = [];
      summary.textContent = '';
      rows.replaceChildren(messageRow(interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true), true));
      pager.hidden = true;
    } finally {
      if (mine === generation) {
        refresh.disabled = false;
        updateButtons();
      }
    }
  }

  async function removeOne(link) {
    const ok = await confirmDialog({ title: interpolate(gettext('Delete short link %(key)s?'), { key: link.key }, true), message: gettext('Once deleted, this short link stops working. Its download log entries are kept.'), confirmText: gettext('Delete'), danger: true });
    if (!ok) return;
    try {
      await api('DELETE', `/api/v1/manage/short-links/${link.id}/`);
      toast(gettext('Deleted'));
      selected.delete(link.id);
      load();
    } catch (err) {
      toast(err.message, 'error');
      if (err.status === 404) load();
    }
  }

  let typingTimer = null;
  const reload = () => {
    clearTimeout(typingTimer);
    go(1);
  };
  filters.addEventListener('input', (e) => {
    if (e.target.type !== 'search') return;
    clearTimeout(typingTimer);
    typingTimer = setTimeout(reload, 400);
  });
  filters.addEventListener('change', (e) => { if (e.target.type !== 'search') reload(); });
  filters.addEventListener('submit', (e) => {
    e.preventDefault();
    reload();
  });
  filters.addEventListener('reset', () => setTimeout(reload));
  refresh.addEventListener('click', () => load());

  checkPage.addEventListener('change', () => {
    for (const id of shown) {
      if (checkPage.checked) selected.add(id);
      else selected.delete(id);
    }
    rows.querySelectorAll('.cell-check input').forEach((box) => { box.checked = checkPage.checked; });
    updateButtons();
  });

  deleteSelected.addEventListener('click', async () => {
    const ids = [...selected];
    if (!ids.length) { toast(gettext('Please select at least one entry'), 'error'); return; }
    const ok = await confirmDialog({ title: interpolate(ngettext('Delete the %(count)s selected short link?', 'Delete the %(count)s selected short links?', ids.length), { count: fmtNum(ids.length) }, true), message: gettext('Once deleted, these short links stop working. Their download log entries are kept.'), confirmText: gettext('Delete'), danger: true });
    if (!ok) return;
    try {
      const done = await api('POST', '/api/v1/manage/short-links/delete/', { ids });
      toast(interpolate(ngettext('Deleted %(deleted)s short link.', 'Deleted %(deleted)s short links.', done.deleted), { deleted: fmtNum(done.deleted) }, true));
      selected.clear();
      load();
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  deleteExpired.addEventListener('click', async () => {
    const ok = await confirmDialog({ title: gettext('Delete expired short links?'), message: gettext("Every expired short link is deleted, along with its download log entries. This can't be undone."), confirmText: gettext('Delete'), danger: true });
    if (!ok) return;
    try {
      const done = await api('POST', '/api/v1/manage/short-links/delete-expired/');
      toast(done.deleted ? interpolate(ngettext('Deleted %(deleted)s short link (and %(logs)s download log entries).', 'Deleted %(deleted)s short links (and %(logs)s download log entries).', done.deleted), { deleted: fmtNum(done.deleted), logs: fmtNum(done.logs) }, true) : gettext('There are no expired short links.'));
      selected.clear();
      go(1);
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  // 导出: the browser downloads the file the server makes (every matching link, not just this page).
  exportButton.addEventListener('click', () => {
    const link = h('a', { href: `/api/v1/manage/short-links/export/?${query({ ordering: false })}`, download: '', hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
  });

  // 统计信息: the rate limit's windows now.
  limitsButton.addEventListener('click', async () => {
    let data;
    try {
      data = await api('GET', '/api/v1/manage/link-limits/');
    } catch (err) {
      toast(err.message, 'error');
      return;
    }
    const titleId = `links-limits-${Math.random().toString(36).slice(2)}`;
    const off = !data.seconds || !data.downloads;
    const text = off ? gettext('Downloads aren\'t rate-limited now (the seconds or the count in "Direct and short links" is 0).')
      : interpolate(ngettext('Each IP may download %(downloads)s time per %(seconds)s s. Counters now: %(count)s (those older than %(seconds)s s are cleared).', 'Each IP may download %(downloads)s times per %(seconds)s s. Counters now: %(count)s (those older than %(seconds)s s are cleared).', data.downloads), { seconds: fmtNum(data.seconds), downloads: fmtNum(data.downloads), count: fmtNum(data.count) }, true);
    let content;
    if (off) content = null;
    else if (!data.windows.length) content = h('p', { class: 'muted', text: interpolate(ngettext('No downloads in the last %(seconds)s second.', 'No downloads in the last %(seconds)s seconds.', data.seconds), { seconds: fmtNum(data.seconds) }, true) });
    else {
      content = h('div', { class: 'table-wrap' }, h('table', { class: 'data-table links-limits-table' },
        h('thead', null, h('tr', null, h('th', { text: 'IP' }), h('th', { class: 'numeric', text: gettext('Downloads') }), h('th', { text: gettext('Window') }), h('th', { text: gettext('Expires at') }))),
        h('tbody', null, ...data.windows.map((item) => h('tr', { class: item.count > data.downloads ? 'selected' : '' },
          h('td', null, h('code', { class: 'ip', text: item.address })),
          h('td', { class: 'numeric', text: fmtNum(item.count) }),
          h('td', { text: interpolate(gettext('From %(started_at)s'), { started_at: fmtDateTime(item.started_at, { seconds: true }) }, true) }),
          h('td', { text: fmtDateTime(item.expires_at, { seconds: true }) }))))));
    }
    const more = data.count > data.windows.length ? h('p', { class: 'hint', text: interpolate(ngettext('Only the top %(count)s is listed.', 'Only the top %(count)s are listed.', data.windows.length), { count: fmtNum(data.windows.length) }, true) }) : null;
    const dialog = h('dialog', { class: 'dialog dialog-wide', 'aria-labelledby': titleId },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: gettext('Statistics') }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        h('div', { class: 'dialog-body' }, h('p', { class: 'links-limits-summary', text }), content, more,
          h('p', { class: 'hint', text: gettext("The counters are kept in the memory of this site's process and start from zero when the site restarts.") }),
          h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-filled', 'data-close': '', text: gettext('Close') })))));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    openDialog(dialog);
  });

  document.addEventListener('manage:show', (e) => {
    if (e.detail === 'short-links' && !loaded) load();
  });
})();
