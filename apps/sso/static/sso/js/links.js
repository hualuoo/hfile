/* 账号绑定 (the user menu's item, sso/links_dialog.html): the enabled providers and the accounts bound
 * to the user (GET /api/v1/sso/links/). 绑定 asks for the login password when the account has one,
 * then POST /api/v1/sso/links/<code>/ answers the provider's address and the page goes there; the
 * provider sends the browser back to this page with ?auth=sso&sso_linked=<code>, which opens the dialog
 * again. 解除绑定 is DELETE; the server refuses to remove the last way to log in.
 *
 * A page that runs the admin's custom JS has neither the dialog nor this script (a password is typed
 * here, D33): its menu item goes to ?auth=sso, the same page without that JS, which opens the dialog. */
(function () {
  'use strict';
  const { api, h, icon, toast, confirmDialog, editDialog, openDialog, fmtDateTime, formError, fieldLabels } = window.App;

  const dialog = document.getElementById('sso-links-dialog');
  const opener = document.getElementById('sso-links-open');
  if (!dialog || !opener) return;
  const list = document.getElementById('sso-link-list');
  const box = dialog.querySelector('.dialog-body');
  Object.assign(fieldLabels, { password: gettext('Login password') });

  const LINKED = 'sso_linked';

  // This page without ?auth=sso and ?sso_linked=; with `auth`, the address that opens the dialog on it
  // (and without custom JS): where the provider sends the browser back to.
  function here(auth = false) {
    const params = new URLSearchParams(location.search);
    params.delete(LINKED);
    if (params.get('auth') === 'sso') params.delete('auth');
    if (auth) params.set('auth', 'sso');
    const query = params.toString();
    return location.pathname + (query ? `?${query}` : '') + location.hash;
  }

  function mark(provider) {
    const content = provider.icon
      ? h('img', { src: provider.icon, alt: '', width: '24', height: '24', referrerpolicy: 'no-referrer' })
      : h('span', { class: 'sso-letter', text: [...provider.name][0] || '?' });
    return h('span', { class: 'sso-button sso-button-static', 'aria-hidden': 'true' }, content);
  }

  function state(provider) {
    if (!provider.linked) return provider.enabled ? gettext('Not linked') : gettext("Not linked (this single sign-on isn't enabled now)");
    const parts = [interpolate(gettext('Linked, ID %(external_id)s'), { external_id: provider.external_id }, true)];
    if (provider.last_login_at) parts.push(interpolate(gettext('Last login with it: %(last_login_at)s'), { last_login_at: fmtDateTime(provider.last_login_at) }, true));
    else parts.push(interpolate(gettext('Linked at %(created_at)s'), { created_at: fmtDateTime(provider.created_at) }, true));
    if (!provider.enabled) parts.push(gettext("This single sign-on isn't enabled now"));
    return parts.join(' · ');
  }

  function row(provider, hasPassword) {
    const action = provider.linked
      ? h('button', { type: 'button', class: 'btn btn-small', onclick: () => unlink(provider) }, icon('link_off'), gettext('Unlink'))
      : h('button', { type: 'button', class: 'btn btn-small btn-tonal', onclick: () => link(provider, hasPassword) }, icon('link'), gettext('Link'));
    return h('li', { class: 'list-item sso-link-item' },
      h('span', { class: 'list-leading' }, mark(provider)),
      h('span', { class: 'list-text' },
        h('span', { class: 'list-headline', text: provider.name }),
        h('span', { class: 'list-supporting sso-link-state', text: state(provider) })),
      h('span', { class: 'list-trailing' }, action));
  }

  let loading = null;
  async function load() {
    formError(box, '');
    list.replaceChildren(h('li', { class: 'list-item muted', text: gettext('Loading…') }));
    try {
      const data = await api('GET', '/api/v1/sso/links/');
      list.replaceChildren(...(data.providers.length
        ? data.providers.map((provider) => row(provider, data.has_password))
        : [h('li', { class: 'list-item muted', text: gettext('There is no single sign-on to link now.') })]));
    } catch (err) {
      list.replaceChildren();
      formError(box, interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true));
    }
  }
  const reload = () => {
    if (!loading) loading = load().finally(() => { loading = null; });
    return loading;
  };

  async function go(provider, body) {
    const data = await api('POST', `/api/v1/sso/links/${encodeURIComponent(provider.code)}/`, { ...body, next: here(true) });
    if (typeof data.url !== 'string' || !/^https?:\/\//i.test(data.url)) throw new Error(gettext('The server gave a wrong address.'));
    location.href = data.url; // the provider's page; it sends the browser back here
    return true;
  }

  async function link(provider, hasPassword) {
    formError(box, '');
    if (!hasPassword) {
      try {
        await go(provider, {});
      } catch (err) {
        formError(box, err.message);
      }
      return;
    }
    // Confirms that it is the account's owner: an open session alone shouldn't add a way in.
    await editDialog({
      title: interpolate(gettext('Link %(name)s'), { name: provider.name }, true),
      body: h('div', {},
        h('p', { class: 'muted', text: interpolate(gettext("Next, the %(name)s login page opens; once you allow it there, you come back here. To confirm it's you, enter this site's login password first."), { name: provider.name }, true) }),
        h('div', { class: 'field' },
          h('label', { for: 'sso-link-password', text: gettext('Login password') }),
          h('span', { class: 'input-box' },
            h('input', { id: 'sso-link-password', name: 'password', type: 'password', autocomplete: 'current-password', maxlength: '4096', required: true, 'data-autofocus': true }),
            h('button', { type: 'button', class: 'icon-button field-button', 'data-toggle-password': '', 'aria-pressed': 'false', 'aria-label': gettext('Show password'), 'data-tooltip': gettext('Show password') }, icon('visibility'))))),
      submitText: gettext('Continue'),
      submitIcon: 'link',
      save: async (fields) => {
        if (!fields.password.value) throw new Error(gettext('The login password is required.'));
        return go(provider, { password: fields.password.value });
      },
    });
  }

  async function unlink(provider) {
    formError(box, '');
    const ok = await confirmDialog({
      title: interpolate(gettext('Unlink %(name)s?'), { name: provider.name }, true),
      message: gettext("After unlinking, this account can't log in any more; it can be linked again later."),
      confirmText: gettext('Unlink'),
      danger: true,
    });
    if (!ok) return;
    try {
      await api('DELETE', `/api/v1/sso/links/${encodeURIComponent(provider.code)}/`);
      toast(interpolate(gettext('%(name)s is unlinked.'), { name: provider.name }, true));
    } catch (err) {
      formError(box, err.message);
    }
    reload();
  }

  opener.addEventListener('click', () => {
    openDialog(dialog);
    reload();
  });

  // ?auth=sso: the menu item of a page with custom JS, or back from the provider after binding
  // (?sso_linked=): the dialog opens, and the address is tidied up.
  const params = new URLSearchParams(location.search);
  if (params.get('auth') === 'sso' || params.has(LINKED)) {
    const linked = params.has(LINKED);
    history.replaceState(null, '', here());
    document.addEventListener('DOMContentLoaded', () => {
      if (linked) toast(gettext('Linked: this account can log in from now on.'));
      openDialog(dialog);
      reload();
    });
  }
})();
