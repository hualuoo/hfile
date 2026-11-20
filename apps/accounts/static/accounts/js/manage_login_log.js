/* /manage/#login-log: the login log (GET /api/v1/manage/login-logs/) with its filters, and deleting the
 * entries ticked on the page (POST …/delete/ {"ids"}) or every entry the filters find (DELETE with the
 * same filters). */
(function () {
  'use strict';
  const { api, h, icon, toast, fmtNum, fmtDateTime, confirmDialog } = window.App;

  const filters = document.getElementById('login-log-filters');
  if (!filters) return;
  const PAGE_SIZE = 50;
  const FILTERS = ['username', 'ip', 'result', 'since', 'until'];
  const rows = document.getElementById('login-log-rows');
  const summary = document.getElementById('login-log-summary');
  const pager = document.getElementById('login-log-pager');
  const checkPage = document.getElementById('login-log-check-page');
  const deleteSelected = document.getElementById('login-log-delete-selected');
  const deleteAll = document.getElementById('login-log-delete-all');
  const refresh = document.getElementById('login-log-refresh');

  let page = 1;
  let count = 0;
  let generation = 0; // a late answer to an older query is dropped
  let loaded = false;
  let shown = []; // the ids on the page
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

  function setLabel(button, text) {
    button.replaceChildren(icon('delete'), text);
  }

  function updateButtons() {
    deleteSelected.disabled = !selected.size;
    setLabel(deleteSelected, selected.size ? interpolate(ngettext('Delete %(count)s selected', 'Delete %(count)s selected', selected.size), { count: fmtNum(selected.size) }, true) : gettext('Delete selected'));
    deleteAll.disabled = !count || !listed;
    const filtered = listed ? [...listed.params.keys()].length > 0 : false;
    setLabel(deleteAll, count ? interpolate(filtered ? ngettext('Delete the %(count)s found', 'Delete all %(count)s found', count) : ngettext('Delete the %(count)s entry', 'Delete all %(count)s entries', count), { count: fmtNum(count) }, true) : gettext('Delete all found'));
    checkPage.checked = shown.length > 0 && shown.every((id) => selected.has(id));
    checkPage.indeterminate = !checkPage.checked && shown.some((id) => selected.has(id));
  }

  function row(entry) {
    const when = fmtDateTime(entry.created_at, { seconds: true });
    const box = h('input', { type: 'checkbox', 'aria-label': interpolate(gettext('Select the entry from %(when)s'), { when }, true), checked: selected.has(entry.id) });
    box.addEventListener('change', () => {
      if (box.checked) selected.add(entry.id);
      else selected.delete(entry.id);
      updateButtons();
    });
    const ok = entry.result === 'success';
    const matched = entry.user && entry.user.username !== entry.username;
    return h('tr', { class: ok ? '' : 'login-failed' },
      h('td', { class: 'cell-check' }, box),
      h('td', { class: 'nowrap', 'data-label': gettext('Time'), text: when }),
      h('td', { 'data-label': gettext('Username') },
        entry.username ? h('span', { class: 'typed-name', text: entry.username }) : h('span', { class: 'muted', text: gettext('(empty)') }),
        matched ? h('span', { class: 'muted small', text: ` → ${entry.user.username}` }) : null,
        entry.source && entry.source !== 'site' ? h('span', { class: 'badge source-badge', text: entry.source_label }) : null),
      h('td', { 'data-label': gettext('Result') },
        h('span', { class: `badge ${ok ? 'badge-ok' : 'badge-error'}`, text: entry.result_label }),
        ok ? null : h('span', { class: 'muted small login-reason', text: entry.reason_label })),
      h('td', { 'data-label': 'IP' }, entry.ip ? h('span', { class: 'ip-line' }, h('code', { class: 'ip', text: entry.ip }),
        entry.ip_location ? h('span', { class: 'ip-place', text: entry.ip_location }) : null) : h('span', { class: 'muted', text: '—' })),
      h('td', { class: 'cell-ua', 'data-label': gettext('Browser') },
        entry.user_agent ? h('span', { class: 'ua', 'data-tooltip': entry.user_agent, text: entry.user_agent }) : h('span', { class: 'muted', text: '—' })));
  }

  const messageRow = (text, error = false) => h('tr', { class: 'message-row' },
    h('td', { colspan: '6' }, h('span', { class: error ? 'field-error' : '', text })));

  function renderPager() {
    const pages = Math.max(1, Math.ceil(count / PAGE_SIZE));
    pager.hidden = pages <= 1;
    if (pages <= 1) {
      pager.replaceChildren();
      return;
    }
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

  async function load() {
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
      const data = await api('GET', `/api/v1/manage/login-logs/?${asked}`);
      if (mine !== generation) return;
      count = data.count;
      listed = { params, newest: data.newest };
      shown = data.results.map((entry) => entry.id);
      summary.textContent = interpolate(filtered ? ngettext('%(count)s found', '%(count)s found', count) : ngettext('%(count)s entry', '%(count)s entries', count), { count: fmtNum(count) }, true);
      rows.replaceChildren(...(data.results.length ? data.results.map(row)
        : [messageRow(filtered ? gettext('Nothing matches.') : gettext('No logins logged yet.'))]));
      renderPager();
    } catch (err) {
      if (mine !== generation) return;
      if (err.status === 404 && page > 1) { // that page is gone (entries were deleted meanwhile)
        go(1);
        return;
      }
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
  filters.addEventListener('reset', () => setTimeout(reload)); // after the fields are cleared
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
    if (!ids.length) return;
    const ok = await confirmDialog({ title: interpolate(ngettext('Delete the %(count)s selected login log entry?', 'Delete the %(count)s selected login log entries?', ids.length), { count: fmtNum(ids.length) }, true), message: gettext("This can't be undone."), confirmText: gettext('Delete'), danger: true });
    if (!ok) return;
    try {
      const done = await api('POST', '/api/v1/manage/login-logs/delete/', { ids });
      toast(interpolate(ngettext('Deleted %(deleted)s entry.', 'Deleted %(deleted)s entries.', done.deleted), { deleted: fmtNum(done.deleted) }, true));
      selected.clear();
      load();
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  // What the list on screen shows, up to its newest entry (API-10): not what the filters say now, nor
  // what was logged after the list was loaded.
  deleteAll.addEventListener('click', async () => {
    if (!listed) return;
    const filtered = [...listed.params.keys()].length > 0;
    const ok = await confirmDialog({
      title: filtered ? interpolate(ngettext('Delete the %(count)s login log entry found?', 'Delete the %(count)s login log entries found?', count), { count: fmtNum(count) }, true) : interpolate(ngettext('Delete the whole login log (%(count)s entry)?', 'Delete the whole login log (%(count)s entries)?', count), { count: fmtNum(count) }, true),
      message: filtered ? gettext("Everything the current filters find is deleted, not only this page; entries logged after the list loaded are kept. This can't be undone.")
        : gettext("No filters are set: every entry in the list is deleted (except those logged after it loaded). This can't be undone."),
      confirmText: filtered ? gettext('Delete all') : pgettext('the whole list', 'Delete all'),
      danger: true,
    });
    if (!ok) return;
    const params = new URLSearchParams(listed.params);
    if (listed.newest) params.set('newest', listed.newest);
    try {
      const done = await api('DELETE', `/api/v1/manage/login-logs/?${params}`);
      toast(interpolate(ngettext('Deleted %(deleted)s entry.', 'Deleted %(deleted)s entries.', done.deleted), { deleted: fmtNum(done.deleted) }, true));
      go(1);
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  // Loads the first time the section is shown (manage.js announces each one).
  document.addEventListener('manage:show', (e) => {
    if (e.detail === 'login-log' && !loaded) load();
  });
  // IP 归属地 was turned on or off, or locations filled in (ipgeo/js/manage_ip_geo.js): loaded again when next shown.
  document.addEventListener('hfile:ip-locations', () => { loaded = false; });
})();
