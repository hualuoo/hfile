/* /manage/#sso: the single sign-on providers (GET/POST/PATCH/DELETE /api/v1/manage/sso/providers/, the
 * order on the login page with POST …/order/), their form with the presets of 新增 and the reading of an
 * OpenID configuration (POST /api/v1/manage/sso/discover/).
 *
 * Also App.ssoIdentities(panel, {userId, user}): the 单点登录 tab of the user dialog (manage_users.js),
 * the accounts bound to a user (/api/v1/manage/users/<id>/identities/). */
(function () {
  'use strict';
  const {
    api, h, icon, toast, fmtNum, fmtDateTime, confirmDialog, editDialog, openDialog, formError, errorText, fieldLabels,
    updateCounters, copyText,
  } = window.App;

  Object.assign(fieldLabels, {
    client_id: 'Client ID', client_secret: 'Client Secret', auth_url: gettext('Authorization endpoint'), token_url: gettext('Token endpoint'),
    userinfo_url: gettext('Userinfo endpoint'), token_auth: gettext('How the Client Secret is sent'), id_field: gettext('User ID field'),
    username_field: gettext('Username field'), nickname_field: gettext('Nickname field'), external_id: gettext('User ID'),
  });

  // A provider's mark: its icon in a light circle (brand marks are dark or colored), or its first letter.
  function mark(provider, extra = '') {
    const content = provider.icon
      ? h('img', { src: provider.icon, alt: '', width: '24', height: '24', referrerpolicy: 'no-referrer' })
      : h('span', { class: 'sso-letter', text: [...(provider.name || '?')][0] || '?' });
    return h('span', { class: `sso-button sso-button-static ${extra}`.trim(), 'aria-hidden': 'true' }, content);
  }

  // ------------------------------------------------------------ App.ssoIdentities (the user dialog)

  async function identitiesEditor(panel, { userId, user }) {
    panel.replaceChildren(h('p', { class: 'muted', text: gettext('Loading…') }));
    if (user && user.kind !== 'normal') {
      panel.replaceChildren(h('p', { class: 'notice notice-info', text: gettext("Built-in users don't log in and can't link single sign-on.") }));
      return;
    }
    let identities;
    let providers;
    try {
      [identities, providers] = await Promise.all([
        api('GET', `/api/v1/manage/users/${userId}/identities/`),
        api('GET', '/api/v1/manage/sso/providers/'),
      ]);
    } catch (err) {
      panel.replaceChildren(h('p', { class: 'notice notice-error', text: interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true) }));
      return;
    }
    if (user && !user.can.edit) { // another admin's: only superusers see and change them
      panel.replaceChildren(h('p', { class: 'notice notice-info', text: interpolate(gettext('%(reason)s Only superusers can see and change their single sign-on links too.'), { reason: user.why.edit }, true) }));
      return;
    }
    // One's own: through 账号绑定, which asks for the password (the server refuses it here).
    const own = String(userId) === (document.body.dataset.user || '');
    const editable = !own;
    const error = h('div', { class: 'form-error', role: 'alert', hidden: true });
    const reloadPanel = () => identitiesEditor(panel, { userId, user });

    async function unbind(identity) {
      const last = user && !user.has_password && identities.length === 1;
      const ok = await confirmDialog({
        title: interpolate(gettext('Unlink %(name)s?'), { name: identity.provider.name }, true),
        message: last ? gettext('They have no password, so this is their only way to log in: after unlinking they couldn\'t log in (you can give them a password in "Basic info").') : gettext("After unlinking, they can't log in with this account; it can be linked again later."),
        confirmText: gettext('Unlink'),
        danger: true,
      });
      if (!ok) return;
      try {
        await api('DELETE', `/api/v1/manage/users/${userId}/identities/${identity.id}/`);
        toast(interpolate(gettext('%(name)s is unlinked.'), { name: identity.provider.name }, true));
        reloadPanel();
      } catch (err) {
        formError(panel, err.message);
      }
    }

    const items = identities.map((identity) => {
      const facts = [`ID ${identity.external_id}`, interpolate(gettext('Linked at %(created_at)s'), { created_at: fmtDateTime(identity.created_at) }, true)];
      facts.push(identity.last_login_at ? interpolate(gettext('Last login with it: %(last_login_at)s'), { last_login_at: fmtDateTime(identity.last_login_at) }, true) : gettext('Never logged in with it'));
      if (!identity.provider.enabled) facts.push(gettext("This single sign-on isn't enabled now"));
      return h('li', { class: 'list-item' },
        h('span', { class: 'list-leading' }, mark(identity.provider)),
        h('span', { class: 'list-text' },
          h('span', { class: 'list-headline', text: identity.provider.name }),
          h('span', { class: 'list-supporting sso-link-state', text: facts.join(' · ') })),
        editable ? h('span', { class: 'list-trailing' },
          h('button', { type: 'button', class: 'btn btn-small', onclick: () => unbind(identity) }, icon('link_off'), gettext('Unlink'))) : null);
    });

    const bound = new Set(identities.map((identity) => identity.provider.id));
    const free = providers.filter((provider) => !bound.has(provider.id));
    let adder = null;
    if (editable && free.length) {
      const select = h('select', { id: 'user-sso-provider', name: 'provider' },
        free.map((provider) => h('option', { value: String(provider.id), text: provider.enabled ? provider.name : interpolate(gettext('%(name)s (not enabled)'), { name: provider.name }, true) })));
      const input = h('input', { id: 'user-sso-external', name: 'external_id', autocomplete: 'off', spellcheck: 'false', maxlength: '255', required: true });
      const button = h('button', { type: 'submit', class: 'btn btn-tonal' }, icon('link'), gettext('Link'));
      adder = h('form', { class: 'sso-identity-add', novalidate: true },
        h('div', { class: 'field' }, h('label', { for: 'user-sso-provider', text: gettext('Provider') }), select),
        h('div', { class: 'field' },
          h('div', { class: 'label-row' }, h('label', { for: 'user-sso-external', text: gettext('User ID at the provider') }),
            h('span', { class: 'hint label-hint', text: gettext('The value of the "User ID field" in the user info') })),
          input),
        h('div', { class: 'sso-identity-add-button' }, button));
      adder.addEventListener('submit', async (e) => {
        e.preventDefault();
        formError(panel, '');
        if (!input.value.trim()) {
          formError(panel, gettext('The user ID at the provider is required.'));
          input.focus();
          return;
        }
        button.disabled = true;
        try {
          const made = await api('POST', `/api/v1/manage/users/${userId}/identities/`, { provider: Number(select.value), external_id: input.value });
          toast(interpolate(gettext('Linked the %(name)s account %(external_id)s.'), { name: made.provider.name, external_id: made.external_id }, true));
          reloadPanel();
        } catch (err) {
          formError(panel, err.message);
          button.disabled = false;
        }
      });
    }

    panel.replaceChildren(...[
      h('p', { class: 'muted', text: gettext('Once linked, they can log in with these providers\' accounts. When someone without a link fails to log in with single sign-on, the login log shows "short-name:user-ID", which you can link here; before linking, check with the provider that the ID really is theirs.') }),
      own ? h('p', { class: 'notice notice-info', text: gettext('This is your own account: to link or unlink, use "Linked accounts" in the menu at the top right (linking asks for your login password).') }) : null,
      error,
      items.length ? h('ul', { class: 'list list-divided sso-identity-list' }, items)
        : h('p', { class: 'muted sso-empty', text: gettext('No single sign-on accounts linked yet.') }),
      adder,
      !providers.length ? h('p', { class: 'notice notice-info', text: gettext('There are no single sign-on providers yet: add one in "Single sign-on" on the Manage page first.') }) : null,
    ].filter(Boolean));
  }
  window.App.ssoIdentities = identitiesEditor;

  // ------------------------------------------------------------ the section

  const rows = document.getElementById('sso-rows');
  if (!rows) return;
  const summary = document.getElementById('sso-summary');
  const presets = JSON.parse(document.getElementById('sso-dialog').dataset.presets || '[]');
  // Superusers change providers; other staff see them (apps/sso/views.py).
  const canEdit = document.getElementById('sso-dialog').dataset.canEdit === '1';
  const READ_ONLY = gettext('Only superusers can change the single sign-on settings.');
  let providers = [];
  let loaded = false;

  const messageRow = (text, isError = false) => h('tr', { class: 'message-row' },
    h('td', { colspan: '5' }, h('span', { class: isError ? 'field-error' : '', text })));

  function iconButton(name, label, onclick, disabled = false) {
    return h('button', { type: 'button', class: 'icon-button', 'aria-label': label, 'data-tooltip': label, disabled, onclick }, icon(name));
  }

  function nameCell(provider) {
    const warnings = [];
    if (provider.client_secret_unreadable) {
      warnings.push(h('span', { class: 'badge badge-error', 'data-tooltip': gettext('secret_key in the settings file has changed; the Client Secret must be entered again'), text: gettext('Secret unreadable') }));
    }
    return h('td', {}, h('div', { class: 'user-cell' },
      mark(provider),
      h('span', { class: 'user-names' },
        h('span', { class: 'user-display', text: provider.name }),
        h('span', { class: 'muted small', text: `/sso/${provider.code}/login` }),
        warnings.length ? h('span', { class: 'user-badges' }, warnings) : null)));
  }

  function row(provider, index) {
    const toggle = h('input', { type: 'checkbox', class: 'switch', role: 'switch', 'aria-label': interpolate(gettext('Enable %(name)s'), { name: provider.name }, true), checked: provider.enabled, disabled: !canEdit });
    toggle.addEventListener('change', () => setEnabled(provider, toggle));
    const tr = h('tr', { class: canEdit ? 'interactive' : '' },
      nameCell(provider),
      h('td', { class: 'cell-switch', 'data-label': gettext('Enabled') }, canEdit ? toggle : h('span', { class: 'reason-wrap', 'data-tooltip': READ_ONLY }, toggle)),
      h('td', { 'data-label': gettext('Linked users'), text: interpolate(ngettext('%(identities)s user', '%(identities)s users', provider.identities), { identities: fmtNum(provider.identities) }, true) }),
      canEdit ? h('td', { class: 'nowrap', 'data-label': gettext('Order') },
        iconButton('arrow_upward', gettext('Move up'), () => move(index, -1), index === 0),
        iconButton('arrow_downward', gettext('Move down'), () => move(index, 1), index === providers.length - 1)) : h('td', { 'data-label': gettext('Order'), text: String(index + 1) }),
      h('td', { class: 'row-actions' }, canEdit ? [
        iconButton('edit', gettext('Edit'), () => edit(provider)),
        iconButton('delete', gettext('Delete'), () => remove(provider))] : null));
    if (canEdit) {
      tr.addEventListener('click', (e) => {
        if (e.target.closest('button, input, a') || String(window.getSelection())) return;
        edit(provider);
      });
    }
    return tr;
  }

  function render() {
    const enabled = providers.filter((provider) => provider.enabled).length;
    summary.textContent = providers.length ? interpolate(ngettext('%(count)s in all, %(enabled)s enabled', '%(count)s in all, %(enabled)s enabled', providers.length), { count: providers.length, enabled }, true) : '';
    rows.replaceChildren(...(providers.length ? providers.map(row)
      : [messageRow(gettext('No single sign-on yet. Click "Add" and choose a provider to start.'))]));
  }

  async function load() {
    loaded = true;
    summary.textContent = gettext('Loading…');
    try {
      providers = await api('GET', '/api/v1/manage/sso/providers/');
      render();
    } catch (err) {
      summary.textContent = '';
      rows.replaceChildren(messageRow(interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true), true));
    }
  }

  async function setEnabled(provider, input) {
    const on = input.checked;
    input.disabled = true;
    try {
      const saved = await api('PATCH', `/api/v1/manage/sso/providers/${provider.id}/`, { enabled: on });
      toast(on ? interpolate(gettext("%(name)s is enabled: it's on the login page now."), { name: saved.name }, true) : interpolate(gettext("%(name)s is disabled: it's no longer on the login page and can't be used to log in."), { name: saved.name }, true));
      load();
    } catch (err) {
      input.checked = !on;
      input.disabled = false;
      toast(err.message, 'error');
    }
  }

  async function move(index, delta) {
    const ids = providers.map((provider) => provider.id);
    const other = index + delta;
    if (other < 0 || other >= ids.length) return;
    [ids[index], ids[other]] = [ids[other], ids[index]];
    try {
      providers = await api('POST', '/api/v1/manage/sso/providers/order/', { ids });
      render();
      const button = rows.querySelectorAll('tr')[other]?.querySelector(`[aria-label="${delta < 0 ? gettext('Move up') : gettext('Move down')}"]`);
      if (button && !button.disabled) button.focus();
    } catch (err) {
      toast(err.message, 'error');
      load();
    }
  }

  async function remove(provider) {
    const bound = provider.identities;
    const ok = await confirmDialog({
      title: interpolate(gettext('Delete the single sign-on "%(name)s"?'), { name: provider.name }, true),
      message: bound
        ? interpolate(ngettext("The %(name)s account linked by %(count)s user is deleted too, and can't log in any more (the user account stays). This can't be undone.", "The %(name)s accounts linked by %(count)s users are deleted too, and can't log in any more (the user accounts stay). This can't be undone.", bound), { count: bound, name: provider.name }, true)
        : gettext("This can't be undone."),
      confirmText: gettext('Delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await api('DELETE', `/api/v1/manage/sso/providers/${provider.id}/`);
      toast(interpolate(gettext('%(name)s deleted.'), { name: provider.name }, true));
      load();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  // ------------------------------------------------------------ the provider dialog

  const dialog = document.getElementById('sso-dialog');
  document.body.append(dialog); // out of the section, which a change of #hash hides
  const form = document.getElementById('sso-form');
  const f = form.elements;
  const title = document.getElementById('sso-dialog-title');
  const presetHelp = document.getElementById('sso-preset-help');
  const callback = document.getElementById('sso-callback-url');
  const secretHint = document.getElementById('sso-secret-hint');
  const secretClear = document.getElementById('sso-secret-clear');
  const preview = document.getElementById('sso-icon-preview');
  const fileInput = document.getElementById('sso-icon-file');
  const saveButton = form.querySelector('[type="submit"]');
  const ORIGIN = dialog.dataset.origin || location.origin;
  const FIELDS = ['code', 'name', 'icon', 'client_id', 'auth_url', 'token_url', 'userinfo_url', 'scope', 'token_auth',
    'id_field', 'username_field', 'nickname_field', 'order'];
  const BLANK = {
    code: '', name: '', icon: '', client_id: '', auth_url: '', token_url: '', userinfo_url: '', scope: '', token_auth: 'post',
    id_field: 'sub', username_field: 'preferred_username', nickname_field: 'name', order: 0, enabled: true,
  };
  const ICON_FILE_MAX = 48 * 1024; // a data: URI is a third longer: within the 64 KB the server takes
  let current = null; // the provider in the dialog, as the server gave it; null for a new one
  let savedState = '';

  const snapshot = () => JSON.stringify([...FIELDS.map((name) => f[name].value), f.client_secret.value, f.clear_secret.checked,
    f.enabled.checked]);

  // Clearing the saved secret (ACCT-05): nothing to type then.
  f.clear_secret.addEventListener('change', () => {
    f.client_secret.disabled = f.clear_secret.checked;
    if (f.clear_secret.checked) f.client_secret.value = '';
  });

  function showCallback() {
    const code = f.code.value.trim().toLowerCase();
    callback.textContent = `${ORIGIN}/sso/${code || gettext('<short-name>')}/login/callback`;
  }

  function showPreview() {
    const value = f.icon.value.trim();
    preview.replaceChildren();
    if (/^(data:image\/|https?:\/\/)/i.test(value)) {
      const img = h('img', { src: value, alt: '', width: '24', height: '24', referrerpolicy: 'no-referrer' });
      img.addEventListener('error', () => {
        preview.replaceChildren(h('span', { class: 'sso-letter', 'data-tooltip': gettext("Can't show it: the address is wrong, or this page doesn't allow that site yet (save and reload to see it)"), text: '!' }));
      });
      preview.append(img);
    } else {
      preview.append(h('span', { class: 'sso-letter', text: [...(f.name.value.trim() || '?')][0] }));
    }
  }

  function clearFieldErrors() {
    form.querySelectorAll('.field.invalid').forEach((field) => field.classList.remove('invalid'));
    form.querySelectorAll('.field > .field-error').forEach((el) => el.remove());
  }

  // Field errors go under their field; the rest above the form.
  function showErrors(err) {
    clearFieldErrors();
    const data = err.data && typeof err.data === 'object' && !Array.isArray(err.data) && typeof err.data.detail !== 'string' ? err.data : null;
    if (!data) {
      formError(form, err.message);
      return;
    }
    const rest = {};
    let first = null;
    for (const [key, value] of Object.entries(data)) {
      const field = form.querySelector(`.field[data-field="${CSS.escape(key)}"]`);
      if (field && Array.isArray(value)) {
        field.classList.add('invalid');
        field.append(h('p', { class: 'field-error', text: value.join(' ') }));
        first = first || field;
      } else if (key !== 'code' || typeof value !== 'string') {
        rest[key] = value;
      }
    }
    formError(form, Object.keys(rest).length ? errorText(rest, err.status) : '');
    if (first) {
      const input = first.querySelector('input, select');
      if (input) input.focus();
    }
  }

  function fill(provider, preset = null) {
    current = provider;
    form.reset();
    formError(form, '');
    clearFieldErrors();
    const values = provider || (preset ? { ...BLANK, ...preset.values } : BLANK);
    for (const name of FIELDS) f[name].value = values[name] ?? '';
    f.enabled.checked = Boolean(values.enabled ?? true);
    f.client_secret.value = '';
    f.client_secret.disabled = false;
    secretClear.hidden = !(provider && (provider.client_secret_set || provider.client_secret_unreadable));
    title.textContent = provider ? interpolate(gettext('Edit single sign-on "%(name)s"'), { name: provider.name }, true) : (preset ? interpolate(gettext('Add single sign-on: %(label)s'), { label: preset.label }, true) : gettext('Add single sign-on'));
    presetHelp.textContent = preset ? preset.help : '';
    presetHelp.hidden = !preset;
    if (provider && provider.client_secret_unreadable) secretHint.textContent = gettext('Unreadable (secret_key has changed); please enter it again');
    else if (provider && provider.client_secret_set) secretHint.textContent = gettext('Already set; leave empty to keep it');
    else secretHint.textContent = gettext('Saved encrypted and not shown again; public clients can leave it empty');
    showCallback();
    showPreview();
    updateCounters(dialog);
    savedState = snapshot();
  }

  function collect() {
    const body = {};
    for (const name of FIELDS) body[name] = name === 'order' ? Number(f.order.value || 0) : f[name].value;
    body.enabled = f.enabled.checked;
    if (f.clear_secret.checked) body.client_secret = null; // a public client from now on
    else if (f.client_secret.value) body.client_secret = f.client_secret.value;
    return body;
  }

  async function edit(provider) {
    let fresh;
    try {
      fresh = await api('GET', `/api/v1/manage/sso/providers/${provider.id}/`);
    } catch (err) {
      toast(err.message, 'error');
      return;
    }
    fill(fresh);
    openDialog(dialog);
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    formError(form, '');
    clearFieldErrors();
    if (Number.isNaN(Number(f.order.value || 0))) {
      showErrors({ data: { order: [gettext('The order should be an integer.')] }, status: 400 });
      return;
    }
    saveButton.disabled = true;
    try {
      const saved = current
        ? await api('PATCH', `/api/v1/manage/sso/providers/${current.id}/`, collect())
        : await api('POST', '/api/v1/manage/sso/providers/', collect());
      savedState = snapshot();
      dialog.close();
      toast(current ? interpolate(gettext('%(name)s saved.'), { name: saved.name }, true) : interpolate(gettext("%(name)s added. Don't forget to register the callback URL with the provider."), { name: saved.name }, true));
      load();
    } catch (err) {
      showErrors(err);
    } finally {
      saveButton.disabled = false;
    }
  });

  f.code.addEventListener('input', showCallback);
  f.icon.addEventListener('input', showPreview);
  f.name.addEventListener('input', () => { if (!f.icon.value.trim()) showPreview(); });

  // Raw SVG markup pasted into the icon box becomes a data: URI.
  function base64(text) {
    const bytes = new TextEncoder().encode(text);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }
  f.icon.addEventListener('change', () => {
    const value = f.icon.value.trim();
    if (/^<svg[\s>]/i.test(value)) {
      f.icon.value = `data:image/svg+xml;base64,${base64(value)}`;
      showPreview();
    }
  });

  document.getElementById('sso-icon-upload').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    fileInput.value = '';
    if (!file) return;
    clearFieldErrors();
    if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'].includes(file.type)) {
      showErrors({ data: { icon: [gettext('Only PNG, JPEG, GIF, WebP or SVG pictures.')] }, status: 400 });
      return;
    }
    if (file.size > ICON_FILE_MAX) {
      showErrors({ data: { icon: [interpolate(gettext('The picture is too large: at most %(limit)s KB.'), { limit: ICON_FILE_MAX / 1024 }, true)] }, status: 400 });
      return;
    }
    const reader = new FileReader();
    reader.addEventListener('load', () => {
      f.icon.value = String(reader.result);
      showPreview();
    });
    reader.readAsDataURL(file);
  });

  document.getElementById('sso-callback-copy').addEventListener('click', async () => {
    toast(await copyText(callback.textContent) ? gettext('Callback URL copied.') : gettext("Can't copy it. Please select and copy it by hand."));
  });

  // Reads an OpenID configuration on the server (this page may fetch only this site) and fills the form.
  document.getElementById('sso-discover').addEventListener('click', async () => {
    let guess = '';
    try {
      if (f.auth_url.value.trim()) guess = `${new URL(f.auth_url.value.trim()).origin}/.well-known/openid-configuration`;
    } catch (err) {
      guess = '';
    }
    const found = await editDialog({
      title: gettext('Fill in from a .well-known address'),
      body: h('div', {},
        h('p', { class: 'muted', text: gettext("Enter the OpenID Connect service's configuration address, like https://gitlab.com/.well-known/openid-configuration. The server reads it and fills in the authorization, token and userinfo endpoints.") }),
        h('div', { class: 'field' },
          h('label', { for: 'sso-discover-url', text: gettext('.well-known address') }),
          h('input', { id: 'sso-discover-url', name: 'url', type: 'url', autocomplete: 'off', spellcheck: 'false', maxlength: '1000', value: guess, required: true, 'data-autofocus': true }))),
      submitText: gettext('Read'),
      submitIcon: 'language',
      save: async (fields) => {
        const url = fields.url.value.trim();
        if (!url) throw new Error(gettext('The .well-known address is required.'));
        try {
          return await api('POST', '/api/v1/manage/sso/discover/', { url });
        } catch (err) {
          throw new Error(err.data && Array.isArray(err.data.url) ? err.data.url.join(' ') : err.message);
        }
      },
    });
    if (!found) return;
    const missing = [];
    for (const [name, label] of [['auth_url', gettext('Authorization endpoint')], ['token_url', gettext('Token endpoint')], ['userinfo_url', gettext('Userinfo endpoint')]]) {
      if (found[name]) f[name].value = found[name];
      else missing.push(label);
    }
    if (found.token_auth) f.token_auth.value = found.token_auth;
    if (!f.scope.value.trim() && found.scopes.includes('openid')) f.scope.value = found.scopes.includes('profile') ? 'openid profile' : 'openid';
    clearFieldErrors();
    toast(missing.length ? interpolate(gettext('Read it, but it has no %(fields)s; please fill them in by hand.'), { fields: missing.join(gettext(', ')) }, true) : gettext('Read it: the three endpoints are filled in.'));
  });

  // Closing with changes not saved asks first (Esc, the close button, the backdrop and 取消 all fire "cancel").
  dialog.addEventListener('cancel', (e) => {
    if (snapshot() === savedState) return;
    e.preventDefault();
    confirmDialog({ title: gettext('Discard unsaved changes?'), message: gettext("This single sign-on's settings have unsaved changes."), confirmText: gettext('Discard'), danger: true })
      .then((ok) => { if (ok) dialog.close(); });
  });
  dialog.addEventListener('close', () => {
    current = null;
    form.querySelectorAll('input[type="text"][name="client_secret"]').forEach((input) => { input.type = 'password'; });
  });

  // 新增: blank, or a preset (common.js runs the menu itself). Superusers only.
  const newMenu = document.getElementById('sso-new-menu');
  if (newMenu) newMenu.addEventListener('click', (e) => {
    const item = e.target.closest('[data-preset]');
    if (!item) return;
    const preset = presets.find((one) => one.key === item.dataset.preset) || null;
    fill(null, preset);
    openDialog(dialog);
  });

  // Loads the first time the section is shown (manage.js announces each one).
  document.addEventListener('manage:show', (e) => {
    if (e.detail === 'sso' && !loaded) load();
  });
})();
