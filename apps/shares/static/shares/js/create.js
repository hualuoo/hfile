/* 创建分享 in files' browser (inventory 10.2, 6U.4; loaded on its pages by apps.files.pages.add_assets): an
 * action for the selected files and folders (contract W4, group 2 "links and shares"), for logged-in
 * users with create_share_link, and the dialog 创建分享链接: what is shared, 分享 Key (custom_share_key),
 * 访问密码 (4-20 characters, or none), 有效期 (永久 / 7 天 / 30 天 / 自定义, never the past), then the
 * link with 复制 and 打开链接. The password is shown apart from the link; 复制带密码链接 is a button of
 * its own (D43). A folder password the request needs is asked for as the browser asks it. */
(function () {
  'use strict';
  const App = window.App;
  if (!App || !App.files) return;
  const { h, icon, api, toast, copyText, openDialog } = App;

  const KEY_RE = /^[A-Za-z0-9_-]{3,8}$/;
  const EXPIRIES = [['forever', gettext('Never expires')], ['7d', gettext('Expires in 7 days')], ['30d', gettext('Expires in 30 days')], ['custom', gettext('Custom')]];
  const PASSWORD_CHARS = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';

  const pad = (n) => String(n).padStart(2, '0');
  // A datetime-local value of a moment, in the browser's time zone.
  function localValue(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function randomPassword(length = 6) {
    const values = new Uint32Array(length);
    crypto.getRandomValues(values);
    return [...values].map((n) => PASSWORD_CHARS[n % PASSWORD_CHARS.length]).join('');
  }

  function field(label, input, hint, id) {
    input.id = id || `share-field-${Math.random().toString(36).slice(2)}`;
    return h('div', { class: 'field' }, h('label', { for: input.id, text: label }), input, hint ? h('p', { class: 'hint', text: hint }) : null);
  }

  function itemsBlock(items) {
    const kindOf = (item) => (item.type === 'folder' ? gettext('Folder') : gettext('File'));
    if (items.length === 1) {
      const item = items[0];
      return h('div', { class: 'share-create-items' }, h('span', { class: 'label', text: gettext('Share files') }),
        h('div', { class: 'share-create-one' }, icon(App.files.kinds.icon(item)), h('span', { class: 'share-create-name', text: item.name }),
          h('span', { class: 'badge badge-info share-kind', text: kindOf(item) })));
    }
    return h('div', { class: 'share-create-items' }, h('span', { class: 'label', text: interpolate(ngettext('Share files: %(count)s item selected', 'Share files: %(count)s items selected', items.length), { count: items.length }, true) }),
      h('ul', { class: 'share-create-list', 'aria-label': gettext('Shared files and folders') },
        ...items.map((item) => h('li', null, icon(App.files.kinds.icon(item)), h('span', { class: 'share-create-name', text: item.name })))));
  }

  function copyRow(label, copyLabel, value, message, id) {
    const input = h('input', { type: 'text', readonly: true, value, spellcheck: 'false' });
    input.id = id;
    const copy = h('button', { type: 'button', class: 'icon-button', 'aria-label': copyLabel, 'data-tooltip': gettext('Copy') }, icon('content_copy'));
    copy.addEventListener('click', async () => {
      if (await copyText(value)) toast(message);
      else {
        input.select();
        toast(gettext('Copying failed. Please copy it by hand.'), 'error');
      }
    });
    input.addEventListener('focus', () => input.select());
    return h('div', { class: 'field' }, h('label', { for: id, text: label }), h('div', { class: 'share-copy-row' }, input, copy));
  }

  function createDialog(ctx, items) {
    const canKey = !!ctx.permissions.custom_share_key;
    const keyInput = h('input', { type: 'text', name: 'key', maxlength: '8', autocomplete: 'off', spellcheck: 'false', placeholder: gettext('Leave empty to generate one') });
    const password = h('input', { type: 'text', name: 'password', maxlength: '20', autocomplete: 'off', spellcheck: 'false', placeholder: gettext('Leave empty for no password') });
    const dice = h('button', { type: 'button', class: 'icon-button field-button', 'aria-label': gettext('Generate a random password'), 'data-tooltip': gettext('Random') }, icon('casino'));
    dice.addEventListener('click', () => { password.value = randomPassword(); password.focus(); });
    let expiry = 'forever';
    const custom = h('input', { type: 'datetime-local', name: 'expires_at', 'aria-label': gettext('Choose when it expires'), hidden: true });
    const chips = EXPIRIES.map(([value, label]) => {
      const chip = h('button', { type: 'button', class: `chip${value === expiry ? ' selected' : ''}`, role: 'radio', 'aria-checked': String(value === expiry), text: label });
      chip.addEventListener('click', () => {
        expiry = value;
        chips.forEach((other) => {
          other.classList.toggle('selected', other === chip);
          other.setAttribute('aria-checked', String(other === chip));
        });
        custom.hidden = value !== 'custom';
        if (value === 'custom') {
          const now = new Date();
          custom.min = localValue(new Date(now.getTime() + 60000));
          if (!custom.value) custom.value = localValue(new Date(now.getTime() + 24 * 3600 * 1000));
          custom.focus();
        }
      });
      return chip;
    });
    const titleId = `share-create-${Math.random().toString(36).slice(2)}`;
    const error = h('div', { class: 'form-error', role: 'alert', hidden: true });
    const submit = h('button', { type: 'submit', class: 'btn btn-filled' }, icon('share'), h('span', { text: gettext('Create share') }));
    const body = h('div', { class: 'share-create-body' },
      h('p', { class: 'share-tip' }, icon('info'), h('span', { text: gettext('What people can open through the share follows your permissions, and changes as soon as they change.') })),
      itemsBlock(items),
      canKey ? field(gettext('Share key'), keyInput, gettext('Letters, digits, underscores and hyphens only, 3 to 8 characters')) : null,
      h('div', { class: 'field' }, h('label', { for: 'share-create-password', text: gettext('Access password') }),
        h('div', { class: 'input-box' }, password, dice), h('p', { class: 'hint', text: gettext('4 to 20 characters; people opening the share must enter it.') })),
      h('div', { class: 'field' }, h('span', { class: 'label', text: gettext('Expires') }),
        h('div', { class: 'chips share-expiry-choice', role: 'radiogroup', 'aria-label': gettext('Expires') }, ...chips),
        h('div', { class: 'share-expiry-custom' }, custom)));
    password.id = 'share-create-password';
    const foot = h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') }), submit);
    const form = h('form', { class: 'dialog-body', novalidate: true }, body, error, foot);
    const dialog = h('dialog', { class: 'dialog dialog-small share-create', 'aria-labelledby': titleId },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: gettext('Create share link') }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        form));
    const showError = (message) => { error.textContent = message || ''; error.hidden = !message; };
    let busy = false;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (busy) return;
      showError('');
      const key = keyInput.value.trim();
      const secret = password.value.trim();
      if (canKey && key && !KEY_RE.test(key)) { showError(gettext('The share key can only have letters, digits, underscores and hyphens, 3 to 8 characters')); keyInput.focus(); return; }
      if (secret && (secret.length < 4 || secret.length > 20)) { showError(gettext('The password must be 4 to 20 characters')); password.focus(); return; }
      const request = {
        storage: ctx.storage.key,
        path: ctx.path,
        entries: items.map((item) => ({ name: item.name, type: item.type })),
        password: secret,
        expiry,
      };
      if (canKey && key) request.key = key;
      if (expiry === 'custom') {
        const moment = custom.value ? new Date(custom.value) : null;
        if (!moment || Number.isNaN(moment.getTime())) { showError(gettext('Please choose when it expires')); custom.focus(); return; }
        if (moment.getTime() <= Date.now()) { showError(gettext('The expiry time must be in the future')); custom.focus(); return; }
        request.expires_at = moment.toISOString();
      }
      busy = true;
      submit.disabled = true;
      submit.querySelector('span').textContent = gettext('Creating…');
      try {
        const share = await ctx.browser.withPasswords(() => api('POST', '/api/v1/shares/', request));
        if (share) {
          toast(gettext('Share link created!'));
          showResult(share);
        }
      } catch (err) {
        showError(err.message);
      } finally {
        busy = false;
        submit.disabled = false;
        submit.querySelector('span').textContent = gettext('Create share');
      }
    });
    dialog.addEventListener('cancel', (e) => { if (busy) e.preventDefault(); });
    dialog.addEventListener('close', () => dialog.remove());

    // The result in the same dialog: the link, the password apart, 打开链接, 完成.
    function showResult(share) {
      const parts = [
        h('p', { class: 'share-result-ok' }, icon('check_circle'), h('span', { text: gettext('Share link created!') })),
        copyRow(gettext('Share link'), gettext('Copy share link'), share.url, gettext('Share link copied to the clipboard'), 'share-result-url'),
      ];
      if (share.password) {
        parts.push(copyRow(gettext('Access password'), gettext('Copy access password'), share.password, gettext('Access password copied to the clipboard'), 'share-result-password'));
      }
      const open = h('a', { class: 'btn btn-tonal', href: App.pageUrl(share.url), target: '_blank', rel: 'noopener' }, icon('open_in_new'), gettext('Open link'));
      const actions = [open];
      if (share.password) {
        const withPassword = h('button', { type: 'button', class: 'btn btn-text' }, icon('link'), gettext('Copy link with password'));
        withPassword.addEventListener('click', async () => {
          const link = `${share.url}?pwd=${encodeURIComponent(share.password)}`;
          if (await copyText(link)) toast(gettext('Share link with password copied to the clipboard'));
          else toast(gettext('Copying failed. Please copy it by hand.'), 'error');
        });
        actions.push(withPassword);
      }
      parts.push(h('div', { class: 'share-result-actions' }, ...actions));
      parts.push(h('p', { class: 'hint', text: share.expires_at ? interpolate(gettext('Valid until %(expires_at)s'), { expires_at: App.fmtDateTime(share.expires_at) }, true) : gettext('Never expires') }));
      body.replaceChildren(...parts);
      foot.replaceChildren(h('button', { type: 'button', class: 'btn btn-filled', 'data-close': '', text: gettext('Done') }));
      const url = dialog.querySelector('#share-result-url');
      if (url) url.focus();
    }

    document.body.append(dialog);
    openDialog(dialog);
    (canKey ? keyInput : password).focus();
  }

  App.files.actions.register({
    key: 'share', label: gettext('Create share'), icon: 'share', group: 2, order: 50,
    when: (ctx, items) => ctx.mode === 'browse' && !!ctx.storage && items.length > 0 && App.loggedIn
      && !!ctx.permissions.create_share_link,
    run: (ctx, items) => createDialog(ctx, items),
  });
})();
