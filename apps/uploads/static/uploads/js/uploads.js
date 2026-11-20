/* The upload panel (contract U, uploads/readme_notes in the run folder): App.uploads.
 *
 *   App.uploads.pick(ctx, {folder})       opens the file chooser (or the folder chooser) and uploads
 *                                         what is chosen
 *   App.uploads.start(ctx, items)         uploads items: File objects (a folder upload's carry
 *                                         webkitRelativePath), {file, path: 'a/b/c.txt'} from dropped
 *                                         entries, {folder: 'a/empty'} for an empty folder
 *   App.uploads.drop(ctx, dataTransfer)   a drop on the page: files and folders, empty folders too
 *   App.uploads.onCount(callback)         callback(n) with the number uploading or queued, now and on
 *                                         every change (a badge); returns a function that stops it
 *   App.uploads.show(), hide()            the panel
 *
 *   ctx  the file browser's context (contract W4): storage {key, name, type}, path (the folder shown,
 *        relative to the user's root), permissions (codes: a list, or an object of booleans),
 *        capabilities, refresh(), toast(text).
 *
 * Every file gets an upload session (POST /api/v1/uploads/, apps/uploads/views.py). Through the site the
 * file goes in chunks of the session's chunk_size with Content-Range; straight to the storage it is one
 * PUT (S3), a form POST (又拍云) or PUTs of chunks (OneDrive), then POST complete. At most
 * max_file_uploads (站点设置) run at once. When files land, document gets hfile:files-changed
 * {storageKey, path} for their folder (W4). Names are only ever inserted as text. */
(function () {
  'use strict';
  const { h, icon, api, ApiError, fmtSize } = window.App;

  // ------------------------------------------------------------------ small helpers

  const can = (ctx, code) => {
    const perms = ctx && ctx.permissions;
    if (!perms) return false;
    if (Array.isArray(perms) || perms instanceof Set) return [...perms].includes(code);
    return Boolean(perms[code]);
  };
  const toast = (ctx, text, kind) => {
    if (ctx && typeof ctx.toast === 'function') ctx.toast(text, kind);
    else App.toast(text, kind);
  };
  const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
  const csrfToken = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const originOf = (url) => {
    try { return new URL(url, location.href).origin; } catch (e) { return String(url || ''); }
  };
  // 'a//b/' -> 'a/b'; '.' and '..' segments dropped (the server refuses them anyway). Only '/' separates:
  // browsers give relative paths with '/' on every system, and '\' is a plain character of a name on the
  // server (a folder 'a\b' made outside HFile is one folder, not two).
  const cleanRel = (path) => String(path || '').split(/\/+/).filter((part) => part && part !== '.' && part !== '..').join('/');
  const joinPath = (base, rel) => {
    const top = cleanRel(base);
    const inner = cleanRel(rel);
    return '/' + [top, inner].filter(Boolean).join('/');
  };

  // The icon of a row, from the name's extension.
  const KIND_ICONS = {
    image: 'image', video: 'movie', audio: 'audio_file', pdf: 'picture_as_pdf', archive: 'folder_zip',
    text: 'description', code: 'code', sheet: 'table_chart', slides: 'slideshow', doc: 'article', other: 'draft', folder: 'folder',
  };
  const EXTENSIONS = {
    image: 'jpg jpeg png gif bmp webp svg ico tif tiff heic avif',
    video: 'mp4 mkv webm mov avi flv wmv m4v ts m3u8 rmvb 3gp',
    audio: 'mp3 flac wav ogg m4a aac ape wma opus',
    pdf: 'pdf',
    archive: 'zip rar 7z tar gz tgz bz2 xz zst iso',
    text: 'txt md markdown log ini conf cfg csv tsv srt vtt nfo',
    code: 'js mjs ts json xml yml yaml html htm css py java c h cpp go rs sh bat ps1 sql php rb kt swift toml vue',
    sheet: 'xls xlsx ods et',
    slides: 'ppt pptx odp dps',
    doc: 'doc docx odt rtf wps epub',
  };
  const kindByExtension = {};
  for (const [kind, list] of Object.entries(EXTENSIONS)) for (const ext of list.split(' ')) kindByExtension[ext] = kind;
  const kindOf = (name) => {
    const dot = name.lastIndexOf('.');
    return (dot > 0 && kindByExtension[name.slice(dot + 1).toLowerCase()]) || 'other';
  };

  // A dialog with buttons of our own (App.confirmDialog's other button is always 取消): resolves to the
  // value of the button pressed, null when closed otherwise.
  let dialogSeq = 0;
  function askDialog({ title, message = '', extra = null, buttons }) {
    return new Promise((resolve) => {
      let chosen = null;
      const titleId = `upload-ask-${++dialogSeq}`;
      const foot = h('div', { class: 'dialog-foot' });
      const dialog = h('dialog', { class: 'dialog dialog-small', role: 'alertdialog', 'aria-labelledby': titleId },
        h('div', { class: 'dialog-box' },
          h('div', { class: 'dialog-body' },
            h('h2', { class: 'confirm-title', id: titleId, text: title }),
            message ? h('p', { class: 'confirm-text', text: message }) : null,
            extra,
            foot)));
      let focus = null;
      for (const button of buttons) {
        const el = h('button', { type: 'button', class: `btn ${button.kind || 'btn-text'}`, text: button.label });
        el.addEventListener('click', () => { chosen = button.value; dialog.close(); });
        foot.append(el);
        if (button.focus) focus = el;
      }
      dialog.addEventListener('close', () => { dialog.remove(); resolve(chosen); });
      document.body.append(dialog);
      App.openDialog(dialog);
      (focus || foot.lastElementChild).focus();
    });
  }

  // One dialog at a time: the questions of uploads running side by side wait for each other.
  let asking = Promise.resolve();
  function serial(fn) {
    const next = asking.then(fn, fn);
    asking = next.catch(() => {});
    return next;
  }

  // ------------------------------------------------------------------ requests

  class NetError extends Error {
    constructor(kind, url) {
      super(kind);
      this.kind = kind; // network | timeout | abort
      this.url = url;
    }
  }

  // XMLHttpRequest, for upload progress (fetch has none). Resolves with {status, data, text} for
  // every answer; rejects with NetError when there is none.
  function send(task, { method, url, body = null, headers = {}, credentials = true, onProgress = null }) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(method, url);
      xhr.withCredentials = credentials;
      for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
      if (onProgress) xhr.upload.addEventListener('progress', (e) => onProgress(e.loaded));
      xhr.addEventListener('load', () => {
        task.xhr = null;
        let data = null;
        if ((xhr.getResponseHeader('Content-Type') || '').includes('json')) {
          try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        }
        resolve({ status: xhr.status, data, text: xhr.responseText || '' });
      });
      xhr.addEventListener('error', () => { task.xhr = null; reject(new NetError('network', url)); });
      xhr.addEventListener('timeout', () => { task.xhr = null; reject(new NetError('timeout', url)); });
      xhr.addEventListener('abort', () => { task.xhr = null; reject(new NetError('abort', url)); });
      task.xhr = xhr;
      xhr.send(body);
    });
  }

  // The site's CSRF token changes when somebody logs in or out in another tab: ask for the current one.
  async function refreshCsrf() {
    const res = await fetch('/api/v1/session/', { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
    const data = await res.json();
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && data.csrf_token) meta.content = data.csrf_token;
  }

  // A request of our own API with the raw body (a chunk): {status, data}; CSRF refreshed once.
  async function siteSend(task, options) {
    for (let attempt = 0; ; attempt += 1) {
      const answer = await send(task, { ...options, headers: { ...options.headers, 'X-CSRFToken': csrfToken(), Accept: 'application/json' } });
      if (answer.status === 403 && answer.data && answer.data.code === 'csrf_failed' && attempt === 0) {
        await refreshCsrf();
        continue;
      }
      return answer;
    }
  }

  // Messages for a backend that didn't take a direct upload (inventory §6U.5).
  function backendMessage(answer, url) {
    let code = '';
    let message = '';
    const text = answer.text || '';
    const xmlCode = /<Code>([^<]{1,100})<\/Code>/.exec(text);
    const xmlMessage = /<Message>([^<]{1,300})<\/Message>/.exec(text);
    if (xmlCode) code = xmlCode[1];
    if (xmlMessage) message = xmlMessage[1];
    if (!code && answer.data) {
      const error = answer.data.error && typeof answer.data.error === 'object' ? answer.data.error : answer.data;
      code = String(error.code || '');
      message = String(error.message || error.msg || '');
    }
    return interpolate(gettext('Upload failed (HTTP %(status)s, target %(target)s, code %(code)s): %(message)s'), { status: answer.status, target: originOf(url), code: code || '-', message: message || gettext('the storage service refused the upload') }, true);
  }
  function netMessage(error, url, direct) {
    if (error.kind === 'timeout') return gettext("Upload failed: a network problem, or the target service isn't answering");
    if (direct) {
      return interpolate(gettext("Upload failed: the browser blocked the cross-origin request, or the target service can't be reached (%(target)s). Please check the storage service's CORS settings or the network."), { target: originOf(url) }, true) +
        gettext(' An administrator can click "Allow direct uploads from this site (set CORS)" in the storage source\'s settings, or turn on "Upload through the site".');
    }
    return gettext("Upload failed: a network problem, or the target service isn't answering");
  }

  // ------------------------------------------------------------------ the queue

  const STATE_ORDER = { failed: 0, uploading: 1, queued: 2, done: 3, skipped: 3 };
  const tasks = [];
  const counters = new Set();
  let limit = 5;
  let running = 0;
  let siteLoaded = null;

  function loadSite() {
    if (!siteLoaded) {
      siteLoaded = api('GET', '/api/v1/site/').then((site) => {
        if (site && Number(site.max_file_uploads) > 0) limit = Number(site.max_file_uploads);
        return site || {};
      }).catch(() => ({}));
    }
    return siteLoaded;
  }

  const active = () => tasks.filter((task) => task.state === 'uploading' || task.state === 'queued').length;
  function counted() {
    const n = active();
    for (const callback of counters) {
      try { callback(n); } catch (e) { /* a badge that broke shouldn't stop the uploads */ }
    }
    panel.update();
  }

  class Task {
    constructor(ctx, batch, file, folder) {
      this.ctx = ctx;
      this.batch = batch;
      this.file = file;
      this.name = file.name;
      this.size = file.size;
      this.folder = folder; // inside ctx.path: '' or 'a/b'
      this.state = 'queued';
      this.sent = 0;
      this.message = gettext('Queued…');
      this.error = '';
      this.session = null;
      this.target = null;
      this.overwrite = false;
      this.xhr = null;
      this.cancelled = false;
      this.samples = [];
      this.drawnAt = 0;
      this.row = null;
    }

    get storageKey() { return this.ctx.storage.key; }

    progress(sent) {
      this.sent = Math.max(0, Math.min(this.size, sent));
      const now = performance.now();
      this.samples.push([now, this.sent]);
      while (this.samples.length > 2 && now - this.samples[0][0] > 3000) this.samples.shift();
      if (now - this.drawnAt > 100 || this.sent === this.size) {
        this.drawnAt = now;
        panel.draw(this);
      }
    }

    speed() {
      if (this.samples.length < 2) return 0;
      const [t0, b0] = this.samples[0];
      const [t1, b1] = this.samples[this.samples.length - 1];
      return t1 > t0 ? ((b1 - b0) * 1000) / (t1 - t0) : 0;
    }

    set(state, message = '', error = '') {
      this.state = state;
      this.message = message;
      this.error = error;
      if (state !== 'uploading') this.samples = [];
      panel.draw(this, true);
      counted();
    }
  }

  function pump() {
    while (running < limit) {
      const next = tasks.find((task) => task.state === 'queued' && !task.cancelled);
      if (!next) break;
      running += 1;
      next.set('uploading', gettext('Getting ready…'));
      run(next).finally(() => {
        running -= 1;
        pump();
      });
    }
    counted();
  }

  // ------------------------------------------------------------------ one upload

  async function run(task) {
    try {
      if (task.batch.failedFolders && task.batch.failedFolders.has(task.folder)) {
        throw new Error(task.batch.failedFolders.get(task.folder));
      }
      let session = task.session;
      if (session && session.kind === 'proxy') {
        session = await refresh(task); // a retry: carry on where it stopped
      }
      if (!session) {
        session = await createSession(task);
        if (!session) return; // skipped
      }
      task.session = session;
      let done = session.state === 'done' ? session : null;
      if (!done && session.kind === 'proxy') done = await sendChunks(task, session);
      else if (!done) done = await sendDirect(task, session);
      if (task.cancelled) return;
      finished(task, done);
    } catch (error) {
      if (task.cancelled) return;
      task.set('failed', gettext('Upload failed'), error.message || String(error));
    } finally {
      if (!task.cancelled) batchDone(task.batch);
    }
  }

  // A visitor who isn't logged in may have no Django session yet: the first upload makes one, and its
  // answer sets the cookie. Uploads made side by side before that would each make their own, and the
  // browser keeps only one cookie: so until one session has been made, they are made one at a time.
  let sessionReady = loggedIn();
  let firstCreate = null;
  function loggedIn() { return document.body.dataset.auth === '1'; }

  async function postSession(body) {
    while (!sessionReady && firstCreate) {
      try { await firstCreate; } catch (e) { /* its own task reports it */ }
    }
    const request = api('POST', '/api/v1/uploads/', body);
    if (sessionReady) return request;
    firstCreate = request;
    try {
      const session = await request;
      sessionReady = true;
      return session;
    } finally {
      if (firstCreate === request) firstCreate = null;
    }
  }

  async function createSession(task) {
    const ctx = task.ctx;
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await postSession({
          storage: task.storageKey, folder: cleanRel(ctx.path), relative_path: task.folder, name: task.name,
          size: task.size, overwrite: task.overwrite,
        });
      } catch (error) {
        if (!(error instanceof ApiError) || task.cancelled) throw error;
        const data = error.data || {};
        if (data.code === 'exists') {
          const choice = await conflict(task, data, { rename: true });
          if (choice === 'overwrite') { task.overwrite = true; continue; }
          if (choice === 'rename') continue;
          if (task.original) task.name = task.original; // what was skipped is the file itself
          task.set('skipped', gettext('Skipped'));
          return null;
        }
        if (data.code === 'invalid_name' && task.tried && task.tried.has(task.name)) {
          // The name typed in 换个名字 is one the server refuses: ask again, saying why. Closed: the file's
          // own name again, so its 文件已存在 comes back (跳过 or another name), as when the input is closed there.
          task.message = gettext("The new name can't be used; waiting for another…");
          panel.draw(task);
          if (!(await serial(() => askName(task, data.detail)))) task.name = task.original;
          continue;
        }
        if ((data.code === 'password_required' || data.code === 'password_wrong') && attempt < 5) {
          if (await unlock(task.ctx, data, data.code === 'password_wrong')) continue;
          throw new Error(gettext('This folder needs a password, and none was entered, so nothing was uploaded.'));
        }
        if (error.status === 429 && attempt < 30) {
          task.message = gettext('Too many uploads; waiting a moment…');
          panel.draw(task);
          await sleep(Math.max(1, Number(data.wait) || 5) * 1000);
          continue;
        }
        throw error;
      }
    }
  }

  // "覆盖 / 跳过" (D17), with 全部覆盖 and 全部跳过 for the rest of this batch. Overwriting needs 删除 (D17a):
  // without it the panel asks for another name (换个名字) where the upload can still take one (`rename`:
  // before its session is made), and offers only 跳过 later.
  async function conflict(task, data, { rename = false } = {}) {
    const mayOverwrite = data.can_overwrite !== false && can(task.ctx, 'delete');
    if (task.batch.decision === 'skip') return 'skip';
    if (task.batch.decision === 'overwrite' && mayOverwrite) return 'overwrite';
    task.message = gettext('Already exists; waiting for a choice…');
    panel.draw(task);
    return serial(async () => {
      if (task.batch.decision === 'skip') return 'skip';
      if (task.batch.decision === 'overwrite' && mayOverwrite) return 'overwrite';
      const many = task.batch.total > 1;
      const renaming = rename && !mayOverwrite;
      const buttons = [];
      if (many) buttons.push({ label: gettext('Skip all'), value: 'skip-all' });
      buttons.push({ label: gettext('Skip'), value: 'skip', focus: !mayOverwrite && !renaming });
      if (mayOverwrite) {
        if (many) buttons.push({ label: gettext('Overwrite all'), value: 'overwrite-all' });
        buttons.push({ label: gettext('Overwrite'), value: 'overwrite', kind: 'btn-filled', focus: true });
      } else if (renaming) {
        buttons.push({ label: pgettext('upload under another name', 'Rename'), value: 'rename', kind: 'btn-filled', focus: true });
      }
      const where = data.path || task.name;
      let message = interpolate(gettext('"%(where)s" already exists. Overwriting replaces the file that is there.'), { where }, true);
      if (!mayOverwrite) {
        message = renaming
          ? interpolate(gettext('"%(where)s" already exists. You don\'t have the "Delete" permission, so you can\'t overwrite it; please upload it with another name.'), { where }, true)
          : interpolate(gettext('"%(where)s" already exists. Overwriting a file needs the "Delete" permission, so it can only be skipped.'), { where }, true);
      }
      for (;;) {
        const choice = await askDialog({ title: gettext('The file already exists'), message, buttons });
        if (choice === 'rename') {
          if (await askName(task)) return 'rename';
          continue; // closed without a name: the question again
        }
        if (choice === 'skip-all') task.batch.decision = 'skip';
        if (choice === 'overwrite-all') task.batch.decision = 'overwrite';
        return choice && choice.startsWith('overwrite') ? 'overwrite' : 'skip';
      }
    });
  }

  // 名字 (1).扩展名, (2)…: what 换个名字 suggests, counted from the file's own name.
  function numbered(name, n) {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? `${name.slice(0, dot)} (${n})${name.slice(dot)}` : `${name} (${n})`;
  }

  // 换个名字: the name this upload goes on with. Checked here for what the server refuses anyway (empty, / or
  // \, only dots and spaces); for anything else `problem` is the server's answer to the last try.
  function askName(task, problem = '') {
    task.original = task.original || task.name;
    task.tried = task.tried || new Set(); // the names sent so far: closing the input sends none
    let n = 1;
    while (task.tried.has(numbered(task.original, n))) n += 1;
    const input = h('input', { id: `upload-name-${++dialogSeq}`, name: 'name', type: 'text', autocomplete: 'off',
      maxlength: '255', value: problem ? task.name : numbered(task.original, n), 'data-autofocus': '' });
    const body = h('div', null,
      h('p', { class: 'muted', text: interpolate(gettext('Upload "%(original)s" with a new name; the file already there stays as it is.'), { original: task.original }, true) }),
      problem ? h('p', { class: 'form-error', role: 'alert', text: problem }) : null,
      h('div', { class: 'field' }, h('label', { for: input.id, text: gettext('New file name') }), input));
    return App.editDialog({
      title: pgettext('upload under another name', 'Rename'), body, submitText: gettext('Upload'), submitIcon: 'upload',
      save() {
        const name = input.value.trim();
        if (!name) throw new Error(gettext('Please enter a new file name.'));
        if (/[\\/]/.test(name)) throw new Error(gettext("A file name can't contain / or \\."));
        if (/^[.\s]+$/.test(name)) throw new Error(gettext("A file name can't be only dots and spaces."));
        task.name = name;
        task.tried.add(name);
        task.message = gettext('Queued…');
        panel.draw(task);
        return true;
      },
    }).then(Boolean);
  }

  // A folder password (D11): unlock it for this browser, then the upload is asked again.
  function unlock(ctx, data, wrong) {
    return serial(async () => {
      const site = await loadSite();
      const input = h('input', { id: `upload-pw-${++dialogSeq}`, name: 'password', type: 'password', autocomplete: 'off', maxlength: '1000', 'data-autofocus': '' });
      const remember = h('input', { type: 'checkbox', name: 'remember' });
      remember.checked = Boolean(site.default_save_password);
      const body = h('div', null,
        h('p', { class: 'muted', text: data.detail || gettext('This folder is protected. Please enter the password:') }),
        wrong ? h('p', { class: 'form-error', role: 'alert', text: gettext('Wrong password. Please try again.') }) : null,
        h('div', { class: 'field' }, h('label', { for: input.id, text: gettext('Password') }), input),
        h('label', { class: 'check-line' }, remember, gettext('Remember password')));
      const result = await App.editDialog({
        title: gettext('This folder is protected'), body, submitText: gettext('OK'), submitIcon: 'lock_open',
        async save() {
          if (!input.value) throw new Error(gettext('Please enter the password.'));
          const payload = { password: input.value, remember: remember.checked };
          if (data.path) payload.path = data.path;
          else payload.rule = data.rule;
          await api('POST', `/api/v1/storages/${encodeURIComponent(ctx.storage.key)}/unlock/`, payload);
          return true;
        },
      });
      return Boolean(result);
    });
  }

  // "busy": another request holds the session, for at most a lease the server renews while it works
  // (90 s); a crashed one's runs out by itself. The answer says how long (`wait`): wait that long, again
  // and again for up to BUSY_BUDGET seconds in all, then give up.
  const BUSY_BUDGET = 200;
  async function waitWhileBusy(task, data, waited) {
    const seconds = Math.min(Math.max(Number(data.wait) || 2, 1), 30);
    if (waited + seconds > BUSY_BUDGET) return null;
    task.message = gettext('Waiting for another request to finish with this upload…');
    panel.draw(task);
    await sleep(seconds * 1000);
    return waited + seconds;
  }

  async function refresh(task) {
    try {
      return await api('GET', task.session.url);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null; // expired or gone: start again
      throw error;
    }
  }

  // Through the site: chunks in order (202 until the last, 201 at the end).
  async function sendChunks(task, session) {
    const size = task.size;
    const chunk = session.chunk_size;
    let offset = session.received || 0;
    let failures = 0;
    let busyWaited = 0;
    task.progress(offset);
    while (!task.cancelled) {
      if (session.state === 'done') return session;
      if (session.state === 'failed' || session.state === 'finishing' || offset >= size) {
        return finishProxy(task, session);
      }
      const end = Math.min(offset + chunk, size);
      const last = end === size;
      const start = offset;
      task.message = gettext('Uploading');
      let answer;
      try {
        answer = await siteSend(task, {
          method: 'PUT', url: session.url, body: task.file.slice(start, end),
          headers: { 'Content-Range': `bytes ${start}-${end - 1}/${size}`, 'Content-Type': 'application/octet-stream' },
          onProgress: (loaded) => {
            task.progress(start + loaded);
            if (last && start + loaded >= size) {
              task.message = gettext('Uploaded; the server is passing it on…');
              panel.draw(task);
            }
          },
        });
      } catch (error) {
        if (task.cancelled) return null;
        // No answer: maybe it arrived. Ask the session how far it got, then go on (a few times).
        failures += 1;
        if (failures > 4) throw new Error(netMessage(error, session.url, false));
        task.message = interpolate(gettext('The network dropped; retrying (attempt %(failures)s)…'), { failures }, true);
        panel.draw(task);
        await sleep(1000 * 2 ** failures);
        const now = await refresh(task);
        if (!now) throw new Error(gettext('The upload session is no longer valid. Please upload again.'));
        session = now;
        task.session = now;
        offset = now.received;
        continue;
      }
      const data = answer.data || {};
      if (answer.status === 201 || answer.status === 202) {
        failures = 0;
        session = data;
        task.session = data;
        offset = data.received;
        task.progress(offset);
        continue;
      }
      if (answer.status === 409 && data.code === 'offset' && Number.isFinite(data.expected)) {
        offset = data.expected;
        continue;
      }
      if (answer.status === 403 && (data.code === 'password_required' || data.code === 'password_wrong') && failures < 5) {
        failures += 1; // the folder's password changed meanwhile: unlock it again, then go on
        if (await unlock(task.ctx, data, false)) continue;
        throw new Error(gettext('This folder needs a password, and none was entered, so the upload stopped.'));
      }
      if (answer.status === 409 && data.code === 'exists') {
        // A file of that name turned up while this one was on its way.
        const choice = await conflict(task, data);
        if (choice !== 'overwrite') {
          await dropSession(task);
          task.set('skipped', gettext('Skipped'));
          return null;
        }
        return finishProxy(task, session, true);
      }
      if (answer.status === 409 && data.code === 'busy') {
        const waited = await waitWhileBusy(task, data, busyWaited);
        if (waited === null) throw new Error(data.detail || gettext('Another request keeps holding this upload. Please try again later.'));
        busyWaited = waited;
        const now = await refresh(task);
        if (!now) throw new Error(gettext('The upload session is no longer valid. Please upload again.'));
        session = now;
        task.session = now;
        offset = now.received;
        continue;
      }
      if ((answer.status === 409 && data.code === 'not_receiving') || answer.status >= 500) {
        failures += 1;
        if (failures > 4) throw new Error(data.detail || interpolate(gettext('The server ran into an error (HTTP %(status)s). Please try again later.'), { status: answer.status }, true));
        await sleep(1000 * 2 ** failures);
        const now = await refresh(task);
        if (!now) throw new Error(gettext('The upload session is no longer valid. Please upload again.'));
        session = now;
        task.session = now;
        offset = now.received;
        if (answer.status >= 500 && now.state === 'failed') throw new Error(data.detail || gettext('Writing to the storage failed.'));
        continue;
      }
      throw new Error(data.detail || App.errorText(data, answer.status));
    }
    return null;
  }

  // Every byte is in: (again) have the site write it to the storage; wait while it does.
  async function finishProxy(task, session, overwrite = false) {
    task.message = gettext('Uploaded; the server is passing it on…');
    panel.draw(task);
    let busyWaited = 0;
    while (!task.cancelled) {
      if (session.state === 'done') return session;
      if (session.state === 'finishing') {
        // The site is writing it into the storage (a big file to a remote one takes a while): its
        // lease says so, and a write that died shows as failed, so waiting here ends.
        await sleep(2000);
        const now = await refresh(task);
        if (!now) throw new Error(gettext('The upload session is no longer valid. Please upload again.'));
        session = now;
        continue;
      }
      try {
        return await api('POST', session.complete_url, { overwrite: overwrite || task.overwrite });
      } catch (error) {
        const data = (error instanceof ApiError && error.data) || {};
        if (data.code === 'busy') {
          const waited = await waitWhileBusy(task, data, busyWaited);
          if (waited !== null) {
            busyWaited = waited;
            session = (await refresh(task)) || session;
            continue;
          }
        }
        if (data.code === 'exists' && !overwrite) {
          const choice = await conflict(task, data);
          if (choice === 'overwrite') { overwrite = true; continue; }
          await dropSession(task);
          task.set('skipped', gettext('Skipped'));
          return null;
        }
        throw error;
      }
    }
    return null;
  }

  // The server keeps a session while a request holds it (a chunk still being written after the browser
  // gave up on it): ask again when it says; past BUSY_BUDGET the session expires by itself.
  async function dropSession(task) {
    if (!task.session) return;
    const url = task.session.url;
    task.session = null;
    let waited = 0;
    for (;;) {
      try {
        await api('DELETE', url);
        return;
      } catch (error) {
        if (!(error instanceof ApiError) || !error.data || error.data.code !== 'busy') return;
        const seconds = Math.min(Math.max(Number(error.data.wait) || 2, 1), 30);
        waited += seconds;
        if (waited > BUSY_BUDGET) return;
        await sleep(seconds * 1000);
      }
    }
  }

  // Straight to the storage, as the driver's UploadTarget says; then the site looks at the file.
  async function sendDirect(task, session) {
    const target = session.target;
    if (!target) throw new Error(gettext('The upload session is no longer valid. Please upload again.'));
    task.target = target;
    task.message = gettext('Uploading');
    if (target.kind === 'chunks') await sendTargetChunks(task, target);
    else {
      let body = task.file;
      if (target.kind === 'form') {
        body = new FormData();
        for (const [name, value] of Object.entries(target.fields || {})) body.append(name, value);
        body.append(target.file_field || 'file', task.file, task.name);
      }
      let answer;
      try {
        answer = await send(task, {
          method: target.method || (target.kind === 'form' ? 'POST' : 'PUT'), url: target.url, body,
          headers: target.headers || {}, credentials: false,
          onProgress: (loaded) => task.progress(Math.min(loaded, task.size)),
        });
      } catch (error) {
        if (task.cancelled) return null;
        throw new Error(netMessage(error, target.url, true));
      }
      if (answer.status < 200 || answer.status >= 300) throw new Error(backendMessage(answer, target.url));
    }
    if (task.cancelled) return null;
    task.message = gettext('Uploaded; confirming…');
    panel.draw(task);
    return api('POST', session.complete_url, {});
  }

  // OneDrive's upload session: PUTs of chunk_size bytes in order; 202 next, 200/201 done.
  async function sendTargetChunks(task, target) {
    const size = task.size;
    if (size === 0) throw new Error(gettext("This kind of storage doesn't support uploading empty files"));
    const chunk = target.chunk_size || 10 * 1024 * 1024;
    try {
      await sendSessionChunks(task, target, size, chunk);
    } catch (error) {
      // A session that failed is deleted (the URL is the credential): on OneDrive for Business and SharePoint it
      // holds the file's name until it expires, and uploading the file again would be refused until then.
      fetch(target.url, { method: 'DELETE', credentials: 'omit' }).catch(() => {});
      throw error;
    }
  }

  // Where an upload session wants the next bytes, from its nextExpectedRanges ("26214400-"); null when it doesn't say.
  function rangeStart(ranges, size) {
    if (!Array.isArray(ranges) || !ranges.length) return null;
    const next = parseInt(String(ranges[0]).split('-')[0], 10);
    return Number.isFinite(next) && next >= 0 && next <= size ? next : null;
  }

  async function sendSessionChunks(task, target, size, chunk) {
    let offset = 0;
    let failures = 0;
    while (offset < size && !task.cancelled) {
      const end = Math.min(offset + chunk, size);
      const start = offset;
      let answer;
      try {
        answer = await send(task, {
          method: target.method || 'PUT', url: target.url, body: task.file.slice(start, end), credentials: false,
          headers: { ...(target.headers || {}), 'Content-Range': `bytes ${start}-${end - 1}/${size}` },
          onProgress: (loaded) => task.progress(start + loaded),
        });
      } catch (error) {
        if (task.cancelled) return;
        failures += 1;
        if (failures > 3) throw new Error(netMessage(error, target.url, true));
        await sleep(1000 * 2 ** failures);
        continue;
      }
      if (answer.status === 200 || answer.status === 201) {
        task.progress(size);
        return;
      }
      if (answer.status === 202) {
        failures = 0;
        offset = end;
        const next = rangeStart(answer.data && answer.data.nextExpectedRanges, size);
        if (next !== null) offset = next;
        task.progress(offset);
        continue;
      }
      if (answer.status === 416 && end < size && failures < 3) {
        // The session has this chunk already: an earlier try got through and its answer was lost. Ask it where to go on.
        failures += 1;
        let status = null;
        try {
          status = await send(task, { method: 'GET', url: target.url, credentials: false });
        } catch (error) {
          if (task.cancelled) return;
        }
        if (status && status.status === 200 && rangeStart(status.data && status.data.nextExpectedRanges, size) === end) {
          offset = end;
          task.progress(offset);
          continue;
        }
      }
      if ((answer.status >= 500 || answer.status === 429) && failures < 3) {
        failures += 1;
        await sleep(1000 * 2 ** failures);
        continue;
      }
      throw new Error(backendMessage(answer, target.url));
    }
  }

  // ------------------------------------------------------------------ landing

  const pendingEvents = new Map();
  function changed(storageKey, path) {
    const key = `${storageKey}\n${path}`;
    if (pendingEvents.has(key)) return;
    pendingEvents.set(key, setTimeout(() => {
      pendingEvents.delete(key);
      document.dispatchEvent(new CustomEvent('hfile:files-changed', { detail: { storageKey, path } }));
    }, 250));
  }

  function finished(task, session) {
    if (!session) return;
    task.session = session;
    task.progress(task.size);
    task.set('done', gettext('Uploaded'));
    const shown = joinPath(task.ctx.path);
    const landed = session.folder || joinPath(task.ctx.path, task.folder);
    changed(task.storageKey, landed);
    if (landed !== shown) changed(task.storageKey, shown); // a new sub-folder appeared in the folder shown
  }

  function batchDone(batch) {
    const mine = tasks.filter((task) => task.batch === batch);
    if (mine.some((task) => task.state === 'queued' || task.state === 'uploading')) return;
    if (batch.reported) return;
    batch.reported = true;
    const ok = mine.filter((task) => task.state === 'done').length;
    const failed = mine.filter((task) => task.state === 'failed').length;
    if (failed) toast(batch.ctx, interpolate(ngettext('%(ok)s uploaded, %(failed)s failed.', '%(ok)s uploaded, %(failed)s failed.', ok), { ok, failed }, true), 'error');
    else if (ok) toast(batch.ctx, ok === 1 ? gettext('Uploaded') : interpolate(ngettext('Uploaded %(ok)s file', 'Uploaded %(ok)s files', ok), { ok }, true));
  }

  // ------------------------------------------------------------------ the panel

  const panel = (() => {
    let root = null;
    let list = null;
    let title = null;
    let summary = null;
    let toggle = null;
    let collapsed = false;
    let noted = '';

    function build() {
      if (root) return;
      title = h('h2', { class: 'upload-panel-title', id: 'upload-panel-title', text: gettext('Upload') });
      summary = h('p', { class: 'upload-panel-summary', 'aria-live': 'polite' });
      toggle = h('button', { type: 'button', class: 'icon-button', 'aria-label': gettext('Collapse'), 'data-tooltip': gettext('Collapse'), 'aria-expanded': 'true' }, icon('expand_more'));
      const close = h('button', { type: 'button', class: 'icon-button', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'));
      list = h('ul', { class: 'upload-list', role: 'list' });
      root = h('section', { class: 'upload-panel', role: 'region', 'aria-labelledby': 'upload-panel-title', hidden: true },
        h('header', { class: 'upload-panel-head' },
          h('span', { class: 'upload-panel-icon' }, icon('upload')),
          h('div', { class: 'upload-panel-text' }, title, summary),
          toggle, close),
        list);
      toggle.addEventListener('click', () => setCollapsed(!collapsed));
      close.addEventListener('click', hide);
      document.body.append(root);
      // Snackbars go above the panel where they would meet it (uploads.css): they need its height.
      if (window.ResizeObserver) {
        new ResizeObserver(() => {
          document.documentElement.style.setProperty('--upload-panel-height', `${root.hidden ? 0 : root.offsetHeight}px`);
        }).observe(root);
      }
    }

    function setCollapsed(value) {
      collapsed = value;
      root.classList.toggle('collapsed', collapsed);
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', collapsed ? gettext('Expand') : gettext('Collapse'));
      toggle.dataset.tooltip = collapsed ? gettext('Expand') : gettext('Collapse');
      toggle.querySelector('use').setAttribute('href', App.iconHref(collapsed ? 'expand_less' : 'expand_more'));
    }

    function show() {
      build();
      root.hidden = false;
      document.body.classList.add('has-upload-panel');
      update();
    }

    // Closing takes the finished rows away (the others go on, and the badge counts them).
    function hide() {
      if (!root) return;
      for (let i = tasks.length - 1; i >= 0; i -= 1) {
        if (tasks[i].state === 'done' || tasks[i].state === 'skipped') remove(tasks[i]);
      }
      root.hidden = true;
      document.body.classList.remove('has-upload-panel');
      counted();
    }

    function remove(task) {
      const index = tasks.indexOf(task);
      if (index >= 0) tasks.splice(index, 1);
      if (task.row) task.row.remove();
      task.row = null;
    }

    function rowFor(task) {
      if (task.row) return task.row;
      const parts = {
        icon: h('span', { class: 'upload-icon' }, icon(KIND_ICONS[kindOf(task.name)])),
        name: h('span', { class: 'upload-name' }),
        where: h('span', { class: 'upload-where' }),
        meta: h('span', { class: 'upload-meta' }),
        error: h('span', { class: 'upload-error', role: 'alert' }),
        bar: h('progress', { class: 'linear-progress', max: '1', value: '0' }),
        actions: h('span', { class: 'upload-actions' }),
        mark: h('span', { class: 'upload-mark' }),
      };
      parts.name.textContent = task.name;
      parts.name.title = task.name;
      if (task.folder) parts.where.textContent = task.folder + '/';
      const row = h('li', { class: 'upload-row' },
        parts.icon,
        h('span', { class: 'upload-main' },
          h('span', { class: 'upload-line' }, parts.where, parts.name),
          parts.meta, parts.error, parts.bar),
        parts.actions, parts.mark);
      row.parts = parts;
      task.row = row;
      list.append(row);
      return row;
    }

    function button(task, iconName, label, onClick) {
      const el = h('button', { type: 'button', class: 'icon-button small', 'aria-label': interpolate(gettext('%(label)s: %(name)s'), { label, name: task.name }, true), 'data-tooltip': label }, icon(iconName));
      el.addEventListener('click', onClick);
      return el;
    }

    const WAITING = new Set([gettext('Uploaded; the server is passing it on…'), gettext('Uploaded; confirming…')]);
    const MARK_ICONS = { done: 'check_circle', failed: 'error', queued: 'schedule', skipped: 'remove' };

    // A row as its task is now. `structural`: the state changed (buttons, mark, place in the list).
    function draw(task, structural = false) {
      if (!root) return;
      const row = rowFor(task);
      const { name, meta, error, bar, actions, mark } = row.parts;
      if (name.textContent !== task.name) { // 换个名字: the name it goes on with
        name.textContent = task.name;
        name.title = task.name;
        row.parts.icon.replaceChildren(icon(KIND_ICONS[kindOf(task.name)]));
      }
      row.dataset.state = task.state;
      const sizeText = fmtSize(task.size);
      if (task.state === 'uploading') {
        const speed = task.speed();
        const bits = [`${fmtSize(task.sent)} / ${sizeText}`];
        if (speed > 0 && task.sent < task.size) bits.push(interpolate(gettext('%(speed)s/s'), { speed: fmtSize(speed) }, true));
        bits.push(task.message);
        meta.textContent = bits.join(' · ');
        if (WAITING.has(task.message)) bar.removeAttribute('value'); // indeterminate meanwhile
        else bar.value = task.size ? task.sent / task.size : 0;
      } else {
        meta.textContent = `${sizeText} · ${task.message}`;
      }
      bar.hidden = task.state !== 'uploading';
      error.textContent = task.state === 'failed' ? task.error : '';
      error.hidden = task.state !== 'failed';
      if (!structural) return;
      actions.replaceChildren();
      if (task.state === 'uploading') actions.append(button(task, 'cancel', gettext('Cancel upload'), () => cancel(task)));
      else if (task.state === 'failed') actions.append(button(task, 'refresh', gettext('Retry'), () => retry(task)), button(task, 'close', gettext('Remove'), () => discard(task)));
      else actions.append(button(task, 'close', gettext('Remove'), () => discard(task)));
      mark.replaceChildren(...(MARK_ICONS[task.state] ? [icon(MARK_ICONS[task.state])] : []));
      reorder();
    }

    // Rows sorted failed, uploading, queued, finished: at most once a frame (a folder of thousands of
    // files changes state thousands of times).
    let reorderPending = false;
    function reorder() {
      if (reorderPending) return;
      reorderPending = true;
      requestAnimationFrame(() => {
        reorderPending = false;
        if (!list) return;
        const sorted = [...tasks].sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state]);
        for (const task of sorted) if (task.row) list.append(task.row);
      });
    }

    function update() {
      if (!root || root.hidden) return;
      const uploading = tasks.filter((task) => task.state === 'uploading').length;
      const queued = tasks.filter((task) => task.state === 'queued').length;
      const failed = tasks.filter((task) => task.state === 'failed').length;
      const done = tasks.filter((task) => task.state === 'done').length;
      const skipped = tasks.filter((task) => task.state === 'skipped').length;
      if (uploading || queued) title.textContent = interpolate(ngettext('Uploading %(n)s file', 'Uploading %(n)s files', uploading + queued), { n: uploading + queued }, true);
      else if (failed) title.textContent = interpolate(ngettext('%(failed)s file failed to upload', '%(failed)s files failed to upload', failed), { failed }, true);
      else title.textContent = tasks.length ? gettext('Uploads finished') : gettext('Upload');
      const bits = [];
      if (uploading) bits.push(interpolate(ngettext('%(uploading)s uploading', '%(uploading)s uploading', uploading), { uploading }, true));
      if (queued) bits.push(interpolate(ngettext('%(queued)s queued', '%(queued)s queued', queued), { queued }, true));
      if (done) bits.push(interpolate(ngettext('%(done)s done', '%(done)s done', done), { done }, true));
      if (skipped) bits.push(interpolate(ngettext('%(skipped)s skipped', '%(skipped)s skipped', skipped), { skipped }, true));
      if (failed) bits.push(interpolate(ngettext('%(failed)s failed', '%(failed)s failed', failed), { failed }, true));
      summary.textContent = noted || bits.join(' · ') || gettext('No uploads');
    }

    // A line under the title while something happens before the files go (creating folders).
    function note(text) {
      build();
      noted = text;
      update();
    }

    return { build, show, hide, draw, update, remove, note, get open() { return Boolean(root && !root.hidden); } };
  })();

  // ------------------------------------------------------------------ row actions

  async function cancel(task) {
    const ok = await askDialog({
      title: gettext('Cancel upload'),
      message: interpolate(gettext('Cancel uploading %(name)s?'), { name: task.name }, true),
      buttons: [{ label: gettext('Back'), value: false }, { label: gettext('OK'), value: true, kind: 'btn-danger', focus: true }],
    });
    if (!ok || task.state !== 'uploading') return;
    task.cancelled = true;
    if (task.xhr) task.xhr.abort();
    if (task.target && task.target.kind === 'chunks') {
      // An OneDrive upload session goes when it is deleted (the URL is the credential).
      fetch(task.target.url, { method: 'DELETE', credentials: 'omit' }).catch(() => {});
    }
    await dropSession(task);
    panel.remove(task);
    counted();
    batchDone(task.batch);
  }

  function retry(task) {
    if (task.state !== 'failed') return;
    task.cancelled = false;
    task.error = '';
    if (task.session && task.session.kind !== 'proxy') task.session = null; // a direct one starts again
    task.set('queued', gettext('Queued…'));
    pump();
  }

  async function discard(task) {
    if (task.state === 'uploading') return;
    task.cancelled = true;
    if (task.state === 'failed') await dropSession(task);
    panel.remove(task);
    counted();
  }

  // ------------------------------------------------------------------ starting

  // Items -> [{file, folder}] and the folders they need (empty ones too), relative to ctx.path.
  function normalize(items) {
    const files = [];
    const folders = new Set();
    for (const item of items || []) {
      if (!item) continue;
      if (typeof File !== 'undefined' && item instanceof File) {
        const rel = cleanRel(item.webkitRelativePath || item.relativePath || '');
        const folder = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
        files.push({ file: item, folder });
      } else if (item.file) {
        const rel = cleanRel(item.path || item.file.webkitRelativePath || '');
        const folder = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
        files.push({ file: item.file, folder });
      } else if (item.folder !== undefined) {
        const folder = cleanRel(item.folder);
        if (folder) folders.add(folder);
      }
    }
    for (const { folder } of files) if (folder) folders.add(folder);
    return { files, folders: [...folders] };
  }

  // POST /api/v1/uploads/folders/ takes at most this many folders, counting the ones it names and every
  // folder above them once (apps/uploads/service.py folder_tree, FOLDERS_MAX): the batches are cut by
  // that same count, so a deep tree never has a batch refused (with all its files).
  const FOLDERS_PER_REQUEST = 1000;

  // ['a/b/c', 'a/b/d', ...] -> batches of them, each naming at most `limit` folders with those above.
  function folderBatches(folders, limit = FOLDERS_PER_REQUEST) {
    const batches = [];
    let part = [];
    let tree = new Map(); // the names of this batch, as the server builds them
    let count = 0;
    const missing = (segments) => { // folders of this one's chain the batch doesn't name yet
      let node = tree;
      for (let i = 0; i < segments.length; i += 1) {
        node = node.get(segments[i]);
        if (!node) return segments.length - i;
      }
      return 0;
    };
    for (const folder of folders) {
      const segments = folder.split('/');
      if (part.length && count + missing(segments) > limit) {
        batches.push(part);
        part = [];
        tree = new Map();
        count = 0;
      }
      let node = tree;
      for (const segment of segments) {
        let child = node.get(segment);
        if (!child) {
          child = new Map();
          node.set(segment, child);
          count += 1;
        }
        node = child;
      }
      part.push(folder);
    }
    if (part.length) batches.push(part);
    return batches;
  }

  async function makeFolders(ctx, batch, folders) {
    batch.failedFolders = new Map();
    if (!folders.length) return;
    for (const part of folderBatches(folders)) {
      for (let attempt = 0; ; attempt += 1) {
        try {
          const answer = await api('POST', '/api/v1/uploads/folders/', { storage: ctx.storage.key, folder: cleanRel(ctx.path), folders: part });
          for (const failure of answer.failed || []) {
            const rel = cleanRel(failure.path).slice(cleanRel(ctx.path).length).replace(/^\//, '');
            batch.failedFolders.set(rel, interpolate(gettext('The folder "%(path)s" wasn\'t created: %(detail)s'), { path: failure.path, detail: failure.detail }, true));
          }
          if ((answer.created || []).length) changed(ctx.storage.key, joinPath(ctx.path));
          break;
        } catch (error) {
          const data = (error instanceof ApiError && error.data) || {};
          if ((data.code === 'password_required' || data.code === 'password_wrong') && attempt < 5) {
            if (await unlock(ctx, data, data.code === 'password_wrong')) continue;
          }
          if (error instanceof ApiError && error.status === 429 && attempt < 30) {
            await sleep(Math.max(1, Number(data.wait) || 5) * 1000);
            continue;
          }
          for (const folder of part) batch.failedFolders.set(folder, interpolate(gettext("The folder wasn't created: %(message)s"), { message: error.message }, true));
          break;
        }
      }
    }
    // Files below a folder that failed fail too.
    for (const [failedFolder, message] of [...batch.failedFolders]) {
      for (const folder of folders) {
        if (folder.startsWith(failedFolder + '/') && !batch.failedFolders.has(folder)) batch.failedFolders.set(folder, message);
      }
    }
  }

  async function start(ctx, items) {
    if (!ctx || !ctx.storage || !ctx.storage.key) throw new Error('App.uploads.start: ctx.storage is missing');
    let { files, folders } = normalize(items);
    if (!files.length && !folders.length) return;
    if (!can(ctx, 'upload')) {
      toast(ctx, gettext('You don\'t have the "Upload" permission.'), 'error');
      return;
    }
    // Without 新建文件夹 what is inside folders is left out: say how much.
    if (folders.length && !can(ctx, 'new_folder')) {
      const nested = files.filter((item) => item.folder).length;
      files = files.filter((item) => !item.folder);
      folders = [];
      toast(ctx, nested ? interpolate(ngettext('You don\'t have the "New folder" permission: %(nested)s file inside folders was skipped.', 'You don\'t have the "New folder" permission: %(nested)s files inside folders were skipped.', nested), { nested }, true) : gettext('You don\'t have the "New folder" permission, so the folder wasn\'t uploaded.'), 'error');
      if (!files.length) return;
    }
    if (files.length > 100) {
      const ok = await App.confirmDialog({ title: gettext('Upload'), message: interpolate(ngettext('Upload %(count)s file?', 'Upload %(count)s files?', files.length), { count: files.length }, true), confirmText: gettext('Upload') });
      if (!ok) return;
    }
    loadSite();
    const batch = { ctx, total: files.length, decision: null, reported: false, failedFolders: new Map() };
    panel.show();
    if (folders.length) {
      panel.note(gettext('Creating folders…'));
      await makeFolders(ctx, batch, folders.sort((a, b) => a.split('/').length - b.split('/').length));
      panel.note('');
    }
    for (const item of files) {
      const task = new Task(ctx, batch, item.file, item.folder);
      tasks.push(task);
      panel.draw(task, true);
    }
    if (!files.length) {
      toast(ctx, gettext('The folder has been created.'));
      panel.update();
      return;
    }
    await loadSite();
    pump();
  }

  // The file chooser: <input type="file" multiple>, with webkitdirectory for a folder.
  let chooser = null;
  function pick(ctx, { folder = false } = {}) {
    if (chooser) chooser.remove();
    const input = h('input', { type: 'file', multiple: true, class: 'sr-only', tabindex: '-1', 'aria-hidden': 'true' });
    chooser = input;
    if (folder) input.setAttribute('webkitdirectory', '');
    input.addEventListener('change', () => {
      const chosen = [...(input.files || [])];
      input.remove();
      if (chosen.length) start(ctx, chosen).catch((error) => toast(ctx, error.message, 'error'));
    });
    input.addEventListener('cancel', () => input.remove());
    document.body.append(input);
    input.click();
  }

  // A drop: the entries have to be taken now, during the event; reading them goes on afterwards.
  function drop(ctx, dataTransfer) {
    const entries = [];
    const plain = [];
    for (const item of [...(dataTransfer && dataTransfer.items ? dataTransfer.items : [])]) {
      if (item.kind !== 'file') continue;
      const entry = typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null;
      if (entry) entries.push(entry);
      else if (item.getAsFile()) plain.push(item.getAsFile());
    }
    // Only when the items gave nothing (an old browser): the same files are in both lists.
    if (!entries.length && !plain.length && dataTransfer && dataTransfer.files) plain.push(...dataTransfer.files);
    return (async () => {
      const items = [...plain];
      const readAll = (reader) => new Promise((resolve, reject) => {
        const found = [];
        const more = () => reader.readEntries((batch) => {
          if (!batch.length) resolve(found);
          else { found.push(...batch); more(); }
        }, reject);
        more();
      });
      const walk = async (entry, prefix) => {
        if (entry.isFile) {
          const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
          items.push({ file, path: prefix + entry.name });
        } else if (entry.isDirectory) {
          const children = await readAll(entry.createReader());
          if (!children.length) items.push({ folder: prefix + entry.name });
          for (const child of children) await walk(child, `${prefix}${entry.name}/`);
        }
      };
      for (const entry of entries) await walk(entry, '');
      return start(ctx, items);
    })().catch((error) => toast(ctx, error.message || gettext('Reading the dropped files failed.'), 'error'));
  }

  function onCount(callback) {
    counters.add(callback);
    try { callback(active()); } catch (e) { /* see counted() */ }
    return () => counters.delete(callback);
  }

  // Leaving the page stops the uploads: the browser asks first.
  window.addEventListener('beforeunload', (e) => {
    if (active()) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  window.App.uploads = {
    pick, start, drop, onCount,
    show: () => panel.show(),
    hide: () => panel.hide(),
    get count() { return active(); },
  };
})();
