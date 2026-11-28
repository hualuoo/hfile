/* /manage/#storages: the storage sources (apps/storages/views.py has the API). Pages, by the address:
 *
 *   #storages                    the list: drag the handle (or Alt+↑/↓ on it) to sort; the ⋮ menu edits,
 *                                opens the rules, copies, checks, enables or disables, deletes
 *   #storages/new, #storages/<id>/setup    the add wizard, one step at a time like the installer: 基本信息,
 *                                密钥 (saved there, off), 授权 or 测试连接, 其他设置 (完成 turns it on)
 *   #storages/<id>                the form, made from the driver's params (GET storage-types/):
 *                                secrets are write-only ("已设置", empty keeps them), "更多设置" holds the
 *                                advanced ones, helper buttons and 授权 run on the server with the form's
 *                                values; what they give that is secret stays there, in the form's draft
 *   #storages/<id>/filters | passwords | readmes   a storage's rules, with the rule tester
 *
 * Everything the admin typed goes in as text (textContent, values), never as HTML. */
(function () {
  'use strict';
  const App = window.App;
  const { api, h, icon, toast, confirmDialog, editDialog, openDialog, setTitle, fmtDateTime, fmtRelative, copyText } = App;
  const root = document.getElementById('storages-root');
  if (!root) return;

  Object.assign(App.fieldLabels, {
    key: gettext('Alias'), name: gettext('Name'), type: gettext('Type'), remark: gettext('Remark'), config: gettext('Settings'), download_domain: gettext('Download domain'),
    link_lifetime: gettext('Download link lifetime'), ids: gettext('Order'), expression: gettext('Expression'), description: gettext('Description'), mode: gettext('Mode'), text: gettext('Document'),
    path_mode: gettext('Path mode'), display: gettext('Shown'), root_path: gettext('Root folder'), permissions: gettext('Permissions'), storage: gettext('Storage source'), path: gettext('Path'),
    clear_secrets: gettext('Keys to clear'), rules: gettext('Rules'), kind: gettext('Rule type'), root: gettext("User's root folder"),
    icon: gettext('Icon'), show_in_nav: gettext('Show in the sidebar'),
  });

  const API = '/api/v1/manage/storages/';
  const TYPE_ICONS = {
    local: 'dns', ftp: 'dns', sftp: 'dns', webdav: 'language', smb: 'dns', onedrive: 'cloud', 'onedrive-china': 'cloud',
    'onedrive-app': 'cloud', 'onedrive-app-china': 'cloud', sharepoint: 'cloud', 'sharepoint-china': 'cloud', 'google-drive': 'cloud', open115: 'cloud', 'alipan-open': 'cloud', 'baidu-netdisk': 'cloud', open123: 'cloud', 'yandex-disk': 'cloud',
    dropbox: 'cloud', 'github-repo': 'code', febbox: 'cloud', s3: 'storage', openlist: 'language', seafile: 'language', kodbox: 'language', cloudreve: 'language', 'cloudreve-v3': 'language', teldrive: 'language', misskey: 'language', 'cf-imgbed': 'image', ipfs: 'language', emby: 'movie',
    'github-releases': 'link', 'cnb-releases': 'link', autoindex: 'link', 'url-tree': 'link', '123-link': 'link', 'openlist-share': 'share', 'lenovo-nas-share': 'share', 'onedrive-share': 'share',
  };
  const typeIcon = (type) => TYPE_ICONS[type] || 'storage';
  const HELPER_ICONS = { buckets: 'search', sites: 'search', drives: 'search', cors: 'visibility', set_cors: 'shield', qrcode: 'qr_code_2' };
  const RULE_KINDS = {
    filters: { title: gettext('Filter rules'), icon: 'filter_list', help: 'storages-help-filters', empty: gettext('No filter rules yet.') },
    passwords: { title: gettext('Folder passwords'), icon: 'lock', help: 'storages-help-passwords', empty: gettext('No folder passwords yet.') },
    readmes: { title: gettext('Folder readme'), icon: 'description', help: 'storages-help-readmes', empty: gettext('No folder readmes yet.') },
  };
  const FILTER_MODES = [['hidden', gettext('Hide only')], ['inaccessible', gettext('Hide and block access')], ['disable_download', gettext('Hide and block downloads')]];
  const PATH_MODES = [['relative', gettext('Relative path')], ['absolute', gettext('Absolute path')]];
  const DISPLAYS = [['top', gettext('At the top')], ['bottom', gettext('Below')], ['dialog', gettext('In a dialog')]];

  let seq = 0;
  const uid = (prefix) => `${prefix}-${++seq}`;

  // ------------------------------------------------------------------ small parts

  // Help texts are plain text; web addresses in them become links (opened in a new tab).
  function linkified(text) {
    const parts = [];
    const pattern = /https?:\/\/[^\s，。、；：（）()「」<>"']+/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(text))) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      parts.push(h('a', { href: match[0], target: '_blank', rel: 'noopener noreferrer', text: match[0] }));
      last = match.index + match[0].length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  }

  function template(id) {
    const tpl = document.getElementById(id);
    return tpl ? tpl.content.cloneNode(true) : document.createDocumentFragment();
  }

  // An M3 outlined text field: the label in the outline's notch, the help under it.
  function field({ id, label, input, required = false, hint = '', wide = false, after = null }) {
    input.id = id;
    const labelEl = h('label', { for: id }, label, required ? h('span', { class: 'required-mark', 'aria-hidden': 'true', text: '*' }) : null);
    if (required) input.setAttribute('aria-required', 'true');
    return h('div', { class: `field${wide ? ' span-2' : ''}`, dataset: { field: id } },
      labelEl, input,
      hint ? h('p', { class: 'hint' }, ...(typeof hint === 'string' ? linkified(hint) : [hint])) : null,
      after);
  }

  function switchLine({ id, label, checked, hint = '' }) {
    const input = h('input', { type: 'checkbox', class: 'switch', role: 'switch', id });
    input.checked = Boolean(checked);
    return {
      input,
      el: h('div', { class: 'switch-field', dataset: { field: id } },
        h('label', { class: 'switch-line', for: id }, h('span', { text: label }), input),
        hint ? h('p', { class: 'hint' }, ...linkified(hint)) : null),
    };
  }

  // A storage's icon: one of storage-types/'s icons, radio buttons drawn as icon buttons; the chosen one filled
  // (as in the sidebar). value() is the chosen name; changed(): not the one it showed at first. Only a changed
  // one is saved: a stored icon this program doesn't offer (a later version's) shows as storage, and stays.
  function iconPicker({ id, icons, value }) {
    const inputs = icons.map((item) => {
      const input = h('input', { type: 'radio', name: id, value: item.name });
      input.checked = item.name === value;
      return input;
    });
    const el = h('fieldset', { class: 'icon-picker', dataset: { field: id } },
      h('legend', { text: gettext('Icon') }),
      h('div', { class: 'icon-choices' }, icons.map((item, index) => h('label', { class: 'icon-choice', dataset: { tooltip: item.label } },
        inputs[index], h('span', { class: 'icon-button' }, icon(item.name, 'off'), icon(`${item.name}-fill`, 'on')),
        h('span', { class: 'sr-only', text: item.label })))),
      h('p', { class: 'hint', text: gettext('Shown in the sidebar, the storage source menu and the list of storage sources on the home page.') }));
    const chosen = () => inputs.find((input) => input.checked).value;
    return { el, value: chosen, changed: () => chosen() !== value };
  }

  function navSwitch(id, checked) {
    return switchLine({ id, label: gettext('Show in the sidebar'), checked,
      hint: gettext("A link to it in the sidebar on the left, for those who may use it. Not on phones, whose bar has no room for it: there it stays on the home page's list and in ⋮ → Preferences.") });
  }

  function select(options, value) {
    const el = h('select', {}, options.map(([v, label]) => h('option', { value: v, text: label })));
    if (value !== undefined && value !== null) el.value = value;
    return el;
  }

  // The storage types (GET storage-types/), in their groups.
  function typeSelect(kinds, value) {
    const groups = new Map();
    const loose = [];
    for (const t of kinds) {
      const option = h('option', { value: t.type, text: t.label });
      if (!t.group) {
        loose.push(option);
        continue;
      }
      if (!groups.has(t.group)) groups.set(t.group, h('optgroup', { label: t.group_label }));
      groups.get(t.group).append(option);
    }
    const el = h('select', {}, [...groups.values(), ...loose]);
    if (value !== undefined && value !== null) el.value = value;
    return el;
  }

  function setFieldError(container, message) {
    if (!container) return false;
    container.classList.toggle('invalid', Boolean(message));
    let box = container.querySelector(':scope > .field-error');
    if (!message) {
      if (box) box.remove();
      return true;
    }
    if (!box) {
      box = h('p', { class: 'field-error', role: 'alert' });
      container.append(box);
    }
    box.textContent = message;
    return true;
  }

  const firstMessage = (value) => (Array.isArray(value) ? value.map(String).join(gettext('; ')) : (value && typeof value === 'object' ? App.errorText(value, 400, pgettext('between clauses', ', ')) : String(value)));

  function backTitle(text, href) {
    return h('div', { class: 'section-title-row' },
      h('div', { class: 'title-with-back' },
        h('a', { class: 'icon-button', href, 'aria-label': gettext('Back to the storage sources'), 'data-tooltip': gettext('Back') }, icon('arrow_back')),
        h('h2', { text })));
  }

  function notice(kind, text) {
    return h('div', { class: `notice ${kind ? `notice-${kind}` : ''}`, role: kind === 'error' ? 'alert' : 'status', text });
  }

  function menuItem(iconName, label, onClick, { danger = false, disabled = false } = {}) {
    return h('button', {
      type: 'button', class: `menu-item${danger ? ' danger' : ''}`, role: 'menuitem',
      'aria-disabled': disabled ? 'true' : null, onclick: disabled ? null : onClick,
    }, icon(iconName), label);
  }

  // A list whose rows move by dragging their .drag-handle (mouse, touch, pen), or with Alt+↑/↓ while
  // the handle has focus. onMoved() runs after a row changed place.
  function sortable(list, onMoved) {
    let dragging = null;
    let moved = false;
    list.addEventListener('pointerdown', (e) => {
      const handle = e.target.closest('.drag-handle');
      if (!handle || e.button !== 0 || !list.contains(handle)) return;
      dragging = handle.closest('.sortable-row');
      moved = false;
      handle.setPointerCapture(e.pointerId);
      dragging.classList.add('dragging');
      e.preventDefault();
    });
    list.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const under = document.elementFromPoint(e.clientX, e.clientY);
      const target = under && under.closest('.sortable-row');
      if (!target || target === dragging || target.parentElement !== list) return;
      const box = target.getBoundingClientRect();
      list.insertBefore(dragging, e.clientY > box.top + box.height / 2 ? target.nextSibling : target);
      moved = true;
    });
    const end = () => {
      if (!dragging) return;
      dragging.classList.remove('dragging');
      dragging = null;
      if (moved) onMoved();
    };
    list.addEventListener('pointerup', end);
    list.addEventListener('pointercancel', end);
    list.addEventListener('keydown', (e) => {
      const handle = e.target.closest('.drag-handle');
      if (!handle || !e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
      const row = handle.closest('.sortable-row');
      const other = e.key === 'ArrowUp' ? row.previousElementSibling : row.nextElementSibling;
      if (!other) return;
      e.preventDefault();
      list.insertBefore(row, e.key === 'ArrowUp' ? other : other.nextSibling);
      handle.focus();
      onMoved();
    });
  }

  function dragHandle(label) {
    return h('button', { type: 'button', class: 'icon-button drag-handle', 'aria-label': label, 'data-tooltip': gettext('Drag to sort (keyboard: Alt + ↑ / ↓)') },
      icon('drag_indicator'));
  }

  // ------------------------------------------------------------------ the pages, by the address

  let current = null; // the page being drawn; an older one that finishes late draws nothing
  let types = null;
  let formState = null; // the storage form on the page, for the OAuth window's message
  let unsaved = false; // the form or the rules on the page have changes that aren't saved

  // Leaving a page with changes asks first: links of the page and of /manage/'s menu (the address alone
  // can't be stopped: the browser's back button just leaves), and closing or reloading the tab.
  document.addEventListener('click', async (e) => {
    const link = e.target.closest('a[href^="#"]');
    if (!link || !unsaved || e.defaultPrevented || link.target) return;
    e.preventDefault();
    e.stopPropagation();
    const leave = await confirmDialog({ title: gettext('Leave this page?'), message: gettext("The changes aren't saved; leaving loses them."), confirmText: gettext('Leave'), danger: true });
    if (leave) {
      unsaved = false;
      location.hash = link.getAttribute('href');
    }
  }, true);
  window.addEventListener('beforeunload', (e) => {
    if (!unsaved) return;
    e.preventDefault();
    e.returnValue = '';
  });

  function route() {
    let raw = location.hash.slice(1);
    try {
      raw = decodeURIComponent(raw);
    } catch (err) {
      // a mangled address (a lone %): read it as it is
    }
    const [path, query = ''] = raw.split('?');
    return { parts: path.split('/').filter(Boolean).slice(1), query: new URLSearchParams(query) };
  }

  async function loadTypes() {
    if (!types) {
      const data = await api('GET', '/api/v1/manage/storage-types/');
      types = { list: data.types, byType: Object.fromEntries(data.types.map((t) => [t.type, t])), redirect: data.oauth_redirect_uri,
        superuser: Boolean(data.superuser), icons: data.icons || [] };
    }
    return types;
  }

  async function show() {
    const { parts, query } = route();
    const page = {};
    current = page;
    unsaved = false;
    const isId = (text) => /^[0-9]+$/.test(text || '');
    try {
      if (!parts.length) await listPage(page);
      else if (parts[0] === 'new' && parts.length === 1) await setupPage(page, null, query);
      else if (isId(parts[0]) && parts.length === 1) await formPage(page, Number(parts[0]), query);
      else if (isId(parts[0]) && parts.length === 2 && parts[1] === 'setup') await setupPage(page, Number(parts[0]), query);
      else if (isId(parts[0]) && parts.length === 2 && RULE_KINDS[parts[1]]) await rulesPage(page, Number(parts[0]), parts[1]);
      else location.hash = '#storages';
    } catch (err) {
      if (current !== page) return;
      root.replaceChildren(backTitle(pgettext('manage menu', 'Storage sources'), '#storages'), notice('error', interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true)));
    }
  }
  const stale = (page) => current !== page;

  document.addEventListener('manage:show', (e) => { if (e.detail === 'storages') show(); });

  // ------------------------------------------------------------------ the list

  async function listPage(page) {
    const items = await api('GET', API);
    if (stale(page)) return;
    App.refreshRail();  // the changes made here, which show the list again
    setTitle(gettext('Storage sources · Manage'));
    const add = h('a', { class: 'btn btn-filled', href: '#storages/new' }, icon('add'), gettext('New storage source'));
    const head = h('div', { class: 'section-title-row' }, h('h2', { text: pgettext('manage menu', 'Storage sources') }), add);
    if (!items.length) {
      root.replaceChildren(head, template('storages-help-list'),
        h('div', { class: 'empty-state' }, icon('storage'), h('h2', { text: gettext('No storage sources yet') }),
          h('p', { text: gettext('Create a storage source to put a folder on the server, an object storage or a cloud drive on the site.') }),
          h('a', { class: 'btn btn-filled', href: '#storages/new' }, icon('add'), gettext('New storage source'))));
      return;
    }
    const search = h('input', { type: 'search', class: 'storages-search', placeholder: gettext('Search name, alias, type or remark'), 'aria-label': gettext('Search storage sources') });
    const list = h('ul', { class: 'list list-divided storage-list', 'aria-label': gettext('Storage sources; drag to sort') });
    const nothing = h('p', { class: 'muted storages-nothing', hidden: true, text: gettext('No storage sources match.') });
    const menus = [];
    items.forEach((item, index) => {
      const { li, button } = storageRow(item, index, items.length);
      list.append(li);
      menus.push(button);
    });
    search.addEventListener('input', () => {
      const words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      for (const li of list.children) {
        const text = li.dataset.search;
        const match = words.every((word) => text.includes(word));
        li.hidden = !match;
        if (match) shown += 1;
      }
      nothing.hidden = shown > 0;
    });
    sortable(list, () => saveOrder(list));
    root.replaceChildren(...App.nodes(head, template('storages-help-list'), items.length > 4 ? search : null, list, nothing));
    menus.forEach((button) => App.menu(button));
  }

  function storageRow(item, index, count) {
    const menuId = uid('storage-menu');
    const button = h('button', { type: 'button', class: 'icon-button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      'aria-controls': menuId, 'aria-label': interpolate(gettext('More actions: %(name)s'), { name: item.name }, true), 'data-tooltip': gettext('More') }, icon('more_vert'));
    const go = (hash) => () => { location.hash = hash; };
    const menuList = h('div', { class: 'menu', id: menuId, role: 'menu', hidden: true },
      menuItem('edit', gettext('Edit'), go(`#storages/${item.id}`)),
      // Off: maybe left in the add wizard (saved at 密钥, before 授权); it goes on from 授权.
      item.enabled ? null : menuItem('arrow_forward', gettext('Continue setup'), go(`#storages/${item.id}/setup`)),
      menuItem('filter_list', gettext('Filter rules'), go(`#storages/${item.id}/filters`)),
      menuItem('lock', gettext('Folder passwords'), go(`#storages/${item.id}/passwords`)),
      menuItem('description', gettext('Folder readme'), go(`#storages/${item.id}/readmes`)),
      h('hr', { role: 'separator' }),
      menuItem('network_check', gettext('Check connection'), () => recheck(item)),
      menuItem('content_copy', gettext('Copy'), () => copyStorage(item)),
      menuItem(item.enabled ? 'toggle_off' : 'toggle_on', item.enabled ? gettext('Disable') : gettext('Enable'), () => toggle(item)),
      menuItem('arrow_upward', gettext('Move up'), () => move(item.id, -1), { disabled: index === 0 }),
      menuItem('arrow_downward', gettext('Move down'), () => move(item.id, 1), { disabled: index === count - 1 }),
      h('hr', { role: 'separator' }),
      menuItem('delete', gettext('Delete'), () => remove(item), { danger: true }));
    const status = item.status;
    const supporting = [`/${item.key}`, item.type_label];
    const lines = [h('span', { class: 'list-supporting', text: supporting.join(' · ') })];
    if (item.remark) lines.push(h('span', { class: 'list-supporting storage-remark', text: item.remark }));
    if (status && !status.ok) {
      lines.push(h('span', { class: 'storage-problem', text: interpolate(gettext('Connection failed: %(message)s (%(at)s)'), { message: status.message, at: fmtRelative(status.at) }, true) }));
    }
    if (item.tokens_saved_at) {
      lines.push(h('span', { class: 'list-supporting small', text: interpolate(gettext('Token renewed automatically %(tokens_saved_at)s'), { tokens_saved_at: fmtDateTime(item.tokens_saved_at) }, true) }));
    }
    const badges = h('span', { class: 'storage-badges' },
      item.enabled ? null : h('span', { class: 'badge', text: gettext('Disabled') }),
      status && !status.ok ? h('span', { class: 'badge badge-error' }, icon('error'), gettext('Error')) : null);
    const li = h('li', { class: `sortable-row storage-row${item.enabled ? '' : ' disabled'}`, dataset: { id: String(item.id),
      search: [item.name, item.key, item.type, item.type_label, item.remark].join(' ').toLowerCase() } },
      dragHandle(interpolate(gettext('Drag "%(name)s" to sort'), { name: item.name }, true)),
      h('a', { class: 'storage-main', href: `#storages/${item.id}` },
        h('span', { class: 'list-leading' }, icon(item.icon && item.icon !== 'storage' ? item.icon : typeIcon(item.type))),
        h('span', { class: 'list-text' }, h('span', { class: 'list-headline' }, h('span', { text: item.name }), badges), ...lines)),
      h('span', { class: 'list-trailing' }, h('div', { class: 'menu-wrap' }, button, menuList)));
    return { li, button };
  }

  async function saveOrder(list) {
    const ids = [...list.children].map((li) => Number(li.dataset.id));
    try {
      await api('POST', `${API}sort/`, { ids });
      toast(gettext('Order saved'));
    } catch (err) {
      toast(interpolate(gettext("The order wasn't saved: %(message)s"), { message: err.message }, true), 'error');
    }
    show();
  }

  async function move(id, step) {
    const list = root.querySelector('.storage-list');
    const row = list.querySelector(`[data-id="${id}"]`);
    const other = step < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (!other) return;
    list.insertBefore(row, step < 0 ? other : other.nextSibling);
    saveOrder(list);
  }

  async function recheck(item) {
    toast(interpolate(gettext('Checking "%(name)s"…'), { name: item.name }, true));
    try {
      const data = await api('POST', `${API}${item.id}/check/`);
      toast(data.status && data.status.ok ? interpolate(gettext('"%(name)s" connects fine'), { name: item.name }, true) : interpolate(gettext('"%(name)s" failed to connect: %(message)s'), { name: item.name, value: data.status ? data.status.message : '' }, true),
        data.status && data.status.ok ? '' : 'error');
    } finally {
      show();
    }
  }

  async function toggle(item) {
    const enabling = !item.enabled;
    const yes = await confirmDialog({
      title: enabling ? interpolate(gettext('Enable "%(name)s"?'), { name: item.name }, true) : interpolate(gettext('Disable "%(name)s"?'), { name: item.name }, true),
      message: enabling ? gettext("The connection is checked first; if it fails, the storage source isn't enabled.") : gettext("Once disabled, users can't see it or open its files, direct links and shares; its settings and permissions are kept."),
      confirmText: enabling ? gettext('Enable') : gettext('Disable'), danger: !enabling,
    });
    if (!yes) return;
    try {
      await api('POST', `${API}${item.id}/${enabling ? 'enable' : 'disable'}/`);
      toast(enabling ? pgettext('notice', 'Enabled') : gettext('Disabled'));
    } catch (err) {
      toast(err.message, 'error');
    }
    show();
  }

  async function remove(item) {
    const yes = await confirmDialog({
      title: interpolate(gettext('Delete "%(name)s"?'), { name: item.name }, true),
      message: gettext("Its filter rules, folder passwords, folder readmes and every user's permissions on it are deleted too, and its direct links, short links and shares stop working. The files in the storage aren't touched."),
      confirmText: gettext('Delete'), danger: true,
    });
    if (!yes) return;
    await api('DELETE', `${API}${item.id}/`);
    toast(interpolate(gettext('Deleted "%(name)s"'), { name: item.name }, true));
    show();
  }

  async function copyStorage(item) {
    const nameInput = h('input', { maxlength: '64', 'data-autofocus': '' });
    nameInput.value = interpolate(gettext('%(name)s copy'), { name: item.name }, true).slice(0, 64);
    const keyInput = h('input', { maxlength: '32', autocapitalize: 'off', spellcheck: 'false' });
    keyInput.value = `${item.key}-copy`.slice(0, 32);
    const body = h('div', {},
      h('p', { class: 'muted', text: gettext("Copies its type, settings, download settings, filter rules, folder passwords, folder readmes and every user's permissions.") }),
      field({ id: uid('copy-name'), label: gettext('Name of the new storage source'), input: nameInput, required: true }),
      field({ id: uid('copy-key'), label: gettext('Alias of the new storage source'), input: keyInput, required: true, hint: gettext('The name in addresses: lowercase letters, digits, - and _ only.') }));
    const created = await editDialog({
      title: interpolate(gettext('Copy "%(name)s"'), { name: item.name }, true), body, submitText: gettext('Copy'), submitIcon: 'content_copy',
      save: async () => {
        try {
          return await api('POST', `${API}${item.id}/copy/`, { name: nameInput.value, key: keyInput.value });
        } catch (err) {
          const data = err.data || {};
          const named = setFieldError(nameInput.closest('.field'), data.name ? firstMessage(data.name) : '')
            && setFieldError(keyInput.closest('.field'), data.key ? firstMessage(data.key) : '');
          if ((data.name || data.key) && named) return undefined;
          throw err;
        }
      },
    });
    if (created) {
      toast(created.enabled || !item.enabled ? interpolate(gettext('Copied as "%(name)s"'), { name: created.name }, true)
        : interpolate(gettext('Copied as "%(name)s". It\'s disabled: two storage sources can\'t share a 115 token, so scan to log in again in its settings, then enable it.'), { name: created.name }, true));
      show();
    }
  }

  // ------------------------------------------------------------------ the form

  async function formPage(page, id, query) {
    const [kinds, storage] = await Promise.all([loadTypes(), id ? api('GET', `${API}${id}/`) : null]);
    if (stale(page)) return;
    // D50: a type only superusers create (local) isn't offered to other admins for a new storage.
    const offered = (t) => Boolean(t) && (kinds.superuser || !t.superuser_only);
    const firstType = ([kinds.byType[query.get('type')], kinds.byType.local, ...kinds.list].find(offered) || kinds.list[0]).type;
    const state = {
      id, storage, page, kinds,
      type: kinds.byType[storage ? storage.type : firstType],
      draft: query.get('draft') || null,
      clear: new Set(), drafted: new Set(), inputs: {}, paramFields: {}, base: {},
    };
    if (storage && !state.type) {
      root.replaceChildren(backTitle(storage.name, '#storages'), notice('error', interpolate(gettext("This program doesn't know this storage source's type (%(type)s), or its driver can't load."), { type: storage.type }, true)));
      return;
    }
    setTitle(storage ? interpolate(gettext('Edit "%(name)s" · Manage'), { name: storage.name }, true) : gettext('New storage source · Manage'));
    drawForm(state);
    if (state.draft) await restoreDraft(state);
  }

  function drawForm(state) {
    const { storage } = state;
    formState = state;
    const form = h('form', { class: 'storage-form', novalidate: true });
    const error = h('div', { class: 'form-error', role: 'alert', hidden: true });
    state.form = form;
    state.error = error;

    // -- 基本信息
    const b = state.base;
    b.name = h('input', { maxlength: '64', autocomplete: 'off' });
    b.key = h('input', { maxlength: '32', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
    b.remark = h('textarea', { rows: '2', maxlength: '500' });
    b.type = typeSelect(state.kinds.list, state.type.type);
    const reserved = storage || state.kinds.superuser ? [] : state.kinds.list.filter((t) => t.superuser_only);
    for (const option of b.type.options) {
      if (reserved.some((t) => t.type === option.value)) option.disabled = true;
    }
    if (storage) {
      b.name.value = storage.name;
      b.key.value = storage.key;
      b.remark.value = storage.remark || '';
      b.type.disabled = true;
    }
    const enabled = switchLine({ id: uid('sf-enabled'), label: gettext('Enabled'), checked: storage ? storage.enabled : true,
      hint: gettext("Disabled storage sources aren't shown on the site and can't be opened. Enabling checks the connection first.") });
    const gallery = switchLine({ id: uid('sf-gallery'), label: gettext('Open in gallery view'), checked: storage && storage.gallery_default,
      hint: gettext('Opens gallery view whenever you switch to this storage source; good for storage sources of pictures.') });
    const nav = navSwitch(uid('sf-nav'), storage && storage.show_in_nav);
    b.enabled = enabled.input;
    b.gallery_default = gallery.input;
    b.icon = iconPicker({ id: uid('sf-icon'), icons: state.kinds.icons, value: storage ? storage.icon : 'storage' });
    b.show_in_nav = nav.input;
    const keyHint = h('span', { text: gettext('The name in addresses: /alias/folder/file. Lowercase letters, digits, - and _ only.') });
    const basic = h('section', { class: 'form-section' },
      h('h3', { text: gettext('Basic info') }),
      h('div', { class: 'form-grid' },
        field({ id: uid('sf-name'), label: gettext('Name'), input: b.name, required: true, hint: gettext('The name users see.') }),
        field({ id: uid('sf-key'), label: gettext('Alias'), input: b.key, required: true, hint: keyHint }),
        field({ id: uid('sf-type'), label: gettext('Storage type'), input: b.type, required: true,
          hint: storage ? gettext("Can't change once created: settings and paths mean nothing for another type.")
            : reserved.length ? interpolate(gettext('%(kinds)s can only be created by superusers: it can read and write any folder the program can reach on the server.'), { kinds: reserved.map((t) => t.label).join(gettext(', ')) }, true) : '' }),
        field({ id: uid('sf-remark'), label: gettext('Remark'), input: b.remark, hint: gettext('Only admins can see it.') })),
      enabled.el, gallery.el, b.icon.el, nav.el);
    state.fieldOf = { name: b.name, key: b.key, type: b.type, remark: b.remark, enabled: b.enabled, gallery_default: b.gallery_default,
      show_in_nav: b.show_in_nav };

    let keyTimer = null;
    b.key.addEventListener('input', () => {
      clearTimeout(keyTimer);
      keyTimer = setTimeout(async () => {
        const value = b.key.value.trim();
        if (!value) return setFieldError(b.key.closest('.field'), '');
        const params = new URLSearchParams({ key: value });
        if (state.id) params.set('id', String(state.id));
        try {
          const check = await api('GET', `${API}key-check/?${params}`);
          if (b.key.value.trim() === value) setFieldError(b.key.closest('.field'), check.ok ? '' : check.message);
        } catch (err) {
          // checked again when saving
        }
      }, 300);
    });

    const params = h('section', { class: 'form-section', id: 'storage-params' });
    const transfers = h('section', { class: 'form-section' });
    state.paramsSection = params;
    state.transfersSection = transfers;
    b.type.addEventListener('change', () => {
      state.type = state.kinds.byType[b.type.value];
      state.draft = null;
      state.clear.clear();
      state.drafted.clear();
      drawParams(state);
      drawTransfers(state);
    });
    drawParams(state);
    drawTransfers(state);

    const result = h('div', { class: 'form-test-result', 'aria-live': 'polite' });
    state.testResult = result;
    const testButton = h('button', { type: 'button', class: 'btn btn-tonal' }, icon('network_check'), gettext('Test connection'));
    const saveButton = h('button', { type: 'submit', class: 'btn btn-filled' }, icon('save'), gettext('Save'));
    testButton.addEventListener('click', () => testConnection(state, testButton));
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      saveForm(state, saveButton);
    });
    form.addEventListener('input', () => { unsaved = true; });
    form.addEventListener('change', () => { unsaved = true; });
    form.append(error, basic, params, transfers, result,
      h('div', { class: 'form-actions' }, testButton, h('a', { class: 'btn btn-text', href: '#storages', text: gettext('Cancel') }), saveButton));
    const links = storage ? h('div', { class: 'chips storage-links' },
      h('a', { class: 'chip', href: `#storages/${storage.id}/filters` }, icon('filter_list'), gettext('Filter rules')),
      h('a', { class: 'chip', href: `#storages/${storage.id}/passwords` }, icon('lock'), gettext('Folder passwords')),
      h('a', { class: 'chip', href: `#storages/${storage.id}/readmes` }, icon('description'), gettext('Folder readme'))) : null;
    root.replaceChildren(...App.nodes(backTitle(storage ? interpolate(gettext('Edit "%(name)s"'), { name: storage.name }, true) : gettext('New storage source'), '#storages'), links, form));
  }

  function secretPlaceholder(state, param) {
    if (state.clear.has(param.key)) return gettext('Cleared when saved');
    if (state.drafted.has(param.key)) return gettext('Filled in (applies once saved)');
    const known = state.storage && state.type.type === state.storage.type ? (state.storage.secrets || {})[param.key] : null;
    if (known === 'set') return gettext('Set; leave empty to keep it');
    if (known === 'unreadable') return gettext("The saved value can't be read (secret_key has changed); please fill it in again");
    return param.placeholder || '';
  }

  function paramInput(state, param) {
    const sameType = state.storage && state.storage.type === state.type.type;
    const stored = sameType ? (state.storage.config || {})[param.key] : undefined;
    const value = stored === undefined || stored === null ? param.default : stored;
    let input;
    if (param.kind === 'bool') {
      input = h('input', { type: 'checkbox', class: 'switch', role: 'switch' });
      input.checked = Boolean(value);
      return input;
    }
    if (param.kind === 'select') {
      input = select(param.options.map((o) => [o.value, o.label]), value ?? (param.options[0] || {}).value);
    } else if (param.kind === 'textarea') {
      input = h('textarea', { rows: '4', spellcheck: 'false', autocomplete: 'off' });
    } else if (param.kind === 'number' && !param.secret) {
      input = h('input', { type: 'number', inputmode: 'numeric', step: '1', autocomplete: 'off',
        min: param.minimum ?? null, max: param.maximum ?? null });
    } else {
      // Secrets get a password box (no autofill of the admin's own password: new-password).
      const hidden = param.secret || param.kind === 'password';
      input = h('input', { type: hidden ? 'password' : 'text', autocomplete: hidden ? 'new-password' : 'off', spellcheck: 'false',
        autocapitalize: 'off', inputmode: param.kind === 'number' ? 'numeric' : null });
    }
    if (param.secret) input.placeholder = secretPlaceholder(state, param);
    else {
      if (param.placeholder) input.placeholder = param.placeholder;
      if (value !== null && value !== undefined && param.kind !== 'select') input.value = String(value);
    }
    return input;
  }

  function paramField(state, param) {
    const id = uid(`sp-${param.key}`);
    const input = paramInput(state, param);
    state.inputs[param.key] = input;
    // D50: on a saved local storage, its folder and the symlink switch are for superusers.
    const locked = Boolean(state.storage) && !state.kinds.superuser && (state.type.superuser_params || []).includes(param.key);
    if (locked) input.disabled = true;
    // The same note as the settings pages' locked fields (siteconf/manage/_field.html).
    const lockNote = locked ? h('p', { class: 'hint locked-hint' }, icon('lock'), gettext('Only superusers can change this.')) : null;
    let container;
    if (param.kind === 'bool') {
      input.id = id;
      container = h('div', { class: 'switch-field', dataset: { param: param.key } },
        h('label', { class: 'switch-line', for: id }, h('span', { text: param.label }), input),
        param.help ? h('p', { class: 'hint' }, ...linkified(param.help)) : null, lockNote);
    } else {
      let control = input;
      const extras = [];
      if (param.secret) {
        const parts = [input];
        if (input.tagName === 'INPUT') {
          parts.push(h('button', { type: 'button', class: 'icon-button field-button', 'data-toggle-password': '', 'aria-pressed': 'false',
            'aria-label': gettext('Show password'), 'data-tooltip': gettext('Show password') }, icon('visibility')));
        }
        control = input.tagName === 'INPUT' ? h('span', { class: 'input-box' }, ...parts) : input;
        const known = state.storage && state.storage.type === state.type.type ? (state.storage.secrets || {})[param.key] : null;
        if (known === 'set' && !param.required) {
          const clear = h('button', { type: 'button', class: 'btn btn-text btn-small secret-clear', text: gettext('Clear') });
          clear.addEventListener('click', () => {
            unsaved = true;
            if (state.clear.has(param.key)) state.clear.delete(param.key);
            else state.clear.add(param.key);
            clear.textContent = state.clear.has(param.key) ? gettext("Don't clear") : gettext('Clear');
            input.value = '';
            input.disabled = state.clear.has(param.key);
            input.placeholder = secretPlaceholder(state, param);
          });
          extras.push(clear);
        }
      }
      const labelEl = h('label', { for: id }, param.label, param.required ? h('span', { class: 'required-mark', 'aria-hidden': 'true', text: '*' }) : null);
      input.id = id;
      if (param.required) input.setAttribute('aria-required', 'true');
      const wide = param.kind === 'textarea' || (param.help || '').length > 90;
      container = h('div', { class: `field${wide ? ' span-2' : ''}`, dataset: { param: param.key } },
        labelEl, control,
        param.help ? h('p', { class: 'hint' }, ...linkified(param.help)) : null, lockNote,
        extras.length ? h('div', { class: 'field-extras' }, ...extras) : null);
    }
    state.paramFields[param.key] = container;
    return container;
  }

  function drawParams(state) {
    const type = state.type;
    state.inputs = {};
    state.paramFields = {};
    const basic = type.params.filter((p) => !p.advanced);
    const advanced = type.params.filter((p) => p.advanced);
    const section = state.paramsSection;
    const parts = [h('h3', { text: interpolate(gettext('%(label)s settings'), { label: type.label }, true) })];
    if (type.oauth) parts.push(redirectNotice(state));
    parts.push(h('div', { class: 'form-grid' }, basic.map((p) => paramField(state, p))));
    const helperButtons = type.helpers.map((helper) => helperButton(state, helper));
    if (type.oauth) {
      const button = h('button', { type: 'button', class: 'btn btn-tonal btn-small' }, icon('login'), gettext('Authorize'));
      button.addEventListener('click', () => authorize(state, button));
      helperButtons.unshift(button);
    }
    state.helperResult = h('div', { class: 'helper-result', 'aria-live': 'polite' });
    if (helperButtons.length) parts.push(h('div', { class: 'helper-row' }, ...helperButtons), state.helperResult);
    if (advanced.length) {
      parts.push(h('details', { class: 'more-settings' },
        h('summary', {}, icon('expand_more'), gettext('More settings')),
        h('div', { class: 'form-grid' }, advanced.map((p) => paramField(state, p)))));
    }
    section.replaceChildren(...parts);
  }

  function redirectNotice(state) {
    const url = state.kinds.redirect;
    const copy = h('button', { type: 'button', class: 'btn btn-text btn-small' }, icon('content_copy'), gettext('Copy'));
    copy.addEventListener('click', async () => toast(await copyText(url) ? gettext('Copied') : gettext('Copying failed. Please select and copy it by hand'), ''));
    return h('div', { class: 'notice notice-info redirect-uri' },
      h('span', { text: gettext("Put this address of the site in the app's redirect URI (callback URL):") }), h('code', { text: url }), copy);
  }

  function helperButton(state, helper) {
    const button = h('button', { type: 'button', class: 'btn btn-tonal btn-small' }, icon(HELPER_ICONS[helper.name] || 'tune'), helper.label);
    button.addEventListener('click', () => runHelper(state, helper, button));
    return button;
  }

  function drawTransfers(state) {
    const { storage } = state;
    const options = state.type.options;
    const s = storage || {};
    const t = {};
    state.transfer = t;
    const parts = [h('h3', { text: gettext('Downloads and uploads') })];
    const switches = [];
    if (options.download_domain) {
      t.download_domain = h('input', { type: 'url', placeholder: gettext('Like https://dl.example.com'), autocomplete: 'off', spellcheck: 'false' });
      t.download_domain.value = s.download_domain || '';
      parts.push(h('div', { class: 'form-grid' }, field({ id: uid('sf-domain'), label: gettext('Download domain'), input: t.download_domain, wide: true,
        hint: gettext("Optional. When set, download links are this address plus the file's path in the storage source, and your own web server or CDN serves the files (WebDAV can use its own address too, if it allows anonymous downloads); when empty, this site passes downloads on.") })));
      state.fieldOf.download_domain = t.download_domain;
    }
    if (options.proxy_download) {
      const sw = switchLine({ id: uid('sf-proxy-down'), label: gettext('Download through the site'), checked: s.proxy_download,
        hint: gettext("When on, downloads go through this site to users instead of browsers downloading straight from the storage. It takes a lot of the server's bandwidth; only turn it on when needed.") });
      t.proxy_download = sw.input;
      const limits = h('div', { class: 'form-grid' }, proxyLimitFields(state, s, true));
      const sync = () => { limits.hidden = !t.proxy_download.checked; };
      t.proxy_download.addEventListener('change', sync);
      sync();
      switches.push(sw.el, limits);
    }
    if (options.proxy_upload) {
      const sw = switchLine({ id: uid('sf-proxy-up'), label: gettext('Upload through the site'), checked: s.proxy_upload,
        hint: gettext("When on, uploads go to this site first and then on to the storage, instead of browsers uploading straight to the storage. It takes a lot of the server's bandwidth; only turn it on when needed.") });
      t.proxy_upload = sw.input;
      switches.push(sw.el);
    }
    t.link_lifetime = h('input', { type: 'number', min: '60', max: '604800', step: '1', inputmode: 'numeric' });
    t.link_lifetime.value = String(s.link_lifetime || 1800);
    parts.push(h('div', { class: 'form-grid' }, field({ id: uid('sf-lifetime'), label: gettext('Download link lifetime (seconds)'), input: t.link_lifetime, required: true,
      hint: gettext("How long download links signed by this site and the storage's own signed links last: 1800 seconds (half an hour) by default, 7 days at most.") }),
      // A storage without links of its own sends every download through the site: its limit is always there.
      ...(options.proxy_download ? [] : proxyLimitFields(state, s, false))));
    state.fieldOf.link_lifetime = t.link_lifetime;
    const rotatable = Boolean(storage) && !state.wizard; // 换一个链接密钥 below
    const permanent = switchLine({ id: uid('sf-permanent'), label: gettext('Permanent download links'), checked: s.permanent_links,
      hint: rotatable ? gettext('When on, download links from this site don\'t expire (they\'re still signed and download only that file). "New link key" below makes all earlier permanent links stop working.')
        : gettext('When on, download links from this site don\'t expire (they\'re still signed and download only that file). Later, "New link key" in the storage source\'s settings makes all earlier permanent links stop working.') });
    t.permanent_links = permanent.input;
    const force = switchLine({ id: uid('sf-force'), label: gettext('Download links force downloading'), checked: storage ? s.force_download : true,
      hint: gettext('When on, browsers always save the file; when off, opening a download link directly (a copied one or a direct link, say) shows files the browser can show (pictures, PDFs and so on). The "Download" buttons on the pages always save the file.') });
    t.force_download = force.input;
    switches.push(permanent.el, force.el);
    parts.push(...switches);
    if (rotatable) {
      const rotate = h('button', { type: 'button', class: 'btn btn-small' }, icon('key'), gettext('New link key'));
      rotate.addEventListener('click', async () => {
        const yes = await confirmDialog({ title: gettext('Change the link key?'), message: gettext('All permanent download links this storage source gave before stop working, and time-limited links stop before they expire.'), confirmText: gettext('Change'), danger: true });
        if (!yes) return;
        await api('POST', `${API}${storage.id}/rotate-link-secret/`);
        toast(gettext('Link key changed'));
      });
      parts.push(h('div', { class: 'btn-row start' }, rotate));
    }
    state.transfersSection.replaceChildren(...parts);
  }

  // How many downloads through the site the storage sends at once, and, for a storage with links of its own,
  // what one more gets (apps/files/limits.py).
  function proxyLimitFields(state, s, withChoice) {
    const t = state.transfer;
    t.proxy_connections = h('input', { type: 'number', min: '1', max: '1000', step: '1', inputmode: 'numeric' });
    t.proxy_connections.value = String(s.proxy_connections || 16);
    state.fieldOf.proxy_connections = t.proxy_connections;
    const fields = [field({ id: uid('sf-connections'), label: gettext('Connections at once'), input: t.proxy_connections, required: true,
      hint: gettext('How many downloads through this site the storage source sends at once. Every connection counts: each of the connections a download tool opens, and each picture a gallery page loads.') })];
    if (withChoice) {
      t.proxy_when_full = select([['link', gettext("Send to the storage's own link")], ['refuse', gettext('Answer 429 (Too Many Requests)')]], s.proxy_when_full || 'refuse');
      state.fieldOf.proxy_when_full = t.proxy_when_full;
      fields.push(field({ id: uid('sf-when-full'), label: gettext('When all are in use'), input: t.proxy_when_full,
        hint: gettext("With the storage's own link, the download comes straight from the storage: it only works if people can reach the storage, and this site's limits per IP don't apply to it. With 429, they're told to try again in a moment.") }));
    }
    return fields;
  }

  // The limit and what one more gets, while the form shows them: hidden under a switch that is off, they're
  // left as they are (a value typed there and then hidden would get an error no one sees).
  function proxyLimits(t) {
    if (t.proxy_download && !t.proxy_download.checked) return {};
    return t.proxy_when_full ? { proxy_connections: t.proxy_connections.value, proxy_when_full: t.proxy_when_full.value }
      : { proxy_connections: t.proxy_connections.value };
  }

  // The params as the form has them (text as typed: the server checks and converts).
  function formConfig(state) {
    const config = {};
    for (const param of state.type.params) {
      const input = state.inputs[param.key];
      if (!input) continue;
      config[param.key] = param.kind === 'bool' ? input.checked : input.value;
    }
    return config;
  }

  // What may be kept on the server while the browser is with the provider (no secrets).
  function formSnapshot(state) {
    const config = formConfig(state);
    for (const param of state.type.params) if (param.secret) delete config[param.key];
    return { name: state.base.name.value, key: state.base.key.value, remark: state.base.remark.value, config };
  }

  function formBody(state) {
    const t = state.transfer;
    const body = {
      name: state.base.name.value, key: state.base.key.value, remark: state.base.remark.value,
      enabled: state.base.enabled.checked, gallery_default: state.base.gallery_default.checked, show_in_nav: state.base.show_in_nav.checked,
      link_lifetime: t.link_lifetime.value, permanent_links: t.permanent_links.checked, force_download: t.force_download.checked,
      config: formConfig(state), clear_secrets: [...state.clear], draft: state.draft,
    };
    if (!state.id || state.base.icon.changed()) body.icon = state.base.icon.value();
    if (t.download_domain) body.download_domain = t.download_domain.value;
    if (t.proxy_download) body.proxy_download = t.proxy_download.checked;
    Object.assign(body, proxyLimits(t));
    if (t.proxy_upload) body.proxy_upload = t.proxy_upload.checked;
    if (!state.id) body.type = state.type.type;
    return body;
  }

  function clearErrors(state) {
    state.form.querySelectorAll('.field.invalid, .switch-field.invalid').forEach((el) => setFieldError(el, ''));
    App.formError(state.form, '');
  }

  function showErrors(state, data, fallback) {
    const rest = [];
    let first = null;
    for (const [key, value] of Object.entries(data || {})) {
      if (key === 'config' && value && typeof value === 'object' && !Array.isArray(value)) {
        for (const [param, messages] of Object.entries(value)) {
          const container = state.paramFields[param];
          if (container && container.isConnected && setFieldError(container, firstMessage(messages))) {
            first = first || container;
            if (container.closest('details')) container.closest('details').open = true;
          } else {
            // A param this page doesn't show (the wizard's other steps): by its label, not its key.
            const label = param ? ((state.type.params.find((p) => p.key === param) || {}).label || param) : '';
            rest.push(`${label ? interpolate(gettext('%(label)s:'), { label }, true) : ''}${firstMessage(messages)}`);
          }
        }
      } else if (state.fieldOf[key] && state.fieldOf[key].isConnected) {
        const container = state.fieldOf[key].closest('.field, .switch-field');
        setFieldError(container, firstMessage(value));
        first = first || container;
      } else if (key !== 'code' && key !== 'draft') {
        rest.push(key === 'detail' ? String(value) : interpolate(gettext('%(label)s: %(message)s'), { label: App.fieldLabels[key] || key, message: firstMessage(value) }, true));
      }
    }
    if (rest.length || (!first && fallback)) App.formError(state.form, rest.length ? rest.join('\n') : fallback);
    const target = rest.length ? state.error : first;
    if (target) target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  // A driver's changes the browser may see fill fields in; secret ones only say they are filled.
  function applyChanges(state, answer) {
    if (answer.draft) state.draft = answer.draft;
    if (Object.keys(answer.changes || {}).length || (answer.secrets || []).length) unsaved = true;
    for (const [key, value] of Object.entries(answer.changes || {})) {
      const input = state.inputs[key];
      if (!input) continue;
      if (input.type === 'checkbox') input.checked = Boolean(value);
      else input.value = value === null || value === undefined ? '' : String(value);
    }
    for (const key of answer.secrets || []) {
      state.drafted.add(key);
      const param = state.type.params.find((p) => p.key === key);
      const input = state.inputs[key];
      if (param && input) {
        input.value = '';
        input.placeholder = secretPlaceholder(state, param);
        setFieldError(state.paramFields[key], '');
      }
    }
  }

  async function restoreDraft(state) {
    try {
      const draft = await api('GET', `${API}drafts/${encodeURIComponent(state.draft)}/`);
      if (draft.type && draft.type !== state.type.type && !state.id) {
        state.base.type.value = draft.type;
        state.type = state.kinds.byType[draft.type];
        drawParams(state);
        drawTransfers(state);
      }
      const form = draft.form || {};
      if (!state.id) {
        if (form.name) state.base.name.value = form.name;
        if (form.key) state.base.key.value = form.key;
        if (form.remark) state.base.remark.value = form.remark;
      }
      applyChanges(state, { changes: { ...(form.config || {}), ...(draft.changes || {}) }, secrets: draft.secrets, draft: draft.id });
      if ((draft.secrets || []).length) {
        state.helperResult.replaceChildren(notice('ok', gettext('The authorization result is filled into the form; click "Save" to apply it.')));
      }
    } catch (err) {
      state.draft = null;
      state.helperResult.replaceChildren(notice('', gettext('The authorization result has expired (over 6 hours old, or saved already); authorize again if needed.')));
    }
  }

  async function testConnection(state, button) {
    clearErrors(state);
    button.disabled = true;
    state.testResult.replaceChildren(h('div', { class: 'testing' }, App.spinner({ small: true, label: gettext('Testing') }), h('span', { text: gettext('Connecting…') })));
    try {
      const answer = await api('POST', `${API}test/`, { storage: state.id, type: state.type.type, config: formConfig(state),
        clear_secrets: [...state.clear], draft: state.draft });
      applyChanges(state, answer);
      state.testResult.replaceChildren(notice(answer.ok ? 'ok' : 'error', answer.ok ? gettext('Connected.') : interpolate(gettext('Connection failed: %(message)s'), { message: answer.message }, true)));
    } catch (err) {
      state.testResult.replaceChildren();
      showErrors(state, err.data, err.message);
    } finally {
      button.disabled = false;
    }
  }

  async function saveForm(state, button) {
    clearErrors(state);
    button.disabled = true;
    state.testResult.replaceChildren();
    try {
      const body = formBody(state);
      const data = state.id ? await api('PATCH', `${API}${state.id}/`, body) : await api('POST', API, body);
      unsaved = false;
      App.refreshRail();
      if (state.id) {
        toast(interpolate(gettext('Saved "%(name)s"'), { name: data.name }, true));
        location.hash = '#storages';
        return;
      }
      const grants = data.grants || { users: 0, enabled: 0 };
      const who = grants.enabled
        ? interpolate(ngettext('By their default permissions, %(enabled)s user can already use it (of %(users)s users).', 'By their default permissions, %(enabled)s users can already use it (of %(users)s users).', grants.enabled), { enabled: grants.enabled, users: grants.users }, true)
        : gettext("No user can use it yet: users' default permissions are all empty.");
      const toUsers = await confirmDialog({
        title: gettext('Saved'), message: interpolate(gettext('"%(name)s" is saved. %(who)s To change who can use it and from which folder, set it for each user in "Users".'), { name: data.name, who }, true),
        confirmText: gettext('Go to users'), cancelText: gettext('Back to the list'),  // 取消 would read as undoing the save
      });
      location.hash = toUsers ? '#users' : '#storages';
    } catch (err) {
      // A token the connection test refreshed waits in a draft: the next save must name it.
      if (err.data && typeof err.data === 'object' && typeof err.data.draft === 'string') state.draft = err.data.draft;
      if (err.status === 400 && err.data && typeof err.data === 'object') showErrors(state, err.data, err.message);
      else App.formError(state.form, err.message);
      state.error.scrollIntoView({ block: 'center' });
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------------ helper buttons, QR codes, 授权

  async function runHelper(state, helper, button, pollToken = null) {
    button.disabled = true;
    state.helperResult.replaceChildren(h('div', { class: 'testing' }, App.spinner({ small: true }), h('span', { text: `${helper.label}…` })));
    try {
      const answer = await api('POST', `${API}helpers/`, { storage: state.id, type: state.type.type, name: helper.name,
        config: formConfig(state), draft: state.draft, poll_token: pollToken });
      state.helperResult.replaceChildren();
      applyChanges(state, answer);
      if (answer.choices) offerChoices(state, answer.param, answer.choices);
      if (answer.image) return showQr(state, helper, answer);
      const done = answer.secrets && answer.secrets.length ? gettext('Filled into the form; click "Save" to apply it.') : '';
      if (answer.message || done) state.helperResult.replaceChildren(notice(answer.secrets ? 'ok' : 'info', [answer.message, done].filter(Boolean).join(' ')));
    } catch (err) {
      state.helperResult.replaceChildren(notice('error', err.message));
    } finally {
      button.disabled = false;
    }
    return null;
  }

  function offerChoices(state, key, choices) {
    const input = state.inputs[key];
    const container = state.paramFields[key];
    if (!input || !container) return;
    if (!choices.length) {
      state.helperResult.replaceChildren(notice('info', gettext('Nothing to choose.')));
      return;
    }
    if (input.tagName === 'SELECT') {
      const value = input.value;
      input.replaceChildren(...choices.map((c) => h('option', { value: c.value, text: c.label })));
      input.value = choices.some((c) => c.value === value) ? value : choices[0].value;
      return;
    }
    let picker = container.querySelector('.choice-picker');
    if (picker) picker.remove();
    const pick = h('select', { 'aria-label': interpolate(gettext('Choose %(label)s from a list'), { label: container.querySelector('label').textContent }, true) },
      h('option', { value: '', text: interpolate(ngettext('Choose from the list (%(count)s)…', 'Choose from the list (%(count)s)…', choices.length), { count: choices.length }, true) }),
      ...choices.map((c) => h('option', { value: c.value, text: c.label })));
    pick.addEventListener('change', () => {
      if (pick.value === '' && !choices.some((c) => c.value === '')) return;
      input.value = pick.value;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    picker = h('div', { class: 'choice-picker' }, pick);
    container.append(picker);
    pick.focus();
  }

  function showQr(state, helper, first) {
    let open = true;
    const image = h('img', { class: 'qr-image', alt: gettext('QR code for logging in'), src: first.image, width: '220', height: '220' });
    const message = h('p', { class: 'qr-message', role: 'status', text: first.message || gettext('Please scan') });
    const dialog = h('dialog', { class: 'dialog dialog-small', 'aria-label': helper.label },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { text: helper.label }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        h('div', { class: 'dialog-body qr-body' }, image, message,
          h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') })))));
    dialog.addEventListener('close', () => { open = false; dialog.remove(); });
    document.body.append(dialog);
    openDialog(dialog);
    (async () => {
      let answer = first;
      while (open && answer.poll) {
        const started = Date.now();
        try {
          answer = await api('POST', `${API}helpers/`, { storage: state.id, type: state.type.type, name: answer.poll,
            config: formConfig(state), draft: state.draft, poll_token: answer.poll_token });
        } catch (err) {
          message.textContent = err.message;
          return;
        }
        if (!open) return;
        applyChanges(state, answer);
        if (answer.image) image.src = answer.image;
        message.textContent = answer.message || '';
        if (answer.poll && Date.now() - started < 1500) await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      if (open && answer.secrets && answer.secrets.length) {
        dialog.close();
        if (state.wizard) await saveAuthorization(state, state.draft);
        else state.helperResult.replaceChildren(notice('ok', interpolate(gettext('%(message)s: filled into the form; click "Save" to apply it.'), { message: answer.message || gettext('Logged in') }, true)));
      }
    })();
  }

  // 授权: a window to the provider; the callback page (storages/oauth/callback/) puts the tokens into the
  // form's draft and tells this page. Without a window (a blocker), this page goes there itself and comes
  // back to #storages/<id or new>?draft=…, the draft holding the form's other values meanwhile.
  async function authorize(state, button) {
    const popup = window.open('', 'hfile-storage-oauth', 'popup,width=560,height=720');
    button.disabled = true;
    try {
      const answer = await api('POST', `${API}oauth/`, { storage: state.id, type: state.type.type, config: formConfig(state),
        draft: state.draft, form: state.wizard ? null : formSnapshot(state), setup: Boolean(state.wizard) });
      state.draft = answer.draft;
      if (popup && !popup.closed) {
        popup.location.href = answer.url;
        state.helperResult.replaceChildren(notice('info', gettext('Log in and agree in the window that opens; when done, you come back here.')));
      } else {
        unsaved = false; // the server keeps the form meanwhile (the draft)
        location.href = answer.url;
      }
    } catch (err) {
      if (popup) popup.close();
      state.helperResult.replaceChildren(notice('error', err.message));
    } finally {
      button.disabled = false;
    }
  }

  // How 授权 went, from the callback window (storages/js/oauth_callback.js): a postMessage while that window
  // still has this page as its opener, else over a BroadcastChannel (same origin only). The admin pages'
  // Cross-Origin-Opener-Policy (same-origin) cuts the window off from this page once it has been to the
  // provider. True when it was this form's 授权.
  function oauthMessage(data) {
    if (!data || data.type !== 'hfile:storage-oauth') return false;
    const state = formState;
    if (!state || state.page !== current || !root.contains(state.form)) return false;
    // `asked` is the draft this form sent; the tokens may be in a new one (`draft`) if that one expired.
    const asked = data.asked || data.draft;
    if (asked && state.draft !== asked && state.draft !== data.draft) return false;
    if (data.draft) state.draft = data.draft;
    if (state.wizard) {
      if (data.ok) saveAuthorization(state, state.draft);
      else state.helperResult.replaceChildren(notice('error', gettext('Authorization didn\'t finish: the reason is in the authorization window. You can click "Authorize" again.')));
    } else if (data.ok) restoreDraft(state);
    return true;
  }

  window.addEventListener('message', (e) => {
    if (e.origin === location.origin) oauthMessage(e.data);
  });
  if (typeof BroadcastChannel === 'function') {
    const oauthChannel = new BroadcastChannel('hfile:storage-oauth');
    oauthChannel.addEventListener('message', (e) => {
      // Heard: the callback window closes (other forms, in other tabs, stay quiet).
      if (oauthMessage(e.data)) oauthChannel.postMessage({ type: 'hfile:storage-oauth-heard', asked: e.data.asked });
    });
  }

  // ------------------------------------------------------------------ the add wizard
  //
  // #storages/new, one step at a time like the installer, the steps above it:
  //   1 基本信息  name, alias, type, remark
  //   2 密钥      the type's setup params (storage-types/: setup); 保存 makes the storage, off, without a
  //               connection test ("setup": required params of the later steps may still be empty)
  //   3 授权      oauth: the provider's consent in a window, the draft's tokens saved into the storage;
  //               qrcode: 扫码登录, the same; the other types: 测试连接 (run once by itself)
  //   4 其他设置  the other params, 下载和上传, 画廊; 完成 saves them and turns the storage on (enable/, which
  //               tests the connection first)
  // #storages/<id>/setup opens a saved storage at step 3: 授权 comes back there when it had no window.

  const WIZARD_HELPER_PARAMS = { buckets: 'bucket', sites: 'site', drives: 'drive_id' };
  const WIZARD_DONE = 4;

  async function setupPage(page, id, query) {
    const [kinds, storage] = await Promise.all([loadTypes(), id ? api('GET', `${API}${id}/`) : null]);
    if (stale(page)) return;
    // D50: a type only superusers create (local) isn't offered to other admins.
    const offered = (t) => Boolean(t) && (kinds.superuser || !t.superuser_only);
    const firstType = ([kinds.byType[query.get('type')], kinds.byType.local, ...kinds.list].find(offered) || kinds.list[0]).type;
    const type = kinds.byType[storage ? storage.type : firstType];
    if (storage && !type) {
      root.replaceChildren(backTitle(storage.name, '#storages'), notice('error', interpolate(gettext("This program doesn't know this storage source's type (%(type)s), or its driver can't load."), { type: storage.type }, true)));
      return;
    }
    const state = {
      wizard: true, id, storage, page, kinds, type, step: storage ? 2 : 0,
      draft: null, clear: new Set(), drafted: new Set(), inputs: {}, paramFields: {}, base: {}, fieldOf: {},
      grants: null, keysFor: null, moreFor: null, busy: false, tested: false, authNote: null,
    };
    setTitle(gettext('Add storage source · Manage'));
    drawWizard(state);
    const draft = query.get('draft');
    if (storage && draft) {
      history.replaceState(null, '', `#storages/${storage.id}/setup`); // a reload doesn't save it again
      await draftBack(state, draft);
    }
  }

  function drawWizard(state) {
    formState = state;
    const form = h('form', { class: 'storage-form setup-form', novalidate: true });
    state.form = form;
    state.error = h('div', { class: 'form-error', role: 'alert', hidden: true });
    state.bar = h('ol', { class: 'setup-steps', 'aria-label': gettext('Steps to add a storage source') });
    state.panel = h('div', { class: 'setup-panel' });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if (state.next && !state.busy) state.next();
    });
    form.addEventListener('input', () => {
      unsaved = true;
      refreshStep(state);
    });
    form.addEventListener('change', () => refreshStep(state));
    form.append(state.bar, state.error, state.panel);
    root.replaceChildren(backTitle(gettext('Add storage source'), '#storages'), form);
    showStep(state, state.step);
  }

  function wizardTitles(type) {
    const keys = type.params.filter((p) => type.setup.includes(p.key));
    const where = !type.auth.kind && !keys.some((p) => p.secret); // a folder of the server (local), no keys
    return [gettext('Basic info'), where ? gettext('Location') : gettext('Keys'), type.auth.kind ? gettext('Authorize') : gettext('Test connection'), gettext('Other settings')];
  }

  // The steps: the ones done can be opened again, the next ones only by 下一步 (as in the installer).
  function drawBar(state) {
    state.bar.replaceChildren(...wizardTitles(state.type).map((title, index) => {
      const button = h('button', { type: 'button', class: index < state.step ? 'done' : '' },
        h('span', { class: 'step-number', text: String(index + 1) }), h('span', { class: 'step-title', text: title }));
      button.disabled = index > state.step;
      if (index === state.step) button.setAttribute('aria-current', 'step');
      button.addEventListener('click', () => {
        if (index < state.step && !state.busy) showStep(state, index);
      });
      return h('li', {}, button);
    }));
  }

  function showStep(state, index) {
    state.step = index;
    state.next = null;
    state.nextButton = null;
    state.required = [];
    state.stepNote = '';
    clearErrors(state);
    drawBar(state);
    if (index === WIZARD_DONE) return wizardDone(state);
    const section = [wizardBasic, wizardKeys, wizardAuth, wizardMore][index](state, wizardTitles(state.type)[index]);
    state.panel.replaceChildren(section);
    refreshStep(state);
    window.scrollTo({ top: 0 });
    section.querySelector('h3').focus({ preventScroll: true });
    return null;
  }

  // 下一步 waits for the step's required fields and says which are still empty.
  function refreshStep(state) {
    if (!state.nextButton || state.busy) return;
    const names = state.required.filter(([input]) => input && !input.disabled && !String(input.value || '').trim())
      .map(([, label]) => label);
    state.nextButton.disabled = names.length > 0;
    state.status.textContent = names.length ? interpolate(gettext('Still to fill in: %(fields)s'), { fields: names.join(gettext(', ')) }, true) : (state.stepNote || '');
  }

  function wizardActions(state, { back = true, nextText = gettext('Next'), nextIcon = null } = {}) {
    state.status = h('p', { class: 'muted step-status', 'aria-live': 'polite' });
    state.nextButton = h('button', { type: 'submit', class: 'btn btn-filled' }, nextIcon ? icon(nextIcon) : null, nextText);
    let backButton;
    if (back) {
      backButton = h('button', { type: 'button', class: 'btn btn-text', text: pgettext('wizard', 'Back') });
      backButton.addEventListener('click', () => {
        if (!state.busy) showStep(state, state.step - 1);
      });
    } else {
      backButton = h('a', { class: 'btn btn-text', href: '#storages', text: state.id ? gettext('Back to the list') : gettext('Cancel') });
    }
    return h('div', { class: 'form-actions setup-actions' }, state.status, backButton, state.nextButton);
  }

  async function busyWith(state, button, work) {
    const focused = document.activeElement === button;
    state.busy = true;
    button.disabled = true;
    try {
      return await work();
    } finally {
      state.busy = false;
      button.disabled = false;
      refreshStep(state);
      // Still on the page (it failed): disabled meanwhile, the button had lost the focus to the page.
      const lost = !document.activeElement || document.activeElement === document.body;
      if (focused && lost && button.isConnected && !button.disabled) button.focus({ preventScroll: true });
    }
  }

  // A save's problems on the fields of the step they belong to: the name or alias on step 1, a param of
  // step 2 there (完成 checks them all again: a local folder that went away meanwhile).
  function wizardError(state, err) {
    const data = err.data && typeof err.data === 'object' && !Array.isArray(err.data) ? err.data : null;
    if (err.status === 400 && data) {
      const params = data.config && typeof data.config === 'object' && !Array.isArray(data.config) ? Object.keys(data.config) : [];
      if (state.step !== 0 && ['name', 'key', 'type', 'remark'].some((key) => key in data)) showStep(state, 0);
      else if (state.step !== 1 && params.some((key) => state.type.setup.includes(key))) showStep(state, 1);
      showErrors(state, data, err.message);
      const field = state.panel.querySelector(':is(.field, .switch-field).invalid :is(input, select, textarea):not(:disabled)');
      if (field) field.focus({ preventScroll: true });
    } else {
      App.formError(state.form, err.message);
      state.error.scrollIntoView({ block: 'center' });
    }
  }

  const secretSet = (state, key) => Boolean(state.storage) && (state.storage.secrets || {})[key] === 'set';
  const authorized = (state) => state.type.auth.params.some((key) => secretSet(state, key));

  function paramValues(state, params) {
    const config = {};
    for (const param of params) {
      const input = state.inputs[param.key];
      if (input) config[param.key] = param.kind === 'bool' ? input.checked : input.value;
    }
    return config;
  }

  // Where a helper button goes: 扫码登录 is step 3's 授权; one listing choices for a param of step 2 goes
  // there, unless it needs the authorization first (an oauth type's all do); the rest wait for step 4.
  function wizardHelperStep(type, helper) {
    if (type.auth.kind === 'qrcode' && helper.name === 'qrcode') return 2;
    const param = WIZARD_HELPER_PARAMS[helper.name];
    if (param && type.setup.includes(param) && type.auth.kind !== 'oauth') return 1;
    return 3;
  }

  function helperRow(state, step) {
    const helpers = state.type.helpers.filter((helper) => wizardHelperStep(state.type, helper) === step);
    state.helperResult = h('div', { class: 'helper-result', 'aria-live': 'polite' });
    if (!helpers.length) return [];
    return [h('div', { class: 'helper-row' }, ...helpers.map((helper) => helperButton(state, helper))), state.helperResult];
  }

  // -- 1 基本信息

  async function checkKey(state) {
    const input = state.base.key;
    const value = input.value.trim();
    const container = input.closest('.field');
    if (!value) {
      setFieldError(container, '');
      return true;
    }
    const params = new URLSearchParams({ key: value });
    if (state.id) params.set('id', String(state.id));
    try {
      const check = await api('GET', `${API}key-check/?${params}`);
      if (input.value.trim() === value) setFieldError(container, check.ok ? '' : check.message);
      return Boolean(check.ok);
    } catch (err) {
      return true; // checked again when saving
    }
  }

  function wizardBasic(state, title) {
    const b = state.base;
    if (!b.name) {
      b.name = h('input', { maxlength: '64', autocomplete: 'off' });
      b.key = h('input', { maxlength: '32', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
      b.remark = h('textarea', { rows: '2', maxlength: '500' });
      b.type = typeSelect(state.kinds.list, state.type.type);
      b.reserved = state.kinds.superuser ? [] : state.kinds.list.filter((t) => t.superuser_only);
      for (const option of b.type.options) {
        if (b.reserved.some((t) => t.type === option.value)) option.disabled = true;
      }
      if (state.storage) {
        b.name.value = state.storage.name;
        b.key.value = state.storage.key;
        b.remark.value = state.storage.remark || '';
      }
      let keyTimer = null;
      b.key.addEventListener('input', () => {
        clearTimeout(keyTimer);
        keyTimer = setTimeout(() => checkKey(state), 300);
      });
      b.type.addEventListener('change', () => {
        state.type = state.kinds.byType[b.type.value];
        state.keysFor = null;
        state.moreFor = null;
        state.inputs = {};
        state.paramFields = {};
        drawBar(state); // the steps' names follow the type
      });
      Object.assign(state.fieldOf, { name: b.name, key: b.key, type: b.type, remark: b.remark });
    }
    b.type.disabled = Boolean(state.id);
    const typeHint = state.id ? gettext("Can't change once saved: settings and paths mean nothing for another type.")
      : b.reserved.length ? interpolate(gettext('%(kinds)s can only be created by superusers: it can read and write any folder the program can reach on the server.'), { kinds: b.reserved.map((t) => t.label).join(gettext(', ')) }, true) : '';
    const section = h('section', { class: 'form-section setup-step' },
      h('h3', { tabindex: '-1', text: title }),
      h('p', { class: 'muted setup-intro', text: gettext('First give it a name and choose the storage type. The settings for connecting come next and the rest at the end; all can be changed later.') }),
      h('div', { class: 'form-grid' },
        field({ id: uid('sw-name'), label: gettext('Name'), input: b.name, required: true, hint: gettext('The name users see.') }),
        field({ id: uid('sw-key'), label: gettext('Alias'), input: b.key, required: true,
          hint: h('span', { text: gettext('The name in addresses: /alias/folder/file. Lowercase letters, digits, - and _ only.') }) }),
        field({ id: uid('sw-type'), label: gettext('Storage type'), input: b.type, required: true, hint: typeHint }),
        field({ id: uid('sw-remark'), label: gettext('Remark'), input: b.remark, hint: gettext('Only admins can see it.') })),
      wizardActions(state, { back: false }));
    state.required = [[b.name, gettext('Name')], [b.key, gettext('Alias')]];
    state.next = () => busyWith(state, state.nextButton, async () => {
      if (!(await checkKey(state))) {
        b.key.focus();
        return;
      }
      const s = state.storage;
      if (s && (b.name.value !== s.name || b.key.value.trim().toLowerCase() !== s.key || b.remark.value !== (s.remark || ''))) {
        try {
          state.storage = await api('PATCH', `${API}${state.id}/`, { name: b.name.value, key: b.key.value, remark: b.remark.value });
        } catch (err) {
          wizardError(state, err);
          return;
        }
        unsaved = false;
      }
      showStep(state, 1);
    });
    return section;
  }

  // -- 2 密钥

  function keysIntro(type, title) {
    if (title === gettext('Location')) return gettext('Fill in where this storage is. After saving, the next step tests the connection.');
    if (type.auth.kind === 'oauth') return gettext("Register an app in the provider's developer console, add the callback URL below to it, then fill in the app's ID and secret here. After saving, the next step logs in to the account to authorize.");
    if (type.auth.kind === 'qrcode') return gettext('Fill in the app ID from the open platform. After saving, the next step is scanning to log in.');
    return gettext('Fill in the address and keys for connecting to this storage. After saving, the next step tests the connection.');
  }

  function wizardKeys(state, title) {
    const t = state.type;
    const keys = t.params.filter((p) => t.setup.includes(p.key));
    if (state.keysFor !== t.type) { // drawn once per type, and again from the saved storage after 保存
      state.keysFor = t.type;
      state.keyFields = keys.map((p) => paramField(state, p));
    }
    const section = h('section', { class: 'form-section setup-step' },
      h('h3', { tabindex: '-1', text: title }),
      h('p', { class: 'muted setup-intro', text: keysIntro(t, title) }),
      t.auth.kind === 'oauth' ? redirectNotice(state) : null,
      h('div', { class: 'form-grid' }, state.keyFields),
      ...helperRow(state, 1),
      wizardActions(state, { nextText: state.id ? gettext('Save and continue') : gettext('Save'), nextIcon: 'save' }));
    state.required = keys.filter((p) => p.required && !(p.secret && secretSet(state, p.key)))
      .map((p) => [state.inputs[p.key], p.label]);
    state.next = () => busyWith(state, state.nextButton, () => saveKeys(state, keys));
    return section;
  }

  async function saveKeys(state, keys) {
    const b = state.base;
    const config = paramValues(state, keys);
    try {
      if (!state.id) {
        const data = await api('POST', API, { type: state.type.type, name: b.name.value, key: b.key.value,
          remark: b.remark.value, enabled: false, setup: true, config });
        state.id = data.id;
        state.storage = data;
        state.grants = data.grants || null;
        history.replaceState(null, '', `#storages/${data.id}/setup`); // a reload goes on with this one
      } else {
        // "setup": the later steps' required params (a SharePoint site) may still be empty
        state.storage = await api('PATCH', `${API}${state.id}/`, { config, clear_secrets: [...state.clear], setup: true });
      }
    } catch (err) {
      wizardError(state, err);
      return;
    }
    unsaved = false;
    state.clear.clear();
    state.keysFor = null; // secrets now say 已设置
    state.tested = false;
    state.authNote = null;
    showStep(state, 2);
  }

  // -- 3 授权, or 测试连接

  function wizardAuth(state, title) {
    const t = state.type;
    const kind = t.auth.kind;
    const result = h('div', { class: 'form-test-result', 'aria-live': 'polite' });
    state.testResult = result;
    state.helperResult = result; // authorize() and runHelper() say there how it goes
    let intro;
    let action;
    if (kind === 'oauth') {
      intro = authorized(state) ? gettext('Already authorized. To use another account, authorize again.')
        : gettext('Click "Authorize", log in to the account and agree in the window that opens; back here afterwards, the token is saved for you.');
      action = h('button', { type: 'button', class: 'btn btn-tonal' }, icon('login'), authorized(state) ? gettext('Authorize again') : gettext('Authorize'));
      action.addEventListener('click', () => authorize(state, action));
    } else if (kind === 'qrcode') {
      const helper = t.helpers.find((x) => x.name === 'qrcode');
      intro = authorized(state) ? gettext('Already logged in. To use another account, scan again.') : gettext('Click "Scan to log in" and scan the QR code with the phone app; after logging in, the token is saved for you.');
      action = h('button', { type: 'button', class: 'btn btn-tonal' }, icon(HELPER_ICONS.qrcode),
        authorized(state) ? gettext('Scan again') : (helper ? helper.label : gettext('Scan to log in')));
      if (helper) action.addEventListener('click', () => runHelper(state, helper, action));
    } else {
      intro = gettext('Connects to the storage once with the settings just saved, to see whether it works.');
      action = h('button', { type: 'button', class: 'btn btn-tonal' }, icon('network_check'), gettext('Test connection'));
      action.addEventListener('click', () => testSaved(state, action));
    }
    if (state.authNote) result.replaceChildren(state.authNote); // what happened, after drawing it again
    const section = h('section', { class: 'form-section setup-step' },
      h('h3', { tabindex: '-1', text: title }),
      h('p', { class: 'muted setup-intro', text: intro }),
      kind === 'oauth' ? redirectNotice(state) : null,
      h('div', { class: 'btn-row start' }, action), result,
      wizardActions(state));
    // 下一步 without 授权 is allowed (other settings first), but it can't be turned on before it.
    state.stepNote = kind && !authorized(state) ? gettext("Not authorized yet: you can go on with the other settings, but it can't be enabled before authorizing.") : '';
    state.next = () => showStep(state, 3);
    if (!kind && !state.tested) {
      state.tested = true;
      setTimeout(() => testSaved(state, action));
    }
    return section;
  }

  // 测试连接 with the saved params (check/: a storage that is off gets a temporary instance).
  async function testSaved(state, button, afterAuth = false) {
    const result = state.testResult;
    if (button) button.disabled = true;
    result.replaceChildren(h('div', { class: 'testing' }, App.spinner({ small: true, label: gettext('Testing') }), h('span', { text: gettext('Connecting…') })));
    try {
      const data = await api('POST', `${API}${state.id}/check/`);
      const status = data.status || {};
      const text = status.ok ? gettext('The connection works.') : interpolate(gettext('Connection failed: %(message)s'), { message: status.message }, true);
      state.authNote = notice(status.ok ? 'ok' : 'error', afterAuth ? interpolate(gettext('Authorized and saved. %(result)s'), { result: text }, true) : text);
    } catch (err) {
      const text = interpolate(gettext('Testing the connection failed: %(message)s'), { message: err.message }, true);
      state.authNote = notice('error', afterAuth ? interpolate(gettext('Authorized and saved. %(result)s'), { result: text }, true) : text);
    } finally {
      if (button) button.disabled = false;
    }
    if (state.testResult === result) result.replaceChildren(state.authNote);
  }

  // The required params the later steps still have to fill (a SharePoint site): no connection test yet.
  function missingLater(state) {
    const t = state.type;
    const config = (state.storage && state.storage.config) || {};
    return t.params.filter((p) => p.required && !p.secret && !t.setup.includes(p.key) && !t.auth.params.includes(p.key)
      && [null, undefined, ''].includes(config[p.key])).map((p) => p.label);
  }

  // 授权 or 扫码登录 left the tokens in a draft: saving it puts them into the storage (still off).
  async function saveAuthorization(state, draftId) {
    try {
      state.storage = await api('PATCH', `${API}${state.id}/`, { draft: draftId, setup: true });
      state.draft = null;
    } catch (err) {
      state.authNote = notice('error', interpolate(gettext("The authorization result wasn't saved: %(message)s Please authorize again."), { message: err.message }, true));
      showStep(state, 2);
      return;
    }
    unsaved = false;
    const missing = missingLater(state);
    state.authNote = notice('ok', missing.length ? interpolate(gettext('Authorized and saved. Next, set: %(fields)s.'), { fields: missing.join(gettext(', ')) }, true) : gettext('Authorized and saved.'));
    showStep(state, 2);
    if (!missing.length) await testSaved(state, null, true);
  }

  // Back from the provider without a window (?draft=…, the callback page's link): the draft's tokens are
  // saved, if it has any. A declined or failed 授权 comes back the same way, with none.
  async function draftBack(state, draftId) {
    let shown;
    try {
      shown = await api('GET', `${API}drafts/${encodeURIComponent(draftId)}/`);
    } catch (err) {
      state.authNote = notice('', gettext('The authorization result has expired (over 6 hours old, or saved already); authorize again if needed.'));
      showStep(state, 2);
      return;
    }
    if ((shown.secrets || []).length) {
      await saveAuthorization(state, draftId);
      return;
    }
    state.authNote = notice('error', gettext('Authorization didn\'t finish: the reason is on the authorization page you just saw. You can click "Authorize" again.'));
    showStep(state, 2);
  }

  // -- 4 其他设置, then 完成

  function wizardMore(state, title) {
    const t = state.type;
    const rest = t.params.filter((p) => !t.setup.includes(p.key) && !t.auth.params.includes(p.key));
    if (state.moreFor !== t.type) {
      state.moreFor = t.type;
      state.moreBasic = rest.filter((p) => !p.advanced).map((p) => paramField(state, p));
      state.moreAdvanced = rest.filter((p) => p.advanced).map((p) => paramField(state, p));
      state.transfersSection = h('div', { class: 'setup-transfers' });
      drawTransfers(state);
      const gallery = switchLine({ id: uid('sw-gallery'), label: gettext('Open in gallery view'), checked: state.storage.gallery_default,
        hint: gettext('Opens gallery view whenever you switch to this storage source; good for storage sources of pictures.') });
      state.base.gallery_default = gallery.input;
      state.fieldOf.gallery_default = gallery.input;
      state.galleryLine = gallery.el;
      state.base.icon = iconPicker({ id: uid('sw-icon'), icons: state.kinds.icons, value: state.storage.icon });
      const nav = navSwitch(uid('sw-nav'), state.storage.show_in_nav);
      state.base.show_in_nav = nav.input;
      state.fieldOf.show_in_nav = nav.input;
      state.navLine = nav.el;
    }
    state.testResult = h('div', { class: 'form-test-result', 'aria-live': 'polite' });
    const empty = rest.filter((p) => p.required && !p.secret && [null, undefined, ''].includes(p.default));
    const section = h('section', { class: 'form-section setup-step' },
      h('h3', { tabindex: '-1', text: title }),
      h('p', { class: 'muted setup-intro', text: empty.length ? interpolate(gettext("Fill in %(fields)s first; everything else has defaults, and all can be changed later in the storage source's settings. Finishing checks the connection, then enables the storage source."),
        { fields: empty.map((p) => interpolate(gettext('"%(label)s"'), { label: p.label }, true)).join(gettext(', ')) }, true)
        : gettext('These all have defaults, so you can click "Done" right away; they can be changed later in the storage source\'s settings. Finishing checks the connection, then enables the storage source.') }),
      state.moreBasic.length ? h('div', { class: 'form-grid' }, state.moreBasic) : null,
      ...helperRow(state, 3),
      state.moreAdvanced.length ? h('details', { class: 'more-settings' }, h('summary', {}, icon('expand_more'), gettext('More settings')),
        h('div', { class: 'form-grid' }, state.moreAdvanced)) : null,
      state.transfersSection, state.galleryLine, state.base.icon.el, state.navLine, state.testResult,
      wizardActions(state, { nextText: gettext('Done'), nextIcon: 'check' }));
    state.required = rest.filter((p) => p.required && !p.secret).map((p) => [state.inputs[p.key], p.label]);
    state.next = () => busyWith(state, state.nextButton, () => finishSetup(state, rest));
    return section;
  }

  async function finishSetup(state, rest) {
    const t = state.transfer;
    const body = {
      config: paramValues(state, rest), clear_secrets: [...state.clear], gallery_default: state.base.gallery_default.checked,
      show_in_nav: state.base.show_in_nav.checked,
      link_lifetime: t.link_lifetime.value, permanent_links: t.permanent_links.checked, force_download: t.force_download.checked,
    };
    if (state.base.icon.changed()) body.icon = state.base.icon.value();
    if (t.download_domain) body.download_domain = t.download_domain.value;
    if (t.proxy_download) body.proxy_download = t.proxy_download.checked;
    Object.assign(body, proxyLimits(t));
    if (t.proxy_upload) body.proxy_upload = t.proxy_upload.checked;
    try {
      state.storage = await api('PATCH', `${API}${state.id}/`, body);
    } catch (err) {
      wizardError(state, err);
      return;
    }
    unsaved = false;
    state.clear.clear();
    if (!state.storage.enabled) {
      state.testResult.replaceChildren(h('div', { class: 'testing' }, App.spinner({ small: true, label: gettext('Enabling') }),
        h('span', { text: gettext('Checking the connection…') })));
      try {
        state.storage = { ...state.storage, ...(await api('POST', `${API}${state.id}/enable/`)) };
      } catch (err) {
        // Saved, but off: why, and a way to finish anyway.
        const later = h('button', { type: 'button', class: 'btn btn-text', text: gettext('Done, without enabling') });
        later.addEventListener('click', () => showStep(state, WIZARD_DONE));
        state.testResult.replaceChildren(notice('error', err.message), h('div', { class: 'btn-row start' }, later));
        return;
      }
    }
    showStep(state, WIZARD_DONE);
  }

  function wizardDone(state) {
    unsaved = false;
    App.refreshRail();
    const s = state.storage;
    const grants = state.grants;
    const who = !grants ? '' : grants.enabled
      ? interpolate(s.enabled ? ngettext('By their default permissions, %(enabled)s user can use it (of %(users)s users).', 'By their default permissions, %(enabled)s users can use it (of %(users)s users).', grants.enabled)
        : ngettext("By their default permissions, %(enabled)s user can use it once it's enabled (of %(users)s users).", "By their default permissions, %(enabled)s users can use it once it's enabled (of %(users)s users).", grants.enabled), { enabled: grants.enabled, users: grants.users }, true)
      : gettext("No user can use it yet: users' default permissions are all empty.");
    const section = h('section', { class: 'form-section setup-step setup-done' },
      h('h3', { tabindex: '-1', text: s.enabled ? interpolate(gettext('"%(name)s" is added'), { name: s.name }, true) : interpolate(gettext('"%(name)s" is saved but not enabled yet'), { name: s.name }, true) }),
      h('p', { text: s.enabled ? gettext("It's enabled: users with permissions can use it now.") : gettext("It's still disabled: you can enable it later in the storage source list, which checks the connection first.") }),
      who ? h('p', { class: 'muted', text: interpolate(gettext('%(who)s To change who can use it and from which folder, set it for each user in "Users".'), { who }, true) }) : null,
      h('div', { class: 'form-actions' },
        h('a', { class: 'btn btn-text', href: '#storages', text: gettext('Back to the list') }),
        h('a', { class: 'btn btn-tonal', href: `#storages/${s.id}` }, icon('tune'), gettext('Storage source settings')),
        h('a', { class: 'btn btn-filled', href: '#users' }, icon('group'), gettext('Go to users'))));
    state.panel.replaceChildren(section);
    window.scrollTo({ top: 0 });
    section.querySelector('h3').focus({ preventScroll: true });
    return null;
  }


  // ------------------------------------------------------------------ rules

  async function rulesPage(page, id, kind) {
    const info = RULE_KINDS[kind];
    const [storage, rules] = await Promise.all([api('GET', `${API}${id}/`), api('GET', `${API}${id}/${kind}/`)]);
    if (stale(page)) return;
    setTitle(interpolate(gettext('%(title)s · %(name)s · Manage'), { title: info.title, name: storage.name }, true));
    const state = { storage, kind, rules: rules.map((rule) => ({ ...rule })), dirty: false };
    const list = h('div', { class: 'rule-list', role: 'list', 'aria-label': info.title });
    const empty = h('p', { class: 'muted rule-empty', text: info.empty });
    const status = h('span', { class: 'muted small rule-status', 'aria-live': 'polite' });
    const saveButton = h('button', { type: 'button', class: 'btn btn-filled' }, icon('save'), gettext('Save'));
    const addButton = h('button', { type: 'button', class: 'btn btn-tonal' }, icon('add'), gettext('Add rule'));
    const error = h('div', { class: 'form-error', role: 'alert', hidden: true });
    const tester = makeTester(state);
    state.list = list;
    state.markDirty = () => {
      state.dirty = true;
      unsaved = true;
      status.textContent = gettext('Unsaved changes');
      tester.run();
    };
    const redraw = () => {
      list.replaceChildren(...state.rules.map((rule, index) => ruleRow(state, rule, index)));
      empty.hidden = state.rules.length > 0;
    };
    state.redraw = redraw;
    addButton.addEventListener('click', () => {
      const rule = { id: null, expression: '', description: '' };
      if (kind === 'filters') rule.mode = 'hidden';
      if (kind === 'passwords') rule.password = '';
      if (kind === 'readmes') Object.assign(rule, { text: '', path_mode: 'relative', display: 'top' });
      state.rules.push(rule);
      redraw();
      state.markDirty();
      const last = list.lastElementChild;
      if (last) last.querySelector('input').focus();
    });
    sortable(list, () => {
      const order = [...list.children].map((row) => Number(row.dataset.index));
      state.rules = order.map((index) => state.rules[index]);
      redraw();
      state.markDirty();
    });
    saveButton.addEventListener('click', async () => {
      error.hidden = true;
      list.querySelectorAll('.field.invalid').forEach((el) => setFieldError(el, ''));
      saveButton.disabled = true;
      try {
        const saved = await api('PUT', `${API}${id}/${kind}/`, state.rules.map((rule) => ({ ...rule, id: rule.id || null })));
        state.rules = saved.map((rule) => ({ ...rule }));
        state.dirty = false;
        unsaved = false;
        status.textContent = '';
        redraw();
        tester.run();
        toast(gettext('Rules saved'));
      } catch (err) {
        const data = err.data;
        let placed = false;
        if (data && typeof data === 'object' && !Array.isArray(data) && !data.detail) {
          for (const [index, fields] of Object.entries(data)) {
            const row = list.children[Number(index)];
            if (!row || !fields || typeof fields !== 'object') continue;
            for (const [name, messages] of Object.entries(fields)) {
              const container = row.querySelector(`[data-rule-field="${name}"]`);
              if (setFieldError(container, firstMessage(messages))) placed = true;
            }
          }
        }
        if (!placed) {
          error.textContent = err.message;
          error.hidden = false;
        }
      } finally {
        saveButton.disabled = false;
      }
    });
    const parts = [
      backTitle(`${info.title} · ${storage.name}`, '#storages'),
      h('div', { class: 'chips storage-links' },
        h('a', { class: 'chip', href: `#storages/${id}` }, icon('edit'), gettext('Storage source settings')),
        ...Object.entries(RULE_KINDS).filter(([other]) => other !== kind)
          .map(([other, value]) => h('a', { class: 'chip', href: `#storages/${id}/${other}` }, icon(value.icon), value.title))),
      template(info.help),
      template('storages-help-glob'),
    ];
    if (kind === 'readmes') parts.push(readmeFileSwitch(storage));
    parts.push(error, list, empty, h('div', { class: 'form-actions rule-actions' }, addButton, status, saveButton), tester.el);
    root.replaceChildren(...parts);
    redraw();
    tester.run();
  }

  function readmeFileSwitch(storage) {
    const sw = switchLine({ id: uid('readme-file'), label: gettext('Read README.md in folders'), checked: storage.readme_from_file,
      hint: gettext('When on, the rules below no longer apply: opening a folder shows the README.md in it (any case), always below the list.') });
    sw.input.addEventListener('change', async () => {
      sw.input.disabled = true;
      try {
        await api('PATCH', `${API}${storage.id}/`, { readme_from_file: sw.input.checked });
        toast(sw.input.checked ? gettext('Now reading README.md') : gettext('Now using the rules below'));
      } catch (err) {
        sw.input.checked = !sw.input.checked;
        toast(err.message, 'error');
      } finally {
        sw.input.disabled = false;
      }
    });
    return h('div', { class: 'panel-inset' }, sw.el);
  }

  function ruleRow(state, rule, index) {
    const { kind } = state;
    const changed = (key, input, read = (el) => el.value) => {
      input.addEventListener('input', () => { rule[key] = read(input); state.markDirty(); });
      input.addEventListener('change', () => { rule[key] = read(input); state.markDirty(); });
    };
    const expression = h('input', { autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', maxlength: '512', placeholder: gettext('/folder/**') });
    expression.value = rule.expression || '';
    changed('expression', expression);
    const description = h('input', { maxlength: '255', autocomplete: 'off' });
    description.value = rule.description || '';
    changed('description', description);
    const fields = [
      ruleField('expression', gettext('Expression'), expression, true),
      ruleField('description', gettext('Description'), description),
    ];
    if (kind === 'filters') {
      const mode = select(FILTER_MODES, rule.mode);
      changed('mode', mode);
      fields.push(ruleField('mode', gettext('Mode'), mode));
    } else if (kind === 'passwords') {
      const password = h('input', { type: 'password', autocomplete: 'new-password', maxlength: '128' });
      password.value = rule.password || '';
      changed('password', password);
      const box = h('span', { class: 'input-box' }, password,
        h('button', { type: 'button', class: 'icon-button field-button', 'data-toggle-password': '', 'aria-pressed': 'false', 'aria-label': gettext('Show password'), 'data-tooltip': gettext('Show password') }, icon('visibility')));
      fields.push(ruleField('password', gettext('Password'), box, true, password));
    } else {
      const pathMode = select(PATH_MODES, rule.path_mode);
      changed('path_mode', pathMode);
      const display = select(DISPLAYS, rule.display);
      changed('display', display);
      fields.push(ruleField('path_mode', gettext('Path mode'), pathMode), ruleField('display', gettext('Shown'), display));
    }
    const actions = [];
    if (kind === 'readmes') {
      const preview = h('span', { class: 'readme-preview muted small', text: (rule.text || '').trim().split('\n')[0].slice(0, 80) || gettext('No content yet') });
      const edit = h('button', { type: 'button', class: 'btn btn-small' }, icon('edit'), gettext('Edit readme'));
      edit.addEventListener('click', async () => {
        const area = h('textarea', { rows: '14', class: 'readme-text', spellcheck: 'false', 'data-autofocus': '' });
        area.value = rule.text || '';
        const text = await editDialog({
          title: gettext('Edit readme'), wide: true, full: true, submitText: gettext('Done'), submitIcon: 'check',
          body: h('div', {}, h('p', { class: 'muted small', text: gettext('Written in Markdown, with some simple HTML if you like (links, pictures, tables, details blocks). It\'s shown as untrusted content: scripts, styles, forms and event attributes are removed. Remember to click "Save" on the page afterwards.') }),
            field({ id: uid('readme'), label: gettext('Document'), input: area })),
          save: async () => area.value,
        });
        if (text !== null && text !== undefined) {
          rule.text = text;
          preview.textContent = text.trim().split('\n')[0].slice(0, 80) || gettext('No content yet');
          state.markDirty();
        }
      });
      actions.push(h('div', { class: 'readme-edit', dataset: { ruleField: 'text' } }, edit, preview));
    }
    const remove = h('button', { type: 'button', class: 'icon-button', 'aria-label': interpolate(gettext('Delete rule %(n)s'), { n: index + 1 }, true), 'data-tooltip': gettext('Delete') }, icon('delete'));
    remove.addEventListener('click', () => {
      state.rules.splice(index, 1);
      state.redraw();
      state.markDirty();
    });
    return h('div', { class: 'sortable-row rule-row card card-outlined', role: 'listitem', dataset: { index: String(index) } },
      h('div', { class: 'rule-head' },
        dragHandle(interpolate(gettext('Drag rule %(n)s to sort'), { n: index + 1 }, true)),
        h('span', { class: 'rule-number', text: `#${index + 1}` }),
        h('span', { class: 'rule-match', 'aria-live': 'polite' }),
        remove),
      h('div', { class: 'form-grid rule-fields' }, ...fields),
      ...actions);
  }

  function ruleField(name, label, control, required = false, input = control) {
    const id = uid(`rule-${name}`);
    input.id = id;
    const container = h('div', { class: 'field', dataset: { ruleField: name } },
      h('label', { for: id }, label, required ? h('span', { class: 'required-mark', 'aria-hidden': 'true', text: '*' }) : null), control);
    return container;
  }

  // The rule tester: which rules match a path, with the page's rules (saved or not).
  function makeTester(state) {
    const path = h('input', { autocomplete: 'off', spellcheck: 'false', placeholder: '/music/rock/a.mp3' });
    const rootInput = h('input', { autocomplete: 'off', spellcheck: 'false', placeholder: '/' });
    const result = h('div', { class: 'tester-result', 'aria-live': 'polite' });
    const readmes = state.kind === 'readmes';
    let timer = null;
    let asked = 0;
    async function run() {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const value = path.value.trim();
        state.list.querySelectorAll('.rule-row').forEach((row) => {
          row.classList.remove('matched', 'decides');
          row.querySelector('.rule-match').replaceChildren();
        });
        if (!value) {
          result.replaceChildren(h('p', { class: 'muted', text: gettext('Enter a path in the storage source to see which rules cover it.') }));
          return;
        }
        const rules = state.rules.filter((rule) => (rule.expression || '').trim());
        const index = state.rules.map((rule, i) => ((rule.expression || '').trim() ? i : -1)).filter((i) => i >= 0);
        const body = { kind: state.kind, path: value, rules: rules.map((rule) => ({ ...rule, id: null, text: rule.text || 'x', password: rule.password || 'x' })) };
        if (readmes) body.root = rootInput.value.trim() || '/';
        const mine = ++asked;
        try {
          const answer = await api('POST', `${API}${state.storage.id}/rules/test/`, body);
          if (mine !== asked) return;
          showTest(state, result, answer, index);
        } catch (err) {
          if (mine === asked) result.replaceChildren(notice('error', err.message));
        }
      }, 250);
    }
    path.addEventListener('input', run);
    rootInput.addEventListener('input', run);
    const fields = [field({ id: uid('tester-path'), label: gettext('Path to test'), input: path, hint: gettext('A path in the storage source, like /music/rock/a.mp3.') })];
    if (readmes) fields.push(field({ id: uid('tester-root'), label: gettext("User's root folder"), input: rootInput, hint: gettext('Relative rules count from it; / by default.') }));
    const el = h('section', { class: 'tester card card-filled' },
      h('div', { class: 'card-body' },
        h('h3', { class: 'card-title' }, icon('science'), gettext('Rule test')),
        h('div', { class: 'form-grid' }, ...fields), result));
    return { el, run };
  }

  function showTest(state, result, answer, index) {
    const rows = [...state.list.children];
    const mark = (position, text, decides) => {
      const row = rows[index[position]];
      if (!row) return;
      row.classList.add('matched');
      if (decides) row.classList.add('decides');
      row.querySelector('.rule-match').replaceChildren(h('span', { class: `badge ${decides ? 'badge-info' : ''}`, text }));
    };
    for (const match of answer.matches) mark(match.index, match.how === 'self' ? gettext('Matches') : gettext('Covers it (a parent matches)'), false);
    const lines = [];
    if (state.kind === 'filters') {
      if (answer.inaccessible) lines.push(gettext("No access: it and what's below it can't be opened."));
      if (answer.download_disabled) lines.push(gettext("No downloads: downloads, previews, direct and short links don't work."));
      if (answer.hidden) lines.push(gettext('Not shown in lists.'));
      if (!lines.length) lines.push(gettext('Not affected by these filter rules.'));
    } else if (state.kind === 'passwords') {
      if (answer.decides === null || answer.decides === undefined) lines.push(gettext('No password needed.'));
      else {
        mark(answer.decides, gettext('Decides the password'), true);
        const rule = state.rules[index[answer.decides]];
        lines.push(rule && rule.description ? interpolate(gettext('Needs the password of rule %(n)s (%(description)s).'), { n: index[answer.decides] + 1, description: rule.description }, true)
          : interpolate(gettext('Needs the password of rule %(n)s.'), { n: index[answer.decides] + 1 }, true));
      }
    } else if (answer.decides === null || answer.decides === undefined) {
      lines.push(answer.relative === null ? gettext("This path isn't in the user's root folder, so no relative rules count; no readme.") : gettext('This folder has no readme.'));
    } else {
      mark(answer.decides, gettext('Shows it'), true);
      const rule = state.rules[index[answer.decides]];
      lines.push(interpolate(gettext('Shows the readme of rule %(n)s (%(place)s).'), { n: index[answer.decides] + 1, place: rule && rule.display === 'bottom' ? gettext('below the list') : rule && rule.display === 'dialog' ? gettext('in a dialog') : gettext('above the list') }, true));
    }
    result.replaceChildren(
      h('p', { class: 'small muted' }, gettext('As compared:'), h('code', { text: answer.canonical })),
      ...lines.map((line) => h('p', { class: 'tester-line', text: line })));
  }
})();
