/* /manage/#activity: the 操作日志 (GET /api/v1/manage/activity/), newest first, with its filters. A user or an IP in
 * the list narrows it down to theirs (the chips above the list); so does a row's 操作日志 button in the user list
 * (window.App.showActivity). Nothing here changes or deletes entries: nothing can. */
(function () {
  'use strict';
  const { api, h, icon, fmtNum, fmtDateTime } = window.App;

  const filters = document.getElementById('activity-filters');
  if (!filters) return;
  const PAGE_SIZE = 50;
  const COLUMNS = 7;
  // Red for failures and what is gone; green for what is new or turned on; blue for the other admin changes.
  const BADGES = {
    login_failed: 'badge-error', delete: 'badge-error', delete_share: 'badge-error', clear_shares: 'badge-error',
    delete_user: 'badge-error', delete_storage: 'badge-error', disable_storage: 'badge-error', delete_provider: 'badge-error',
    delete_short_links: 'badge-error', delete_download_logs: 'badge-error', totp_off: 'badge-warning', unlink_sso: 'badge-warning',
    unlink_identity: 'badge-warning', rotate_link_secret: 'badge-warning', reset_admin: 'badge-warning',
    upload: 'badge-ok', mkdir: 'badge-ok', create_share: 'badge-ok', short_link: 'badge-ok', create_user: 'badge-ok',
    copy_user: 'badge-ok', create_storage: 'badge-ok', copy_storage: 'badge-ok', enable_storage: 'badge-ok',
    create_provider: 'badge-ok', sso_register: 'badge-ok', totp_on: 'badge-ok', install: 'badge-ok',
  };
  const ADMIN = new Set(['users', 'storages', 'site']); // the categories of admins' changes: blue unless above
  const rows = document.getElementById('activity-rows');
  const summary = document.getElementById('activity-summary');
  const pager = document.getElementById('activity-pager');
  const chips = document.getElementById('activity-chips');
  const refresh = document.getElementById('activity-refresh');

  let page = 1;
  let count = 0;
  let generation = 0; // a late answer to an older query is dropped
  let storagesAsked = false;
  // Set by clicking in the list, not typed: { user: {id, username}, ip: '…' }.
  let only = {};

  function query() {
    const f = filters.elements;
    const params = new URLSearchParams();
    const search = f.search.value.trim();
    if (search) params.set('search', search);
    const kind = f.kind.value;
    if (kind.startsWith('category:')) params.set('category', kind.slice('category:'.length));
    else if (kind) params.set('kind', kind);
    for (const name of ['storage', 'since', 'until']) {
      if (f[name].value) params.set(name, f[name].value);
    }
    if (only.user) params.set('user', only.user.id);
    if (only.ip) params.set('ip', only.ip);
    return params;
  }

  // ------------------------------------------------------------ one entry

  const dash = () => h('span', { class: 'muted', text: '—' });

  // A moment on two lines (the day, then the time): the table has many columns.
  function moment(value) {
    const text = fmtDateTime(value, { seconds: true });
    return h('span', { class: 'when' }, text.slice(0, 10), h('span', { class: 'when-time', text: text.slice(11) }));
  }

  // A user: a button that shows only their entries, or just the name when no account is there (deleted since;
  // for a failed login, maybe a name nobody had).
  function person(user, gone = gettext('This account has been deleted')) {
    const name = user.kind === 'guest' ? user.nickname : user.username;
    const label = h('span', { class: 'person-name', text: name },
      user.nickname && user.kind !== 'guest' ? h('span', { class: 'muted small', text: ` ${user.nickname}` }) : null);
    if (!user.id) return h('span', { class: 'person gone', 'data-tooltip': gone }, label);
    return h('button', {
      type: 'button', class: 'person', 'data-tooltip': interpolate(gettext("Show only %(name)s's entries"), { name }, true),
      onclick: () => setOnly('user', { id: user.id, username: name }),
    }, label);
  }

  function actorCell(entry) {
    if (entry.actor) return person(entry.actor);
    if (entry.kind === 'login_failed') return h('span', { class: 'muted', text: gettext('Not logged in') });
    return h('span', { class: 'muted', 'data-tooltip': gettext('A command on the server, or OnlyOffice saving'), text: pgettext('activity log', 'System') });
  }

  // What the entry was about: a user, a storage source and a path in it, a share's or short link's key, a page of /manage/.
  function aboutCell(entry) {
    const parts = [];
    if (entry.target && !(entry.actor && entry.actor.id && entry.actor.id === entry.target.id)) {
      parts.push(person(entry.target, entry.kind === 'login_failed' ? gettext('No account has this name now') : undefined));
    }
    if (entry.storage) {
      parts.push(entry.storage.id
        ? h('a', { class: 'storage-name', href: `#storages/${entry.storage.id}`, 'data-tooltip': gettext('Open the storage source'), text: entry.storage.name })
        : h('span', { class: 'storage-name gone', 'data-tooltip': gettext('This storage source has been deleted'), text: entry.storage.name }));
    }
    if (entry.path) {
      const folder = (entry.kind === 'mkdir' || (entry.detail && entry.detail.folder)) && entry.path !== '/';
      parts.push(h('code', { class: 'activity-path', text: folder ? `${entry.path}/` : entry.path }));
    }
    if (entry.key) parts.push(h('code', { class: 'link-key', text: entry.key }));
    if (entry.page) parts.push(h('a', { href: `#${entry.page.section}`, text: entry.page.label }));
    return parts.length ? h('div', { class: 'activity-about' }, parts) : dash();
  }

  function ipCell(entry) {
    if (!entry.ip) return dash();
    return h('span', { class: 'ip-line' },
      h('button', { type: 'button', class: 'ip-button', 'data-tooltip': gettext('Show only the entries from this IP'), onclick: () => setOnly('ip', entry.ip) },
        h('code', { class: 'ip', text: entry.ip })),
      entry.ip_location ? h('span', { class: 'ip-place', text: entry.ip_location }) : null);
  }

  function row(entry) {
    const badge = BADGES[entry.kind] || (ADMIN.has(entry.category) ? 'badge-info' : '');
    return h('tr', { class: entry.kind === 'login_failed' ? 'login-failed' : '' },
      h('td', { 'data-label': gettext('Time') }, moment(entry.created_at)),
      h('td', { class: 'cell-user', 'data-label': gettext('User') }, actorCell(entry)),
      h('td', { class: 'cell-kind', 'data-label': gettext('Action') }, h('span', { class: `badge ${badge}`.trim(), text: entry.kind_label })),
      h('td', { class: 'cell-about', 'data-label': gettext('About') }, aboutCell(entry)),
      h('td', { class: 'cell-detail', 'data-label': gettext('Details') }, entry.detail_text || dash()),
      h('td', { class: 'cell-ip', 'data-label': 'IP' }, ipCell(entry)),
      h('td', { class: 'cell-ua', 'data-label': gettext('Browser') },
        entry.user_agent ? h('span', { class: 'ellipsis', 'data-tooltip': entry.user_agent, text: entry.user_agent }) : dash()));
  }

  const messageRow = (text, error = false) => h('tr', { class: 'message-row' },
    h('td', { colspan: String(COLUMNS) }, h('span', { class: error ? 'field-error' : '', text })));

  // ------------------------------------------------------------ the list

  function renderChips() {
    const items = [];
    if (only.user) items.push(['user', interpolate(gettext('User %(name)s'), { name: only.user.username }, true)]);
    if (only.ip) items.push(['ip', interpolate(gettext('IP %(ip)s'), { ip: only.ip }, true)]);
    chips.hidden = !items.length;
    chips.replaceChildren(...items.map(([name, text]) => h('span', { class: 'chip chip-input' }, text,
      h('button', { type: 'button', class: 'chip-remove', 'aria-label': interpolate(gettext('Remove the filter "%(filter)s"'), { filter: text }, true), onclick: () => setOnly(name, null) }, icon('close')))));
  }

  function setOnly(name, value) {
    only = { ...only, [name]: value };
    go(1);
  }

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
    load();
  }

  async function storages() {
    storagesAsked = true;
    const select = filters.elements.storage;
    try {
      const list = await api('GET', '/api/v1/manage/storages/');
      select.append(...list.map((storage) => h('option', { value: String(storage.id), text: storage.name })));
    } catch (err) {
      // the filter just offers no storage sources
    }
  }

  async function load() {
    if (!storagesAsked) storages();
    renderChips();
    const mine = ++generation;
    const params = query();
    const filtered = [...params.keys()].length > 0;
    const asked = new URLSearchParams(params);
    asked.set('page', page);
    asked.set('page_size', PAGE_SIZE);
    summary.textContent = gettext('Loading…');
    refresh.disabled = true;
    try {
      const data = await api('GET', `/api/v1/manage/activity/?${asked}`);
      if (mine !== generation) return;
      count = data.count;
      summary.textContent = interpolate(filtered ? ngettext('%(count)s found', '%(count)s found', count) : ngettext('%(count)s entry', '%(count)s entries', count), { count: fmtNum(count) }, true);
      rows.replaceChildren(...(data.results.length ? data.results.map(row)
        : [messageRow(filtered ? gettext('Nothing matches.') : gettext('Nothing logged yet.'))]));
      renderPager();
    } catch (err) {
      if (mine !== generation) return;
      if (err.status === 404 && page > 1) { // that page is gone
        go(1);
        return;
      }
      count = 0;
      summary.textContent = '';
      rows.replaceChildren(messageRow(interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true), true));
      pager.hidden = true;
    } finally {
      if (mine === generation) refresh.disabled = false;
    }
  }

  // The two dates can't cross.
  function linkDates() {
    const f = filters.elements;
    f.until.min = f.since.value;
    f.since.max = f.until.value;
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
  filters.addEventListener('change', (e) => {
    if (e.target.type === 'search') return;
    if (e.target.type === 'date') linkDates();
    reload();
  });
  filters.addEventListener('submit', (e) => {
    e.preventDefault();
    reload();
  });
  filters.addEventListener('reset', () => setTimeout(() => { // after the fields are cleared
    only = {};
    linkDates();
    reload();
  }));
  refresh.addEventListener('click', () => load());

  // From elsewhere on the page (操作记录 in the user list): everything about one user, with the other filters
  // cleared, so that nothing hides part of it.
  window.App.showActivity = function (wanted) {
    for (const field of filters.elements) {
      if (field.name) field.value = '';
    }
    linkDates();
    only = { ...wanted };
    page = 1;
    if (location.hash === '#activity') load();
    else location.hash = '#activity'; // manage.js shows the section, which loads it
  };

  // Loads each time the section is shown (manage.js announces each one): the newest entries first.
  document.addEventListener('manage:show', (e) => {
    if (e.detail === 'activity') load();
  });
})();
