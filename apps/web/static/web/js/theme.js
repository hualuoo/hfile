/* Before the page is drawn: the theme chosen in the 主题 menu (跟随系统, 浅色 or 深色), kept in
 * localStorage, as <html data-theme="light|dark">; none means the system's (prefers-color-scheme,
 * m3.css). Loaded in <head> and not deferred, so a dark page never flashes light. App.theme in
 * common.js changes it. */
(function () {
  'use strict';
  var theme = null;
  try {
    theme = window.localStorage.getItem('hfile-theme');
  } catch (e) {
    // storage blocked (a private window, a sandbox): the system's theme
  }
  if (theme === 'light' || theme === 'dark') document.documentElement.setAttribute('data-theme', theme);
})();
