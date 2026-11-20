/* The lists of shares (shares/_list.html): 我的分享 on /shares/ (data-mode="mine", GET /api/v1/shares/) and
 * 分享列表 on /manage/#shares (data-mode="admin", GET /api/v1/manage/shares/, with 所属用户), inventory 10.8.
 *
 * Filters (关键词, 所属用户, 存储源, 状态, 创建时间) and pages; per row 复制 Share Key, 复制分享链接,
 * 复制访问密码 and 复制带密码链接 (when it has one), 打开分享 and 删除 (asks first; the owner or a superuser);
 * 清理所有已过期分享 above. The rows come with the password: only the owner and admins get them. */
(function () {
  'use strict';
  const { api, h, icon, toast, fmtNum, fmtDateTime, confirmDialog, copyText, openDialog } = window.App;

  const PAGE_SIZE = 20;
  const FILTERS = ['keyword', 'user', 'storage', 'status', 'since', 'until'];

  // -- one menu at a time, at its button (fixed: the table's wrapper clips what overflows)
  let openMenu = null;
  let menuButton = null;
  function closeMenu(refocus = false) {
    if (!openMenu) return;
    openMenu.remove();
    openMenu = null;
    document.removeEventListener('pointerdown', outside, true);
    if (refocus && menuButton && menuButton.isConnected) menuButton.focus();
  }
  function outside(e) {
    if (openMenu && !openMenu.contains(e.target)) closeMenu();
  }
  function popup(anchor, entries) {
    closeMenu();
    menuButton = anchor;
    const menu = h('div', { class: 'menu share-row-menu', role: 'menu', 'aria-label': gettext('Actions') },
      ...entries.map(({ label, iconName, run, danger }) => h('button', {
        type: 'button', class: `menu-item${danger ? ' danger' : ''}`, role: 'menuitem',
        onclick: () => { closeMenu(true); run(); },
      }, icon(iconName), label)));
    document.body.append(menu);
    const box = anchor.getBoundingClientRect();
    const own = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(window.innerWidth - own.width - 8, box.right - own.width))}px`;
    menu.style.top = `${box.bottom + own.height > window.innerHeight - 8 ? Math.max(8, box.top - own.height) : box.bottom}px`;
    openMenu = menu;
    const items = () => [...menu.querySelectorAll('.menu-item')];
    items()[0].focus();
    menu.addEventListener('keydown', (e) => {
      const all = items();
      const at = all.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); all[(at + 1) % all.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); all[(at - 1 + all.length) % all.length].focus(); }
      else if (e.key === 'Home') { e.preventDefault(); all[0].focus(); }
      else if (e.key === 'End') { e.preventDefault(); all[all.length - 1].focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
      else if (e.key === 'Tab') closeMenu();
    });
    setTimeout(() => {
      document.addEventListener('pointerdown', outside, true);
      window.addEventListener('scroll', () => closeMenu(), { once: true, passive: true });
      window.addEventListener('resize', () => closeMenu(), { once: true });
    });
  }

  async function copy(text, done) {
    if (await copyText(text)) toast(done);
    else toast(gettext('Copying failed. Please copy it by hand.'), 'error');
  }

  // 包含的文件 / 文件夹
  function entriesDialog(share) {
    const titleId = `share-entries-${Math.random().toString(36).slice(2)}`;
    const dialog = h('dialog', { class: 'dialog dialog-small', 'aria-labelledby': titleId },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: gettext('Files and folders in it') }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        h('div', { class: 'dialog-body' },
          h('p', { class: 'muted small', text: `${share.storage.name} · ${share.folder}` }),
          h('ul', { class: 'share-entries-list' }, ...share.entries.map((entry) => h('li', null,
            icon(entry.type === 'folder' ? 'folder' : 'draft'), h('span', { text: entry.name })))),
          h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-filled', 'data-close': '', text: gettext('Close') })))));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    openDialog(dialog);
  }

  function setup(root) {
    const admin = root.dataset.mode === 'admin';
    const base = root.dataset.api;
    const filters = root.querySelector('[data-filters]');
    const rows = root.querySelector('[data-rows]');
    const summary = root.querySelector('[data-summary]');
    const pager = root.querySelector('[data-pager]');
    const refresh = root.querySelector('[data-refresh]');
    const clear = root.querySelector('[data-clear-expired]');
    const columns = admin ? 7 : 6;
    let page = 1;
    let count = 0;
    let generation = 0;
    let loaded = false;

    function query() {
      const params = new URLSearchParams();
      for (const name of FILTERS) {
        const input = filters.elements[name];
        if (!input) continue;
        const value = input.value.trim();
        if (value && !(name === 'status' && value === 'all')) params.set(name, value);
      }
      return params;
    }

    const messageRow = (text, error = false) => h('tr', { class: 'message-row' },
      h('td', { colspan: String(columns) }, h('span', { class: error ? 'field-error' : '', text })));

    function expiry(share) {
      if (!share.expires_at) return h('span', { text: gettext('Never expires') });
      const when = fmtDateTime(share.expires_at);
      return share.expired ? h('span', { class: 'share-expired', text: interpolate(gettext('Expired · %(when)s'), { when }, true) }) : h('span', { text: when });
    }

    function actions(share) {
      const list = [
        { label: gettext('Copy share key'), iconName: 'key', run: () => copy(share.key, gettext('Share key copied to the clipboard')) },
        { label: gettext('Copy share link'), iconName: 'link', run: () => copy(share.url, gettext('Share link copied to the clipboard')) },
      ];
      if (share.has_password) {
        list.push({ label: gettext('Copy access password'), iconName: 'password', run: () => (share.password ? copy(share.password, gettext('Access password copied to the clipboard')) : toast(gettext("Couldn't get the share's password"), 'error')) });
        list.push({ label: gettext('Copy link with password'), iconName: 'content_copy', run: () => (share.password
          ? copy(`${share.url}?pwd=${encodeURIComponent(share.password)}`, gettext('Share link with password copied to the clipboard')) : toast(gettext("Couldn't get the share's password"), 'error')) });
      }
      list.push({ label: gettext('Open share'), iconName: 'open_in_new', run: () => window.open(window.App.pageUrl(share.url), '_blank', 'noopener') });
      if (share.can_delete) {
        list.push({ label: gettext('Delete'), iconName: 'delete', danger: true, run: () => remove(share) });
      }
      return list;
    }

    function row(share) {
      const first = share.entries[0] || { name: '', type: 'file' };
      const many = share.entry_count > 1;
      const more = h('button', { type: 'button', class: 'icon-button small', 'aria-label': interpolate(gettext('Actions for %(label)s'), { label: share.label }, true), 'data-tooltip': gettext('Actions') }, icon('more_vert'));
      more.addEventListener('click', () => popup(more, actions(share)));
      const name = h('span', { class: 'share-content-name' }, h('span', { text: first.name }),
        many ? h('button', { type: 'button', class: 'share-more-items', text: interpolate(ngettext('and others (%(entry_count)s item)', 'and others (%(entry_count)s items)', share.entry_count), { entry_count: share.entry_count }, true), onclick: () => entriesDialog(share) }) : null,
        share.has_password ? h('span', { class: 'badge badge-info' }, icon('lock'), pgettext('share badge', 'Password')) : null);
      const cells = [
        h('td', { 'data-label': gettext('Content') }, h('div', { class: 'share-content' },
          icon(many ? 'file_copy' : (first.type === 'folder' ? 'folder' : 'draft')),
          h('div', { class: 'share-content-text' }, name,
            h('span', { class: 'share-folder muted small' }, h('span', { class: 'share-key', text: share.key }), ` · ${share.folder}`)))),
      ];
      if (admin) {
        const owner = share.owner || {};
        cells.push(h('td', { 'data-label': gettext('Owner') }, h('span', { text: owner.nickname || owner.username || '' }),
          owner.nickname ? h('span', { class: 'muted small', text: ` @${owner.username}` }) : null));
      }
      cells.push(
        h('td', { class: 'share-storage', 'data-label': gettext('Storage source'), text: share.storage.name }),
        h('td', { 'data-label': gettext('Valid until') }, expiry(share)),
        h('td', { class: 'share-counts', 'data-label': gettext('Stats'), text: interpolate(gettext('Visits %(access_count)s · Downloads %(download_count)s'), { access_count: fmtNum(share.access_count), download_count: fmtNum(share.download_count) }, true) }),
        h('td', { class: 'nowrap', 'data-label': gettext('Created at'), text: fmtDateTime(share.created_at) }),
        h('td', { class: 'cell-actions row-actions' }, more));
      return h('tr', null, ...cells);
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

    async function load() {
      loaded = true;
      const mine = ++generation;
      const params = query();
      const filtered = [...params.keys()].length > 0;
      params.set('page', page);
      params.set('page_size', PAGE_SIZE);
      summary.textContent = gettext('Loading…');
      refresh.disabled = true;
      try {
        const data = await api('GET', `${base}?${params}`);
        if (mine !== generation) return;
        count = data.count;
        summary.textContent = interpolate(filtered ? ngettext('%(count)s share found', '%(count)s shares found', count) : ngettext('%(count)s share', '%(count)s shares', count), { count: fmtNum(count) }, true);
        rows.replaceChildren(...(data.results.length ? data.results.map(row)
          : [messageRow(filtered ? gettext('No shares match.') : (admin ? gettext('No one has created a share yet.') : gettext('You haven\'t created any shares yet: select files or folders in the file list, right-click and choose "Create share".')))]));
        renderPager();
      } catch (err) {
        if (mine !== generation) return;
        if (err.status === 404 && page > 1) { // that page is gone (shares were deleted meanwhile)
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

    async function remove(share) {
      const ok = await confirmDialog({ title: gettext('Delete this share?'), message: interpolate(gettext('Once deleted, the share link of "%(label)s" stops working.'), { label: share.label }, true), confirmText: gettext('Delete'), danger: true });
      if (!ok) return;
      try {
        await api('DELETE', `${base}${encodeURIComponent(share.key)}/`);
        toast(gettext('Share deleted'));
        load();
      } catch (err) {
        toast(err.message, 'error');
      }
    }

    clear.addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: gettext('Delete all expired shares?'),
        message: admin ? gettext("Every user's expired shares are deleted. This can't be undone.") : gettext("Your expired shares are deleted. This can't be undone."),
        confirmText: gettext('Clean up'),
        danger: true,
      });
      if (!ok) return;
      try {
        const done = await api('POST', `${base}clear-expired/`);
        toast(interpolate(ngettext('Deleted %(deleted)s expired share', 'Deleted %(deleted)s expired shares', done.deleted), { deleted: fmtNum(done.deleted) }, true));
        go(1);
      } catch (err) {
        toast(err.message, 'error');
      }
    });

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

    if (admin) {
      // Loads the first time the section is shown (manage.js announces each one).
      document.addEventListener('manage:show', (e) => {
        if (e.detail === 'shares' && !loaded) load();
      });
    } else {
      load();
    }
  }

  document.querySelectorAll('[data-share-list]').forEach(setup);
})();
