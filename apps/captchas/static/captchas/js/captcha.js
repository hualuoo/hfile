/* Image captchas in forms (server side: apps/captchas/service.py), as rolehomes has them.
 *
 *   const captcha = App.captcha(box, 'login');   fills the empty element `box`
 *   captcha.load()        asks POST /api/v1/captcha/ {"scene"} for a picture; hides the box when the
 *                         form needs none
 *   captcha.fields()      what to send with the form: {captcha_key, captcha}, or {} without a picture.
 *                         Throws an Error to show when nothing is typed or the picture has expired
 *                         (a new one is loaded then)
 *   captcha.failed(err)   after a request that failed: the picture is used up, so a new one comes
 *
 * Each picture is good for one try; answers are compared without case. */
(function () {
  'use strict';
  const { api, h, icon } = window.App;

  const EARLY_MS = 20 * 1000; // a picture counts as expired a little before the server says so
  const HINT = gettext("Not case-sensitive. Can't read it? Click the picture for another one.");
  let seq = 0;

  function captcha(box, scene) {
    const id = `captcha-${++seq}`;
    const input = h('input', {
      id, name: 'captcha', autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false', maxlength: '4',
      'aria-describedby': `${id}-hint`,
    });
    const img = h('img', { alt: gettext('Captcha picture'), width: '160', height: '60' });
    const picture = h('button', { type: 'button', class: 'captcha-picture', 'aria-label': gettext('Show another captcha picture'), 'data-tooltip': gettext("Can't read it? Get another one") }, img);
    const again = h('button', { type: 'button', class: 'icon-button field-button', 'aria-label': gettext('Show another captcha picture'), 'data-tooltip': gettext('Another one') }, icon('refresh'));
    const hint = h('span', { class: 'hint label-hint', id: `${id}-hint`, text: HINT });
    box.classList.add('captcha-box');
    box.replaceChildren(
      h('div', { class: 'field' },
        h('div', { class: 'label-row' }, h('label', { for: id, text: gettext('Captcha') }), hint),
        h('span', { class: 'input-box' }, input, again)),
      picture);

    let shown = null; // { key, expires } while a picture is on screen
    let generation = 0; // a late answer to an older request is dropped

    function setHint(text, error = false) {
      hint.textContent = text;
      hint.classList.toggle('captcha-error', error);
    }

    const expired = () => shown && Date.now() > shown.expires;

    async function load({ focus = false } = {}) {
      const mine = ++generation;
      box.classList.add('loading');
      try {
        const data = await api('POST', '/api/v1/captcha/', { scene });
        if (mine !== generation) return;
        shown = data.required ? { key: data.key, expires: Date.now() + data.expires_in * 1000 - EARLY_MS } : null;
        box.hidden = !data.required;
        if (!data.required) return;
        img.src = data.image;
        img.width = data.width;
        img.height = data.height;
        input.value = '';
        input.maxLength = data.length;
        setHint(HINT);
        if (focus) input.focus();
      } catch (err) {
        if (mine !== generation) return;
        shown = null;
        box.hidden = false;
        setHint(interpolate(gettext("The captcha didn't load: %(message)s Click the refresh button to try again."), { message: err.message }, true), true);
      } finally {
        if (mine === generation) box.classList.remove('loading');
      }
    }

    picture.addEventListener('click', () => load({ focus: true }));
    again.addEventListener('click', () => load({ focus: true }));
    input.addEventListener('focus', () => { if (expired()) load({ focus: true }); });
    input.addEventListener('input', () => { if (hint.classList.contains('captcha-error')) setHint(HINT); });

    function fields() {
      if (box.hidden && !shown) return {};
      if (!shown) {
        load({ focus: true });
        throw new Error(gettext("The captcha hasn't loaded yet. Please wait a moment and try again."));
      }
      if (expired()) {
        load({ focus: true });
        throw new Error(gettext('The captcha expired and was replaced by a new one. Please enter it again.'));
      }
      const answer = input.value.trim();
      if (!answer) {
        input.focus();
        throw new Error(gettext('Please enter the captcha.'));
      }
      return { captcha_key: shown.key, captcha: answer };
    }

    // The server used the picture up (or never got the answer): a new one. Focus goes to it when the
    // captcha itself was what failed.
    function failed(err) {
      const aboutCaptcha = Boolean(err && err.data && err.data.captcha);
      const typed = Boolean(input.value.trim());
      shown = null;
      load({ focus: aboutCaptcha }).then(() => {
        if (!aboutCaptcha && typed && shown) setHint(gettext('The previous captcha has been used. Please enter this new one.'), true);
      });
    }

    return { load, fields, failed };
  }

  window.App.captcha = captcha;
})();
