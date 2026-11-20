/* The file browser as a component (contract W4; documented in RUN/files/readme_notes.md):
 *
 *   App.files.mount(element, options) -> browser
 *       options: { mode: 'browse' | 'share', source, path, storage, site, origin, crumbs, history,
 *                  onNavigate(browser), root (the storages, for the list at "/") }
 *       source:  { list(path, sort), item(path), unlock({path, rule, password, remember}),
 *                  href(path), and in browse mode mkdir(path, name), rename(path, name, newName),
 *                  move(path, names, target), copy(path, names, target), remove(path, names);
 *                  optional: downloadUrl(item), inlineUrl(item, {fresh}), forgetPasswords() }
 *   App.files.actions.register({ key, label, icon, group, order, when(ctx, items), run(ctx, items), danger })
 *   App.files.previewers.register(kinds, { open(ctx, item, siblings) })
 *   App.files.prefs.get(name) / set(name, value)       the per-browser settings (localStorage)
 *   App.files.clickedUrl(url)                           the URL a 下载 button opens (dl=1 on the site's /pd/ links)
 *   App.files.openSettings(options)                     the 个人设置 side sheet
 *   document events: hfile:files-changed {storageKey, path} refreshes a browser showing that folder.
 *
 * The views (list, grid, gallery), selection, the context menu, the per-row overflow button, the
 * selection bar, the keyboard shortcuts and the dialogs are here; the page (browse.js, or a share
 * page) gives the data source and its own chrome. Names are only ever inserted as text. */
(function () {
  'use strict';
  const App = window.App;
  const { h, icon, toast, fmtSize, fmtDateTime, confirmDialog, openDialog, copyText, ApiError } = App;

  // A download from a 下载 button says so on the site's own /pd/ links: dl=1, outside the signature. The site
  // then writes the download log (页面下载, apps/files/download.py); the same link in the gallery, a player
  // or copied stays as it is. Other URLs (a bucket's, a download domain's, a share's) are left alone.
  function clickedUrl(url) {
    if (typeof url !== 'string' || !url) return url;
    let parsed;
    try {
      parsed = new URL(url, location.href);
    } catch (err) {
      return url;
    }
    if (parsed.origin !== location.origin || !parsed.pathname.startsWith('/pd/') || parsed.hash) return url;
    return `${url}${parsed.search ? '&' : '?'}dl=1`;
  }

  // ------------------------------------------------------------------ registries

  const actionList = [];
  const actions = {
    register(spec) {
      if (!spec || !spec.key || typeof spec.run !== 'function') throw new Error('an action needs key and run');
      const index = actionList.findIndex((a) => a.key === spec.key);
      const action = { group: 4, order: 100, when: () => true, icon: 'more_horiz', ...spec };
      if (index >= 0) actionList.splice(index, 1, action);
      else actionList.push(action);
      actionList.sort((a, b) => a.group - b.group || a.order - b.order);
      return action;
    },
    unregister(key) {
      const index = actionList.findIndex((a) => a.key === key);
      if (index >= 0) actionList.splice(index, 1);
    },
    list() { return [...actionList]; },
  };

  const viewers = new Map();
  const previewers = {
    register(kinds, viewer) {
      if (!viewer || typeof viewer.open !== 'function') throw new Error('a previewer needs open()');
      for (const kind of [].concat(kinds)) viewers.set(kind, viewer);
    },
    get(kind) { return viewers.get(kind) || null; },
    kinds() { return [...viewers.keys()]; },
  };

  // ------------------------------------------------------------------ per-browser settings (6U.2)

  // Only what the visitor changed is stored: the rest follows the defaults (and the admin's values
  // where there is one), also after they change.
  const PREFS_KEY = 'hfile-files-prefs';
  const phone = () => window.matchMedia('(max-width: 767px)').matches;
  const DEFAULTS = {
    view: 'list', // list | grid
    size: 'default', // small | default | large
    galleryColumns: null, // null: 3, on phones 2
    galleryColumnGap: 12,
    galleryRowGap: 12,
    galleryRounded: true,
    galleryNames: true,
    galleryNameMode: 'hover', // hover | bottom
    previewMode: 'full', // full: all the folder's images in the viewer; only: the one clicked
    galleryZoom: true, // a click in the gallery opens the viewer
    backToTop: true,
  };
  function readPrefs() {
    try {
      const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
      return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    } catch (e) {
      return {};
    }
  }
  const prefs = {
    get(name) {
      const own = readPrefs();
      const value = name in own ? own[name] : DEFAULTS[name];
      if (name === 'galleryColumns' && !value) return phone() ? 2 : 3;
      return value;
    },
    all() {
      const out = {};
      for (const name of Object.keys(DEFAULTS)) out[name] = prefs.get(name);
      return out;
    },
    set(name, value) {
      const own = readPrefs();
      if (value === DEFAULTS[name]) delete own[name];
      else own[name] = value;
      try { localStorage.setItem(PREFS_KEY, JSON.stringify(own)); } catch (e) { /* kept for this page only */ }
      memory[name] = value;
      document.dispatchEvent(new CustomEvent('hfile:files-prefs', { detail: { name, value } }));
    },
  };
  // When storage is refused (a private window), what was set lasts for the page.
  const memory = {};
  const prefsGet = prefs.get;
  prefs.get = (name) => (name in memory ? memory[name] : prefsGet(name));

  // ------------------------------------------------------------------ kinds and icons (W3)

  const KIND_ICONS = {
    folder: 'folder', storage: 'storage', back: 'arrow_upward', image: 'image', video: 'movie',
    audio: 'music_note', text: 'description', markdown: 'article', pdf: 'picture_as_pdf', office: 'docs',
    kkfileview: 'draft', archive: 'folder_zip', executable: 'terminal', document: 'menu_book',
    three_d: 'view_in_ar', other: 'draft',
  };
  const SORT_ICONS = { asc: 'arrow_upward', desc: 'arrow_downward' };
  const OFFICE_ICONS = { xls: 'table_chart', xlsx: 'table_chart', xlsm: 'table_chart', csv: 'table_chart', ppt: 'slideshow', pptx: 'slideshow' };
  const KIND_LABELS = {
    image: gettext('Image'), video: gettext('Video'), audio: gettext('Audio'), text: gettext('Text'), markdown: 'Markdown', pdf: 'PDF', office: gettext('Office document'),
    kkfileview: gettext('File'), archive: gettext('Archive'), executable: gettext('Program'), document: gettext('Document'), three_d: gettext('3D model'), other: gettext('File'),
  };
  function extension(name) {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  }
  function itemIcon(item) {
    if (item.type === 'folder') return KIND_ICONS.folder;
    if (item.type === 'storage') return KIND_ICONS.storage;
    if (item.type === 'back') return KIND_ICONS.back;
    if (item.kind === 'office' && OFFICE_ICONS[extension(item.name)]) return OFFICE_ICONS[extension(item.name)];
    return KIND_ICONS[item.kind] || KIND_ICONS.other;
  }

  // ------------------------------------------------------------------ paths

  const segments = (path) => String(path || '/').split('/').filter(Boolean);
  const normal = (path) => '/' + segments(path).join('/');
  const parentOf = (path) => normal(segments(path).slice(0, -1).join('/'));
  const join = (folder, name) => normal(`${folder}/${name}`);
  const baseName = (path) => segments(path).pop() || '';
  const encodePath = (path) => segments(path).map(encodeURIComponent).join('/');

  // ------------------------------------------------------------------ small helpers

  const isTouch = () => window.matchMedia('(pointer: coarse)').matches;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || '') || /Mac OS/.test(navigator.userAgent);
  const modKey = (e) => (isMac ? e.metaKey : e.ctrlKey);
  const dialogOpen = () => !!document.querySelector('dialog[open]');
  const typing = (target) => target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
    && !(target.type === 'checkbox' && target.closest('.files'));
  const codeOf = (err) => (err && err.data && typeof err.data === 'object' ? err.data.code : '') || '';

  function sizeText(item) {
    if (item.type !== 'file') return '-';
    if (item.size === null || item.size === undefined) return gettext('Unknown');
    return fmtSize(item.size);
  }
  function timeText(item) {
    if (!item.modified) return '-';
    return fmtDateTime(item.modified, { seconds: false });
  }

  // A dialog with a form: { title, text, fields (nodes), submitText, onSubmit(form) -> true to close }.
  function formDialog({ title, body, submitText = gettext('OK'), danger = false, onSubmit, small = true }) {
    return new Promise((resolve) => {
      let result = null;
      const error = h('div', { class: 'form-error', role: 'alert', hidden: true });
      const submit = h('button', { type: 'submit', class: danger ? 'btn btn-danger' : 'btn btn-filled', text: submitText });
      const form = h('form', { class: 'dialog-body', novalidate: true }, body, error,
        h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') }), submit));
      const titleId = `files-dialog-${Math.random().toString(36).slice(2)}`;
      const dialog = h('dialog', { class: `dialog${small ? ' dialog-small' : ''}`, 'aria-labelledby': titleId },
        h('div', { class: 'dialog-box' },
          h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: title }),
            h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
          form));
      let busy = false;
      const showError = (message) => { error.textContent = message || ''; error.hidden = !message; };
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (busy) return;
        busy = true;
        submit.disabled = true;
        showError('');
        try {
          const done = await onSubmit(form, showError);
          if (done !== undefined && done !== false) {
            result = done;
            dialog.close();
          }
        } catch (err) {
          showError(err.message);
        } finally {
          busy = false;
          submit.disabled = false;
        }
      });
      dialog.addEventListener('cancel', (e) => { if (busy) e.preventDefault(); });
      dialog.addEventListener('close', () => { dialog.remove(); resolve(result); });
      document.body.append(dialog);
      openDialog(dialog);
    });
  }

  function field(label, input, hint) {
    const id = input.id || (input.id = `files-field-${Math.random().toString(36).slice(2)}`);
    return h('div', { class: 'field' }, h('label', { for: id, text: label }), input, hint ? h('p', { class: 'hint', text: hint }) : null);
  }

  // ------------------------------------------------------------------ the component

  class Browser {
    constructor(element, options) {
      this.el = element;
      this.options = options;
      this.mode = options.mode || 'browse';
      this.source = options.source;
      this.site = options.site || {};
      this.storage = options.storage || null;
      this.isRoot = !this.storage;
      this.path = normal(options.path || '/');
      this.listing = null;
      this.items = [];
      this.sort = { field: this.site.default_sort_field || 'name', order: this.site.default_sort_order || 'asc' };
      this.gallery = false;
      this.galleryChosen = false;
      this.shown = 0;
      this.selected = new Set();
      this.anchor = -1;
      this.focus = -1;
      this.loadSeq = 0;
      this.inline = new Map();
      this.ctx = this.makeContext();
      this.build();
      this.listen();
      this.el.filesBrowser = this;
    }

    // -- the context actions, previewers and plug-ins see (W4)
    makeContext() {
      const browser = this;
      return {
        get mode() { return browser.mode; },
        get storage() { return browser.storage; },
        get path() { return browser.path; },
        get permissions() { return (browser.listing && browser.listing.permissions) || {}; },
        get capabilities() { return (browser.listing && browser.listing.capabilities) || {}; },
        get root() { return browser.listing ? browser.listing.root : null; },
        get items() { return browser.items; },
        get selection() { return browser.selectedItems(); },
        get prefs() { return prefs.all(); },
        site: browser.site,
        origin: browser.options.origin || location.origin,
        browser,
        refresh: () => browser.refresh(),
        open: (path) => browser.open(path),
        toast: (text, kind) => toast(text, kind),
        downloadUrl: (item) => browser.downloadUrl(item),
        inlineUrl: (item, options) => browser.inlineUrl(item, options),
        absoluteUrl: (url) => browser.absoluteUrl(url),
        href: (path) => browser.href(path),
      };
    }

    // -- DOM
    build() {
      const el = this.el;
      el.classList.add('files');
      el.dataset.mode = this.mode;
      el.dataset.layout = this.site.layout || 'full';
      el.dataset.mobileLayout = this.site.mobile_layout || this.site.layout || 'full';
      this.applyPrefs();
      this.crumbs = h('ol', { class: 'breadcrumbs files-crumb-list' });
      this.sortButton = h('button', { type: 'button', class: 'icon-button', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-label': gettext('Sort'), 'data-tooltip': gettext('Sort') }, icon('sort'));
      this.sortMenu = h('div', { class: 'menu', role: 'menu', 'aria-label': gettext('Sort'), hidden: true });
      this.sortMenu.id = `files-sort-${Math.random().toString(36).slice(2)}`;
      this.sortButton.setAttribute('aria-controls', this.sortMenu.id);
      this.listButton = h('button', { type: 'button', class: 'segment', 'aria-pressed': 'false', 'aria-label': gettext('List view'), 'data-tooltip': gettext('List view') }, icon('view_list'));
      this.gridButton = h('button', { type: 'button', class: 'segment', 'aria-pressed': 'false', 'aria-label': gettext('Icon view'), 'data-tooltip': gettext('Icon view') }, icon('grid_view'));
      this.galleryButton = h('button', { type: 'button', class: 'icon-button', 'aria-pressed': 'false', 'aria-label': gettext('Gallery view'), 'data-tooltip': gettext('Open gallery view') }, icon('photo_library'));
      this.toolbar = h('div', { class: 'files-toolbar' },
        h('nav', { class: 'files-crumbs', 'aria-label': gettext('Location') }, this.crumbs),
        h('div', { class: 'files-tools' },
          h('div', { class: 'menu-wrap files-sort-wrap' }, this.sortButton, this.sortMenu),
          h('div', { class: 'segmented files-views', role: 'group', 'aria-label': gettext('View') }, this.listButton, this.gridButton),
          this.galleryButton));
      this.selbarCount = h('span', { class: 'files-selbar-count' });
      this.selbarActions = h('div', { class: 'files-selbar-actions' });
      this.selbar = h('div', { class: 'files-selbar', role: 'toolbar', 'aria-label': gettext('Actions for the selected items'), hidden: true },
        h('button', { type: 'button', class: 'icon-button', 'aria-label': gettext('Deselect'), 'data-tooltip': gettext('Deselect (Esc)'), onclick: () => this.clearSelection() }, icon('close')),
        this.selbarCount, this.selbarActions);
      this.progress = h('progress', { class: 'linear-progress files-progress', 'aria-label': gettext('Loading'), hidden: true });
      this.readmeTop = h('section', { class: 'files-readme files-readme-top markdown-body', 'aria-label': gettext('Folder readme'), hidden: true });
      this.readmeBottom = h('section', { class: 'files-readme files-readme-bottom markdown-body', 'aria-label': gettext('Folder readme'), hidden: true });
      this.view = h('div', { class: 'files-view' });
      this.state = h('div', { class: 'files-state', hidden: true });
      this.more = h('div', { class: 'files-more', hidden: true });
      this.surface = h('div', { class: 'files-surface' }, this.view, this.state, this.more);
      this.backTop = h('button', { type: 'button', class: 'fab fab-small fab-surface files-backtop', 'aria-label': gettext('Back to top'), 'data-tooltip': gettext('Back to top'), hidden: true,
        onclick: () => window.scrollTo({ top: 0, behavior: 'smooth' }) }, icon('vertical_align_top'));
      this.live = h('div', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });
      // The selection bar lies over the toolbar: choosing an item never moves the list (a double
      // click's second click lands where the first did).
      this.head = h('div', { class: 'files-head' }, this.toolbar, this.selbar, this.progress);
      el.replaceChildren(this.head, this.readmeTop, this.surface, this.readmeBottom, this.backTop, this.live);
      App.menu(this.sortButton);
      this.renderSortMenu();
      this.updateViewButtons();
      if (this.isRoot) {
        this.galleryButton.hidden = true;
      }
    }

    applyPrefs() {
      this.el.dataset.size = prefs.get('size');
      this.el.dataset.view = prefs.get('view');
      const style = this.el.style;
      style.setProperty('--gallery-columns', String(Math.max(1, Math.min(12, Number(prefs.get('galleryColumns')) || 3))));
      style.setProperty('--gallery-column-gap', `${Math.max(0, Math.min(100, Number(prefs.get('galleryColumnGap')) || 0))}px`);
      style.setProperty('--gallery-row-gap', `${Math.max(0, Math.min(100, Number(prefs.get('galleryRowGap')) || 0))}px`);
      this.el.dataset.galleryRounded = prefs.get('galleryRounded') ? '1' : '0';
      this.el.dataset.galleryNames = prefs.get('galleryNames') ? prefs.get('galleryNameMode') : 'none';
    }

    listen() {
      this.listButton.addEventListener('click', () => this.setView('list'));
      this.gridButton.addEventListener('click', () => this.setView('grid'));
      this.galleryButton.addEventListener('click', () => this.setGallery(!this.gallery));
      this.view.addEventListener('click', (e) => this.onClick(e));
      this.view.addEventListener('dblclick', (e) => this.onDoubleClick(e));
      this.view.addEventListener('contextmenu', (e) => this.onContextMenu(e));
      this.view.addEventListener('keydown', (e) => this.onKey(e));
      this.view.addEventListener('pointerdown', (e) => this.onPointerDown(e));
      this.surface.addEventListener('contextmenu', (e) => {
        if (e.defaultPrevented || e.target.closest('.files-item')) return;
        e.preventDefault();
        this.clearSelection();
        this.showMenu(e.clientX, e.clientY, [], { emptyArea: true });
      });
      this.el.addEventListener('keydown', (e) => this.onAreaKey(e));
      document.addEventListener('hfile:files-changed', (e) => {
        const detail = e.detail || {};
        if (!this.storage || detail.storageKey !== this.storage.key) return;
        if (detail.path === undefined || normal(detail.path) === this.path) this.refresh();
      });
      document.addEventListener('hfile:files-prefs', () => {
        this.applyPrefs();
        this.updateViewButtons();
        this.render();
      });
      // Phones have their own defaults (gallery columns) and click mode: follow a rotation or a resize.
      window.matchMedia('(max-width: 767px)').addEventListener('change', () => {
        this.applyPrefs();
        this.renderSelbar();
      });
      window.addEventListener('scroll', () => {
        this.backTop.hidden = !(prefs.get('backToTop') && window.scrollY > 600);
      }, { passive: true });
      let resized = null;
      window.addEventListener('resize', () => {
        clearTimeout(resized);
        resized = setTimeout(() => { if (this.listing) this.renderCrumbs(); }, 150);
      });
      if (this.options.history) {
        window.addEventListener('popstate', (e) => {
          const state = e.state;
          if (state && state.filesPath !== undefined && (!this.storage || state.storageKey === this.storage.key)) {
            this.open(state.filesPath, { push: false });
          }
        });
      }
    }

    // -- loading
    async open(path, { push = true, replace = false } = {}) {
      const target = normal(path);
      const seq = ++this.loadSeq;
      this.setLoading(true);
      let listing;
      try {
        listing = await this.load(target);
      } catch (err) {
        if (seq !== this.loadSeq) return;
        this.setLoading(false);
        await this.failed(err, target, { push });
        return;
      }
      if (seq !== this.loadSeq) return;
      this.setLoading(false);
      const changed = target !== this.path || !this.listing;
      // Whether the keyboard was in the list (or lost with the rows the click replaced): it goes on there.
      const active = document.activeElement;
      const keepFocus = !!this.listing && !dialogOpen() && (this.view.contains(active) || active === document.body || active === null);
      this.listing = listing;
      this.path = this.isRoot ? '/' : normal(listing.path || target);
      this.items = listing.items || [];
      if (listing.sort) this.sort = { ...listing.sort };
      this.inline.clear();
      if (changed) {
        this.selected.clear();
        this.anchor = -1;
        this.focus = -1;
        this.shown = this.pageSize();
      }
      if (!this.galleryChosen && !this.isRoot) this.gallery = !!listing.gallery_default;
      if (this.options.history && (push || replace)) {
        const url = this.href(this.path);
        const state = { filesPath: this.path, storageKey: this.storage ? this.storage.key : null };
        if (replace || !changed) history.replaceState(state, '', url);
        else if (location.pathname + location.search !== url) history.pushState(state, '', url);
        else history.replaceState(state, '', url);
      }
      this.render();
      this.renderReadme();
      if (changed && this.el.getBoundingClientRect().top < 0) window.scrollTo(0, 0);
      if (keepFocus && this.rowItems && this.rowItems.length) {
        const row = this.rowAt(Math.max(0, this.focus));
        if (row) row.focus({ preventScroll: true });
      }
      if (this.options.onNavigate) this.options.onNavigate(this);
      if (changed) this.announce(interpolate(ngettext('%(title)s, %(count)s item', '%(title)s, %(count)s items', this.items.length), { title: this.title(), count: this.items.length }, true));
    }

    load(path) {
      if (this.isRoot) {
        return Promise.resolve({ path: '/', items: (this.options.root || []).map((s) => ({ ...s, type: 'storage', path: '/', size: null, modified: null })), permissions: {}, capabilities: {} });
      }
      return this.source.list(path, this.sort);
    }

    refresh() {
      return this.open(this.path, { push: false });
    }

    setLoading(on) {
      this.loading = on;
      this.progress.hidden = !on || !this.listing;
      this.el.classList.toggle('files-busy', on);
      if (on && !this.listing) this.renderSkeleton();
    }

    async failed(err, target, { push }) {
      const code = codeOf(err);
      if (code === 'password_required' || code === 'password_wrong') {
        const unlocked = await this.askPassword(err, { path: target, listing: true });
        if (unlocked) return this.open(target, { push });
        // Cancelled: back to the folder above (or the storages at a root).
        if (this.listing && normal(target) !== this.path) return undefined;
        if (normal(target) !== '/') return this.open(parentOf(target), { push });
        this.showState('lock', gettext('Password required'), gettext('This folder is protected. Enter the password to view it.'), [{ label: gettext('Enter password'), icon: 'key', run: () => this.open(target, { push }) }]);
        return undefined;
      }
      if (this.listing && normal(target) !== this.path) {
        toast(err.message, 'error');
        return undefined;
      }
      this.listing = null;
      this.items = [];
      this.view.replaceChildren();
      this.more.hidden = true;
      this.hideReadme();
      if (err instanceof ApiError && err.status === 401) {
        const buttons = this.options.loginUrl ? [{ label: gettext('Log in'), icon: 'login', href: this.options.loginUrl }] : [];
        this.showState('lock', gettext('Please log in first'), err.message, buttons);
        return undefined;
      }
      const buttons = [{ label: gettext('Retry'), icon: 'refresh', run: () => this.open(target, { push }) }];
      if (normal(target) !== '/') buttons.push({ label: gettext('Up one level'), icon: 'arrow_upward', run: () => this.open(parentOf(target)) });
      // The path above says where the address points: the list has nothing to show.
      if (!this.isRoot) {
        this.path = normal(target);
        this.renderCrumbs();
      }
      const missing = code === 'not_found';
      const title = missing ? gettext("The folder doesn't exist") : gettext("Can't open this folder");
      // The server's sentence for a missing folder is the title again (文件夹不存在。): say what may have happened.
      const text = err.message && err.message.replace(/[。.]$/, '') !== title ? err.message
        : (missing ? gettext('It may have been renamed, moved or deleted.') : '');
      this.showState(missing ? 'folder_off' : 'error', title, text, buttons);
      return undefined;
    }

    // -- folder passwords (D11): the prompt, then the source unlocks for this browser
    askPassword(err, { path, listing = false } = {}) {
      const data = (err && err.data) || {};
      const hint = data.path || null;
      const rule = data.rule || null;
      const input = h('input', { type: 'password', name: 'password', autocomplete: 'off', placeholder: gettext('Please enter'), 'data-autofocus': '' });
      const remember = h('input', { type: 'checkbox', name: 'remember' });
      remember.checked = !!this.site.default_save_password;
      const text = listing || data.code === 'password_wrong' ? gettext('This folder is protected. Please enter the password:') : (data.detail || gettext('This folder is protected. Please enter the password:'));
      // Inside a share an unlock lasts as long as the share's own cookie: no 记住密码 there (shares app).
      const offerRemember = this.mode !== 'share';
      const body = [
        h('p', { class: 'files-prompt-text', text }),
        field(gettext('Password'), input),
        offerRemember ? h('label', { class: 'check-line' }, remember, gettext('Remember password')) : null,
        offerRemember ? h('p', { class: 'hint', text: gettext("Once remembered, this browser won't ask again for 30 days (it remembers that the folder is unlocked, not the password).") }) : null,
      ];
      return formDialog({
        title: gettext('Note'),
        body,
        onSubmit: async (form, showError) => {
          const password = input.value;
          if (!password) { showError(gettext("The password can't be empty.")); input.focus(); return false; }
          try {
            await this.source.unlock({ path: hint || (rule ? null : path), rule: hint ? null : rule, password, remember: offerRemember && remember.checked });
          } catch (e) {
            if (codeOf(e) === 'password_wrong') { showError(gettext('Wrong password. Please try again.')); input.select(); return false; }
            throw e;
          }
          return true;
        },
      }).then((ok) => !!ok);
    }

    // Runs an operation of the source; a folder password it needs is asked for, then it runs again.
    async withPasswords(run) {
      for (let attempt = 0; attempt < 6; attempt += 1) {
        try {
          return await run();
        } catch (err) {
          if (codeOf(err) !== 'password_required') throw err;
          const unlocked = await this.askPassword(err);
          if (!unlocked) return null;
        }
      }
      throw new Error(gettext('Too many passwords are needed. Please open those folders and enter their passwords first.'));
    }

    // -- URLs
    href(path) {
      if (this.source && this.source.href) return this.source.href(normal(path));
      return location.pathname;
    }
    itemHref(item) {
      if (item.type === 'storage') return `/${encodeURIComponent(item.key)}/`;
      if (item.type === 'folder' || item.type === 'back') return this.href(item.path);
      return null;
    }
    absoluteUrl(url) {
      if (!url) return url;
      if (url.startsWith('/') && !url.startsWith('//')) return (this.options.origin || location.origin) + url;
      return url;
    }
    downloadUrl(item) {
      if (this.source.downloadUrl) return this.source.downloadUrl(item);
      return item.url || null;
    }
    // The item's inline_url, asked for once per listing; again when the one kept is about to end (its
    // e=; a minute before, or a quarter of its time for short ones), or with fresh (the link failed).
    inlineUrl(item, { fresh = false } = {}) {
      if (this.source.inlineUrl) return Promise.resolve(this.source.inlineUrl(item, { fresh }));
      const kept = this.inline.get(item.path);
      if (kept && !fresh && kept.renew > Date.now()) return kept.url;
      const entry = { renew: Infinity, url: null };
      entry.url = this.source.item(item.path).then((data) => {
        const url = data.inline_url || null;
        const ends = url ? Number(new URL(url, location.href).searchParams.get('e')) * 1000 : 0;
        if (ends > 0) entry.renew = ends - Math.min(60000, (ends - Date.now()) / 4);
        return url;
      }).catch((err) => {
        if (this.inline.get(item.path) === entry) this.inline.delete(item.path);
        throw err;
      });
      this.inline.set(item.path, entry);
      return entry.url;
    }

    title() {
      if (this.isRoot) return gettext('Home');
      return baseName(this.path) || (this.storage ? this.storage.name : '');
    }

    pageSize() {
      return Math.max(1, Number(this.site.max_show_size) || 1000);
    }

    // -- selection
    visibleItems() {
      if (this.gallery) return this.items.filter((item) => item.kind === 'image');
      return this.items;
    }
    selectedItems() {
      return this.items.filter((item) => this.selected.has(this.keyOf(item)));
    }
    keyOf(item) {
      return item.type === 'storage' ? `storage:${item.key}` : item.name;
    }
    clearSelection() {
      if (!this.selected.size) return;
      this.selected.clear();
      this.anchor = -1;
      this.syncSelection();
    }
    selectOnly(index) {
      const item = this.rowItems[index];
      this.selected.clear();
      if (item && item.type !== 'back') this.selected.add(this.keyOf(item));
      this.anchor = index;
      this.syncSelection();
    }
    toggle(index) {
      const item = this.rowItems[index];
      if (!item || item.type === 'back') return;
      const key = this.keyOf(item);
      if (this.selected.has(key)) this.selected.delete(key);
      else this.selected.add(key);
      this.anchor = index;
      this.syncSelection();
    }
    selectRange(index) {
      const from = this.anchor >= 0 ? this.anchor : index;
      const [a, b] = from < index ? [from, index] : [index, from];
      this.selected.clear();
      for (let i = a; i <= b; i += 1) {
        const item = this.rowItems[i];
        if (item && item.type !== 'back') this.selected.add(this.keyOf(item));
      }
      this.syncSelection();
    }
    selectAll() {
      for (const item of this.items) this.selected.add(this.keyOf(item));
      this.syncSelection();
    }
    syncSelection() {
      for (const row of this.view.querySelectorAll('.files-item')) {
        const item = this.rowItems[Number(row.dataset.index)];
        const on = !!item && this.selected.has(this.keyOf(item));
        row.setAttribute('aria-selected', String(on));
        const box = row.querySelector('.files-check input');
        if (box) box.checked = on;
      }
      if (this.headCheck) {
        const n = this.selected.size;
        this.headCheck.checked = n > 0 && n === this.items.length;
        this.headCheck.indeterminate = n > 0 && n < this.items.length;
      }
      this.renderSelbar();
    }

    // -- rendering
    render() {
      this.view.classList.remove('files-skeleton-view');
      this.state.hidden = true;
      this.renderCrumbs();
      this.updateViewButtons();
      this.galleryButton.hidden = this.isRoot;
      this.galleryButton.setAttribute('aria-pressed', String(this.gallery));
      this.galleryButton.dataset.tooltip = this.gallery ? gettext('Close gallery view') : gettext('Open gallery view');
      this.el.classList.toggle('files-gallery-on', this.gallery);
      if (!this.listing) return;
      if (this.gallery) this.renderGallery();
      else if (prefs.get('view') === 'grid') this.renderGrid();
      else this.renderList();
      this.renderSelbar();
    }

    rowsToShow() {
      const items = this.visibleItems();
      const rows = items.slice(0, this.shown);
      if (!this.gallery && !this.isRoot && this.path !== '/') {
        rows.unshift({ type: 'back', name: gettext('Up one level'), path: parentOf(this.path) });
      }
      return { rows, total: items.length };
    }

    renderEmpty() {
      if (this.isRoot) {
        this.showState('storage', gettext('No storage sources to browse yet'), this.options.emptyRoot || gettext('Files are listed here once the administrator has added a storage source.'),
          this.options.emptyRootActions || []);
        return;
      }
      const canUpload = this.mode === 'browse' && this.ctx.permissions.upload && window.App.uploads;
      this.showState('folder_open', gettext('This folder is empty'), canUpload ? gettext('Drag files here, or use Upload above.') : '');
    }

    renderList() {
      const { rows, total } = this.rowsToShow();
      this.rowItems = rows;
      const mobileSize = this.site.mobile_show_size !== false;
      this.el.dataset.mobileSize = mobileSize ? '1' : '0';
      const head = this.listHead();
      const list = h('div', { class: 'files-list', role: 'listbox', 'aria-multiselectable': 'true', 'aria-label': this.isRoot ? pgettext('list of them', 'Storage sources') : pgettext('list of them', 'Files') });
      rows.forEach((item, index) => list.append(this.listRow(item, index)));
      this.view.replaceChildren(head, list);
      this.listbox = list;
      this.finishRows(total);
    }

    listHead() {
      const cell = (fieldName, label, extra) => {
        const on = this.sort.field === fieldName;
        const button = h('button', { type: 'button', class: `files-sort${on ? ' on' : ''}`, 'aria-label': interpolate(gettext('Sort by %(label)s'), { label }, true),
          onclick: () => this.sortBy(fieldName) }, h('span', { text: label }),
          on ? icon(SORT_ICONS[this.sort.order] || SORT_ICONS.asc) : null);
        return h('span', { class: `files-col ${extra}`, role: 'columnheader', 'aria-sort': on ? (this.sort.order === 'asc' ? 'ascending' : 'descending') : 'none' }, button);
      };
      this.headCheck = h('input', { type: 'checkbox', 'aria-label': gettext('Select all'), onchange: (e) => (e.target.checked ? this.selectAll() : this.clearSelection()) });
      if (this.isRoot) {
        this.headCheck = null;
        return h('div', { class: 'files-row files-head', role: 'presentation' },
          h('span', { class: 'files-check' }), h('span', { class: 'files-col files-name-col', text: gettext('Name') }),
          h('span', { class: 'files-col files-time' }), h('span', { class: 'files-col files-size' }), h('span', { class: 'files-row-end' }));
      }
      return h('div', { class: 'files-row files-head', role: 'presentation' },
        h('span', { class: 'files-check' }, this.headCheck),
        cell('name', gettext('File name'), 'files-name-col'),
        cell('time', gettext('Modified'), 'files-time'),
        cell('size', gettext('Size'), 'files-size'),
        h('span', { class: 'files-row-end' }));
    }

    itemName(item) {
      const href = this.itemHref(item);
      const label = h('span', { class: 'files-label', text: item.name });
      if (href) return h('a', { class: 'files-link', href, tabindex: '-1', draggable: 'false' }, label);
      return label;
    }

    listRow(item, index) {
      const back = item.type === 'back';
      const row = h('div', { class: `files-row files-item${back ? ' files-back' : ''}`, role: 'option', tabindex: '-1',
        'aria-selected': 'false', dataset: { index: String(index) } });
      const check = back || this.isRoot ? h('span', { class: 'files-check' })
        : h('span', { class: 'files-check' }, h('input', { type: 'checkbox', tabindex: '-1', 'aria-label': interpolate(gettext('Select %(name)s'), { name: item.name }, true) }));
      const name = h('span', { class: 'files-name' }, icon(itemIcon(item), `files-icon kind-${item.type === 'file' ? item.kind : item.type}`),
        this.itemName(item),
        item.locked ? h('span', { class: 'files-lock', 'aria-label': gettext('Password required'), 'data-tooltip': gettext('Password required') }, icon('lock')) : null);
      const end = back ? h('span', { class: 'files-row-end' })
        : h('span', { class: 'files-row-end' }, h('button', { type: 'button', class: 'icon-button small files-more-button', tabindex: '-1', 'aria-label': interpolate(gettext('More actions for %(name)s'), { name: item.name }, true), 'data-tooltip': gettext('More actions') }, icon('more_vert')));
      // The storage list's head has no 修改时间 and 大小: no dashes under it either.
      const bare = back || this.isRoot;
      row.append(check, name,
        h('span', { class: 'files-time', text: bare ? '' : timeText(item) }),
        h('span', { class: 'files-size', text: bare ? '' : sizeText(item) }),
        end);
      if (back) {
        name.querySelector('.files-label').textContent = gettext('Up one level');
      }
      return row;
    }

    renderGrid() {
      const { rows, total } = this.rowsToShow();
      this.rowItems = rows;
      this.headCheck = null;
      const grid = h('div', { class: 'files-grid', role: 'listbox', 'aria-multiselectable': 'true', 'aria-label': this.isRoot ? pgettext('list of them', 'Storage sources') : pgettext('list of them', 'Files') });
      rows.forEach((item, index) => {
        const back = item.type === 'back';
        const tile = h('div', { class: `files-tile files-item${back ? ' files-back' : ''}`, role: 'option', tabindex: '-1', 'aria-selected': 'false', dataset: { index: String(index) } },
          back || this.isRoot ? null : h('span', { class: 'files-check' }, h('input', { type: 'checkbox', tabindex: '-1', 'aria-label': interpolate(gettext('Select %(name)s'), { name: item.name }, true) })),
          h('span', { class: 'files-tile-icon' }, icon(itemIcon(item), `files-icon kind-${item.type === 'file' ? item.kind : item.type}`),
            item.locked ? h('span', { class: 'files-lock', 'aria-label': gettext('Password required') }, icon('lock')) : null),
          h('span', { class: 'files-tile-name' }, back ? h('span', { class: 'files-label', text: gettext('Up one level') }) : this.itemName(item)),
          back ? null : h('button', { type: 'button', class: 'icon-button small files-more-button', tabindex: '-1', 'aria-label': interpolate(gettext('More actions for %(name)s'), { name: item.name }, true), 'data-tooltip': gettext('More actions') }, icon('more_vert')));
        grid.append(tile);
      });
      this.view.replaceChildren(grid);
      this.listbox = grid;
      this.finishRows(total);
    }

    renderGallery() {
      const images = this.visibleItems();
      const rows = images.slice(0, this.shown);
      this.rowItems = rows;
      this.headCheck = null;
      if (!images.length) {
        this.view.replaceChildren();
        this.listbox = null;
        this.more.hidden = true;
        this.showState('photo_library', gettext('No images in this folder'), '', [{ label: gettext('Leave gallery view'), icon: 'close', run: () => this.setGallery(false) }]);
        return;
      }
      const gallery = h('div', { class: 'files-gallery', role: 'listbox', 'aria-label': gettext('Image'), 'aria-multiselectable': 'true' });
      rows.forEach((item, index) => {
        const url = this.downloadUrl(item);
        // No link, or the picture doesn't load (a format the browser can't show, a file that went away):
        // the same placeholder, not the browser's broken image.
        const placeholder = () => h('span', { class: 'files-photo-missing', role: 'img', 'aria-label': item.name }, icon('image'));
        const picture = url ? h('img', { src: url, alt: item.name, loading: 'lazy', decoding: 'async', draggable: 'false' }) : placeholder();
        if (url) picture.addEventListener('error', () => picture.replaceWith(placeholder()), { once: true });
        gallery.append(h('figure', { class: 'files-photo files-item', role: 'option', tabindex: '-1', 'aria-selected': 'false', dataset: { index: String(index) } },
          picture,
          h('figcaption', { class: 'files-photo-caption' }, h('span', { class: 'files-photo-name', text: item.name }), h('span', { class: 'files-photo-size', text: sizeText(item) }))));
      });
      this.view.replaceChildren(gallery);
      this.listbox = gallery;
      this.finishRows(images.length);
    }

    finishRows(total) {
      const count = this.rowItems.filter((item) => item.type !== 'back').length;
      if (!total) {
        this.more.hidden = true;
        this.renderEmpty();
        if (this.listbox && this.rowItems.length) this.setFocus(Math.min(Math.max(this.focus, 0), this.rowItems.length - 1), false);
        return;
      }
      this.state.hidden = true;
      if (count < total) {
        const step = Math.max(1, Number(this.site.load_more_size) || 50);
        const next = Math.min(step, total - count);
        this.more.replaceChildren(h('button', { type: 'button', class: 'btn btn-tonal', 'data-tooltip': interpolate(ngettext('%(total)s file in all, %(count)s loaded; click to load %(next)s more', '%(total)s files in all, %(count)s loaded; click to load %(next)s more', total), { total, count, next }, true),
          onclick: () => { this.shown += step; this.render(); } }, icon('expand_more'), gettext('Load more')));
        this.more.hidden = false;
      } else {
        this.more.hidden = true;
      }
      const index = Math.min(Math.max(this.focus, 0), this.rowItems.length - 1);
      this.setFocus(index, false);
      this.syncSelection();
    }

    renderSkeleton() {
      const rows = [];
      for (let i = 0; i < 8; i += 1) rows.push(h('div', { class: 'files-row files-skeleton-row', 'aria-hidden': 'true' }, h('span', { class: 'files-check' }), h('span', { class: 'files-skeleton-bar' }), h('span', { class: 'files-time' }), h('span', { class: 'files-size' }), h('span', { class: 'files-row-end' })));
      this.view.replaceChildren(h('div', { class: 'files-list', 'aria-busy': 'true', 'aria-label': gettext('Loading') }, ...rows));
      this.view.classList.add('files-skeleton-view');
      this.state.hidden = true;
      this.more.hidden = true;
    }

    showState(iconName, title, text, buttons = []) {
      // Not null into replaceChildren(): the DOM would write it out as the text "null".
      this.state.replaceChildren(...[icon(iconName), h('h2', { text: title }), text ? h('p', { text }) : null,
        buttons.length ? h('div', { class: 'btn-row files-state-actions' }, ...buttons.map((b) => (b.href
          ? h('a', { class: 'btn btn-tonal', href: b.href }, icon(b.icon), b.label)
          : h('button', { type: 'button', class: 'btn btn-tonal', onclick: b.run }, icon(b.icon), b.label)))) : null].filter(Boolean));
      this.state.className = 'files-state empty-state';
      this.state.hidden = false;
    }

    renderCrumbs() {
      const crumbs = this.options.crumbs || {};
      const items = [];
      if (crumbs.home) items.push({ label: crumbs.home.label, href: crumbs.home.href, icon: 'home' });
      if (crumbs.storage && this.storage) items.push({ label: crumbs.storage.label, href: crumbs.storage.href, path: '/' });
      const parts = segments(this.path);
      parts.forEach((part, i) => items.push({ label: part, path: '/' + parts.slice(0, i + 1).join('/') }));
      if (!items.length) items.push({ label: this.storage ? this.storage.name : gettext('Home'), path: '/' });
      const last = items.length - 1;
      const nodes = items.map((item, i) => {
        const current = i === last;
        const content = [item.icon ? icon(item.icon) : null, h('span', { text: item.label })];
        let node;
        if (current) node = h('span', { class: 'breadcrumb', 'aria-current': 'page' }, ...content);
        else if (item.path !== undefined) node = h('a', { class: 'breadcrumb', href: this.href(item.path), onclick: (e) => { if (!e.ctrlKey && !e.metaKey && !e.shiftKey && e.button === 0) { e.preventDefault(); this.open(item.path); } } }, ...content);
        else node = h('a', { class: 'breadcrumb', href: item.href }, ...content);
        return h('li', { dataset: { crumb: String(i) } }, node);
      });
      this.crumbs.replaceChildren(...nodes);
      this.crumbItems = items;
      requestAnimationFrame(() => this.collapseCrumbs());
    }

    // Middle items go into a ··· menu while the breadcrumb doesn't fit (6U.1).
    collapseCrumbs() {
      const list = this.crumbs;
      // Measured with the current folder at its full width: middle items go before its name is cut.
      list.classList.add('files-measuring');
      try {
        this.collapseMiddle(list);
      } finally {
        list.classList.remove('files-measuring');
      }
    }

    collapseMiddle(list) {
      const lis = [...list.children];
      if (lis.length <= 3 || list.scrollWidth <= list.clientWidth + 1) return;
      const first = this.options.crumbs && this.options.crumbs.storage && this.storage ? 2 : 1;
      const hidden = [];
      const button = h('button', { type: 'button', class: 'breadcrumb files-crumb-more', 'aria-haspopup': 'menu', 'aria-label': gettext('More locations'), 'data-tooltip': gettext('More locations') },
        h('span', { text: '···' }));
      const holder = h('li', null, button);
      list.insertBefore(holder, lis[first]);
      for (let i = first; i < lis.length - 1 && list.scrollWidth > list.clientWidth + 1; i += 1) {
        lis[i].hidden = true;
        hidden.push(this.crumbItems[i]);
      }
      if (!hidden.length) { holder.remove(); return; }
      // The list clips what overflows: the menu is a popup of the page, as the context menu is.
      button.addEventListener('click', () => {
        const box = button.getBoundingClientRect();
        this.popup(box.left, box.bottom + 4, hidden.map((item) => h('a', {
          class: 'menu-item', role: 'menuitem', href: item.path !== undefined ? this.href(item.path) : item.href,
          onclick: (e) => {
            if (item.path === undefined || e.button !== 0 || e.ctrlKey || e.metaKey) return;
            e.preventDefault();
            this.closeMenu();
            this.open(item.path);
          },
        }, icon('folder'), h('span', { text: item.label }))), { anchor: button, label: gettext('More locations') });
      });
    }

    renderSortMenu() {
      const options = [['name', gettext('File name')], ['time', gettext('Modified')], ['size', gettext('Size')]];
      this.sortMenu.replaceChildren(
        ...options.map(([fieldName, label]) => h('button', { type: 'button', class: 'menu-item', role: 'menuitemradio', 'aria-checked': String(this.sort.field === fieldName),
          onclick: () => this.sortBy(fieldName, this.sort.field === fieldName ? this.sort.order : 'asc') }, icon('sort'), label)),
        h('hr', { role: 'separator' }),
        ...[['asc', gettext('Ascending'), 'arrow_upward'], ['desc', gettext('Descending'), 'arrow_downward']].map(([order, label, iconName]) => h('button', { type: 'button', class: 'menu-item', role: 'menuitemradio', 'aria-checked': String(this.sort.order === order),
          onclick: () => this.sortBy(this.sort.field, order) }, icon(iconName), label)));
    }

    sortBy(fieldName, order) {
      if (order === undefined) order = this.sort.field === fieldName && this.sort.order === 'asc' ? 'desc' : 'asc';
      this.sort = { field: fieldName, order };
      this.renderSortMenu();
      if (this.isRoot) return;
      this.open(this.path, { push: false });
    }

    updateViewButtons() {
      const view = prefs.get('view');
      this.listButton.setAttribute('aria-pressed', String(view === 'list'));
      this.gridButton.setAttribute('aria-pressed', String(view === 'grid'));
      this.el.dataset.view = view;
      const sortable = !this.isRoot && (view === 'grid' || this.gallery);
      this.sortButton.closest('.files-sort-wrap').hidden = !sortable;
    }

    setView(view) {
      if (this.gallery) {
        this.gallery = false;
        this.galleryChosen = true;
      }
      prefs.set('view', view);
    }

    setGallery(on) {
      this.gallery = !!on;
      this.galleryChosen = true;
      this.shown = this.pageSize();
      this.render();
      if (this.options.onNavigate) this.options.onNavigate(this);
    }

    // -- readme (5.3): top, bottom, or a dialog with 不再显示
    hideReadme() {
      this.readmeTop.hidden = true;
      this.readmeBottom.hidden = true;
    }
    renderReadme() {
      this.hideReadme();
      const readme = this.listing && this.listing.readme;
      if (!readme || !readme.html || this.site.show_document === false) return;
      if (readme.display === 'dialog') {
        this.readmeDialog(readme);
        return;
      }
      const box = readme.display === 'top' ? this.readmeTop : this.readmeBottom;
      box.innerHTML = readme.html; // sanitized on the server (apps.core.markdown.render)
      box.hidden = false;
    }
    readmeDialog(readme) {
      const KEY = 'hfile-readme-dialog';
      const id = `${this.storage ? this.storage.key : ''}_${this.path}`;
      let seen = {};
      try { seen = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { seen = {}; }
      if (seen[id] === readme.hash) return;
      if (this.readmeShown === `${id}:${readme.hash}`) return;
      this.readmeShown = `${id}:${readme.hash}`;
      const body = h('div', { class: 'dialog-body markdown-body files-readme-dialog-body' });
      body.innerHTML = readme.html; // sanitized on the server (apps.core.markdown.render)
      const titleId = `files-readme-${Math.random().toString(36).slice(2)}`;
      const dialog = h('dialog', { class: 'dialog dialog-wide', 'aria-labelledby': titleId },
        h('div', { class: 'dialog-box' },
          h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: gettext('Folder readme') }),
            h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
          body));
      dialog.addEventListener('close', async () => {
        dialog.remove();
        const forget = await confirmDialog({ title: gettext('Folder readme'), message: gettext("Don't show this again until the content changes?"), confirmText: gettext('Yes') });
        if (!forget) return;
        try {
          const all = JSON.parse(localStorage.getItem(KEY) || '{}') || {};
          all[id] = readme.hash;
          localStorage.setItem(KEY, JSON.stringify(all));
        } catch (e) { /* not kept: it shows again next time */ }
      });
      document.body.append(dialog);
      openDialog(dialog);
    }

    announce(text) {
      this.live.textContent = text;
    }

    // -- focus and keyboard (D39: shortcuts on keyboard focus in the list)
    rowAt(index) {
      return this.view.querySelector(`.files-item[data-index="${index}"]`);
    }
    setFocus(index, move = true) {
      if (!this.rowItems || !this.rowItems.length) return;
      const clamped = Math.max(0, Math.min(this.rowItems.length - 1, index));
      for (const row of this.view.querySelectorAll('.files-item[tabindex="0"]')) row.tabIndex = -1;
      const row = this.rowAt(clamped);
      if (!row) return;
      row.tabIndex = 0;
      this.focus = clamped;
      if (move) {
        row.focus({ preventScroll: false });
        row.scrollIntoView({ block: 'nearest' });
      }
    }
    indexOf(target) {
      const row = target.closest('.files-item');
      return row ? Number(row.dataset.index) : -1;
    }
    clickMode() {
      return (phone() ? this.site.mobile_file_click_mode : this.site.file_click_mode) || 'dbclick';
    }

    onPointerDown(e) {
      // iOS long press: no contextmenu event there; open the menu after a moment of holding.
      if (e.pointerType !== 'touch') return;
      const index = this.indexOf(e.target);
      if (index < 0) return;
      clearTimeout(this.pressTimer);
      const start = { x: e.clientX, y: e.clientY };
      const cancel = () => { clearTimeout(this.pressTimer); this.view.removeEventListener('pointermove', move); this.view.removeEventListener('pointerup', cancel); this.view.removeEventListener('pointercancel', cancel); };
      const move = (ev) => { if (Math.hypot(ev.clientX - start.x, ev.clientY - start.y) > 10) cancel(); };
      this.view.addEventListener('pointermove', move);
      this.view.addEventListener('pointerup', cancel);
      this.view.addEventListener('pointercancel', cancel);
      this.pressTimer = setTimeout(() => {
        cancel();
        this.longPressed = Date.now();
        // Lifting the finger makes a click where the menu now is: that click isn't a choice.
        const swallow = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
        document.addEventListener('click', swallow, { capture: true, once: true });
        setTimeout(() => document.removeEventListener('click', swallow, true), 800);
        this.menuFor(index, start.x, start.y);
      }, 550);
    }

    onClick(e) {
      const index = this.indexOf(e.target);
      if (index < 0) return;
      const item = this.rowItems[index];
      if (this.longPressed && Date.now() - this.longPressed < 800) { e.preventDefault(); return; }
      if (e.target.closest('.files-more-button')) {
        e.preventDefault();
        const box = e.target.closest('.files-more-button').getBoundingClientRect();
        this.menuFor(index, box.right, box.bottom, { anchor: e.target.closest('.files-more-button') });
        return;
      }
      if (e.target.closest('.files-check')) {
        if (e.target.tagName !== 'INPUT') e.preventDefault();
        if (e.shiftKey) this.selectRange(index);
        else this.toggle(index);
        this.setFocus(index, false);
        return;
      }
      e.preventDefault();
      this.setFocus(index, false);
      if (item.type === 'back') { this.open(item.path); return; }
      if (modKey(e)) { this.toggle(index); return; }
      if (e.shiftKey) { this.selectRange(index); return; }
      if (this.clickMode() === 'click' || this.gallery) {
        if (this.gallery) {
          if (prefs.get('galleryZoom')) this.openItem(item);
          else this.selectOnly(index);
          return;
        }
        this.openItem(item);
        return;
      }
      this.selectOnly(index);
    }

    onDoubleClick(e) {
      const index = this.indexOf(e.target);
      if (index < 0 || e.target.closest('.files-check, .files-more-button')) return;
      if (this.clickMode() === 'click' || this.gallery) return;
      e.preventDefault();
      this.openItem(this.rowItems[index]);
    }

    onContextMenu(e) {
      const index = this.indexOf(e.target);
      if (index < 0) return;
      e.preventDefault();
      if (this.longPressed && Date.now() - this.longPressed < 800) return; // the long press opened it already
      this.menuFor(index, e.clientX, e.clientY);
    }

    // The menu of a row: a row that isn't selected becomes the only selection (6U.3).
    menuFor(index, x, y, { anchor = null } = {}) {
      const item = this.rowItems[index];
      if (!item || item.type === 'back') return;
      if (!this.selected.has(this.keyOf(item))) this.selectOnly(index);
      this.setFocus(index, false);
      this.showMenu(x, y, this.selectedItems(), { anchor });
    }

    onKey(e) {
      if (e.target.closest('.menu')) return;
      const index = this.indexOf(e.target);
      if (index < 0) return;
      const columns = this.columns();
      let next = null;
      if (e.key === 'ArrowDown') next = index + columns;
      else if (e.key === 'ArrowUp') next = index - columns;
      else if (e.key === 'ArrowRight' && columns > 1) next = index + 1;
      else if (e.key === 'ArrowLeft' && columns > 1) next = index - 1;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = this.rowItems.length - 1;
      if (next !== null) {
        e.preventDefault();
        next = Math.max(0, Math.min(this.rowItems.length - 1, next));
        this.setFocus(next);
        if (e.shiftKey) this.selectRange(next);
        return;
      }
      if (e.key === ' ' && !e.target.closest('a, button, input')) {
        e.preventDefault();
        this.toggle(index);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        const item = this.rowItems[index];
        const chosen = this.selectedItems();
        if (chosen.length === 1 && chosen[0] !== item && item.type !== 'back') this.openItem(chosen[0]);
        else this.openItem(item);
        return;
      }
      if (e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey)) {
        e.preventDefault();
        const box = this.rowAt(index).getBoundingClientRect();
        this.menuFor(index, box.left + 48, box.bottom);
      }
    }

    columns() {
      if (!this.listbox) return 1;
      if (this.listbox.classList.contains('files-list')) return 1;
      const children = [...this.listbox.children];
      if (children.length < 2) return 1;
      const top = children[0].getBoundingClientRect().top;
      let n = 0;
      for (const child of children) {
        if (Math.abs(child.getBoundingClientRect().top - top) > 4) break;
        n += 1;
      }
      return Math.max(1, n);
    }

    // Shortcuts anywhere in the browser's area (focus inside it), not while typing or in a dialog.
    onAreaKey(e) {
      if (e.defaultPrevented || dialogOpen() || typing(e.target) || e.target.closest('.menu')) return;
      if (e.key === 'Escape' && this.selected.size) {
        e.preventDefault();
        this.clearSelection();
      } else if ((e.key === 'a' || e.key === 'A') && modKey(e) && !e.altKey) {
        if (this.isRoot) return;
        e.preventDefault();
        this.selectAll();
      } else if (e.key === 'Delete' && this.selected.size) {
        const action = actionList.find((a) => a.key === 'delete');
        const items = this.selectedItems();
        if (action && this.applies(action, items)) {
          e.preventDefault();
          action.run(this.ctx, items);
        }
      } else if (e.key === 'Backspace' && !this.isRoot && this.path !== '/') {
        e.preventDefault();
        this.open(parentOf(this.path));
      }
    }

    // -- opening (6U.3): folders, the viewer of a file's kind, else the download
    openItem(item) {
      if (!item) return;
      if (item.type === 'storage') { location.href = this.itemHref(item); return; }
      if (item.type === 'folder' || item.type === 'back') { this.open(item.path); return; }
      this.openFile(item);
    }
    openFile(item) {
      if (item.locked) {
        // A folder password covers the file itself (`/dir/**`): asked for here, then it opens.
        this.unlockItem(item).then((fresh) => { if (fresh) this.openFile(fresh); });
        return null;
      }
      const viewer = previewers.get(item.kind);
      if (viewer && this.ctx.permissions.preview) {
        const siblings = this.items.filter((other) => other.type === 'file');
        try {
          return viewer.open(this.ctx, item, siblings);
        } catch (err) {
          toast(err.message || gettext('Preview failed'), 'error');
          return null;
        }
      }
      return this.download(item);
    }
    async unlockItem(item) {
      const unlocked = await this.askPassword({ data: { path: item.path } }, { path: item.path, listing: true });
      if (!unlocked) return null;
      await this.refresh();
      return this.items.find((other) => other.path === item.path && !other.locked) || null;
    }

    async download(item) {
      const url = clickedUrl(this.downloadUrl(item));
      if (!url) { toast(gettext("You don't have permission to download this file."), 'error'); return; }
      if (this.site.download_confirm !== false) {
        const ok = await confirmDialog({ title: gettext('Download'), message: interpolate(gettext('Download the file %(name)s?'), { name: item.name }, true), confirmText: gettext('Download') });
        if (!ok) return;
      }
      const link = h('a', { href: url, rel: 'noopener noreferrer' });
      const sameSite = new URL(url, location.href).origin === location.origin;
      // A share's /share/<key>/download opens in a tab of its own, as the viewers' 下载 there: once the share's
      // cookie has ended it answers with the share's page (the password form), which a download attribute
      // saved under the file's name.
      if (sameSite && this.mode !== 'share') link.setAttribute('download', item.name);
      else link.target = '_blank';
      document.body.append(link);
      link.click();
      link.remove();
    }

    // -- menus: the context menu, a row's overflow button, the selection bar (same items)
    applies(action, items, { emptyArea = false } = {}) {
      if (this.isRoot && !action.root) return false;
      if (emptyArea && action.group !== 4) return false;
      if (!emptyArea && items.length === 0 && action.group !== 4) return false;
      try {
        return !!action.when(this.ctx, items);
      } catch (err) {
        return false;
      }
    }
    labelOf(action, items) {
      return typeof action.label === 'function' ? action.label(this.ctx, items) : action.label;
    }

    showMenu(x, y, items, { emptyArea = false, anchor = null } = {}) {
      const list = actionList.filter((action) => this.applies(action, items, { emptyArea }));
      if (!list.length) { this.closeMenu(); return; }
      const entries = [];
      let group = null;
      for (const action of list) {
        if (group !== null && action.group !== group) entries.push(h('hr', { role: 'separator' }));
        group = action.group;
        entries.push(h('button', { type: 'button', class: `menu-item${action.danger ? ' danger' : ''}`, role: 'menuitem',
          onclick: () => { this.closeMenu(); this.runAction(action, items); } }, icon(action.icon), this.labelOf(action, items)));
      }
      this.popup(x, y, entries, { anchor, label: gettext('Actions') });
    }

    // A menu at a point of the page (fixed, so nothing clips it); keyboard as App.menu's.
    popup(x, y, entries, { anchor = null, label = '' } = {}) {
      this.closeMenu();
      const menu = h('div', { class: 'menu files-context', role: 'menu', 'aria-label': label }, ...entries);
      document.body.append(menu);
      const box = menu.getBoundingClientRect();
      const left = Math.max(8, Math.min(window.innerWidth - box.width - 8, x));
      const top = y + box.height > window.innerHeight - 8 ? Math.max(8, y - box.height) : y;
      menu.style.left = `${left}px`;
      menu.style.top = `${top}px`;
      this.contextMenu = menu;
      this.menuReturn = anchor || document.activeElement;
      const buttons = () => [...menu.querySelectorAll('.menu-item')];
      if (buttons().length) buttons()[0].focus();
      menu.addEventListener('keydown', (e) => {
        const all = buttons();
        const at = all.indexOf(document.activeElement);
        if (e.key === 'ArrowDown') { e.preventDefault(); all[(at + 1) % all.length].focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); all[(at - 1 + all.length) % all.length].focus(); }
        else if (e.key === 'Home') { e.preventDefault(); all[0].focus(); }
        else if (e.key === 'End') { e.preventDefault(); all[all.length - 1].focus(); }
        else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.closeMenu(true); }
        else if (e.key === 'Tab') this.closeMenu();
      });
      setTimeout(() => {
        this.outside = (e) => { if (!menu.contains(e.target)) this.closeMenu(); };
        document.addEventListener('pointerdown', this.outside, true);
        window.addEventListener('scroll', this.onScrollClose = () => this.closeMenu(), { passive: true, once: true });
        window.addEventListener('resize', this.onScrollClose, { once: true });
      });
    }
    closeMenu(refocus = false) {
      if (!this.contextMenu) return;
      this.contextMenu.remove();
      this.contextMenu = null;
      document.removeEventListener('pointerdown', this.outside, true);
      if (refocus && this.menuReturn && this.menuReturn.isConnected) this.menuReturn.focus();
    }
    async runAction(action, items) {
      try {
        await action.run(this.ctx, items);
      } catch (err) {
        if (err) toast(err.message || gettext('Failed'), 'error');
      }
    }

    renderSelbar() {
      const items = this.selectedItems();
      if (!items.length || this.isRoot) {
        this.selbar.hidden = true;
        this.toolbar.inert = false;
        return;
      }
      this.selbarCount.textContent = interpolate(ngettext('%(count)s selected', '%(count)s selected', items.length), { count: items.length }, true);
      const list = actionList.filter((action) => action.group < 4 && this.applies(action, items));
      const shown = phone() ? 3 : 7;
      const buttons = list.slice(0, shown).map((action) => h('button', { type: 'button', class: `icon-button${action.danger ? ' files-danger' : ''}`,
        'aria-label': this.labelOf(action, items), 'data-tooltip': this.labelOf(action, items), onclick: () => this.runAction(action, items) }, icon(action.icon)));
      const rest = list.slice(shown);
      if (rest.length) {
        const more = h('button', { type: 'button', class: 'icon-button', 'aria-label': gettext('More actions'), 'data-tooltip': gettext('More actions') }, icon('more_vert'));
        more.addEventListener('click', () => {
          const box = more.getBoundingClientRect();
          this.showMenu(box.left, box.bottom, items, { anchor: more });
        });
        buttons.push(more);
      }
      this.selbarActions.replaceChildren(...buttons);
      this.selbar.hidden = false;
      // The bar covers the toolbar: its path and view buttons are out of reach, for Tab too.
      this.toolbar.inert = true;
    }

    // -- the operations' dialogs (6U.4)
    async newFolder(base = this.path, browser = this) {
      const input = h('input', { name: 'name', autocomplete: 'off', maxlength: '255', 'data-autofocus': '' });
      const path = h('b', { text: base });
      const name = await formDialog({
        title: gettext('New folder'),
        submitText: gettext('Create'),
        body: [h('p', { class: 'files-prompt-text' }, ...around(gettext('Create a folder in %(path)s. Enter its name:'), path)), field(gettext('Folder name'), input)],
        onSubmit: async (form, showError) => {
          const value = input.value.trim();
          if (!value) { showError(gettext("The folder name can't be empty")); return false; }
          if (value.includes('/')) { showError(gettext("A folder name can't contain / (nested folders can't be created at once)")); return false; }
          const done = await browser.withPasswords(() => browser.source.mkdir(base, value));
          return done ? value : false;
        },
      });
      if (name) {
        toast(gettext('Created'));
        if (browser === this && base === this.path) await this.refresh();
      }
      return name;
    }

    async rename(item) {
      if (item.type === 'folder' && this.ctx.capabilities.rename === false) { toast(gettext("This storage source doesn't support renaming folders."), 'error'); return; }
      const input = h('input', { name: 'name', autocomplete: 'off', maxlength: '255', 'data-autofocus': '' });
      input.value = item.name;
      const done = formDialog({
        title: gettext('Rename'),
        submitText: gettext('Rename'),
        body: [h('p', { class: 'files-prompt-text' }, ...around(gettext('Rename %(name)s to:'), h('b', { text: item.name }))), field(gettext('New name'), input)],
        onSubmit: async (form, showError) => {
          const value = input.value.trim();
          if (!value) { showError(gettext("The name can't be empty")); return false; }
          if (value.includes('/')) { showError(gettext("The name can't contain /")); return false; }
          if (value === item.name) return true;
          this.announce(gettext('Renaming…'));
          const answer = await this.withPasswords(() => this.source.rename(this.path, item.name, value));
          return answer ? value : false;
        },
      });
      // Select the name without its extension, as file managers do.
      requestAnimationFrame(() => {
        const dot = item.type === 'file' ? item.name.lastIndexOf('.') : -1;
        input.setSelectionRange(0, dot > 0 ? dot : item.name.length);
      });
      const value = await done;
      if (value && value !== true) {
        toast(gettext('Renamed'));
        await this.refresh();
      }
    }

    async remove(items) {
      if (!items.length) return;
      const folders = items.filter((i) => i.type === 'folder').length;
      const files = items.length - folders;
      let message;
      if (items.length === 1) message = interpolate(gettext('Delete %(name)s?'), { name: items[0].name }, true);
      else {
        const parts = [];
        if (folders) parts.push(interpolate(ngettext('%(folders)s folder', '%(folders)s folders', folders), { folders }, true));
        if (files) parts.push(interpolate(ngettext('%(files)s file', '%(files)s files', files), { files }, true));
        message = interpolate(gettext('Delete %(what)s?'), { what: parts.join(pgettext('between clauses', ', ')) }, true);
      }
      if (folders) message += gettext('\n(What is inside the folders is deleted too.)');
      const ok = await confirmDialog({ title: gettext('Delete'), message, confirmText: gettext('Delete'), danger: true });
      if (!ok) return;
      this.announce(gettext('Deleting…'));
      const answer = await this.withPasswords(() => this.source.remove(this.path, items.map((i) => i.name)));
      if (!answer) return;
      await this.refresh();
      this.reportBatch(answer, 'delete');
    }

    async relocate(items, operation) {
      if (operation === 'copy' && items.some((i) => i.type === 'folder') && !this.ctx.capabilities.copy_folders) {
        toast(gettext("This storage source doesn't support copying folders."), 'error');
        return;
      }
      if (operation === 'move' && this.ctx.capabilities.move === false) {
        toast(gettext("This storage source doesn't support moving."), 'error');
        return;
      }
      const target = await this.pickFolder(operation, items);
      if (target === null) return;
      if (normal(target) === this.path) { toast(gettext('The target folder you chose is the folder they are already in.'), 'error'); return; }
      const names = items.map((i) => i.name);
      const run = operation === 'move' ? () => this.source.move(this.path, names, target) : () => this.source.copy(this.path, names, target);
      const answer = await this.withPasswords(run);
      if (!answer) return;
      await this.refresh();
      this.reportBatch(answer, operation);
    }

    async reportBatch(answer, operation) {
      const results = answer.results || [];
      const texts = OPERATION_TEXTS[operation];
      if (!answer.failed) { toast(texts.done); return; }
      if (results.length === 1) { toast(interpolate(texts.failed, { error: results[0].message || gettext('Failed') }, true), 'error'); return; }
      const message = answer.succeeded ? texts.someFailed : texts.allFailed;
      const view = await confirmDialog({ title: texts.results, message, confirmText: gettext('View details') });
      if (view) batchResults(results, operation);
    }

    // The folder picker of 移动 and 复制: folders only, with password prompts and 新建文件夹.
    pickFolder(operation, items) {
      const texts = OPERATION_TEXTS[operation];
      return new Promise((resolve) => {
        let current = this.path;
        let chosen = null;
        const crumbs = h('ol', { class: 'breadcrumbs files-picker-crumbs' });
        const list = h('ul', { class: 'list files-picker-list', role: 'listbox', 'aria-label': pgettext('list of them', 'Folders') });
        const status = h('p', { class: 'muted files-picker-status' });
        const newButton = h('button', { type: 'button', class: 'icon-button', 'aria-label': gettext('New folder'), 'data-tooltip': gettext('New folder') }, icon('create_new_folder'));
        newButton.hidden = !this.ctx.permissions.new_folder;
        const confirm = h('button', { type: 'button', class: 'btn btn-filled', text: gettext('Confirm') });
        const titleId = `files-picker-${Math.random().toString(36).slice(2)}`;
        const dialog = h('dialog', { class: 'dialog dialog-small files-picker', 'aria-labelledby': titleId },
          h('div', { class: 'dialog-box' },
            h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: texts.to }), newButton,
              h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
            h('div', { class: 'dialog-body' },
              h('p', { class: 'muted files-picker-what', text: items.length === 1 ? interpolate(texts.one, { name: items[0].name }, true) : interpolate(texts.many(items.length), { count: items.length }, true) }),
              h('nav', { 'aria-label': gettext('Target folder') }, crumbs), list, status,
              h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') }), confirm))));
        const moving = new Set(items.filter((i) => i.type === 'folder').map((i) => i.path));
        const show = async (path) => {
          status.textContent = gettext('Loading…');
          list.replaceChildren();
          confirm.disabled = true;
          let listing;
          try {
            listing = await this.source.list(path, { field: 'name', order: 'asc' });
          } catch (err) {
            if (codeOf(err) === 'password_required') {
              const unlocked = await this.askPassword(err, { path, listing: true });
              if (unlocked) return show(path);
              if (path !== current) return show(current);
              status.textContent = gettext('This folder needs a password to open.');
              return undefined;
            }
            status.textContent = err.message;
            return undefined;
          }
          current = normal(listing.path || path);
          const parts = segments(current);
          const crumbItems = [{ label: gettext('Root folder'), path: '/' }, ...parts.map((part, i) => ({ label: part, path: '/' + parts.slice(0, i + 1).join('/') }))];
          crumbs.replaceChildren(...crumbItems.map((c, i) => h('li', null, i === crumbItems.length - 1
            ? h('span', { class: 'breadcrumb', 'aria-current': 'page', text: c.label })
            : h('button', { type: 'button', class: 'breadcrumb', text: c.label, onclick: () => show(c.path) }))));
          const folders = (listing.items || []).filter((i) => i.type === 'folder');
          list.replaceChildren(...folders.map((folder) => {
            const inside = [...moving].some((p) => normal(folder.path) === p || normal(folder.path).startsWith(`${p}/`));
            const row = h('li', { class: `list-item interactive files-picker-item${inside ? ' disabled' : ''}`, role: 'option', tabindex: '0', 'aria-disabled': inside ? 'true' : null },
              h('span', { class: 'list-leading' }, icon('folder')), h('span', { class: 'list-text' }, h('span', { class: 'list-headline', text: folder.name })),
              folder.locked ? h('span', { class: 'list-trailing' }, icon('lock')) : null);
            const go = () => { if (!inside) show(folder.path); };
            row.addEventListener('click', go);
            row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
            return row;
          }));
          status.textContent = folders.length ? '' : gettext('No folders');
          confirm.disabled = false;
          newButton.hidden = !(listing.permissions && listing.permissions.new_folder);
          return undefined;
        };
        newButton.addEventListener('click', async () => {
          const made = await this.newFolder(current, this);
          if (made) show(current);
        });
        confirm.addEventListener('click', () => { chosen = current; dialog.close(); });
        dialog.addEventListener('close', () => { dialog.remove(); resolve(chosen); });
        document.body.append(dialog);
        openDialog(dialog);
        show(current);
      });
    }

    async copyLinks(items) {
      const urls = items.map((item) => this.absoluteUrl(this.downloadUrl(item))).filter(Boolean);
      if (!urls.length) { toast(gettext('There is no download link to copy.'), 'error'); return; }
      if (await copyText(urls.join('\n'))) toast(gettext('Download link copied to the clipboard'));
      else toast(gettext('Copying failed. Please copy it by hand.'), 'error');
    }

    destroy() {
      this.closeMenu();
      this.el.replaceChildren();
    }
  }

  // A translated sentence with an element where its one %(name)s is: [before, element, after].
  function around(message, element) {
    const at = message.search(/%\(\w+\)s/);
    if (at < 0) return [message, element];
    return [message.slice(0, at), element, message.slice(message.indexOf(')s', at) + 2)];
  }

  // What 移动, 复制 and 删除 say, whole sentences for each: a verb put into one sentence doesn't translate.
  // failed, one: interpolate() them with error, name; many(n): ngettext's, with count.
  const OPERATION_TEXTS = {
    move: {
      done: gettext('Moved'), failed: gettext('Move failed: %(error)s'), someFailed: gettext("Some items couldn't be moved. View the details?"),
      allFailed: gettext('None of the items could be moved. View the details?'), results: gettext('Move results'), to: gettext('Move to'),
      one: gettext('Move %(name)s'), many: (n) => ngettext('Move %(count)s item', 'Move %(count)s items', n),
    },
    copy: {
      done: gettext('Copied'), failed: gettext('Copy failed: %(error)s'), someFailed: gettext("Some items couldn't be copied. View the details?"),
      allFailed: gettext('None of the items could be copied. View the details?'), results: gettext('Copy results'), to: gettext('Copy to'),
      one: gettext('Copy %(name)s'), many: (n) => ngettext('Copy %(count)s item', 'Copy %(count)s items', n),
    },
    delete: {
      done: gettext('Deleted'), failed: gettext('Delete failed: %(error)s'), someFailed: gettext("Some items couldn't be deleted. View the details?"),
      allFailed: gettext('None of the items could be deleted. View the details?'), results: gettext('Delete results'),
    },
  };

  // 批量操作结果: the failed items (all with 包含成功).
  function batchResults(results, operation) {
    const table = h('tbody');
    const all = h('input', { type: 'checkbox', class: 'switch', role: 'switch' });
    const fill = () => {
      const rows = results.filter((r) => all.checked || !r.ok);
      table.replaceChildren(...rows.map((r) => h('tr', null,
        h('td', { class: 'files-result-name' }, icon(r.ok ? 'check_circle' : 'error', r.ok ? 'files-ok' : 'files-bad'), h('span', { text: r.name })),
        h('td', { text: r.ok ? (r.message || OPERATION_TEXTS[operation].done) : (r.message || gettext('Failed')) }))));
    };
    all.addEventListener('change', fill);
    fill();
    const titleId = `files-results-${Math.random().toString(36).slice(2)}`;
    const dialog = h('dialog', { class: 'dialog dialog-wide', 'aria-labelledby': titleId },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: gettext('Results') }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        h('div', { class: 'dialog-body' },
          h('label', { class: 'switch-line files-results-switch' }, h('span', { text: gettext('Include successes') }), all),
          h('div', { class: 'table-wrap' }, h('table', { class: 'data-table files-results' },
            h('thead', null, h('tr', null, h('th', { text: gettext('File') }), h('th', { text: gettext('Message') }))), table)),
          h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-filled', 'data-close': '', text: gettext('Close') })))));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    openDialog(dialog);
  }

  // ------------------------------------------------------------------ files' own actions

  const one = (items) => items.length === 1;
  const onlyFiles = (items) => items.length > 0 && items.every((i) => i.type === 'file');
  const isFolderish = (item) => item.type === 'folder' || item.type === 'storage';
  const browse = (ctx) => ctx.mode === 'browse' && !!ctx.storage;

  actions.register({ key: 'open', label: gettext('Open'), icon: 'folder_open', group: 1, order: 10, root: true,
    when: (ctx, items) => one(items) && isFolderish(items[0]),
    run: (ctx, items) => ctx.browser.openItem(items[0]) });
  actions.register({ key: 'open_tab', label: gettext('Open in new tab'), icon: 'open_in_new', group: 1, order: 20, root: true,
    when: (ctx, items) => one(items) && isFolderish(items[0]),
    run: (ctx, items) => window.open(ctx.browser.itemHref(items[0]), '_blank', 'noopener') });
  actions.register({ key: 'preview', label: gettext('Preview'), icon: 'visibility', group: 1, order: 30,
    when: (ctx, items) => one(items) && items[0].type === 'file' && !!ctx.permissions.preview && !!previewers.get(items[0].kind),
    run: (ctx, items) => ctx.browser.openFile(items[0]) });
  actions.register({ key: 'download', label: gettext('Download'), icon: 'download', group: 1, order: 50,
    when: (ctx, items) => one(items) && items[0].type === 'file' && !!ctx.permissions.download && !!ctx.downloadUrl(items[0]),
    run: (ctx, items) => ctx.browser.download(items[0]) });
  actions.register({ key: 'copy_link', label: gettext('Copy download link'), icon: 'content_copy', group: 1, order: 60,
    when: (ctx, items) => onlyFiles(items) && !!ctx.permissions.copy_download_link && items.every((i) => !!ctx.downloadUrl(i)),
    run: (ctx, items) => ctx.browser.copyLinks(items) });
  actions.register({ key: 'rename', label: gettext('Rename'), icon: 'drive_file_rename_outline', group: 3, order: 10,
    when: (ctx, items) => browse(ctx) && one(items) && !!ctx.permissions.rename,
    run: (ctx, items) => ctx.browser.rename(items[0]) });
  actions.register({ key: 'move', label: gettext('Move'), icon: 'drive_file_move', group: 3, order: 20,
    when: (ctx, items) => browse(ctx) && items.length > 0 && !!ctx.permissions.move && ctx.capabilities.move !== false,
    run: (ctx, items) => ctx.browser.relocate(items, 'move') });
  actions.register({ key: 'copy', label: gettext('Copy'), icon: 'file_copy', group: 3, order: 30,
    when: (ctx, items) => browse(ctx) && items.length > 0 && !!ctx.permissions.copy
      && (ctx.capabilities.copy_folders !== false || items.every((i) => i.type === 'file')),
    run: (ctx, items) => ctx.browser.relocate(items, 'copy') });
  actions.register({ key: 'delete', label: (ctx, items) => interpolate(gettext('Delete (%(count)s)'), { count: items.length }, true), icon: 'delete', group: 3, order: 90, danger: true,
    when: (ctx, items) => browse(ctx) && items.length > 0 && !!ctx.permissions.delete,
    run: (ctx, items) => ctx.browser.remove(items) });
  actions.register({ key: 'new_folder', label: gettext('New folder'), icon: 'create_new_folder', group: 4, order: 10,
    when: (ctx) => browse(ctx) && !!ctx.permissions.new_folder,
    run: (ctx) => ctx.browser.newFolder() });
  actions.register({ key: 'upload_file', label: gettext('Upload files'), icon: 'upload_file', group: 4, order: 20,
    when: (ctx) => browse(ctx) && !!window.App.uploads && !!ctx.permissions.upload,
    run: (ctx) => window.App.uploads.pick(ctx, { folder: false }) });
  actions.register({ key: 'upload_folder', label: gettext('Upload folder'), icon: 'drive_folder_upload', group: 4, order: 30,
    when: (ctx) => browse(ctx) && !!window.App.uploads && !!ctx.permissions.upload && !!ctx.permissions.new_folder,
    run: (ctx) => window.App.uploads.pick(ctx, { folder: true }) });
  actions.register({ key: 'refresh', label: gettext('Refresh'), icon: 'refresh', group: 4, order: 90, root: true,
    when: () => true,
    run: (ctx) => ctx.refresh() });

  // ------------------------------------------------------------------ 个人设置 (a side sheet)

  function openSettings({ storages = [], current = null, onForgetPasswords = null } = {}) {
    const sheetId = `files-settings-${Math.random().toString(36).slice(2)}`;
    const segmentedPref = (name, label, choices) => h('div', { class: 'field files-setting' },
      h('span', { class: 'label', text: label }),
      h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': label }, ...choices.map(([value, text]) => {
        const button = h('button', { type: 'button', class: 'segment', role: 'radio', 'aria-checked': String(prefs.get(name) === value), text });
        button.addEventListener('click', () => {
          prefs.set(name, value);
          button.parentElement.querySelectorAll('.segment').forEach((b) => b.setAttribute('aria-checked', String(b === button)));
        });
        return button;
      })));
    const switchPref = (name, label, hint) => {
      const input = h('input', { type: 'checkbox', class: 'switch', role: 'switch' });
      input.checked = !!prefs.get(name);
      input.addEventListener('change', () => prefs.set(name, input.checked));
      return h('label', { class: 'switch-line files-setting' }, h('span', { class: 'switch-text' }, h('span', { text: label }), hint ? h('span', { class: 'hint', text: hint }) : null), input);
    };
    const numberPref = (name, label, min, max, unit = '') => {
      const input = h('input', { type: 'number', min: String(min), max: String(max), step: '1', inputmode: 'numeric' });
      input.value = String(prefs.get(name));
      input.addEventListener('change', () => {
        const n = Math.max(min, Math.min(max, Math.round(Number(input.value) || min)));
        input.value = String(n);
        prefs.set(name, n);
      });
      return field(`${label}${unit ? interpolate(gettext(' (%(unit)s)'), { unit }, true) : ''}`, input);
    };
    const storageSelect = storages.length ? (() => {
      const select = h('select', { 'aria-label': gettext('Storage source') }, h('option', { value: '', text: gettext('Choose a storage source') }),
        ...storages.map((s) => h('option', { value: s.key, text: s.name, selected: current === s.key })));
      select.addEventListener('change', () => { if (select.value) location.href = `/${encodeURIComponent(select.value)}/`; });
      return h('section', { class: 'files-settings-group files-settings-storage' }, h('h3', { text: gettext('Storage source') }), field(gettext('Storage source'), select));
    })() : null;
    const forget = h('button', { type: 'button', class: 'btn', text: gettext('Forget unlocked folders') });
    forget.addEventListener('click', async () => {
      forget.disabled = true;
      try {
        if (onForgetPasswords) await onForgetPasswords();
        toast(pgettext('notice', 'Done'));
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        forget.disabled = false;
      }
    });
    const sheet = h('dialog', { class: 'dialog side-sheet files-settings', 'aria-labelledby': `${sheetId}-title` },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { id: `${sheetId}-title`, text: gettext('Preferences') }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        h('div', { class: 'dialog-body' },
          h('p', { class: 'hint files-settings-tip', text: gettext('Tip: settings are saved automatically and stay after a reload.') }),
          storageSelect,
          h('section', { class: 'files-settings-group' }, h('h3', { text: gettext('View') }),
            segmentedPref('view', gettext('Display'), [['list', gettext('List view')], ['grid', gettext('Icon view')]]),
            segmentedPref('size', gettext('Size'), [['small', gettext('Small')], ['default', gettext('Medium')], ['large', gettext('Large')]])),
          h('section', { class: 'files-settings-group' }, h('h3', { text: gettext('Gallery') }),
            numberPref('galleryColumns', gettext('Columns'), 1, 12),
            numberPref('galleryColumnGap', gettext('Column gap'), 0, 100, 'px'),
            numberPref('galleryRowGap', gettext('Row gap'), 0, 100, 'px'),
            switchPref('galleryRounded', gettext('Rounded image borders')),
            switchPref('galleryNames', gettext('Show names')),
            segmentedPref('galleryNameMode', gettext('Name position'), [['hover', gettext('On hover')], ['bottom', gettext('Below')]]),
            segmentedPref('previewMode', gettext('Double-click preview'), [['full', gettext('Show all')], ['only', gettext('Show one')]]),
            switchPref('galleryZoom', gettext('Click to enlarge')),
            switchPref('backToTop', gettext('Back-to-top button'))),
          h('section', { class: 'files-settings-group' }, h('h3', { text: gettext('Other') }), h('div', { class: 'btn-row files-settings-actions' }, forget)))));
    sheet.addEventListener('close', () => sheet.remove());
    document.body.append(sheet);
    openDialog(sheet);
    return sheet;
  }

  window.App.files = {
    mount: (element, options) => new Browser(element, options),
    actions,
    previewers,
    prefs,
    openSettings,
    clickedUrl,
    kinds: { icon: itemIcon, labels: KIND_LABELS, extension },
    paths: { normal, parent: parentOf, join, name: baseName, encode: encodePath },
    batchResults,
  };
})();
