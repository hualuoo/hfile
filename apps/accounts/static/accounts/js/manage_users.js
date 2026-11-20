/* /manage/#users: the users (GET/POST/PATCH/DELETE /api/v1/manage/users/), copying one (POST …/copy/),
 * and in the user dialog their default permissions, their grants on each storage source
 * (App.grantsEditor, from the storages app) and their single sign-on accounts (App.ssoIdentities, from
 * the sso app). The server decides who may change whom
 * (apps/accounts/managing.py); each user's `can` and `why` say what to offer and why not. */
(function () {
  'use strict';
  const {
    api, h, icon, toast, fmtNum, fmtDateTime, confirmDialog, editDialog, openDialog, tabs, formError, errorText,
    fieldLabels, updateCounters, applyRequires,
  } = window.App;

  const filters = document.getElementById('user-filters');
  if (!filters) return;
  Object.assign(fieldLabels, { default_permissions: gettext('Default permissions'), is_active: gettext('Enabled') });

  const PAGE_SIZE = 20;
  const rows = document.getElementById('user-rows');
  const summary = document.getElementById('user-summary');
  const pager = document.getElementById('user-pager');

  const ROLE_BADGES = { superuser: 'badge-info', staff: 'badge-info', guest: 'badge-warning', template: 'badge-warning' };
  const ROLE_TIPS = {
    guest: gettext("Visitors who aren't logged in act as this user: its permissions are what visitors can do. Disable it and nothing can be seen without logging in."),
    template: gettext('Accounts that single sign-on creates later start with a copy of its default permissions and storage source permissions.'),
    superuser: gettext('Can open the Manage page and the Django admin. Superusers are made at installation, with the createsuperuser command or in the Django admin.'),
    staff: gettext('Can open the Manage page.'),
  };
  const BUILTIN_NOTES = {
    guest: gettext('This is the built-in "Anonymous user": visitors who aren\'t logged in act as it, and its permissions are what visitors can do. Its name can\'t be changed and it has no password; turn the switch below off and nothing can be seen without logging in.'),
    template: gettext('This is the built-in "New user template": accounts that single sign-on creates later start with a copy of its default permissions and storage source permissions. Its name can\'t be changed, and it can\'t log in.'),
  };

  const firstChar = (user) => [...(user.nickname || user.username)][0] || '?';

  // A control that can't be used, wrapped so that its reason still shows as a tooltip.
  function withReason(control, reason) {
    if (!reason) return control;
    control.disabled = true;
    return h('span', { class: 'reason-wrap', 'data-tooltip': reason }, control);
  }

  function iconButton(name, label, onclick, reason = '') {
    return withReason(h('button', { type: 'button', class: 'icon-button', 'aria-label': label, 'data-tooltip': reason ? null : label, onclick }, icon(name)), reason);
  }

  // ------------------------------------------------------------ the list

  function nameCell(user) {
    const badges = [];
    if (user.role !== 'user') {
      // The built-ins are called by their kind already: their badge just says that they are built in.
      const text = user.kind === 'normal' ? user.role_label : gettext('Built-in');
      badges.push(h('span', { class: `badge ${ROLE_BADGES[user.role]}`, 'data-tooltip': ROLE_TIPS[user.role], text }));
    }
    if (user.totp_enabled) badges.push(h('span', { class: 'badge badge-ok', 'data-tooltip': gettext('Logging in also asks for the digits in a phone authenticator app') }, icon('shield'), gettext('Two-step verification')));
    if (user.kind === 'normal' && !user.has_password) badges.push(h('span', { class: 'badge', 'data-tooltip': gettext("No password: can't log in with a password"), text: gettext('No password') }));
    if (!user.is_active && user.kind !== 'template') badges.push(h('span', { class: 'badge badge-error', text: user.kind === 'guest' ? gettext('No access') : gettext('Disabled') }));
    return h('td', {}, h('div', { class: 'user-cell' },
      h('span', { class: 'avatar', 'aria-hidden': 'true', text: firstChar(user) }),
      h('span', { class: 'user-names' },
        h('span', { class: 'user-display', text: user.nickname || user.username }),
        h('span', { class: 'muted small', text: `@${user.username} · ID ${user.id}` }),
        badges.length ? h('span', { class: 'user-badges' }, badges) : null)));
  }

  function activeCell(user) {
    if (user.kind === 'template') return h('td', { class: 'cell-switch', 'data-label': gettext('Enabled') }, h('span', { class: 'muted', 'data-tooltip': user.why.toggle, text: '—' }));
    const label = user.kind === 'guest' ? gettext('Allow access without logging in') : interpolate(gettext('Enable %(username)s'), { username: user.username }, true);
    const input = h('input', { type: 'checkbox', class: 'switch', role: 'switch', 'aria-label': label, checked: user.is_active });
    input.addEventListener('change', () => toggle(user, input));
    return h('td', { class: 'cell-switch', 'data-label': gettext('Enabled') }, withReason(input, user.why.toggle));
  }

  function loginCell(user) {
    if (user.kind !== 'normal') return h('td', { 'data-label': gettext('Last login') }, h('span', { class: 'muted', text: gettext("Doesn't log in") }));
    if (!user.last_login) return h('td', { 'data-label': gettext('Last login') }, h('span', { class: 'muted', text: gettext('Never') }));
    return h('td', { 'data-label': gettext('Last login') },
      h('span', { class: 'nowrap', text: fmtDateTime(user.last_login) }),
      user.last_login_ip ? h('div', {}, ipLine(user)) : null);
  }

  // The IP of the last login, with its location while IP 归属地 is on (/manage/#ip-geo).
  function ipLine(user) {
    return h('span', { class: 'ip-line' }, h('code', { class: 'ip', text: user.last_login_ip }),
      user.last_login_location ? h('span', { class: 'ip-place', text: user.last_login_location }) : null);
  }

  function row(user) {
    const tr = h('tr', { class: 'interactive' },
      nameCell(user),
      activeCell(user),
      loginCell(user),
      h('td', { class: 'nowrap', 'data-label': gettext('Created at'), text: fmtDateTime(user.date_joined) }),
      h('td', { class: 'row-actions' },
        iconButton('edit', gettext('Edit'), () => openUser(user.id), user.why.edit),
        iconButton('content_copy', gettext('Copy'), () => copyUser(user), user.why.copy),
        iconButton('history', gettext('Activity log'), () => showActivity(user)),
        iconButton('delete', gettext('Delete'), () => deleteUser(user), user.why.delete)));
    // The whole row opens the user, but its own controls keep working and selecting text doesn't.
    tr.addEventListener('click', (e) => {
      if (e.target.closest('button, input, a, .reason-wrap') || String(window.getSelection())) return;
      if (user.can.edit) openUser(user.id);
    });
    return tr;
  }

  const messageRow = (text, error = false) => h('tr', { class: 'message-row' },
    h('td', { colspan: '5' }, h('span', { class: error ? 'field-error' : '', text })));

  let page = 1;
  let generation = 0; // a late answer to an older query is dropped
  let loaded = false;

  function renderPager(count) {
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
    const f = filters.elements;
    const search = f.search.value.trim();
    const params = new URLSearchParams({ page, page_size: PAGE_SIZE, ordering: f.ordering.value });
    if (search) params.set('search', search);
    if (f.is_active.value) params.set('is_active', f.is_active.value);
    const filtered = Boolean(search || f.is_active.value);
    summary.textContent = gettext('Loading…');
    try {
      const data = await api('GET', `/api/v1/manage/users/?${params}`);
      if (mine !== generation) return;
      summary.textContent = interpolate(filtered ? ngettext('%(count)s user found', '%(count)s users found', data.count) : ngettext('%(count)s user', '%(count)s users', data.count), { count: fmtNum(data.count) }, true);
      rows.replaceChildren(...(data.results.length ? data.results.map(row) : [messageRow(filtered ? gettext('No users match.') : gettext('No users yet.'))]));
      renderPager(data.count);
    } catch (err) {
      if (mine !== generation) return;
      if (err.status === 404 && page > 1) { // that page is gone (users were deleted meanwhile)
        go(1);
        return;
      }
      summary.textContent = '';
      rows.replaceChildren(messageRow(interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true), true));
      pager.hidden = true;
    }
  }

  let searchTimer = null;
  const reload = () => {
    clearTimeout(searchTimer);
    go(1);
  };
  filters.addEventListener('input', (e) => {
    if (e.target.name !== 'search') return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(reload, 300);
  });
  filters.addEventListener('change', (e) => { if (e.target.name !== 'search') reload(); });
  filters.addEventListener('submit', (e) => {
    e.preventDefault();
    reload();
  });

  async function toggle(user, input) {
    const on = input.checked;
    input.disabled = true;
    try {
      const saved = await api('PATCH', `/api/v1/manage/users/${user.id}/`, { is_active: on });
      if (saved.kind === 'guest') toast(on ? gettext("Access without logging in is allowed now (with the anonymous user's permissions).") : gettext('Anonymous access is off: nothing can be seen without logging in.'));
      else toast(on ? interpolate(gettext('%(username)s is enabled.'), { username: saved.username }, true) : interpolate(gettext("%(username)s is disabled: they can't log in, and were logged out where logged in."), { username: saved.username }, true));
      load();
    } catch (err) {
      input.checked = !on;
      input.disabled = false;
      toast(err.message, 'error');
    }
  }

  // ------------------------------------------------------------ the user dialog

  const dialog = document.getElementById('user-dialog');
  document.body.append(dialog); // out of the section, which a change of #hash hides
  const form = document.getElementById('user-form');
  const f = form.elements;
  const title = document.getElementById('user-dialog-title');
  const note = document.getElementById('user-builtin-note');
  const facts = document.getElementById('user-facts');
  const passwordField = document.getElementById('user-password-field');
  const passwordHint = document.getElementById('user-password-hint');
  const usernameHint = document.getElementById('user-username-hint');
  const USERNAME_HINT = usernameHint.textContent;
  const activeLine = document.getElementById('user-active-line');
  const activeLabel = document.getElementById('user-active-label');
  const activeHint = document.getElementById('user-active-hint');
  const grantsPanel = document.getElementById('user-tab-grants');
  const ssoPanel = document.getElementById('user-tab-sso');
  const saveButton = form.querySelector('[type="submit"]');
  const permissionBoxes = [...form.querySelectorAll('input[name="default_permissions"]')];
  let permissionsEditable = true;
  // A permission that needs another first (data-requires, D55) is offered once that one is ticked.
  const permissionRules = (fresh = false) => applyRequires(permissionBoxes, { editable: permissionsEditable, fresh });
  let current = null; // the user in the dialog, as the server last gave them; null for a new one
  let grantsShown = false;
  let ssoShown = false;
  let grants = null; // what App.grantsEditor returned: { reload(), dirty() }
  let savedState = '';

  const tabControl = tabs(document.getElementById('user-tabs'), (name) => {
    form.hidden = name === 'grants' || name === 'sso'; // these two tabs save by themselves
    if (name === 'grants') showGrants();
    if (name === 'sso') showSso();
  });

  // The storages app's editor, for a saved user (the guest and the template too).
  function showGrants() {
    if (grantsShown) return;
    grantsPanel.replaceChildren();
    if (!current) {
      grantsPanel.append(h('p', { class: 'muted', text: gettext('Save this user in "Basic info" first, then set their permissions on each storage source.') }));
      return;
    }
    grantsShown = true;
    const editor = window.App.grantsEditor; // looked up now: the storages app's script may come later
    if (typeof editor !== 'function') {
      grantsPanel.append(h('p', { class: 'notice', text: gettext("Storage source management isn't ready yet.") }));
      return;
    }
    grants = editor(grantsPanel, { userId: current.id });
  }

  // The sso app's list of the user's bound accounts (built-in users get its explanation why not).
  function showSso() {
    if (ssoShown) return;
    ssoPanel.replaceChildren();
    if (!current) {
      ssoPanel.append(h('p', { class: 'muted', text: gettext('Save this user in "Basic info" first, then link their single sign-on accounts.') }));
      return;
    }
    ssoShown = true;
    const editor = window.App.ssoIdentities; // looked up now, like the grants editor
    if (typeof editor !== 'function') {
      ssoPanel.append(h('p', { class: 'notice', text: gettext("Single sign-on isn't ready yet.") }));
      return;
    }
    editor(ssoPanel, { userId: current.id, user: current });
  }
  const grantsDirty = () => Boolean(grants && typeof grants.dirty === 'function' && grants.dirty());

  const snapshot = () => JSON.stringify([f.username.value, f.nickname.value, f.password.value, f.is_active.checked,
    permissionBoxes.map((box) => box.checked)]);

  function clearFieldErrors() {
    form.querySelectorAll('.field.invalid').forEach((field) => field.classList.remove('invalid'));
    form.querySelectorAll('.field > .field-error').forEach((el) => el.remove());
  }

  // Field errors go under their field (and its tab is shown); the rest above the form.
  function showErrors(err) {
    clearFieldErrors();
    const data = err.data && typeof err.data === 'object' && !Array.isArray(err.data) && typeof err.data.detail !== 'string' ? err.data : null;
    if (!data) {
      formError(form, err.message);
      return;
    }
    const rest = {};
    let firstField = null;
    for (const [key, value] of Object.entries(data)) {
      const field = form.querySelector(`.field[data-field="${CSS.escape(key)}"]`);
      if (field && Array.isArray(value)) {
        field.classList.add('invalid');
        field.append(h('p', { class: 'field-error', text: value.join(' ') }));
        firstField = firstField || field;
      } else if (key !== 'code') {
        rest[key] = value;
      }
    }
    formError(form, Object.keys(rest).length ? errorText(rest, err.status) : '');
    if (firstField) {
      tabControl.select('basic');
      const input = firstField.querySelector('input');
      if (input) input.focus();
    }
  }

  function fact(label, value) {
    return h('div', {}, h('dt', { text: label }), h('dd', {}, value));
  }

  function fill(user) {
    current = user;
    grantsShown = false;
    grants = null;
    grantsPanel.replaceChildren();
    ssoShown = false;
    ssoPanel.replaceChildren();
    form.reset();
    formError(form, '');
    clearFieldErrors();
    const builtin = Boolean(user && user.kind !== 'normal');
    const editable = !user || user.can.edit;
    title.textContent = user ? interpolate(gettext('Edit user "%(username)s"'), { username: user.username }, true) : gettext('New user');
    note.hidden = !(builtin || !editable);
    note.textContent = builtin ? BUILTIN_NOTES[user.kind] : (editable ? '' : user.why.edit);

    f.username.value = user ? user.username : '';
    f.nickname.value = user ? user.nickname : '';
    f.username.readOnly = f.nickname.readOnly = Boolean(user && !user.can.rename);
    usernameHint.textContent = user && !user.can.rename ? user.why.rename : USERNAME_HINT;

    passwordField.hidden = builtin;
    f.password.disabled = Boolean(user && !user.can.password);
    if (!user) passwordHint.textContent = gettext("At least 8 characters, not too common and not all digits. Left empty, the account can't log in with a password for now");
    else if (!user.can.password) passwordHint.textContent = user.why.password;
    else passwordHint.textContent = user.has_password ? gettext('Leave empty to keep it. Once changed, their logins on every device end') : gettext("They have no password now and can't log in with one");

    activeLine.hidden = Boolean(user && user.kind === 'template');
    f.is_active.checked = user ? user.is_active : true;
    f.is_active.disabled = Boolean(user && !user.can.toggle);
    activeLabel.textContent = user && user.kind === 'guest' ? gettext('Allow access without logging in') : gettext('Enabled');
    if (user && user.kind === 'guest') activeHint.textContent = gettext("Once off, people who aren't logged in see no storage sources.");
    else if (user && !user.can.toggle) activeHint.textContent = user.why.toggle;
    else activeHint.textContent = gettext("Once disabled, the user can't log in, and is logged out right away where already logged in.");

    const chosen = new Set(user ? user.default_permissions : []);
    permissionsEditable = editable;
    permissionBoxes.forEach((box) => { box.checked = chosen.has(box.value); });
    permissionRules(true);
    form.querySelectorAll('[data-permissions]').forEach((button) => { button.disabled = !editable; });
    saveButton.hidden = !editable;

    facts.hidden = !user;
    facts.replaceChildren(...(user ? [
      fact(gettext('Role'), user.role_label),
      fact(gettext('Created at'), fmtDateTime(user.date_joined)),
      ...(user.kind === 'normal' ? [
        fact(gettext('Last login'), user.last_login ? fmtDateTime(user.last_login) : gettext('Never')),
        fact(gettext('Last login IP'), user.last_login_ip ? ipLine(user) : '—'),
        fact(gettext('Password'), user.has_password ? gettext('Set') : gettext('None')),
      ] : []),
    ] : []));

    tabControl.select('basic');
    updateCounters(dialog);
    savedState = snapshot();
  }

  async function openUser(id) {
    let user;
    try {
      user = await api('GET', `/api/v1/manage/users/${id}/`);
    } catch (err) {
      toast(err.message, 'error');
      return;
    }
    fill(user);
    openDialog(dialog);
  }

  function collect() {
    const body = {};
    if (!current || current.can.rename) {
      body.username = f.username.value.trim();
      body.nickname = f.nickname.value;
    }
    if (f.password.value && !f.password.disabled && !passwordField.hidden) body.password = f.password.value;
    if (!activeLine.hidden && !f.is_active.disabled) body.is_active = f.is_active.checked;
    body.default_permissions = permissionBoxes.filter((box) => box.checked).map((box) => box.value);
    return body;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (current && !current.can.edit) return;
    formError(form, '');
    clearFieldErrors();
    if (!f.username.value.trim()) {
      showErrors({ data: { username: [gettext('Enter your username.')] }, status: 400 });
      return;
    }
    saveButton.disabled = true;
    try {
      const created = !current;
      const user = created
        ? await api('POST', '/api/v1/manage/users/', collect())
        : await api('PATCH', `/api/v1/manage/users/${current.id}/`, collect());
      load();
      if (created) {
        // A new user can't do anything yet: on to their grants, in the same dialog.
        fill(user);
        tabControl.select('grants');
        toast(interpolate(gettext('User %(username)s created. Next, set their permissions on each storage source.'), { username: user.username }, true));
      } else {
        savedState = snapshot();
        dialog.close();
        toast(interpolate(gettext('%(username)s saved.'), { username: user.username }, true));
      }
    } catch (err) {
      showErrors(err);
    } finally {
      saveButton.disabled = false;
    }
  });

  form.addEventListener('click', (e) => {
    const button = e.target.closest('[data-permissions]');
    if (!button || !permissionsEditable) return;
    const how = button.dataset.permissions;
    permissionBoxes.forEach((box) => {
      box.checked = how === 'all' ? true : how === 'none' ? false : !box.checked;
    });
    permissionRules(true);
  });
  form.addEventListener('change', (e) => {
    if (e.target.name === 'default_permissions') permissionRules();
  });

  // Closing with changes not saved asks first (Esc, the close button, the backdrop and 取消 all fire "cancel").
  dialog.addEventListener('cancel', (e) => {
    const formChanged = snapshot() !== savedState;
    const grantsChanged = grantsDirty();
    if (!formChanged && !grantsChanged) return;
    e.preventDefault();
    const message = formChanged && grantsChanged ? gettext('The basic info, default permissions and storage source permissions have unsaved changes.')
      : formChanged ? gettext('The basic info or default permissions have unsaved changes.') : gettext('The storage source permissions have unsaved changes.');
    confirmDialog({ title: gettext('Discard unsaved changes?'), message, confirmText: gettext('Discard'), danger: true })
      .then((ok) => { if (ok) dialog.close(); });
  });
  dialog.addEventListener('close', () => {
    grantsPanel.replaceChildren();
    grantsShown = false;
    ssoPanel.replaceChildren();
    ssoShown = false;
    grants = null;
    current = null;
    form.querySelectorAll('input[type="text"][name="password"]').forEach((input) => { input.type = 'password'; });
  });

  document.getElementById('user-new').addEventListener('click', () => {
    fill(null);
    openDialog(dialog);
  });

  // ------------------------------------------------------------ copy and delete

  function textField(id, label, attrs = {}, hint = '') {
    return h('div', { class: 'field' },
      h('div', { class: 'label-row' }, h('label', { for: id, text: label }), hint ? h('span', { class: 'hint label-hint', text: hint }) : null),
      h('input', { id, autocomplete: 'off', ...attrs }));
  }

  async function copyUser(user) {
    const passwordInput = h('input', { id: 'copy-password', name: 'password', type: 'password', autocomplete: 'new-password', maxlength: '128' });
    const body = h('div', {},
      h('p', { class: 'muted', text: interpolate(user.kind === 'normal' ? gettext('The new user gets the same default permissions, storage source permissions and enabled status as "%(username)s"; the password, admin role and two-step verification aren\'t copied.')
        : gettext('The new user gets the same default permissions and storage source permissions as "%(username)s"; the password, admin role and two-step verification aren\'t copied.'), { username: user.username }, true) }),
      textField('copy-username', gettext('New username'), { name: 'username', autocapitalize: 'off', spellcheck: 'false', maxlength: '150', required: true, 'data-autofocus': true }),
      textField('copy-nickname', gettext('New nickname'), { name: 'nickname', maxlength: '80' }, gettext('Optional')),
      h('div', { class: 'field' },
        h('div', { class: 'label-row' }, h('label', { for: 'copy-password', text: gettext('New password') }), h('span', { class: 'hint label-hint', text: gettext("Optional: without it, the new user can't log in with a password for now") })),
        h('span', { class: 'input-box' }, passwordInput,
          h('button', { type: 'button', class: 'icon-button field-button', 'data-toggle-password': '', 'aria-pressed': 'false', 'aria-label': gettext('Show password'), 'data-tooltip': gettext('Show password') }, icon('visibility')))));
    const copy = await editDialog({
      title: interpolate(gettext('Copy user "%(username)s"'), { username: user.username }, true),
      body,
      submitText: gettext('Copy'),
      submitIcon: 'content_copy',
      save: async (fields) => {
        const username = fields.username.value.trim();
        if (!username) throw new Error(gettext('A new username is required.'));
        const data = { username, nickname: fields.nickname.value };
        if (fields.password.value) data.password = fields.password.value;
        return api('POST', `/api/v1/manage/users/${user.id}/copy/`, data);
      },
    });
    if (!copy) return;
    toast(interpolate(copy.has_password ? gettext('Copied as %(username)s.') : gettext('Copied as %(username)s (no password yet).'), { username: copy.username }, true));
    load();
  }

  // The activity log, only what this user did and what was about them (activity/js/manage_activity_log.js).
  function showActivity(user) {
    if (window.App.showActivity) window.App.showActivity({ user: { id: user.id, username: user.kind === 'guest' ? user.nickname : user.username } });
  }

  async function deleteUser(user) {
    const ok = await confirmDialog({
      title: interpolate(gettext('Delete user "%(username)s"?'), { username: user.username }, true),
      message: gettext("Their permissions on every storage source and their shares are deleted too; their entries in the activity log stay. This can't be undone."),
      confirmText: gettext('Delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await api('DELETE', `/api/v1/manage/users/${user.id}/`);
      toast(interpolate(gettext('%(username)s deleted.'), { username: user.username }, true));
      load();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  // Loads the first time the section is shown (manage.js announces each one).
  document.addEventListener('manage:show', (e) => {
    if (e.detail === 'users' && !loaded) load();
  });
  // IP 归属地 was turned on or off, or locations filled in (ipgeo/js/manage_ip_geo.js): the list loads again
  // when it is next shown.
  document.addEventListener('hfile:ip-locations', () => { loaded = false; });
})();
