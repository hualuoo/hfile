/* What the site's settings add to public pages (base.html with public_page; apps/siteconf/pages.py): the
 * announcement (显示设置 → 公告), which a visitor can close. Closing it is remembered in this browser
 * for this very text: a new announcement shows again. Storage can be refused (a private window):
 * then it shows on every page, which is what it did before anyway. */
(function () {
  'use strict';
  const KEY = 'hfile-announcement-closed';
  const box = document.getElementById('announcement');
  if (!box) return;
  let closed = null;
  try { closed = localStorage.getItem(KEY); } catch (e) { /* storage refused */ }
  if (closed === box.dataset.version) return;
  box.hidden = false;
  box.querySelector('.announcement-close').addEventListener('click', () => {
    box.hidden = true;
    try { localStorage.setItem(KEY, box.dataset.version); } catch (e) { /* not kept: it shows again next time */ }
  });
})();
