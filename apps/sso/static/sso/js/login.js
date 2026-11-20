/* The single sign-on buttons under the login form (sso/_login_buttons.html, in the login dialog and on
 * the login page): each goes to /sso/<code>/login with ?next= where to come back to. That is the login
 * form's own next (App.loginNext(): ?next= of the login page, or the link that opened the dialog), else
 * the page the dialog was opened on; the login page itself goes to the home page. */
(function () {
  'use strict';

  function target(link) {
    const App = window.App || {};
    let next = typeof App.loginNext === 'function' ? App.loginNext() : null;
    if (!next) next = document.body.dataset.page === 'login' ? '/' : location.pathname + location.search + location.hash;
    const url = new URL(link.getAttribute('href'), location.origin);
    url.search = '';
    if (next && next !== '/') url.searchParams.set('next', next);
    return url.pathname + url.search;
  }

  // Set just before the browser follows the link: a click, a middle click, Enter.
  for (const type of ['click', 'auxclick']) {
    document.addEventListener(type, (e) => {
      const link = e.target.closest && e.target.closest('a[data-sso-login]');
      if (link) link.setAttribute('href', target(link));
    });
  }
})();
