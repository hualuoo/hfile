/* /manage/: switches between the sections of the menu (apps/web/navigation.py SECTIONS): each section is a
 * <section data-section="key"> and its link <a class="manage-link" data-section="key" href="#key">.
 * The address's #key says which one shows (the first one without one); a section may have pages of its
 * own after a slash or a question mark (#storages/12, #storages/new?draft=…), which its script reads from
 * location.hash. Each section's own script loads its data when it is first shown: it listens for the
 * "manage:show" event, whose detail is the key; the event comes again whenever the address changes. */
(function () {
  'use strict';
  const { setTitle } = window.App;

  const sections = [...document.querySelectorAll('section[data-section]')];
  const links = [...document.querySelectorAll('.manage-link[data-section]')];
  if (!sections.length) return;

  function current() {
    let hash = location.hash.slice(1);
    try {
      hash = decodeURIComponent(hash);
    } catch (err) {
      // a mangled address (a lone %): read it as it is
    }
    const key = hash.split(/[/?]/, 1)[0];
    return sections.some((section) => section.dataset.section === key) ? key : sections[0].dataset.section;
  }

  function show() {
    const key = current();
    for (const section of sections) section.hidden = section.dataset.section !== key;
    for (const link of links) {
      const on = link.dataset.section === key;
      if (on) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
      // On narrow screens the menu is one row that scrolls sideways: bring the current one into it.
      const menu = link.parentElement;
      if (on && menu.scrollWidth > menu.clientWidth) {
        menu.scrollLeft = link.offsetLeft - menu.offsetLeft - (menu.clientWidth - link.offsetWidth) / 2;
      }
    }
    const link = links.find((item) => item.dataset.section === key);
    setTitle(link ? interpolate(gettext('%(section)s · Manage'), { section: link.textContent.trim() }, true) : gettext('Manage'));
    document.dispatchEvent(new CustomEvent('manage:show', { detail: key }));
  }

  // Another section, or another page of one (the storage form, its list): from the top, as a new
  // page would be, not at the scroll of the long form left behind.
  window.addEventListener('hashchange', () => {
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    show();
  });
  // After the sections' scripts (they come after this one in the page) have set up their listeners.
  document.addEventListener('DOMContentLoaded', show);
})();
