/* The choice of language ({% language_menu %}, apps/web/templatetags/ui.py): kept in the cookie that
 * LocaleMiddleware reads (settings.LANGUAGE_COOKIE_NAME), then the page is loaded again in it: by GET
 * (the installer's page may be the answer to its form), or with reload() when the address has a #part,
 * which replace() would only scroll to. */
(function () {
  'use strict';

  document.addEventListener('click', (e) => {
    const item = e.target.closest('[data-language]');
    const holder = item && item.closest('[data-language-cookie]');
    if (!holder || item.disabled) return;
    const secure = location.protocol === 'https:' ? '; secure' : '';
    document.cookie = `${holder.dataset.languageCookie}=${encodeURIComponent(item.dataset.language)}; path=/; `
      + `max-age=${holder.dataset.maxAge}; samesite=lax${secure}`;
    if (location.hash) location.reload();
    else location.replace(location.href);
  });
})();
