/* /manage/#version: the program's version, whether the database matches it, and the upgrade log
 * (GET /api/v1/manage/version/). The upgrading itself happens when a new version starts
 * (apps/upgrades/service.py); nothing here changes anything. */
(function () {
  'use strict';
  const { api, h, fmtDateTime } = window.App;

  const facts = document.getElementById('version-facts');
  const problem = document.getElementById('version-problem');
  const rows = document.getElementById('version-rows');
  const refresh = document.getElementById('version-refresh');
  if (!facts) return;
  let loaded = false;
  let loading = null;

  const fact = (label, ...value) => h('div', {}, h('dt', { text: label }), h('dd', {}, ...value));
  const wideFact = (label, ...value) => h('div', { class: 'wide' }, h('dt', { text: label }), h('dd', {}, ...value));
  const list = (names) => names.join(gettext(', '));

  function databaseState(data) {
    if (data.newer.length) return gettext('newer than the program');
    if (data.pending.length) return interpolate(ngettext('%(count)s migration pending', '%(count)s migrations pending', data.pending.length), { count: data.pending.length }, true);
    return gettext('matches the program');
  }

  // Shown only when the database doesn't match: the running site can only be in that state
  // when it runs from the source code without upgrading.
  function problemText(data) {
    if (data.newer.length) {
      return interpolate(gettext("The database has migrations this program doesn't have: %(list)s. A newer version upgraded it, and this version may fail on it; please go back to the newer program."), { list: list(data.newer) }, true);
    }
    if (data.pending.length) {
      return interpolate(gettext('The database still needs these migrations: %(list)s. '), { list: list(data.pending) }, true)
        + (data.auto_upgrade ? gettext('Restart the program and it upgrades by itself.') : gettext('When running from source, upgrade with python manage.py upgrade (it backs up first).'));
    }
    return '';
  }

  function versionText(u) {
    if (!u.previous) return interpolate(gettext('%(version)s (first entry)'), { version: u.version }, true);
    return u.previous === u.version ? u.version : `${u.previous} → ${u.version}`;
  }

  function changes(u) {
    if (!u.migrations.length) return h('span', { class: 'muted', text: gettext('no structure changes') });
    return h('details', { class: 'migration-list' },
      h('summary', { text: interpolate(ngettext('%(count)s migration applied', '%(count)s migrations applied', u.migrations.length), { count: u.migrations.length }, true) }),
      h('ul', {}, u.migrations.map((name) => h('li', {}, h('code', { text: name })))));
  }

  // How long an upgrade that changed the database took, backup included: a small one is done in less
  // than 0.1 s. A record of a version that only started (nothing to change) has none.
  function took(u) {
    if (!u.migrations.length) return null;
    const seconds = (new Date(u.finished_at) - new Date(u.started_at)) / 1000;
    return seconds >= 0.1 ? interpolate(gettext('took %(seconds)s s'), { seconds: seconds.toFixed(1) }, true) : gettext('took under 0.1 s');
  }

  function row(u) {
    // data-label: the column's name, shown before each value when the table turns into blocks on phones.
    return h('tr', {},
      h('td', { class: 'nowrap', 'data-label': gettext('Finished at') },
        fmtDateTime(u.finished_at), took(u) ? h('div', { class: 'muted small', text: took(u) }) : null),
      h('td', { 'data-label': gettext('Version'), text: versionText(u) }),
      h('td', { 'data-label': gettext('Database') }, changes(u)),
      h('td', { 'data-label': gettext('Backup before upgrade') },
        u.backup ? h('code', { class: 'backup-path', text: u.backup }) : h('span', { class: 'muted', text: '—' })));
  }

  function render(data) {
    facts.replaceChildren(
      fact(gettext('Program version'), h('strong', { text: data.version })),
      fact(gettext('Database'), databaseState(data)),
      fact(gettext('Upgrade at startup'), data.auto_upgrade ? pgettext('setting', 'On') : (data.frozen ? gettext('Off (turned off in the settings file)') : gettext('Off (running from source)'))),
      wideFact(gettext('Backup folder'), h('code', { class: 'backup-path', text: data.backup_dir })),
      wideFact(gettext('Environment'), `Django ${data.django} · Python ${data.python} · ${data.database}`));
    const text = problemText(data);
    problem.textContent = text;
    problem.hidden = !text;
    rows.replaceChildren(...(data.upgrades.length ? data.upgrades.map(row) : [
      h('tr', { class: 'message-row' }, h('td', { colspan: '4', text: gettext('No upgrades yet. One is recorded here when the program upgrades the database at startup, or first starts on this database.') })),
    ]));
  }

  async function load() {
    refresh.disabled = true;
    try {
      render(await api('GET', '/api/v1/manage/version/'));
      loaded = true;
    } catch (err) {
      problem.className = 'notice notice-error';
      problem.textContent = interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true);
      problem.hidden = false;
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.addEventListener('click', () => {
    if (!loading) loading = load().finally(() => { loading = null; });
  });

  // Loads the first time the section is shown (manage.js announces each one).
  document.addEventListener('manage:show', (e) => {
    if (e.detail !== 'version' || loaded || loading) return;
    loading = load().finally(() => { loading = null; });
  });
})();
