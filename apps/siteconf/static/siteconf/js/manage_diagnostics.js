/* /manage/#diagnostics (诊断信息, decision D42): the request as the site sees it, from
 * GET /api/v1/manage/diagnostics/: the client IP after the [server] proxies rule, the scheme and the
 * host, against config.toml's [site] url, what looks wrong, and the request's headers with their
 * secrets hidden by the server (Cookie values, logins, tokens), to copy into a question. */
(function () {
  'use strict';
  const { api, h, toast, copyText } = window.App;

  const facts = document.getElementById('diagnostics-facts');
  if (!facts) return;
  const notesBox = document.getElementById('diagnostics-notes');
  const rows = document.getElementById('diagnostics-headers');
  const refresh = document.getElementById('diagnostics-refresh');
  const copy = document.getElementById('diagnostics-copy');
  const NOTICE = { warning: 'notice', info: 'notice notice-info', ok: 'notice notice-ok' };
  let loaded = null;
  let loading = null;

  const fact = (label, value, wide = false) => h('div', { class: wide ? 'wide' : null }, h('dt', { text: label }), h('dd', {}, value));
  const code = (text) => h('code', { text });
  const none = (text) => h('span', { class: 'muted', text });

  function render(data) {
    loaded = data;
    notesBox.replaceChildren(...data.notes.map((note) => h('p', { class: NOTICE[note.kind] || 'notice', text: note.text })));
    facts.replaceChildren(
      fact(gettext('Your IP (as the site sees it)'), data.client_ip ? code(data.client_ip) : none(gettext('unreadable'))),
      fact(gettext('Connection from'), code(data.remote_addr || '—')),
      fact('X-Forwarded-For', data.forwarded_for.length ? code(data.forwarded_for.join(', ')) : none(gettext('None'))),
      fact('[server] proxies', code(String(data.proxies))),
      fact(gettext('Address the site sees'), code(data.origin)),
      fact('[site] url', data.site_url ? code(data.site_url) : none(gettext('not set'))),
      fact('allowed_hosts', code(data.allowed_hosts.join(', ') || '—'), true),
    );
    rows.replaceChildren(...Object.entries(data.headers).map(([name, value]) => h('tr', {},
      h('td', { class: 'nowrap', 'data-label': gettext('Name') }, code(name)),
      h('td', { 'data-label': gettext('Value'), text: value }))));
  }

  async function load() {
    refresh.disabled = true;
    try {
      render(await api('GET', '/api/v1/manage/diagnostics/'));
    } catch (err) {
      notesBox.replaceChildren(h('p', { class: 'notice notice-error', text: interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true) }));
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.addEventListener('click', () => {
    if (!loading) loading = load().finally(() => { loading = null; });
  });
  copy.addEventListener('click', async () => {
    if (!loaded) return;
    const text = [
      interpolate(gettext('Your IP (as the site sees it): %(ip)s'), { ip: loaded.client_ip || gettext('unreadable') }, true),
      interpolate(gettext('Connection from: %(remote_addr)s'), { remote_addr: loaded.remote_addr }, true),
      interpolate(gettext('[server] proxies: %(proxies)s'), { proxies: loaded.proxies }, true),
      interpolate(gettext('Address the site sees: %(origin)s'), { origin: loaded.origin }, true),
      interpolate(gettext('[site] url: %(url)s'), { url: loaded.site_url || gettext('not set') }, true),
      '',
      ...Object.entries(loaded.headers).map(([name, value]) => `${name}: ${value}`),
    ].join('\n');
    toast(await copyText(text) ? gettext('Copied to the clipboard.') : gettext("Can't copy it. Please select and copy it by hand."));
  });

  document.addEventListener('manage:show', (e) => {
    if (e.detail !== 'diagnostics' || loaded || loading) return;
    loading = load().finally(() => { loading = null; });
  });
})();
