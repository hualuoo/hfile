/* /manage/ 站点设置, 显示设置, 直链和短链, 代理下载, 安全设置, 访问控制 (apps/web/navigation.py SECTIONS): one
 * script for the six forms. Each <form class="settings-form" data-settings="<group>" data-section-key="<key>">
 * is filled from GET /api/v1/manage/settings/<group>/ the first time its section is shown, and 保存
 * PATCHes the fields changed since (a tab left open doesn't undo what another admin saved meanwhile).
 * The server checks them (apps/siteconf/serializers.py) and answers the problems by field: each shows
 * under its field, and the form opens the tab of the first one.
 *
 * Fields (apps/siteconf/templates/siteconf/manage/_field.html) are read and written by their name; a few
 * kinds have their own parts: the logo and the icon (upload, URL, back to the default), the write-only
 * OnlyOffice secret, the short-link lifetimes, the suffix lists' 恢复默认, and on 访问控制 the live
 * test of the blocklists and 屏蔽迅雷下载. Fields that only superusers may change come disabled. */
(function () {
  'use strict';
  const { api, h, icon, toast, tabs, formError, updateCounters, confirmDialog, errorText } = window.App;

  const UNITS = [['s', gettext('seconds')], ['m', gettext('minutes')], ['h', gettext('hours')], ['d', gettext('days')], ['w', gettext('weeks')], ['M', gettext('months')], ['y', gettext('years')], ['forever', gettext('Forever')]];
  const forms = [...document.querySelectorAll('form.settings-form[data-settings]')];
  if (!forms.length) return;

  // The page asks before it is left (or reloaded) with changes not saved.
  const unsaved = new Set();
  window.addEventListener('beforeunload', (e) => {
    if (unsaved.size) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  const controls = (form) => [...form.querySelectorAll('input[name], select[name], textarea[name]')];
  const control = (form, name) => form.querySelector(`[name="${CSS.escape(name)}"]`);

  // A field's problems as text: DRF gives a list of strings, or for a list of items, the items' own.
  function problemText(value) {
    if (Array.isArray(value) && value.every((item) => typeof item === 'string')) return value.join('\n');
    if (Array.isArray(value)) {
      return value.map((item, i) => (item && typeof item === 'object' && Object.keys(item).length
        ? interpolate(gettext('Item %(n)s: %(errors)s'), { n: i + 1, errors: errorText(item, 400, pgettext('between clauses', ', ')) }, true) : '')).filter(Boolean).join('\n');
    }
    if (value && typeof value === 'object') return errorText(value, 400, '\n');
    return String(value);
  }

  function setup(form) {
    const group = form.dataset.settings;
    const url = `/api/v1/manage/settings/${group}/`;
    const save = form.querySelector('.settings-actions [type="submit"]');
    const dirtyNote = form.querySelector('[data-dirty]');
    const tablist = form.querySelector('[role="tablist"]');
    const tabControl = tablist ? tabs(tablist) : null;
    let loaded = false;
    let loading = null;
    let initial = {}; // what collect() read just after the form was filled: 保存 sends what differs

    function setDirty(on) {
      if (on) unsaved.add(form);
      else unsaved.delete(form);
      if (dirtyNote) dirtyNote.hidden = !on;
    }

    // ------------------------------------------------------------ filling and reading

    function fill(data) {
      for (const el of controls(form)) {
        if (el.closest('[data-image]') || el.closest('[data-secret]') || !(el.name in data)) continue;
        const value = data[el.name];
        if (el.type === 'checkbox') el.checked = Boolean(value);
        else el.value = value === null || value === undefined ? '' : String(value);
      }
      form.querySelectorAll('[data-image]').forEach((box) => fillImage(box, data));
      form.querySelectorAll('[data-secret]').forEach((box) => fillSecret(box, data));
      form.querySelectorAll('[data-expiries]').forEach((box) => fillExpiries(box, data[box.dataset.expiries] || []));
      fillYou(data);
      clearProblems();
      formError(form, '');
      updateCounters(form);
      updateDependents();
      initial = collect();
      setDirty(false);
    }

    function changes() {
      const body = collect();
      return Object.fromEntries(Object.entries(body).filter(([name, value]) => JSON.stringify(value) !== JSON.stringify(initial[name])));
    }

    // What 保存 sends: every field but those an admin can't change, and those a choice hides (the
    // referer rules while the check is off, say): they keep what was saved.
    function collect() {
      const body = {};
      for (const el of controls(form)) {
        if (el.hasAttribute('data-locked') || el.type === 'file' || el.closest('[data-show-when][hidden]')) continue;
        const image = el.closest('[data-image]');
        const secret = el.closest('[data-secret]');
        if (image) body[el.name] = el.value.trim() || image.dataset.upload || '';
        else if (secret) {
          if (secret.dataset.clear === '1') body[el.name] = '';
          else if (el.value) body[el.name] = el.value;
        } else body[el.name] = el.type === 'checkbox' ? el.checked : el.value;
      }
      form.querySelectorAll('[data-expiries]').forEach((box) => { body[box.dataset.expiries] = readExpiries(box); });
      return body;
    }

    // ------------------------------------------------------------ problems by field

    function clearProblems() {
      form.querySelectorAll('[data-field].invalid').forEach((box) => box.classList.remove('invalid'));
      form.querySelectorAll('.field-error[data-problem]').forEach((p) => p.remove());
    }

    function showProblems(data) {
      clearProblems();
      const elsewhere = [];
      let firstBox = null;
      for (const [name, value] of Object.entries(data)) {
        const text = problemText(value);
        const box = form.querySelector(`[data-field="${CSS.escape(name)}"]`);
        if (!box) {
          if (name !== 'code') elsewhere.push(text);
          continue;
        }
        box.classList.add('invalid');
        const at = box.querySelector(':scope > .hint') || null;
        const problem = h('p', { class: 'field-error', 'data-problem': '', text });
        if (box.matches('.switch-line')) box.after(problem);
        else box.insertBefore(problem, at);
        if (!firstBox) firstBox = box;
      }
      const count = Object.keys(data).filter((name) => name !== 'code').length;
      formError(form, [interpolate(ngettext("%(count)s item needs changing; see what's marked in red.", "%(count)s items need changing; see what's marked in red.", count), { count }, true), ...elsewhere].join('\n'));
      if (firstBox) {
        const panel = firstBox.closest('[role="tabpanel"]');
        if (panel && tabControl) {
          const tab = tablist.querySelector(`[aria-controls="${CSS.escape(panel.id)}"]`);
          if (tab) tabControl.select(tab.dataset.tab);
        }
        const input = firstBox.querySelector('input:not([type="file"]):not([disabled]), select, textarea:not([disabled])');
        if (input) input.focus({ preventScroll: true });
        firstBox.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }

    // ------------------------------------------------------------ fields that show, hide or explain others

    function updateDependents() {
      form.querySelectorAll('[data-show-when]').forEach((part) => {
        const source = control(form, part.dataset.showWhen);
        if (!source) return;
        const value = source.value;
        part.hidden = part.dataset.showIs !== undefined ? value !== part.dataset.showIs : value === part.dataset.showNot;
      });
      const referer = control(form, 'referer_type');
      if (referer) {
        const label = form.querySelector('[data-field="referer_rules"] label');
        if (label) label.textContent = referer.value === 'allowlist' ? gettext('Allowlist') : gettext('Blocklist');
      }
      const prefix = control(form, 'direct_link_prefix');
      const prefixExample = form.querySelector('[data-prefix-example]');
      if (prefix && prefixExample) {
        prefixExample.textContent = interpolate(gettext('Direct link address: %(origin)s/%(prefix)s/storage-alias/folder/file-name'), { origin: location.origin, prefix: prefix.value.trim() || gettext('prefix') }, true);
      }
      const seconds = control(form, 'link_limit_seconds');
      const downloads = control(form, 'link_limit_downloads');
      const summary = form.querySelector('[data-limit-summary]');
      if (seconds && downloads && summary) {
        const s = Number(seconds.value);
        const n = Number(downloads.value);
        summary.textContent = s > 0 && n > 0 ? interpolate(ngettext('Now: each IP can download at most %(downloads)s time in %(seconds)s s.', 'Now: each IP can download at most %(downloads)s times in %(seconds)s s.', n), { seconds: s, downloads: n }, true) : gettext("Downloads aren't rate-limited now.");
      }
      const entry = control(form, 'secure_login_entry');
      const showLogin = control(form, 'show_login');
      const entryExample = form.querySelector('[data-entry-example]');
      if (entry && entryExample) {
        const value = entry.value.trim().replace(/^\/+|\/+$/g, '');
        entryExample.textContent = value
          ? interpolate(gettext('Login address: %(origin)s/login/%(entry)s/ (logging in only works here)'), { origin: location.origin, entry: value }, true)
          : gettext('No secure login entrance: "Log in" on the pages logs in.');
        if (showLogin) {
          showLogin.disabled = Boolean(value);
          showLogin.closest('.switch-line').classList.toggle('disabled', Boolean(value));
        }
      }
    }

    // ------------------------------------------------------------ the logo and the icon

    function fillImage(box, data) {
      const name = box.dataset.image;
      const value = data[name] || '';
      const upload = value.startsWith('site/');
      box.dataset.upload = upload ? value : '';
      control(form, name).value = upload ? '' : value;
      box.querySelector('[data-image-preview]').src = data[`${name}_preview`] || '';
      box.querySelector('[data-image-state]').textContent = upload
        ? gettext('Using the uploaded picture now.') : value ? gettext('Using the picture at the address below now.') : gettext("Using HFile's own icon now.");
      box.querySelector('[data-image-reset]').disabled = !value;
    }

    form.querySelectorAll('[data-image]').forEach((box) => {
      const name = box.dataset.image;
      const file = box.querySelector('[data-image-file]');
      const imageUrl = `/api/v1/manage/settings/site/${name}/`;
      // A new picture or the default replaces the setting at once; the rest of the form stays as typed.
      const replaced = (data) => {
        fillImage(box, data);
        initial[name] = data[name] || ''; // saved already: not a change of this form
        box.classList.remove('invalid');
        box.querySelectorAll('.field-error[data-problem]').forEach((p) => p.remove());
      };
      box.querySelector('[data-image-upload]').addEventListener('click', () => file.click());
      file.addEventListener('change', async () => {
        const chosen = file.files[0];
        file.value = '';
        if (!chosen) return;
        const body = new FormData();
        body.append('file', chosen);
        try {
          replaced(await api('PUT', imageUrl, body));
          toast(gettext('The picture has been changed.'));
        } catch (err) {
          showProblems(err.data && err.data.file ? { [name]: err.data.file } : { [name]: [err.message] });
        }
      });
      box.querySelector('[data-image-reset]').addEventListener('click', async () => {
        if (!await confirmDialog({ title: gettext('Restore the default?'), message: gettext("Goes back to HFile's own icon; the uploaded picture is deleted."), confirmText: gettext('Restore the default') })) return;
        try {
          replaced(await api('DELETE', imageUrl));
          toast(gettext('The default is restored.'));
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    });

    // ------------------------------------------------------------ write-only secrets

    function fillSecret(box, data) {
      const name = box.dataset.secret;
      const set = Boolean(data[`${name}_set`]);
      box.dataset.clear = '';
      box.dataset.set = set ? '1' : '';
      box.querySelector('input').value = '';
      box.querySelector('[data-secret-state]').textContent = set ? gettext('Set') : gettext('Not set');
      const clear = box.querySelector('[data-secret-clear]');
      if (clear) clear.hidden = !set;
    }

    // A locked secret (only superusers change it, D49) has no 清除 and is never sent (collect()).
    form.querySelectorAll('[data-secret]').forEach((box) => {
      const clear = box.querySelector('[data-secret-clear]');
      if (!clear) return;
      clear.addEventListener('click', () => {
        box.dataset.clear = '1';
        box.querySelector('input').value = '';
        box.querySelector('[data-secret-state]').textContent = gettext('Cleared when saved');
        clear.hidden = true;
        setDirty(true);
      });
      box.querySelector('input').addEventListener('input', () => {
        if (box.dataset.clear === '1') {
          box.dataset.clear = '';
          box.querySelector('[data-secret-state]').textContent = box.dataset.set ? gettext('Set') : gettext('Not set');
        }
      });
    });

    // ------------------------------------------------------------ short-link lifetimes

    function expiryRow(item) {
      const value = h('input', { type: 'number', min: '1', step: '1', inputmode: 'numeric', 'aria-label': gettext('Length'), value: String(item.value || 1) });
      const unit = h('select', { 'aria-label': gettext('Unit') },
        UNITS.map(([key, label]) => h('option', { value: key, text: label, selected: key === item.unit })));
      const sync = () => { value.disabled = unit.value === 'forever'; };
      unit.addEventListener('change', sync);
      sync();
      const row = h('li', { class: 'expiry-row' }, value, unit);
      const move = (step) => {
        const sibling = step < 0 ? row.previousElementSibling : row.nextElementSibling;
        if (!sibling) return;
        if (step < 0) sibling.before(row);
        else sibling.after(row);
        ends(row.parentElement);
        row.querySelector(step < 0 ? '[data-up]' : '[data-down]').focus();
        setDirty(true);
      };
      row.append(
        h('button', { type: 'button', class: 'icon-button small', 'aria-label': gettext('Move up'), 'data-tooltip': gettext('Move up'), 'data-up': '', onclick: () => move(-1) }, icon('arrow_upward')),
        h('button', { type: 'button', class: 'icon-button small', 'aria-label': gettext('Move down'), 'data-tooltip': gettext('Move down'), 'data-down': '', onclick: () => move(1) }, icon('arrow_downward')),
        h('button', { type: 'button', class: 'icon-button small', 'aria-label': gettext('Delete this item'), 'data-tooltip': gettext('Delete'), onclick: () => {
          const list = row.parentElement;
          row.remove();
          ends(list);
          setDirty(true);
        } }, icon('delete')));
      return row;
    }

    // The first row can't go up, nor the last one down (a disabled button that keeps focus would trap it).
    function ends(list) {
      const rows = [...list.children];
      rows.forEach((row, i) => {
        const up = row.querySelector('[data-up]');
        const down = row.querySelector('[data-down]');
        if (i === 0 && document.activeElement === up) down.focus();
        if (i === rows.length - 1 && document.activeElement === down) up.focus();
        up.disabled = i === 0;
        down.disabled = i === rows.length - 1;
      });
    }

    function fillExpiries(box, items) {
      const list = box.querySelector('[data-expiry-list]');
      list.replaceChildren(...items.map(expiryRow));
      ends(list);
    }

    function readExpiries(box) {
      return [...box.querySelectorAll('.expiry-row')].map((row) => {
        const unit = row.querySelector('select').value;
        return { value: unit === 'forever' ? 1 : row.querySelector('input').value, unit };
      });
    }

    form.querySelectorAll('[data-expiries]').forEach((box) => {
      box.querySelector('[data-expiry-add]').addEventListener('click', () => {
        const row = expiryRow({ value: 7, unit: 'd' });
        const list = box.querySelector('[data-expiry-list]');
        list.append(row);
        ends(list);
        row.querySelector('input').focus();
        setDirty(true);
      });
    });

    // ------------------------------------------------------------ suffix lists: 恢复默认

    form.querySelectorAll('[data-reset]').forEach((button) => {
      button.addEventListener('click', () => {
        const target = control(form, button.dataset.reset);
        target.value = button.dataset.default;
        target.dispatchEvent(new Event('input', { bubbles: true }));
      });
    });

    // ------------------------------------------------------------ 访问控制: who you are, the tester, Thunder

    function fillYou(data) {
      const ip = form.querySelector('[data-your-ip]');
      if (ip && 'your_ip' in data) {
        ip.textContent = interpolate(gettext("The site sees your IP as %(ip)s; don't put it in the IP blocklist " +
          '(saving checks this too). If it isn\'t your IP, see "Diagnostics": ' +
          'most likely [server] proxies in the settings file is wrong.'), { ip: data.your_ip || gettext('(unreadable)') }, true);
      }
      const ua = form.querySelector('[data-your-ua]');
      if (ua && 'your_user_agent' in data) {
        ua.textContent = interpolate(gettext("The browser you're using (User-Agent): %(agent)s"), { agent: data.your_user_agent || gettext('(none)') }, true);
      }
    }

    form.querySelectorAll('[data-rule-test]').forEach((box) => {
      const kind = box.dataset.ruleTest; // ip or ua
      const input = box.querySelector('input');
      const result = box.querySelector('[data-rule-result]');
      const listName = kind === 'ip' ? 'ip_blocklist' : 'ua_blocklist';
      let timer = null;
      let asked = 0;
      async function test() {
        const value = kind === 'ip' ? input.value.trim() : input.value;
        if (!value) {
          result.textContent = '';
          result.className = 'rule-result';
          return;
        }
        const mine = ++asked;
        const body = { [listName]: control(form, listName).value, [kind === 'ip' ? 'ip' : 'user_agent']: value };
        try {
          const answer = await api('POST', '/api/v1/manage/settings/access/test/', body);
          if (mine !== asked) return;
          const rule = (kind === 'ip' ? answer.ip : answer.user_agent).rule;
          result.textContent = rule ? interpolate(gettext('Blocked: matches "%(rule)s".'), { rule }, true) : gettext('Not blocked: no rule matches.');
          result.className = `rule-result ${rule ? 'blocked' : 'passed'}`;
        } catch (err) {
          if (mine !== asked) return;
          const data = err.data || {};
          result.textContent = data[listName] ? interpolate(gettext('The list has problems: %(problems)s'), { problems: problemText(data[listName]) }, true) : (data.ip ? problemText(data.ip) : err.message);
          result.className = 'rule-result blocked';
        }
      }
      const later = () => {
        clearTimeout(timer);
        timer = setTimeout(test, 300);
      };
      input.addEventListener('input', later);
      control(form, listName).addEventListener('input', () => { if (input.value) later(); });
    });

    const thunder = form.querySelector('[data-thunder]');
    if (thunder) {
      thunder.addEventListener('click', () => {
        const list = control(form, 'ua_blocklist');
        const lines = list.value.split('\n').map((line) => line.trim()).filter(Boolean);
        const added = thunder.dataset.rules.split('\n').filter((rule) => !lines.includes(rule));
        if (!added.length) {
          toast(gettext("The list already has Thunder's rules."));
          return;
        }
        list.value = [...lines, ...added].join('\n');
        list.dispatchEvent(new Event('input', { bubbles: true }));
        toast(interpolate(ngettext('Added %(count)s Thunder rule; it applies once saved.', 'Added %(count)s Thunder rules; they apply once saved.', added.length), { count: added.length }, true));
      });
    }

    // ------------------------------------------------------------ loading and saving

    async function load() {
      save.disabled = true;
      // Not to be changed before the values come: fill() would put them back over what was changed.
      form.inert = true;
      try {
        fill(await api('GET', url));
        loaded = true;
      } catch (err) {
        formError(form, interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true));
      } finally {
        form.inert = false;
        save.disabled = !loaded;
      }
    }

    form.addEventListener('input', (e) => {
      if (!loaded || e.target.closest('[data-rule-test]') || e.target.type === 'file') return;
      setDirty(true);
      updateDependents();
    });
    form.addEventListener('change', (e) => {
      if (!loaded || e.target.closest('[data-rule-test]') || e.target.type === 'file') return;
      setDirty(true);
      updateDependents();
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!loaded || save.disabled) return;
      const body = changes();
      if (!Object.keys(body).length) {
        setDirty(false);
        toast(gettext('Nothing changed.'));
        return;
      }
      save.disabled = true;
      formError(form, '');
      try {
        fill(await api('PATCH', url, body));
        toast(gettext('Saved.'));
      } catch (err) {
        if (err.status === 400 && err.data && typeof err.data === 'object' && !err.data.detail) showProblems(err.data);
        else formError(form, err.message);
      } finally {
        save.disabled = false;
      }
    });

    document.addEventListener('manage:show', (e) => {
      if (e.detail !== form.dataset.sectionKey || loaded || loading) return;
      loading = load().finally(() => { loading = null; });
    });
  }

  forms.forEach(setup);
})();
