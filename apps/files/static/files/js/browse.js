/* The file browser's pages (apps/files/pages.py): "/" (the storages) and /<storage key>/<path>. Mounts the
 * component (files.js, App.files.mount) with the files API as its data source, and adds the page's
 * own parts: the top app bar's storage menu, upload menu (with the upload panel's badge) and 个人设置,
 * the overflow menu on phones, the title, and uploads by drag and drop or paste (contract U as the
 * uploads app built it: App.uploads.pick() from the click itself, App.uploads.drop() inside the drop
 * event, start() for pasted files, onCount() and show() for the badge; all left out when App.uploads
 * isn't there). It starts once every script of the page has run, so that viewers and actions of other
 * apps are registered by then. */
(function () {
  'use strict';
  const App = window.App;
  const { h, icon, api } = App;

  function source(config, storage) {
    const key = encodeURIComponent(storage.key);
    const base = `${config.api}files/${key}/`;
    const query = (params) => new URLSearchParams(params).toString();
    return {
      list: (path, sort) => api('GET', `${base}list/?${query({ path, sort: sort.field, order: sort.order })}`),
      item: (path) => api('GET', `${base}item/?${query({ path })}`),
      unlock: ({ path, rule, password, remember }) => api('POST', `${config.api}storages/${key}/unlock/`,
        rule ? { rule, password, remember } : { path: path || '/', password, remember }),
      mkdir: (path, name) => api('POST', `${base}mkdir/`, { path, name }),
      rename: (path, name, newName) => api('POST', `${base}rename/`, { path, name, new_name: newName }),
      move: (path, names, target) => api('POST', `${base}move/`, { path, names, target }),
      copy: (path, names, target) => api('POST', `${base}copy/`, { path, names, target }),
      remove: (path, names) => api('POST', `${base}delete/`, { path, names }),
      href: (path) => `/${key}/${App.files.paths.encode(path)}`,
      forgetPasswords: () => api('DELETE', `${config.api}storages/unlocks/`),
    };
  }

  function emptyRootText(config) {
    if (config.user.admin) return gettext('There are no storage sources yet. You can add one on the Manage page.');
    if (config.user.authenticated) return gettext("You don't have access to any storage source. Please ask the administrator to give your account access.");
    return gettext('There are no storage sources visitors can browse. If you have an account, log in first.');
  }

  function start() {
    const element = document.getElementById('files');
    if (!element || !element.dataset.config || !App.files) return;
    const config = JSON.parse(element.dataset.config);
    const site = config.site || {};
    const storage = config.storage;
    const first = config.storages[0];
    const homeHref = site.root_show_storage || !first ? '/' : `/${encodeURIComponent(first.key)}/`;
    const loginUrl = config.login ? `/login/?next=${encodeURIComponent(location.pathname + location.search)}` : null;
    const src = storage ? source(config, storage) : {
      href: () => '/',
      forgetPasswords: () => api('DELETE', `${config.api}storages/unlocks/`),
    };
    const browser = App.files.mount(element, {
      mode: 'browse',
      source: src,
      storage,
      path: config.path,
      site,
      origin: config.origin,
      root: storage ? null : config.storages,
      history: !!storage,
      loginUrl,
      emptyRoot: emptyRootText(config),
      // The admin gets the way to the storages page with the empty state (6U.2), a visitor the login.
      emptyRootActions: config.user.admin ? [{ label: gettext('Add a storage source on the Manage page'), icon: 'admin_panel_settings', href: '/manage/#storages' }]
        : (loginUrl ? [{ label: gettext('Log in'), icon: 'login', href: loginUrl }] : []),
      crumbs: {
        home: { label: site.home_name || gettext('Home'), href: homeHref },
        storage: storage && site.root_show_storage ? { label: storage.name, href: `/${encodeURIComponent(storage.key)}/` } : null,
      },
      onNavigate: (b) => {
        App.setTitle(b.title());
        const heading = document.getElementById('files-heading');
        if (heading) heading.textContent = b.title();
        chrome(b);
      },
    });
    window.App.files.current = browser;
    topbar(config, browser);
    dropAndPaste(browser);
    browser.open(config.path, { replace: true });
  }

  // -- the top app bar
  function topbar(config, browser) {
    const storageButton = document.getElementById('files-storage-button');
    const storageMenu = document.getElementById('files-storage-menu');
    if (storageButton && storageMenu) {
      storageMenu.replaceChildren(...config.storages.map((s) => h('a', {
        class: 'menu-item', role: 'menuitemradio', href: `/${encodeURIComponent(s.key)}/`,
        'aria-checked': String(!!config.storage && config.storage.key === s.key),
      }, icon(s.icon || 'storage'), h('span', { text: s.name }))));
      if (!config.storages.length) storageMenu.append(h('p', { class: 'menu-label', text: gettext('No storage sources available') }));
      App.menu(storageButton);
    }
    const settings = () => App.files.openSettings({
      storages: config.storages,
      current: config.storage ? config.storage.key : null,
      onForgetPasswords: () => api('DELETE', `${config.api}storages/unlocks/`).then(() => browser.refresh()),
    });
    for (const id of ['files-settings-button']) {
      const button = document.getElementById(id);
      if (button) button.addEventListener('click', settings);
    }
    // Called from the click handler itself: the uploads' file chooser needs the user's gesture.
    const run = (key) => {
      if (key === 'upload_panel') {
        if (window.App.uploads) window.App.uploads.show();
        return;
      }
      const action = App.files.actions.list().find((a) => a.key === key);
      if (action) action.run(browser.ctx, []);
    };
    document.querySelectorAll('[data-files-action]').forEach((item) => {
      item.addEventListener('click', () => {
        if (item.dataset.filesAction === 'settings') settings();
        else run(item.dataset.filesAction);
      });
    });
    ['files-upload-button', 'files-overflow-button'].forEach((id) => {
      const button = document.getElementById(id);
      if (button) App.menu(button);
    });
    const badge = document.getElementById('files-upload-badge');
    if (badge && window.App.uploads && typeof window.App.uploads.onCount === 'function') {
      window.App.uploads.onCount((n) => {
        badge.hidden = !n;
        badge.textContent = n > 99 ? '99+' : String(n);
      });
      // The count opens the upload panel (the button around it opens the menu).
      badge.addEventListener('click', (e) => {
        e.stopPropagation();
        window.App.uploads.show();
      });
    }
  }

  // Which of the top bar's entries apply here (permissions per 6U.4).
  function chrome(browser) {
    const ctx = browser.ctx;
    const inStorage = !!ctx.storage && !!browser.listing;
    const perms = ctx.permissions;
    const uploads = !!window.App.uploads;
    const allowed = {
      new_folder: inStorage && !!perms.new_folder,
      upload_file: inStorage && uploads && !!perms.upload,
      upload_folder: inStorage && uploads && !!perms.upload && !!perms.new_folder,
      upload_panel: inStorage && uploads && !!perms.upload,
    };
    document.querySelectorAll('[data-files-action]').forEach((item) => {
      const key = item.dataset.filesAction;
      if (key in allowed) item.hidden = !allowed[key];
    });
    const upload = document.getElementById('files-upload-wrap');
    if (upload) upload.hidden = !(allowed.new_folder || allowed.upload_file || allowed.upload_folder);
    document.querySelectorAll('.files-overflow-divider').forEach((hr) => {
      hr.hidden = !(allowed.new_folder || allowed.upload_file || allowed.upload_folder);
    });
  }

  // -- uploads by drag and drop, and by pasting (6U.3, contract U)
  function dropAndPaste(browser) {
    const ctx = browser.ctx;
    const can = () => !!window.App.uploads && !!ctx.storage && !!browser.listing && !!ctx.permissions.upload
      && !document.querySelector('dialog[open]');
    const overlay = h('div', { class: 'files-drop', hidden: true, 'aria-hidden': 'true' },
      h('div', { class: 'files-drop-box' }, icon('cloud_upload'), h('p', { class: 'files-drop-text' })));
    document.body.append(overlay);
    let depth = 0;
    const hasFiles = (e) => !!e.dataTransfer && [...e.dataTransfer.types].includes('Files');
    const hide = () => { depth = 0; overlay.hidden = true; };
    document.addEventListener('dragenter', (e) => {
      if (!hasFiles(e) || !can()) return;
      e.preventDefault();
      depth += 1;
      overlay.querySelector('.files-drop-text').textContent = interpolate(gettext('Upload files to %(path)s'), { path: ctx.path }, true);
      overlay.hidden = false;
    });
    document.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      // Always taken: where nothing may be uploaded (a visitor, a dialog open) a dropped file does nothing,
      // instead of the browser opening it in place of the folder.
      e.preventDefault();
      e.dataTransfer.dropEffect = can() ? 'copy' : 'none';
    });
    document.addEventListener('dragleave', (e) => {
      if (!hasFiles(e)) return;
      depth -= 1;
      if (depth <= 0) hide();
    });
    document.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      hide();
      if (!can()) return;
      // The uploads app takes the entries now, during the event, and walks dropped folders itself.
      window.App.uploads.drop(ctx, e.dataTransfer);
    });
    document.addEventListener('paste', (e) => {
      const target = e.target;
      if (!can() || (target && (target.isContentEditable || /^(INPUT|TEXTAREA)$/.test(target.tagName)))) return;
      const files = [...((e.clipboardData && e.clipboardData.files) || [])];
      if (!files.length) return; // text: not an upload
      e.preventDefault();
      window.App.uploads.start(ctx, files);
    });
  }

  document.addEventListener('DOMContentLoaded', start);
})();
