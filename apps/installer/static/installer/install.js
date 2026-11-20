/* The installer's two pages.
 *
 * The form, one step at a time: 安装口令, 数据库, 网站, 管理员 (the sections of install.html).
 * A step's 下一步 waits until its required fields (marked *) are filled in; then the program checks
 * the step (POST /install/check/, apps/installer/views.py CheckView), and only a step that is right
 * leads on. On the database's step that check is a test of the database, the same as 测试连接:
 * it connects, and changes nothing. 开始安装 sends the whole form, which the program checks
 * again. Also: only the chosen database's fields show, the site's address is the one the browser
 * uses, and the button waits while the tables are made (about half a minute).
 * Without this script all the steps show at once, and the form is sent as it is.
 *
 * The page after it: waits for the program to start again with its new settings, which is when the
 * site's API answers instead of the installer's 503. */
(function () {
  'use strict';

  const form = document.getElementById('install-form');
  if (form) setUpForm(form);
  const restart = document.getElementById('restart');
  if (restart && restart.dataset.wait) waitForRestart();

  function setUpForm(form) {
    // Without this script both engines' fields show; the other one's are sent too, and ignored.
    const showEngine = () => {
      const chosen = form.querySelector('input[name="db_engine"]:checked');
      form.querySelectorAll('.db-part').forEach((part) => {
        part.hidden = Boolean(chosen) && part.dataset.engine !== chosen.value;
      });
    };
    form.querySelectorAll('input[name="db_engine"]').forEach((input) => input.addEventListener('change', showEngine));
    showEngine();

    // 网站地址: the program could only guess it from the request, and behind a reverse proxy that ends
    // https it sees http (and maybe another host). The browser knows: unless typed in, use its own.
    const url = form.elements.url;
    if (url.value === url.dataset.guess && location.origin !== url.dataset.guess) url.value = location.origin;
    const prefix = form.querySelector('.path-prefix');
    const showPrefix = () => {
      prefix.textContent = `${(url.value.trim() || location.origin).replace(/\/+$/, '')}/`;
    };
    url.addEventListener('input', showPrefix);
    showPrefix();

    // 反向代理: https in the browser while the program got plain http means one ends https in front.
    const proxies = form.elements.proxies;
    if (form.dataset.scheme === 'http' && location.protocol === 'https:' && proxies.value === '0') proxies.value = '1';

    const steps = [...form.querySelectorAll('.install-section[data-step]')];
    const database = steps.find((step) => step.dataset.step === 'database');
    const parts = (step) => ({
      notice: step.querySelector('.step-notice'),
      status: step.querySelector('.step-status'),
      next: step.querySelector('[data-action="next"]'),
      test: step.querySelector('[data-action="test"]'),
    });
    let current = 0;
    let busy = false; // asking the program
    let sending = false; // the form is on its way
    let tested = null; // the database's fields as they were when the test last found them right
    let said = null; // and as they were for what the test says on the page now
    let blamed = null; // the field the test's problem is about, red

    // The steps above the form: the ones done can be opened again; the next ones only by 下一步.
    const bar = document.createElement('ol');
    bar.className = 'install-steps';
    bar.setAttribute('aria-label', gettext('Install steps'));
    steps.forEach((step, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.innerHTML = '<span class="step-number"></span><span class="step-title"></span>';
      button.firstChild.textContent = index + 1;
      button.lastChild.textContent = step.dataset.title;
      button.addEventListener('click', () => {
        if (index < current && !busy && !sending) show(index);
      });
      const item = document.createElement('li');
      item.append(button);
      bar.append(item);
    });
    form.querySelector('.install-legend').before(bar);
    form.querySelectorAll('.step-actions, [data-action="back"]').forEach((element) => {
      element.hidden = false;
    });

    function show(index, focus = true) {
      current = index;
      steps.forEach((step, i) => {
        step.hidden = i !== index;
      });
      bar.querySelectorAll('button').forEach((button, i) => {
        button.disabled = i > index;
        button.classList.toggle('done', i < index);
        if (i === index) button.setAttribute('aria-current', 'step');
        else button.removeAttribute('aria-current');
      });
      refresh();
      if (focus) {
        window.scrollTo({ top: 0 });
        steps[index].querySelector('h2').focus({ preventScroll: true });
      }
    }

    // A field's name as its label says it, without the *.
    const nameOf = (input) => {
      const label = input.closest('.field')?.querySelector('label');
      if (!label) return input.name;
      return [...label.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE).map((node) => node.textContent).join('').trim();
    };

    // The required fields of a step that are still empty; of the database's, the chosen engine's only.
    function missing(step) {
      const names = [];
      step.querySelectorAll('input[required], select[required], textarea[required]').forEach((input) => {
        if (input.closest('[hidden]')) return;
        const empty = input.type === 'radio' ? !form.querySelector(`input[name="${input.name}"]:checked`) : !input.value.trim();
        if (empty && !names.includes(nameOf(input))) names.push(nameOf(input));
      });
      return names;
    }

    function refresh() {
      if (busy || sending) return;
      const step = steps[current];
      const { status, next, test } = parts(step);
      const names = missing(step);
      next.disabled = names.length > 0;
      if (test) test.disabled = names.length > 0;
      status.textContent = names.length ? interpolate(gettext('Still to fill in: %(fields)s'), { fields: names.join(gettext(', ')) }, true) : '';
    }

    const databaseValues = () => {
      const engine = form.querySelector('input[name="db_engine"]:checked')?.value;
      const names = engine === 'sqlite' ? ['db_file'] : ['db_host', 'db_port', 'db_name', 'db_user', 'db_password'];
      return JSON.stringify([engine, ...names.map((name) => form.elements[name].value)]);
    };

    function say(step, kind, text) {
      const { notice } = parts(step);
      notice.className = `notice notice-${kind} step-notice`;
      notice.textContent = text;
      notice.hidden = !text;
    }

    function clearProblems(step) {
      step.querySelectorAll('.field-error').forEach((error) => error.remove());
      step.querySelectorAll('.field.invalid').forEach((field) => field.classList.remove('invalid'));
      say(step, 'error', '');
    }

    // The field's own place for what is wrong with it: red, with the messages under it. False when
    // it has none (the database's kind): the messages go into the step's notice then.
    function mark(name, messages) {
      const input = form.elements[name];
      const field = (input instanceof RadioNodeList ? input[0] : input)?.closest('.field');
      if (!field) return false;
      field.classList.add('invalid');
      const note = [...field.children].find((child) => child.matches('p.hint'));
      messages.forEach((message) => {
        const error = document.createElement('p');
        error.className = 'field-error';
        error.textContent = message;
        field.insertBefore(error, note || null);
      });
      return true;
    }

    async function ask(name) {
      const body = new FormData(form);
      body.set('step', name);
      let response;
      try {
        response = await fetch(form.dataset.check, { method: 'POST', body, credentials: 'same-origin', cache: 'no-store' });
      } catch (err) {
        return { ok: false, message: gettext("Can't reach the program: check its command-line window to see whether it has stopped, then try again.") };
      }
      if (response.status === 409) {
        location.reload(); // installed meanwhile, in another window: the page says so
        return { ok: false, message: '' };
      }
      try {
        return await response.json();
      } catch (err) {
        return { ok: false, message: interpolate(gettext("The program's answer couldn't be understood (HTTP %(status)s). Try again."), { status: response.status }, true) };
      }
    }

    // Asks the program whether the step is right, and shows what it said. Returns whether it is.
    async function check(step) {
      const { status, next, test } = parts(step);
      const values = step === database ? databaseValues() : null;
      busy = true;
      next.disabled = true;
      if (test) test.disabled = true;
      clearProblems(step);
      if (step === database) {
        say(step, 'info', gettext('Testing the connection…'));
        status.textContent = '';
      } else {
        status.textContent = gettext('Checking…');
      }
      const answer = await ask(step.dataset.step);
      busy = false;
      say(step, 'error', '');
      // A wrong token, found on any step: back to its own.
      const where = steps.find((other) => other.dataset.step === answer.step) || step;
      if (where !== step) show(steps.indexOf(where));
      const loose = [];
      Object.entries(answer.errors || {}).forEach(([name, messages]) => {
        if (!mark(name, messages)) loose.push(...messages);
      });
      if (step === database) {
        said = values;
        blamed = answer.field && mark(answer.field, []) ? answer.field : null; // the problem is in the notice
      }
      if (answer.ok && step === database) {
        tested = values;
        say(step, 'ok', answer.message);
      } else if (!answer.ok) {
        say(where, 'error', [answer.message, ...loose].filter(Boolean).join('\n'));
      }
      refresh();
      if (!answer.ok) {
        const first = where.querySelector('.field.invalid input, .field.invalid select');
        if (first) first.focus();
      }
      return Boolean(answer.ok);
    }

    // 下一步, and Enter in a field: on to the next step once this one is filled in and right.
    async function next() {
      if (busy || sending) return;
      const step = steps[current];
      if (missing(step).length) {
        const empty = [...step.querySelectorAll('input[required]')].find((input) => !input.closest('[hidden]') && !input.value.trim());
        if (empty) empty.focus();
        return;
      }
      if (!(step === database && tested === databaseValues()) && !(await check(step))) return;
      if (current < steps.length - 1) show(current + 1);
      else install();
    }

    function install() {
      sending = true;
      if (form.requestSubmit) form.requestSubmit();
      else {
        installing();
        form.submit();
      }
    }

    function installing() {
      const button = document.getElementById('install-submit');
      button.disabled = true;
      button.textContent = gettext('Installing…');
      form.querySelectorAll('[data-action="back"], .install-steps button').forEach((other) => {
        other.disabled = true;
      });
      parts(steps[current]).status.hidden = true;
      document.getElementById('install-wait').hidden = false;
    }

    form.addEventListener('submit', (event) => {
      if (sending) {
        installing();
        return;
      }
      event.preventDefault(); // 开始安装 (a submit button, for the page without this script)
      next();
    });
    form.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button || button.type === 'submit' || busy || sending) return;
      if (button.dataset.action === 'next') next();
      else if (button.dataset.action === 'back') show(current - 1);
      else if (button.dataset.action === 'test') check(database);
    });
    form.addEventListener('keydown', (event) => {
      // Not while an input method is still composing: Chinese is typed with Enter too.
      if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || ['checkbox', 'radio', 'button', 'submit'].includes(input.type)) return;
      event.preventDefault();
      next();
    });
    const edited = (event) => {
      const field = event.target.closest('.field.invalid');
      if (field) {
        field.classList.remove('invalid');
        field.querySelectorAll('.field-error').forEach((error) => error.remove());
      }
      // What the test says is about the database's fields as they were then.
      if (database.contains(event.target) && !busy && databaseValues() !== said) {
        say(database, 'error', '');
        if (blamed) form.elements[blamed].closest('.field').classList.remove('invalid');
        blamed = null;
      }
      refresh();
    };
    form.addEventListener('input', edited);
    form.addEventListener('change', edited);

    // The form came back with what is wrong: the step with the first problem, else the last one.
    let start = 0;
    if (form.hasAttribute('data-sent')) {
      const problem = form.querySelector('.field-error, .field.invalid');
      const index = problem ? steps.findIndex((step) => step.contains(problem)) : -1;
      start = index >= 0 ? index : steps.length - 1;
    }
    show(start, false);
  }

  function waitForRestart() {
    const state = document.getElementById('restart-state');
    const open = document.getElementById('open-site');
    open.setAttribute('aria-disabled', 'true');
    const started = Date.now();

    async function poll() {
      try {
        const response = await fetch('/api/v1/site/', { cache: 'no-store', credentials: 'same-origin' });
        if (response.ok) {
          state.textContent = gettext('The program has started with the new settings. Open the site and log in with the admin account you just created.');
          open.removeAttribute('aria-disabled');
          open.focus();
          return;
        }
      } catch (err) {
        // restarting: nothing is listening for a moment
      }
      if (Date.now() - started > 60000) {
        state.textContent = gettext("The program still hasn't started after a minute. Check the output in its command-line window, or stop the program and run it again.");
        open.removeAttribute('aria-disabled');
        return;
      }
      setTimeout(poll, 1500);
    }
    setTimeout(poll, 2000);
  }
})();
