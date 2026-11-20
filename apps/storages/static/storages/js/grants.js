/* App.grantsEditor(element, { userId }): what one user (a person, the guest or the template) may do on each
 * storage source, edited in place: 允许使用, the root folder (the user's "/" in that storage) and the
 * permissions. It has its own 保存 (PUT /api/v1/manage/users/<id>/grants/, contract 1 of the storages
 * app); after saving, the root folders that changed are looked up in their storages, and one that
 * isn't there is a warning, not an error. Loaded on /manage/ by the storages section; the user dialog
 * (accounts) calls it for its 存储源权限 tab. Returns { reload(), dirty() }. */
(function () {
  'use strict';
  const App = window.App;
  if (!App) return;
  const { api, h, icon, toast, editDialog, applyRequires } = App;

  let seq = 0;
  const uid = (prefix) => `${prefix}-${++seq}`;
  let options = null; // GET grant-options/: the permission codes with their labels, help and `requires`

  async function loadOptions(userId) {
    const data = await api('GET', `/api/v1/manage/grant-options/?user=${encodeURIComponent(userId)}`);
    if (!options) options = data.permissions;
    return data.default_permissions || [];
  }

  const summary = (codes) => {
    if (!codes.length) return gettext('No permissions');
    const labels = options.filter((p) => codes.includes(p.code)).map((p) => p.label);
    // "等 4 项" kept on one line (no-break spaces)
    return labels.length <= 3 ? labels.join(gettext(', ')) : interpolate(gettext('%(labels)s and others (%(count)s in all)'), { labels: labels.slice(0, 3).join(gettext(', ')), count: labels.length }, true);
  };

  const sameCodes = (a, b) => a.length === b.length && a.every((code) => b.includes(code));

  // What a permission needs first: one of these (D55). usable() leaves out those that miss it.
  const needsOf = (code) => (options.find((p) => p.code === code) || {}).requires || [];
  const met = (code, codes) => {
    const needs = needsOf(code);
    return !needs.length || needs.some((need) => codes.includes(need));
  };
  function usable(codes) {
    let kept = [...codes];
    for (;;) {
      const next = kept.filter((code) => met(code, kept));
      if (next.length === kept.length) return next;
      kept = next;
    }
  }

  // The permissions dialog, for one row or several: resolves to { chosen, held }, or null. A permission
  // that needs another first is offered once that one is ticked (App.applyRequires); `held`: the codes
  // whose tick the rules took away and which are still greyed out (not unticked by hand).
  function choosePermissions(title, chosen, defaults, note = '') {
    const boxes = options.map((p) => {
      const box = h('input', { type: 'checkbox', value: p.code });
      if (p.requires && p.requires.length) box.dataset.requires = p.requires.join(' ');
      box.checked = chosen.includes(p.code);
      return box;
    });
    const rules = (fresh = false) => applyRequires(boxes, { fresh });
    const set = (pick) => {
      boxes.forEach((box) => { box.checked = pick(box); });
      rules(true);
      // As a tick by hand does: editDialog then keeps the dialog open on a click beside it.
      list.dispatchEvent(new Event('input', { bubbles: true }));
    };
    const quick = h('div', { class: 'btn-row start grant-quick' },
      h('button', { type: 'button', class: 'btn btn-text btn-small', text: gettext('Select all'), onclick: () => set(() => true) }),
      h('button', { type: 'button', class: 'btn btn-text btn-small', text: gettext('Select none'), onclick: () => set(() => false) }),
      h('button', { type: 'button', class: 'btn btn-text btn-small', text: gettext('Invert selection'), onclick: () => set((box) => !box.checked) }),
      h('button', { type: 'button', class: 'btn btn-text btn-small', text: gettext("Use this user's default permissions"), onclick: () => set((box) => defaults.includes(box.value)) }));
    const list = h('div', { class: 'grant-permissions' }, options.map((p, i) => {
      const hintId = p.help ? uid('grant-perm-hint') : null;
      if (hintId) boxes[i].setAttribute('aria-describedby', hintId);
      return h('div', { class: 'grant-permission' },
        h('label', { class: 'check-line' }, boxes[i], h('span', { text: p.label })),
        p.help ? h('p', { class: 'hint', id: hintId, text: p.help }) : null);
    }));
    list.addEventListener('change', () => rules());
    rules(true);
    return editDialog({ title, body: h('div', {}, note ? h('p', { class: 'hint', text: note }) : null, quick, list),
      submitText: gettext('OK'), submitIcon: 'check', full: true,
      save: async () => ({
        chosen: boxes.filter((box) => box.checked).map((box) => box.value),
        held: boxes.filter((box) => 'heldBack' in box.dataset).map((box) => box.value),
      }) });
  }

  function rootPrompt(title, value) {
    const input = h('input', { autocomplete: 'off', spellcheck: 'false', placeholder: '/', 'data-autofocus': '' });
    input.value = value || '/';
    const id = uid('grant-root-all');
    input.id = id;
    return editDialog({
      title, submitText: gettext('OK'), submitIcon: 'check',
      body: h('div', { class: 'field' }, h('label', { for: id, text: gettext('Root folder') }), input,
        h('p', { class: 'hint', text: gettext('Which folder the user sees as "/" in these storage sources, like / or /team/alice.') })),
      save: async () => input.value,
    });
  }

  function grantsEditor(element, { userId }) {
    let rows = [];
    let defaults = [];
    let dirty = false;
    const selected = new Set();
    const status = h('span', { class: 'muted small grants-status', 'aria-live': 'polite' });
    const saveButton = h('button', { type: 'button', class: 'btn btn-filled' }, icon('save'), gettext('Save'));
    const box = h('div', { class: 'grants-editor' });
    element.replaceChildren(box);

    const markDirty = () => {
      dirty = true;
      status.textContent = gettext('Unsaved changes');
    };

    function toolbar() {
      const count = selected.size;
      const all = h('input', { type: 'checkbox', 'aria-label': gettext('Select all') });
      all.checked = count > 0 && count === rows.length;
      all.indeterminate = count > 0 && count < rows.length;
      all.addEventListener('change', () => {
        selected.clear();
        if (all.checked) rows.forEach((row) => selected.add(row.storage.id));
        draw();
      });
      const act = (label, iconName, fn) => h('button', { type: 'button', class: 'btn btn-small', disabled: count ? null : true,
        onclick: fn }, icon(iconName), label);
      const each = (fn) => {
        rows.filter((row) => selected.has(row.storage.id)).forEach(fn);
        markDirty();
        draw();
      };
      return h('div', { class: 'grants-toolbar' },
        h('label', { class: 'check-line' }, all, h('span', { text: count ? interpolate(ngettext('%(count)s selected', '%(count)s selected', count), { count }, true) : gettext('Select all') })),
        act(gettext('Allowed'), 'check', () => each((row) => { row.enabled = true; row.granted = true; })),
        act(gettext('Not allowed'), 'cancel', () => each((row) => { row.enabled = false; })),
        act(gettext('Set permissions'), 'tune', async () => {
          // Starts from what every selected storage has: a tick is added to each of them, an untick taken
          // from each, and a permission only some of them have stays as it is (adding 下载 keeps 预览).
          const codes = options.map((p) => p.code);
          const picked = rows.filter((row) => selected.has(row.storage.id));
          const common = codes.filter((code) => picked.every((row) => (row.permissions || []).includes(code)));
          const answer = await choosePermissions(interpolate(ngettext('Set permissions of %(count)s storage source', 'Set permissions of %(count)s storage sources', count), { count }, true), common, defaults,
            gettext('Checked are the permissions all of these storage sources have. Checking adds a permission to each, unchecking removes it from each; permissions only some of them have stay as they are.')
            + gettext("Permissions grayed out because what they need isn't checked (rather than unchecked by hand) stay only on the storage sources that still have what they need."));
          if (!answer) return;
          const { chosen, held } = answer;
          const added = chosen.filter((code) => !common.includes(code));
          // A tick the rules took away (held) isn't an untick: each storage keeps it while it still has
          // what it needs (复制下载链接 with 预览 when 下载 is taken away). One unticked by hand goes.
          const removed = common.filter((code) => !chosen.includes(code) && !held.includes(code));
          const next = (row) => {
            const have = (row.permissions || []).filter((code) => !removed.includes(code));
            return usable(codes.filter((code) => have.includes(code) || added.includes(code)));
          };
          // 确定 that changes none of them (下载 off and on again, say): nothing to save, as for one row.
          if (picked.every((row) => sameCodes(next(row), row.permissions || []))) return;
          each((row) => {
            row.permissions = next(row);
            row.granted = true;
          });
        }),
        act(gettext('Set root folder'), 'folder', async () => {
          const value = await rootPrompt(interpolate(ngettext('Set the root folder of %(count)s storage source', 'Set the root folder of %(count)s storage sources', count), { count }, true), '/');
          if (value !== null && value !== undefined) each((row) => { row.root_path = value; row.granted = true; });
        }));
    }

    function rowElement(row) {
      const storage = row.storage;
      const pick = h('input', { type: 'checkbox', 'aria-label': interpolate(gettext('Select "%(name)s"'), { name: storage.name }, true) });
      pick.checked = selected.has(storage.id);
      pick.addEventListener('change', () => {
        if (pick.checked) selected.add(storage.id);
        else selected.delete(storage.id);
        draw();
      });
      const enabled = h('input', { type: 'checkbox', class: 'switch', role: 'switch', 'aria-label': interpolate(gettext('Allow "%(name)s"'), { name: storage.name }, true) });
      enabled.checked = row.enabled;
      enabled.addEventListener('change', () => {
        row.enabled = enabled.checked;
        row.granted = true;
        markDirty();
      });
      const rootId = uid('grant-root');
      const root = h('input', { id: rootId, autocomplete: 'off', spellcheck: 'false', placeholder: '/', 'aria-label': interpolate(gettext('Root folder of "%(name)s"'), { name: storage.name }, true) });
      root.value = row.root_path;
      root.addEventListener('input', () => {
        row.root_path = root.value;
        row.granted = true;
        markDirty();
      });
      const permissionsText = h('span', { class: 'grant-summary', text: summary(row.permissions) });
      const edit = h('button', { type: 'button', class: 'icon-button small', 'aria-label': interpolate(gettext('Change the permissions of "%(name)s"'), { name: storage.name }, true), 'data-tooltip': gettext('Change permissions') }, icon('edit'));
      edit.addEventListener('click', async () => {
        const answer = await choosePermissions(interpolate(gettext('Permissions of "%(name)s"'), { name: storage.name }, true), row.permissions, defaults);
        if (!answer) return;
        const { chosen } = answer;
        if (sameCodes(chosen, row.permissions || [])) return; // nothing changed
        row.permissions = chosen;
        row.granted = true;
        permissionsText.textContent = summary(chosen);
        markDirty();
      });
      const notes = h('div', { class: 'grant-notes' },
        row.problem ? h('p', { class: 'field-error', text: row.problem }) : null,
        row.warning ? h('p', { class: 'grant-warning', text: row.warning }) : null);
      return h('tr', {},
        h('td', { class: 'grant-pick' }, pick),
        h('td', { 'data-label': gettext('Storage source') },
          h('div', { class: 'grant-storage' }, h('strong', { text: storage.name }),
            h('span', { class: 'muted small', text: `/${storage.key}` }),
            storage.enabled ? null : h('span', { class: 'badge', text: gettext('Disabled') }),
            row.granted ? null : h('span', { class: 'badge badge-info', 'data-tooltip': gettext('A new storage source, not set up for this user yet'), text: gettext('Not set') }))),
        h('td', { 'data-label': gettext('Allowed') }, enabled),
        h('td', { 'data-label': gettext('Root folder') }, root, notes),
        h('td', { 'data-label': gettext('Permissions') }, h('div', { class: 'grant-permission-cell' }, permissionsText, edit)));
    }

    function draw() {
      if (!rows.length) {
        box.replaceChildren(h('div', { class: 'empty-state' }, icon('storage'), h('h2', { text: gettext('No storage sources yet') }),
          h('p', { text: gettext('Create one in "Storage sources" first, then come back to set this user\'s permissions.') }),
          h('a', { class: 'btn btn-tonal', href: '#storages/new' }, icon('add'), gettext('New storage source'))));
        return;
      }
      const table = h('div', { class: 'table-wrap' },
        h('table', { class: 'data-table stack-table grants-table' },
          h('thead', {}, h('tr', {}, h('th', { class: 'grant-pick' }, h('span', { class: 'sr-only', text: gettext('Select') })),
            h('th', { text: gettext('Storage source') }), h('th', { text: gettext('Allowed') }), h('th', { text: gettext('Root folder') }), h('th', { text: gettext('Permissions') }))),
          h('tbody', {}, rows.map(rowElement))));
      box.replaceChildren(
        h('p', { class: 'muted small grants-intro', text: gettext('Each storage source is set separately: with "Allowed" off the storage source can\'t be seen; the root folder is what the user sees as "/" in it; the permissions decide what they can do (preview, download, upload…).') }),
        toolbar(), table,
        h('div', { class: 'grants-foot' }, status, saveButton));
    }

    async function save() {
      saveButton.disabled = true;
      rows.forEach((row) => { row.problem = ''; row.warning = ''; });
      const before = new Map(saved.map((row) => [row.storage.id, row.root_path]));
      try {
        const answer = await api('PUT', `/api/v1/manage/users/${userId}/grants/`, rows.map((row) => ({
          storage: row.storage.id, granted: row.granted, enabled: row.enabled, root_path: row.root_path, permissions: row.permissions,
        })));
        rows = answer.map((row) => ({ ...row }));
        saved = answer.map((row) => ({ ...row }));
        dirty = false;
        status.textContent = gettext('Saved');
        toast(gettext('Storage source permissions saved'));
        draw();
        // Root folders that changed: are they there? (only a warning; a remote storage may be slow)
        for (const row of rows) {
          if (!row.granted || row.root_path === '/' || before.get(row.storage.id) === row.root_path || !row.storage.enabled) continue;
          api('POST', `/api/v1/manage/storages/${row.storage.id}/check-path/`, { path: row.root_path })
            .then((check) => {
              if (check.message) {
                row.warning = check.message;
                draw();
              }
            })
            .catch(() => {});
        }
      } catch (err) {
        const data = err.data;
        let placed = false;
        if (data && typeof data === 'object' && !Array.isArray(data) && !data.detail) {
          for (const [index, fields] of Object.entries(data)) {
            const row = rows[Number(index)];
            if (row && fields && typeof fields === 'object') {
              row.problem = App.errorText(fields, 400, gettext('; '));
              placed = true;
            }
          }
        }
        status.textContent = placed ? gettext('Some settings are wrong; see the red text below') : err.message;
        draw();
      } finally {
        saveButton.disabled = false;
      }
    }
    saveButton.addEventListener('click', save);

    let saved = [];
    async function reload() {
      box.replaceChildren(h('div', { class: 'grants-loading' }, App.spinner({ small: true }), h('span', { text: gettext('Loading…') })));
      try {
        const [list, userDefaults] = await Promise.all([api('GET', `/api/v1/manage/users/${userId}/grants/`), loadOptions(userId)]);
        defaults = userDefaults;
        rows = list.map((row) => ({ ...row }));
        saved = list.map((row) => ({ ...row }));
        selected.clear();
        dirty = false;
        status.textContent = '';
        draw();
      } catch (err) {
        box.replaceChildren(h('div', { class: 'notice notice-error', role: 'alert', text: interpolate(gettext('Loading the storage source permissions failed: %(message)s'), { message: err.message }, true) }));
      }
    }
    reload();
    return { reload, dirty: () => dirty };
  }

  App.grantsEditor = grantsEditor;
})();
