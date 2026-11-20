/* The user menu's 两步验证 (superusers, and anyone who has it on): its state (GET /api/v1/users/me/totp/),
 * setting it up (POST …/setup/ gives a QR code; POST …/ with a code from the phone and the password
 * turns it on) and turning it off (POST …/disable/ with the password). The secret shows only while
 * it is being set up. */
(function () {
  'use strict';
  const { api, h, icon, toast, openDialog, formError, fmtDateTime, copyText } = window.App;

  const opener = document.getElementById('totp-open');
  if (!opener) return;

  const INTRO = gettext('After the password, logging in also asks for the 6 digits an authenticator app on ' +
    "your phone shows: someone who knows your password still can't log in. Any app " +
    'that supports TOTP works, like Google Authenticator or Microsoft Authenticator.');
  let dialog = null;
  let body = null;

  function build() {
    body = h('div', { class: 'dialog-body totp-body' });
    dialog = h('dialog', { class: 'dialog dialog-small dialog-full', 'aria-labelledby': 'totp-title' },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' },
          h('h2', { id: 'totp-title', text: gettext('Two-step verification') }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        body));
    document.body.append(dialog);
  }

  function passwordField(id) {
    return h('div', { class: 'field' },
      h('label', { for: id, text: gettext('Your login password') }),
      h('span', { class: 'input-box' },
        h('input', { id, name: 'password', type: 'password', autocomplete: 'current-password', maxlength: '4096', required: true }),
        h('button', { type: 'button', class: 'icon-button field-button', 'data-toggle-password': '', 'aria-pressed': 'false', 'aria-label': gettext('Show password'), 'data-tooltip': gettext('Show password') }, icon('visibility'))));
  }

  // A form in the dialog whose submit runs `action(fields)`; errors show at its top.
  function form(children, submit, action, danger = false) {
    const button = h('button', { type: 'submit', class: danger ? 'btn btn-danger' : 'btn btn-filled', text: submit });
    const el = h('form', { novalidate: true },
      h('div', { class: 'form-error', role: 'alert', hidden: true }),
      ...children,
      h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') }), button));
    el.addEventListener('submit', async (e) => {
      e.preventDefault();
      formError(el, '');
      button.disabled = true;
      try {
        await action(el.elements);
      } catch (err) {
        formError(el, err.message);
      } finally {
        button.disabled = false;
      }
    });
    return el;
  }

  function showOff() {
    body.replaceChildren(
      h('p', { class: 'totp-state' }, h('span', { class: 'badge', text: gettext('Off') })),
      h('p', { class: 'muted', text: INTRO }),
      form([], gettext('Set up'), startSetup));
  }

  async function startSetup() {
    showSetup(await api('POST', '/api/v1/users/me/totp/setup/'));
  }

  function showSetup(data) {
    const grouped = data.secret.match(/.{1,4}/g).join(' ');
    const code = h('input', { id: 'totp-code', name: 'code', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '8', required: true });
    const copy = h('button', { type: 'button', class: 'icon-button', 'aria-label': gettext('Copy key'), 'data-tooltip': gettext('Copy key') }, icon('content_copy'));
    copy.addEventListener('click', async () => {
      toast(await copyText(data.secret) ? gettext('Key copied.') : gettext('Copying failed. Please type it in.'));
    });
    // No QR code when the site name and the username are too long for one (ACCT-03): the secret is typed in.
    const scan = data.qr
      ? [h('p', { class: 'muted', text: gettext('1. Scan this QR code with the authenticator app; if it can\'t scan, type the key below into the app (choose "Time based").') }),
        h('div', { class: 'totp-qr' }, h('img', { src: data.qr, alt: gettext('Two-step verification QR code'), width: '200', height: '200' }))]
      : [h('p', { class: 'muted', text: gettext('1. The site name and username are too long for a QR code: type the key below into the authenticator app (choose "Time based").') })];
    body.replaceChildren(
      ...scan,
      h('p', { class: 'totp-secret' }, h('code', { text: grouped }), copy),
      h('p', { class: 'muted', text: gettext('2. Enter the 6 digits the app shows for this site now and your login password to turn it on. Valid for 10 minutes.') }),
      form([h('div', { class: 'field' }, h('label', { for: 'totp-code', text: gettext('6 digits') }), code), passwordField('totp-password')],
        gettext('Verify and turn on'), async (f) => {
          const typed = f.code.value.normalize('NFKC').replace(/\s+/g, '');
          if (!/^[0-9]{6}$/.test(typed)) throw new Error(gettext('Please enter the 6 digits the app shows.'));
          if (!f.password.value) throw new Error(gettext('Your login password is required.'));
          const state = await api('POST', '/api/v1/users/me/totp/', { code: typed, password: f.password.value });
          toast(gettext('Two-step verification is on. Next time you log in, enter the digits from your phone.'));
          showOn(state);
        }));
    code.focus();
  }

  function showOn(state) {
    body.replaceChildren(
      h('p', { class: 'totp-state' }, h('span', { class: 'badge badge-ok' }, icon('shield'), gettext('On')),
        state.enabled_at ? h('span', { class: 'muted small', text: interpolate(gettext(' since %(enabled_at)s'), { enabled_at: fmtDateTime(state.enabled_at) }, true) }) : null),
      h('p', { class: 'muted', text: gettext("After the password, logging in also asks for the 6 digits an authenticator app on your phone shows. New phone: turn it off here first, then set it up again. Lost the phone and can't log in: run hfile resetadmin on the server.") }),
      form([passwordField('totp-off-password')], gettext('Turn off two-step verification'), async (f) => {
        if (!f.password.value) throw new Error(gettext('Your login password is required.'));
        const next = await api('POST', '/api/v1/users/me/totp/disable/', { password: f.password.value });
        toast(gettext('Two-step verification is off.'));
        if (next.available) showOff();
        else dialog.close();
      }, true));
  }

  async function open() {
    if (!dialog) build();
    body.replaceChildren(h('p', { class: 'muted', text: gettext('Loading…') }));
    openDialog(dialog);
    try {
      const state = await api('GET', '/api/v1/users/me/totp/');
      if (state.enabled) showOn(state);
      else showOff();
    } catch (err) {
      body.replaceChildren(h('p', { class: 'form-error', text: interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true) }));
    }
  }

  opener.addEventListener('click', open);

  // ?auth=totp opens it. Pages that run the admin's custom JS have no 两步验证 dialog and send the
  // menu item here: the same page without that JS (apps/siteconf/pages.py), as for ?auth=password.
  const params = new URLSearchParams(location.search);
  if (params.get('auth') === 'totp') {
    params.delete('auth');
    const query = params.toString();
    history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + location.hash);
    open();
  }
})();
