/* /storages/oauth/callback/: in the window the storage form opened for 授权, tell the form (same origin
 * only) that its draft has new tokens, and close. The form is this window's opener only while nothing cut
 * the tie: the admin pages send Cross-Origin-Opener-Policy: same-origin, so after the provider's pages
 * this window has no opener any more. Then the message goes over a BroadcastChannel, and this window
 * closes when the form that started this 授权 says it heard it. Opened by itself (a blocked popup, a
 * reload), no form answers: it is a page with a link back to the form. */
(function () {
  'use strict';
  const box = document.getElementById('oauth-result');
  if (!box) return;
  const ok = box.dataset.ok === '1';
  const message = { type: 'hfile:storage-oauth', ok, draft: box.dataset.draft, asked: box.dataset.asked };
  const closeSoon = () => {
    if (!ok) return; // an error stays, to be read
    const note = document.getElementById('oauth-closing');
    if (note) note.hidden = false;
    setTimeout(() => {
      window.close();
      // Still here: the browser didn't let it close (a window it no longer counts as opened by a script).
      setTimeout(() => { if (note) note.textContent = gettext('The authorization result went to the page that started it; you can close this window.'); }, 500);
    }, 1500);
  };
  if (window.opener && !window.opener.closed) {
    try {
      window.opener.postMessage(message, window.location.origin);
    } catch (err) {
      return; // the opener is another site: nothing to tell
    }
    closeSoon();
    return;
  }
  // Without a draft (an unknown flow) no form could tell it is its own.
  if (!message.asked || typeof BroadcastChannel !== 'function') return;
  const channel = new BroadcastChannel('hfile:storage-oauth');
  channel.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'hfile:storage-oauth-heard' && e.data.asked === message.asked) {
      channel.close();
      closeSoon();
    }
  });
  channel.postMessage(message);
})();
