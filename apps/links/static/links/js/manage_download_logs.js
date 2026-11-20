/* /manage/#download-logs (下载日志): downloads through path links and short links, and those from the 下载
 * button through the site (页面下载; GET /api/v1/manage/download-logs/) with their filters and who was logged
 * in; deleting the ticked ones (POST …/delete/ {"ids"}) or
 * exactly what the list shows: DELETE with the filters of the list on screen and the newest id it counted,
 * so that what was typed in a filter but not searched yet, or downloads logged since, aren't deleted. */
(function () {
  'use strict';
  const { api, h, icon, toast, fmtNum, fmtDateTime, confirmDialog, copyText } = window.App;

  const filters = document.getElementById('download-logs-filters');
  if (!filters) return;
  const PAGE_SIZE = 50;
  const FILTERS = ['storage', 'kind', 'key', 'path', 'user', 'ip', 'user_agent', 'referer', 'since', 'until'];
  const BADGES = { short: 'badge-info', page: 'badge-ok' };
  const rows = document.getElementById('download-logs-rows');
  const summary = document.getElementById('download-logs-summary');
  const pager = document.getElementById('download-logs-pager');
  const checkPage = document.getElementById('download-logs-check-page');
  const deleteSelected = document.getElementById('download-logs-delete-selected');
  const deleteAll = document.getElementById('download-logs-delete-all');
  const refresh = document.getElementById('download-logs-refresh');

  let page = 1;
  let count = 0;
  let generation = 0;
  let loaded = false;
  let shown = [];
  let listed = null; // {params, newest}: the query the list on screen answers
  const selected = new Set();

  function query() {
    const params = new URLSearchParams();
    for (const name of FILTERS) {
      const value = filters.elements[name].value.trim();
      if (value) params.set(name, value);
    }
    return params;
  }

  function updateButtons() {
    deleteSelected.disabled = !selected.size;
    deleteSelected.replaceChildren(icon('delete'), selected.size ? interpolate(ngettext('Delete %(count)s selected', 'Delete %(count)s selected', selected.size), { count: fmtNum(selected.size) }, true) : gettext('Delete selected'));
    deleteAll.disabled = !count || !listed;
    const filtered = listed && [...listed.params.keys()].length > 0;
    deleteAll.replaceChildren(icon('delete'), count ? interpolate(filtered ? ngettext('Delete the %(count)s found', 'Delete all %(count)s found', count) : ngettext('Delete the %(count)s entry', 'Delete all %(count)s entries', count), { count: fmtNum(count) }, true) : gettext('Delete all found'));
    checkPage.checked = shown.length > 0 && shown.every((id) => selected.has(id));
    checkPage.indeterminate = !checkPage.checked && shown.some((id) => selected.has(id));
  }

  async function copy(text) {
    if (await copyText(text)) toast(gettext('Copied'));
    else toast(gettext('Copying failed. Please copy it by hand.'), 'error');
  }

  const dash = () => h('span', { class: 'muted', text: '—' });
  // A moment on two lines (the day, then the time): the table has many columns.
  function moment(value) {
    const text = fmtDateTime(value, { seconds: true });
    return h('span', { class: 'when' }, text.slice(0, 10), h('span', { class: 'when-time', text: text.slice(11) }));
  }

  function row(entry) {
    const when = fmtDateTime(entry.created_at, { seconds: true });
    const box = h('input', { type: 'checkbox', 'aria-label': interpolate(gettext('Select the entry from %(when)s'), { when }, true), checked: selected.has(entry.id) });
    box.addEventListener('change', () => {
      if (box.checked) selected.add(entry.id);
      else selected.delete(entry.id);
      updateButtons();
    });
    const short = entry.kind === 'short';
    // A page download has no link to give again: the site's signed one isn't kept.
    const actions = entry.url ? [
      h('button', { type: 'button', class: 'icon-button small', 'aria-label': gettext('Copy download link'), 'data-tooltip': interpolate(gettext('Copy %(url)s'), { url: entry.url }, true), onclick: () => copy(entry.url) }, icon('content_copy')),
      h('a', { class: 'icon-button small', href: entry.url, target: '_blank', rel: 'noopener noreferrer', 'aria-label': gettext('Open the download link'), 'data-tooltip': gettext('Open') }, icon('open_in_new')),
    ] : [];
    return h('tr', null,
      h('td', { class: 'cell-check' }, box),
      h('td', { 'data-label': gettext('Downloaded at') }, moment(entry.created_at)),
      h('td', { class: 'cell-storage', 'data-label': gettext('Storage source') }, h('span', { text: entry.storage.name }), h('span', { class: 'muted small storage-type', text: entry.storage.type_label })),
      h('td', { class: 'cell-kind', 'data-label': gettext('Type') }, h('span', { class: `badge ${BADGES[entry.kind] || ''}`, text: entry.kind_label }),
        short ? h('code', { class: 'link-key', text: ` ${entry.short_key}` }) : null),
      h('td', { class: 'cell-path', 'data-label': gettext('File path'), text: entry.path }),
      h('td', { class: 'cell-user', 'data-label': gettext('User') }, entry.username ? h('span', { text: entry.username }) : dash()),
      h('td', { 'data-label': 'IP' }, entry.ip ? h('code', { class: 'ip', text: entry.ip }) : dash()),
      h('td', { class: 'cell-ua', 'data-label': gettext('Browser and referer') },
        h('span', { class: 'ellipsis', 'data-tooltip': entry.user_agent || null, text: entry.user_agent ? interpolate(gettext('UA: %(user_agent)s'), { user_agent: entry.user_agent }, true) : gettext('UA: —') }),
        h('span', { class: 'ellipsis', 'data-tooltip': entry.referer || null, text: entry.referer ? interpolate(gettext('Referer: %(referer)s'), { referer: entry.referer }, true) : gettext('Referer: —') })),
      h('td', { class: 'row-actions' }, ...actions));
  }

  const messageRow = (text, error = false) => h('tr', { class: 'message-row' },
    h('td', { colspan: '9' }, h('span', { class: error ? 'field-error' : '', text })));

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
    const filtered = [...params.keys()].length > 0;
    const asked = new URLSearchParams(params);
    asked.set('page', page);
    asked.set('page_size', PAGE_SIZE);
    summary.textContent = gettext('Loading…');
    refresh.disabled = true;
    try {
      const data = await api('GET', `/api/v1/manage/download-logs/?${asked}`);
      if (mine !== generation) return;
      count = data.count;
      listed = { params, newest: data.newest };
      shown = data.results.map((entry) => entry.id);
      summary.textContent = interpolate(filtered ? ngettext('%(count)s found', '%(count)s found', count) : ngettext('%(count)s entry', '%(count)s entries', count), { count: fmtNum(count) }, true);
      rows.replaceChildren(...(data.results.length ? data.results.map(row)
        : [messageRow(filtered ? gettext('Nothing matches.') : gettext('No downloads logged yet.'))]));
      renderPager();
    } catch (err) {
      if (mine !== generation) return;
      if (err.status === 404 && page > 1) { go(1); return; }
      count = 0;
      shown = [];
      listed = null;
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
    const ok = await confirmDialog({ title: interpolate(ngettext('Delete the %(count)s selected download log entry?', 'Delete the %(count)s selected download log entries?', ids.length), { count: fmtNum(ids.length) }, true), message: gettext("This can't be undone."), confirmText: gettext('Delete'), danger: true });
    if (!ok) return;
    try {
      const done = await api('POST', '/api/v1/manage/download-logs/delete/', { ids });
      toast(interpolate(ngettext('Deleted %(deleted)s entry.', 'Deleted %(deleted)s entries.', done.deleted), { deleted: fmtNum(done.deleted) }, true));
      selected.clear();
      load();
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  deleteAll.addEventListener('click', async () => {
    if (!listed) return;
    const filtered = [...listed.params.keys()].length > 0;
    const ok = await confirmDialog({
      title: filtered ? interpolate(ngettext('Delete the %(count)s download log entry these filters find?', 'Delete the %(count)s download log entries these filters find?', count), { count: fmtNum(count) }, true) : interpolate(ngettext('Delete the whole download log (%(count)s entry)?', 'Delete the whole download log (%(count)s entries)?', count), { count: fmtNum(count) }, true),
      message: filtered ? gettext("Everything the current filters find is deleted, not only this page; entries logged after the list loaded are kept. This can't be undone.")
        : pgettext('download log', "No filters are set: every entry in the list is deleted (except those logged after it loaded). This can't be undone."),
      confirmText: filtered ? gettext('Delete all') : pgettext('the whole list', 'Delete all'),
      danger: true,
    });
    if (!ok) return;
    const params = new URLSearchParams(listed.params);
    if (listed.newest) params.set('newest', listed.newest);
    try {
      const done = await api('DELETE', `/api/v1/manage/download-logs/?${params}`);
      toast(interpolate(ngettext('Deleted %(deleted)s entry.', 'Deleted %(deleted)s entries.', done.deleted), { deleted: fmtNum(done.deleted) }, true));
      go(1);
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  document.addEventListener('manage:show', (e) => {
    if (e.detail === 'download-logs' && !loaded) load();
  });
})();
