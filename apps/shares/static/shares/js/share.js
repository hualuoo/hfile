/* The share page /share/<key> (apps/shares/pages.py SharePage).
 *
 * Before the share's password: its form. ?pwd= (a link with the password, "复制带密码链接") only fills
 * it in and is taken out of the address at once (history.replaceState); the visitor presses 访问. The
 * right password gives this browser the share's cookie, and the page loads again: then it holds files'
 * browser in share mode (files.js, contract W4) on the share's API, with the viewers (files/_assets.html)
 * working read only. Downloads, previews and the viewers' inline links all come from the share's own
 * endpoints, which check the share every time; a cookie that ended takes the page back to the form.
 *
 * In share mode the browser offers the files' own 打开, 新标签打开, 预览, 下载, 复制下载链接 and 刷新 (by
 * the owner's preview and download), the viewers' actions, and "下载" for folders, which says
 * 暂不支持下载文件夹 (D19). Folder passwords inside the share are asked without 记住密码. */
(function () {
  'use strict';
  const App = window.App;
  const { api } = App;
  const codeOf = (err) => (err && err.data && typeof err.data === 'object' ? err.data.code : '') || '';

  // ?pwd= out of the address (and so out of the history); what it said.
  function takePassword() {
    const params = new URLSearchParams(location.search);
    if (!params.has('pwd')) return null;
    const password = params.get('pwd');
    params.delete('pwd');
    const rest = params.toString();
    history.replaceState(history.state, '', `${location.pathname}${rest ? `?${rest}` : ''}${location.hash}`);
    return password;
  }

  function again() {
    location.replace(location.pathname + location.search + location.hash);
  }

  // -- the password
  function passwordForm(config, given) {
    const form = document.getElementById('share-password-form');
    if (!form) return;
    const input = form.elements.password;
    const button = form.querySelector('[type="submit"]');
    const label = button.querySelector('span');
    if (given) {
      input.value = given;
      button.focus();
    } else {
      input.focus();
    }
    App.handleSubmit(form, async () => {
      const password = input.value;
      if (!password.trim()) {
        input.focus();
        throw new Error(gettext('Enter the access password'));
      }
      label.textContent = gettext('Checking…');
      try {
        await api('POST', `${config.api}shares/${encodeURIComponent(config.key)}/verify/`, { password });
      } catch (err) {
        label.textContent = pgettext('share', 'Open');
        if (codeOf(err) === 'share_password_wrong') {
          input.select();
          throw new Error(gettext('Wrong password. Please try again'));
        }
        throw err;
      }
      // Loaded again with the share's cookie: the browser, and the CSP for the storage's media.
      again();
      return true;
    });
  }

  // -- the browser's data source (W4): the share's API
  function source(config) {
    const base = `${config.api}shares/${encodeURIComponent(config.key)}/`;
    const query = (params) => new URLSearchParams(params).toString();
    // A path in the address as it reads: /share/<key>?path=/docs/a
    const pathParam = (path) => encodeURIComponent(path).replace(/%2F/g, '/');
    const guard = (promise) => promise.catch((err) => {
      // The share's cookie ended (idle, or the password asked again): back to the form.
      if (codeOf(err) === 'share_password_required') again();
      throw err;
    });
    return {
      list: (path, sort) => guard(api('GET', `${base}list/?${query({ path, sort: sort.field, order: sort.order })}`)),
      item: (path) => guard(api('GET', `${base}item/?${query({ path })}`)),
      unlock: ({ path, rule, password }) => guard(api('POST', `${base}unlock/`, rule ? { rule, password } : { path: path || '/', password })),
      href: (path) => (path === '/' ? config.page : `${config.page}?path=${pathParam(path)}`),
      // The share's download (the owner's download permission), else its preview URL (the gallery).
      downloadUrl: (item) => item.download_url || item.url || null,
      // No inlineUrl of its own: files.js asks item() for the viewers' inline_url (a files-signed
      // same-origin /pd/ link) and renews it before its e= ends, or at once with {fresh: true}.
      forgetPasswords: () => api('DELETE', `${base}unlocks/`),
    };
  }

  function mount(config) {
    const element = document.getElementById('share-files');
    if (!element || !App.files) return;
    const src = source(config);
    App.files.actions.register({
      key: 'share_folder_download', label: gettext('Download'), icon: 'download', group: 1, order: 50,
      when: (ctx, items) => ctx.mode === 'share' && items.length > 0 && items.some((item) => item.type === 'folder') && !!ctx.permissions.download,
      run: () => App.toast(gettext("Folders can't be downloaded yet"), 'error'),
    });
    const browser = App.files.mount(element, {
      mode: 'share',
      source: src,
      storage: config.storage,
      path: config.path,
      site: config.site || {},
      origin: config.origin,
      history: true,
      crumbs: { home: null, storage: { label: gettext('All files'), href: config.page } },
      onNavigate: (b) => {
        const atTop = b.path === '/';
        const title = atTop ? gettext('File share') : b.title();
        App.setTitle(atTop ? title : interpolate(gettext('%(title)s · File share'), { title }, true));
        const heading = document.getElementById('share-heading');
        if (heading) heading.textContent = title;
      },
    });
    App.files.current = browser;
    const settings = document.getElementById('share-settings-button');
    if (settings) {
      settings.addEventListener('click', () => App.files.openSettings({
        onForgetPasswords: () => src.forgetPasswords().then(() => browser.refresh()),
      }));
    }
    browser.open(config.path, { replace: true });
  }

  function start() {
    const root = document.getElementById('share');
    if (!root || !root.dataset.config) return;
    const config = JSON.parse(root.dataset.config);
    const given = takePassword();
    if (config.info && config.info.verified) mount(config);
    else passwordForm(config, given);
  }

  document.addEventListener('DOMContentLoaded', start);
})();
