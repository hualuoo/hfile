/* What the login's steps show (apps/accounts/login_steps.py; the protocol is in shell.js): the image captcha
 * before the password, the code of two-step verification after it. */
(function () {
  'use strict';
  const { h, loginSteps, captcha, fieldLabels } = window.App;

  Object.assign(fieldLabels, { captcha: gettext('Captcha'), captcha_key: gettext('Captcha'), code: gettext('Two-step verification code') });

  // A captcha with every password: shown when the form opens (the page knows the site wants one), or
  // when the API asks for it.
  loginSteps.captcha = ({ area }) => {
    const box = h('div');
    area.append(box);
    const widget = captcha(box, 'login');
    widget.load();
    return { fields: () => widget.fields(), failed: (err) => widget.failed(err) };
  };

  // The password was right (or single sign-on, answer.resumed); the account has two-step verification:
  // the code from the phone.
  loginSteps.totp = ({ answer, area, send }) => {
    const input = h('input', {
      id: 'login-totp', name: 'code', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '8',
      spellcheck: 'false', 'aria-describedby': 'login-totp-hint',
    });
    area.append(
      h('p', {
        class: 'muted login-step-text', id: 'login-totp-hint',
        text: answer && answer.resumed ? gettext('One more step: open the authenticator app on your phone and enter the 6 digits it shows for this site now.')
          : gettext('The password is right. Open the authenticator app on your phone and enter the 6 digits it shows for this site now.'),
      }),
      h('div', { class: 'field' }, h('label', { for: 'login-totp', text: gettext('Two-step verification code') }), input));
    input.focus();
    return {
      label: gettext('Verify'),
      submit: async () => {
        const code = input.value.normalize('NFKC').replace(/\s+/g, '');
        if (!/^[0-9]{6}$/.test(code)) {
          input.focus();
          throw new Error(gettext('Please enter the 6 digits.'));
        }
        return send({ code });
      },
      failed: () => {
        input.value = '';
        input.focus();
      },
    };
  };
})();
