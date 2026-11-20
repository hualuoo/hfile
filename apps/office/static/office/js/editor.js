/* The OnlyOffice editor's page (apps/office/views.py editor_page). It asks for the editor config of the file
 * the address names (POST /api/v1/office/config/, with this page's CSRF token), loads the Document
 * Server's api.js (the page's CSP lets in that origin and nothing else) and opens the editor in place
 * of #office-editor. When something fails, a message takes the editor's place: the server's (no
 * permission, not found...), or "调用在线文档服务失败" when api.js doesn't load. */
(function () {
  'use strict';
  const App = window.App;
  const { api, h, icon } = App;
  const LOAD_FAILED = gettext("Couldn't reach the online document server. Please check that it is working.");
  const SCRIPT_TIMEOUT = 30000; // ms: a Document Server that doesn't answer at all

  const main = document.getElementById('office');
  const state = document.getElementById('office-state');
  let page = {};
  try {
    page = JSON.parse(main.dataset.config || '{}');
  } catch (e) {
    page = {};
  }

  function show({ text, retry = false, login = false }) {
    const actions = [];
    if (login) {
      const next = encodeURIComponent(location.pathname + location.search);
      actions.push(h('a', { class: 'btn btn-filled', href: `/login/?next=${next}` }, icon('login'), gettext('Log in')));
    }
    if (retry) actions.push(h('button', { type: 'button', class: 'btn btn-filled', onclick: () => location.reload() }, icon('refresh'), gettext('Retry')));
    if (page.back) actions.push(h('a', { class: 'btn btn-tonal', href: page.back }, icon('arrow_back'), gettext('Back to the folder')));
    state.replaceChildren(h('div', { class: 'office-message', role: 'alert' },
      icon('error'),
      h('p', { class: 'office-message-text', text }),
      actions.length ? h('p', { class: 'btn-row' }, ...actions) : null));
    state.hidden = false;
  }

  // The Document Server's script, from its origin (the only one this page's CSP adds to script-src).
  function loadScript(url) {
    return new Promise((resolve, reject) => {
      const tag = document.createElement('script');
      const timer = setTimeout(() => {
        tag.remove();
        reject(new Error('timeout'));
      }, SCRIPT_TIMEOUT);
      tag.addEventListener('load', () => {
        clearTimeout(timer);
        resolve();
      });
      tag.addEventListener('error', () => {
        clearTimeout(timer);
        tag.remove();
        reject(new Error('error'));
      });
      tag.src = url;
      document.head.append(tag);
    });
  }

  async function start() {
    if (page.error || !page.request || !page.api_url) return; // the page says why already
    // OnlyOffice's layout for phones and its dark theme go into the signed config, so they are asked for.
    const theme = document.documentElement.dataset.theme;
    const dark = theme ? theme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    const mobile = window.matchMedia('(max-width: 767px)').matches;
    let config;
    try {
      config = await api('POST', page.config_url, { ...page.request, mobile, dark });
    } catch (err) {
      const status = err.status || 0;
      show({ text: err.message || gettext('Opening the document failed.'), retry: !status || status >= 500 || status === 429 || status === 409, login: status === 401 });
      return;
    }
    const title = (config.document && config.document.title) || gettext('Online document');
    App.setTitle(title);
    try {
      await loadScript(page.api_url);
    } catch (e) {
      show({ text: LOAD_FAILED, retry: true });
      return;
    }
    if (!window.DocsAPI || typeof window.DocsAPI.DocEditor !== 'function') {
      show({ text: LOAD_FAILED, retry: true });
      return;
    }
    config.events = {
      // A tab with changes the Document Server hasn't stored yet says so in its title.
      onDocumentStateChange(event) {
        App.setTitle(event && event.data ? `* ${title}` : title);
      },
      onError(event) {
        const data = (event && event.data) || {};
        App.snackbar(interpolate(gettext('The online document server reported an error: %(error)s'), { error: data.errorDescription || data.errorCode || gettext('unknown error') }, true), { kind: 'error' });
      },
    };
    try {
      // eslint-disable-next-line no-new
      new window.DocsAPI.DocEditor('office-editor', config);
    } catch (e) {
      show({ text: LOAD_FAILED, retry: true });
      return;
    }
    state.hidden = true;
  }

  start();
})();
