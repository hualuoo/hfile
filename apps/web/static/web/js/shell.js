/* The frame of every page (base.html): the top app bar, the user menu with 修改密码, the 主题 choice
 * and 退出, the login form (in the login dialog, or on the login page /login/…/) and the password dialog.
 *
 * Logging in may take more than the password (apps/accounts/login.py): the API then answers
 * {"authenticated": false, "step": "<name>", "before_password": bool, "detail"}. The scripts of the
 * steps (accounts/js/login_steps.js) register what a step shows: App.loginSteps[name] = (ctx) => handle.
 *   ctx      { answer, area, form, send }: the API's answer (null when the form shows the step before
 *            being asked: the captcha, while the page knows the site wants one; answer.resumed when it is a
 *            login begun earlier, maybe without a password: single sign-on), an empty box in the
 *            form for the step's fields, and send(fields), which posts them (with the username and
 *            password again for a step before the password) and goes on as the answer says
 *   handle   (optional) { fields(), failed(err), submit(), label }:
 *            fields()     a step before the password: its fields, sent with every try of the password;
 *                         may throw an Error for the form to show
 *            failed(err)  a try failed (the API's ApiError): a used captcha picture is replaced, say
 *            submit()     a step after the password: the form was submitted; returns send(...)
 *            label        the form's button meanwhile ("验证")
 * On /login/<entry>/ (a secure login entry, apps/accounts/views.py) the form has data-entry, sent with every
 * POST.
 *
 * A login waiting for a step after the password (GET /api/v1/session/ says which: a dialog closed
 * halfway, or single sign-on handing two-step verification over, apps/sso/flow.py) is shown again when the
 * form opens, with 换个方式登录 to drop it. App.loginNext() is where the form goes after logging in
 * (null: the page reloads), for the single sign-on buttons under it. */
(function () {
  'use strict';
  const { api, ApiError, h, iconHref, toast, flash, safeNext, openDialog, formError, theme, loggedIn } = window.App;

  // 跳到正文 moves the focus to <main> without changing the address: /manage/ reads its section from
  // the hash, and #main would switch it to the first one.
  const skip = document.querySelector('.skip-link');
  const main = document.getElementById('main');
  if (skip && main) {
    skip.addEventListener('click', (e) => {
      e.preventDefault();
      main.focus();
      main.scrollIntoView({ block: 'start' });
    });
  }

  // Back after logging out (here or in another tab) can bring a page made for the user who was logged
  // in back from the browser's back/forward cache. Such pages aren't kept (Cache-Control: no-store),
  // and should a browser keep one all the same, it is loaded again unless the same user is still in.
  window.addEventListener('pageshow', async (e) => {
    if (!e.persisted || !loggedIn) return;
    try {
      const res = await fetch('/api/v1/session/', { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
      const data = await res.json();
      if (String(data.user ? data.user.id : '') === (document.body.dataset.user || '')) return;
    } catch (err) {
      // can't tell: load it again
    }
    location.reload();
  });

  // ------------------------------------------------------------ navigation rail

  // Phones leave the rail's storages out of the bar at the bottom (app.css, up to 640px): in one of them
  // 首页 is the current destination there (data-phone-current), for screen readers too.
  const phoneBar = window.matchMedia('(max-width: 640px)');
  const markPhoneCurrent = () => document.querySelectorAll('.nav-link[data-phone-current]').forEach((link) => {
    if (phoneBar.matches) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  markPhoneCurrent();
  phoneBar.addEventListener('change', markPhoneCurrent);

  // ------------------------------------------------------------ top app bar

  // M3: the app bar takes the container color once the page scrolls under it.
  const topbar = document.getElementById('topbar');
  if (topbar) {
    const onScroll = () => topbar.classList.toggle('scrolled', window.scrollY > 0);
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  // ------------------------------------------------------------ 主题

  const THEME_ICONS = { system: 'contrast', light: 'light_mode', dark: 'dark_mode' };
  function showTheme() {
    const current = theme.get();
    document.querySelectorAll('[data-theme-choice]').forEach((item) => {
      item.setAttribute('aria-checked', String(item.dataset.themeChoice === current));
    });
    const button = document.getElementById('theme-button');
    if (button) button.querySelector('use').setAttribute('href', iconHref(THEME_ICONS[current]));
  }
  document.addEventListener('click', (e) => {
    const item = e.target.closest('[data-theme-choice]');
    if (item) theme.set(item.dataset.themeChoice);
  });
  document.addEventListener('theme:change', showTheme);
  showTheme();

  // ------------------------------------------------------------ 显示密码

  document.addEventListener('click', (e) => {
    const toggle = e.target.closest('[data-toggle-password]');
    if (!toggle) return;
    const input = toggle.parentElement.querySelector('input');
    const shown = input.type === 'password';
    input.type = shown ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(shown));
    toggle.setAttribute('aria-label', shown ? gettext('Hide password') : gettext('Show password'));
    toggle.dataset.tooltip = shown ? gettext('Hide password') : gettext('Show password');
    toggle.querySelector('use').setAttribute('href', iconHref(shown ? 'visibility_off' : 'visibility'));
    input.focus();
  });

  // ------------------------------------------------------------ 登录

  const loginSteps = {};
  const loginDialog = document.getElementById('login-dialog');
  const loginForm = document.getElementById('login-form'); // in the dialog, or on the login page
  let afterLogin = null;
  let startLogin = () => {};

  // Opens the login dialog (visitors only). After logging in the page goes to `next`, or reloads.
  function openLogin(next = null) {
    if (!loginDialog) return;
    afterLogin = next ? safeNext(next) : null;
    startLogin();
    openDialog(loginDialog);
  }

  if (loginForm) {
    const form = loginForm;
    const area = form.querySelector('.login-step');
    const button = form.querySelector('[type="submit"]');
    const entry = form.dataset.entry || '';
    const passwordPart = () => form.querySelectorAll('.login-password-part');
    let step = null; // { name, before, handle }: the step the form shows now

    const post = (body) => api('POST', '/api/v1/session/', entry ? { ...body, entry } : body);
    const setButton = (text) => { button.textContent = text; };

    const finish = () => {
      if (afterLogin) location.href = afterLogin;
      else if (loginDialog) location.reload();
      else location.href = '/'; // the login page
      return true; // keep the button disabled while the page changes
    };

    function clearLogin() {
      formError(form, '');
      area.replaceChildren();
      area.hidden = true;
      step = null;
      setButton(gettext('Log in'));
      passwordPart().forEach((part) => { part.hidden = false; });
    }

    // Shows the step `name`; `answer` is the API's, or null when the form asks before the API does.
    function showStep(name, answer, credentials) {
      const show = loginSteps[name];
      if (!show) throw new Error(interpolate(gettext("%(detail)s This page can't do that step yet (%(name)s)."), { detail: answer ? answer.detail : gettext('One more step is needed.'), name }, true));
      const before = answer ? answer.before_password : true;
      area.replaceChildren();
      area.hidden = false;
      if (!before) passwordPart().forEach((part) => { part.hidden = true; });
      const handle = show({
        answer,
        area,
        form,
        send: async (fields) => proceed(await post(before ? { ...credentials, ...fields } : { step: name, ...fields }), credentials),
      }) || {};
      step = { name, before, handle };
      setButton(handle.label || gettext('Log in'));
    }

    // Goes on as the API's answer says: logged in, or show the step it asks for.
    function proceed(answer, credentials) {
      if (answer.authenticated) return finish();
      if (step && step.name === answer.step) throw new Error(answer.detail); // shown already: its fields were missing
      showStep(answer.step, answer, credentials);
      return false;
    }

    // A login begun earlier that waits for a step after the password: that step, not the password again.
    async function resume() {
      let data;
      try {
        data = await api('GET', '/api/v1/session/');
      } catch (err) {
        return; // nothing to resume then
      }
      if (data.authenticated || !data.step || !loginSteps[data.step] || (step && step.name === data.step)) return;
      showStep(data.step, { authenticated: false, step: data.step, before_password: false, detail: '', resumed: true }, null);
      area.append(h('p', { class: 'login-restart' },
        h('button', {
          type: 'button',
          class: 'btn btn-text btn-small',
          text: gettext('Log in another way'),
          onclick: async () => {
            try { await api('DELETE', '/api/v1/session/'); } catch (err) { /* ended already */ }
            startLogin();
          },
        })));
    }

    // A form that is opened (again): the site wants a captcha with every password, so it shows at once.
    startLogin = () => {
      clearLogin();
      if (form.dataset.captcha === '1' && loginSteps.captcha) showStep('captcha', null, null);
      resume();
    };

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      formError(form, '');
      button.disabled = true;
      let leaving = false;
      try {
        if (step && !step.before) {
          if (step.handle.submit) leaving = await step.handle.submit();
        } else {
          const credentials = { username: form.elements.username.value.trim(), password: form.elements.password.value };
          if (!credentials.username) throw new Error(gettext('Enter your username.'));
          if (!credentials.password) throw new Error(gettext('Enter your password.'));
          const fields = step && step.handle.fields ? step.handle.fields() : {};
          leaving = proceed(await post({ ...credentials, ...fields }), credentials);
        }
      } catch (err) {
        if (err instanceof ApiError && err.data && err.data.step) startLogin(); // a login begun too long ago: again
        else if (err instanceof ApiError && step && step.handle.failed) step.handle.failed(err);
        formError(form, err.message);
      } finally {
        if (!leaving) button.disabled = false;
      }
    });

    if (loginDialog) {
      loginDialog.addEventListener('close', () => {
        form.reset();
        clearLogin();
      });
      document.querySelectorAll('[data-login]').forEach((opener) => {
        opener.addEventListener('click', () => openLogin());
      });
      // Links that need an account open the dialog instead, then go on to the link.
      document.addEventListener('click', (e) => {
        const link = e.target.closest('[data-require-login]');
        if (!link || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; // a new tab: it asks there
        e.preventDefault();
        openLogin(link.getAttribute('href'));
      });
    } else {
      // The login page: ?next= says where to go afterwards. Started once the steps' scripts are in.
      const next = new URLSearchParams(location.search).get('next');
      afterLogin = next ? safeNext(next) : null;
      document.addEventListener('DOMContentLoaded', () => {
        startLogin();
        // As in the dialog: the cursor in 用户名 (a step resumed above puts it in its own field).
        const first = form.querySelector('[data-autofocus]');
        if (first && document.activeElement === document.body) first.focus();
      });
    }
  }

  // ?auth=login&next=/path opens the dialog; pages that need a login redirect here (settings.LOGIN_URL).
  const params = new URLSearchParams(location.search);
  if (params.get('auth') === 'login') {
    const next = params.get('next');
    params.delete('auth');
    params.delete('next');
    const query = params.toString();
    history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + location.hash);
    // Once the login steps' scripts (after this one) are in: the captcha may show at once.
    if (!loggedIn) document.addEventListener('DOMContentLoaded', () => openLogin(next));
    else if (next) location.replace(safeNext(next));
  }
  // ?auth=password opens the password dialog. Pages that run the admin's custom JS have none, and send
  // 修改密码 here: the same page without that JS (apps/siteconf/pages.py).
  if (params.get('auth') === 'password') {
    params.delete('auth');
    const query = params.toString();
    history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + location.hash);
    const dialog = document.getElementById('password-dialog');
    if (dialog) openDialog(dialog);
  }

  // ------------------------------------------------------------ 退出

  const logout = document.getElementById('logout');
  if (logout) {
    logout.addEventListener('click', async () => {
      try { await api('DELETE', '/api/v1/session/'); } catch (e) { /* logged out already */ }
      flash(gettext('Logged out.'));
      location.href = '/';
    });
  }

  // ------------------------------------------------------------ 修改密码

  const passwordDialog = document.getElementById('password-dialog');
  if (passwordDialog) {
    const form = document.getElementById('password-form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = form.elements;
      const button = form.querySelector('[type="submit"]');
      formError(form, '');
      if (f.new_password.value !== f.new_password2.value) {
        formError(form, gettext("The two new passwords don't match."));
        f.new_password2.focus();
        return;
      }
      button.disabled = true;
      // An account without a password (made by single sign-on) has no old one: the dialog leaves it out.
      const first = !f.old_password;
      try {
        const body = { new_password: f.new_password.value };
        if (!first) body.old_password = f.old_password.value;
        await api('PUT', '/api/v1/users/me/password/', body);
        passwordDialog.close();
        if (first) {
          flash(gettext('Your password is set: you can now also log in with your username and password.'));
          location.reload(); // the menu and this dialog now ask for the old one
          return;
        }
        toast(gettext('Your password has been changed. Logins on other devices have ended; log in there again with the new password.'));
      } catch (err) {
        formError(form, err.message);
      } finally {
        button.disabled = false;
      }
    });
    passwordDialog.addEventListener('close', () => {
      form.reset();
      formError(form, '');
      form.querySelectorAll('input[type="text"][name$="password"], input[type="text"][name$="password2"]').forEach((input) => {
        input.type = 'password';
      });
    });
  }

  Object.assign(window.App, { openLogin, loginSteps, loginNext: () => afterLogin });
})();
