/* /manage/#copy-tasks: 复制任务 (/api/v1/manage/copy-tasks/). The tasks, newest first, each a card with its state and
 * progress, and on a click its details: how it copies, the counts, the files being copied (each with its progress and
 * speed, and the error of the try before when it is being tried again) and the files and folders it couldn't copy.
 * Its detailed log (a dialog) has what each run did, newest first: the files being copied, then each file copied,
 * skipped, tried again or failed, and when the runs began and ended; it follows a run while it is open.
 * "New copy task" asks from which storage source's folder into which one's, and how: threads, speed limit, what to do
 * with the files already in the target, retries. While a task of the page runs, the list is asked again every POLL_MS
 * (not while this section or the browser tab is hidden). The copying is the server's: closing the page doesn't stop it. */
(function () {
  'use strict';
  const { api, h, nodes, icon, toast, fmtNum, fmtSize, fmtDateTime, confirmDialog, editDialog, openDialog, errorText } = window.App;

  const root = document.getElementById('copy-tasks');
  if (!root) return;
  const API = '/api/v1/manage/copy-tasks/';
  const POLL_MS = 1500;
  const PAGE_SIZE = 20;
  const LOG_KEEP = 1000; // log lines on screen while following a run: the older ones are loaded again on request
  const MB = 1024 * 1024;
  const SPEED_MAX_MB = 10240; // the server's SPEED_LIMIT_MAX, 10 GB a second
  const THREADS_MAX = 16;
  const RETRIES_MAX = 10;
  const summary = document.getElementById('copy-summary');
  const pager = document.getElementById('copy-pager');
  const refresh = document.getElementById('copy-refresh');

  const BADGES = { running: 'badge-info', stopping: 'badge-warning', done: 'badge-ok', failed: 'badge-error', stopped: '' };
  const POLICY = { skip: gettext('Skip'), overwrite: gettext('Overwrite') };
  const SEP = ' · ';

  let page = 1;
  let count = 0;
  let shown = false; // the section is the one on screen
  let generation = 0; // a late answer to an older load is dropped
  let pollTimer = null;
  const cards = new Map(); // task id -> its card (kept between loads, with whether it is open)

  const live = (task) => task.status === 'running' || task.status === 'stopping';
  const normal = (path) => `/${String(path || '').split('/').filter(Boolean).join('/')}`;
  const parentOf = (path) => normal(path).split('/').slice(0, -1).join('/') || '/';

  // 40 秒, 5 分 12 秒, 3 小时 5 分, 2 天
  function duration(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return interpolate(ngettext('%(n)s second', '%(n)s seconds', s), { n: s }, true);
    const m = Math.floor(s / 60);
    if (m < 60) return interpolate(gettext('%(m)s min %(s)s s'), { m, s: s % 60 }, true);
    const hours = Math.floor(m / 60);
    if (hours < 48) return interpolate(gettext('%(h)s h %(m)s min'), { h: hours, m: m % 60 }, true);
    const days = Math.floor(hours / 24);
    return interpolate(ngettext('%(n)s day', '%(n)s days', days), { n: days }, true);
  }

  const speed = (bytes) => interpolate(gettext('%(speed)s/s'), { speed: fmtSize(bytes) }, true);
  const ofTotal = (done, total) => interpolate(gettext('%(done)s of %(total)s'), { done: fmtSize(done), total: fmtSize(total) }, true);
  const fact = (label, ...value) => h('div', {}, h('dt', { text: label }), h('dd', {}, ...value));

  // A <progress>: `done` of `total`; spinning (no value) when it can't say yet.
  function setBar(bar, done, total, { spinning = false, full = false } = {}) {
    if (spinning) {
      bar.removeAttribute('value');
      return;
    }
    bar.max = Math.max(total, 1);
    bar.value = full && !total ? 1 : Math.min(done, bar.max);
  }

  // ------------------------------------------------------------ a task's card

  function place(storage, path) {
    return h('span', { class: 'copy-place' },
      storage.id
        ? h('a', { class: 'copy-storage', href: `#storages/${storage.id}`, 'data-tooltip': gettext('Open the storage source'), text: storage.name })
        : h('span', { class: 'copy-storage gone', 'data-tooltip': gettext('This storage source has been deleted'), text: storage.name }),
      h('code', { class: 'copy-path', text: path }));
  }

  function makeCard(id) {
    const card = { id, open: false, task: null, detail: null, failures: null, routeKey: '' };
    card.badge = h('span', { class: 'badge' });
    card.route = h('div', { class: 'copy-task-route' });
    card.when = h('span', { class: 'muted small copy-task-when' });
    card.stop = h('button', { type: 'button', class: 'btn btn-small', hidden: true, onclick: () => stopTask(card) }, icon('cancel'), gettext('Stop'));
    card.resume = h('button', { type: 'button', class: 'btn btn-small', hidden: true, onclick: () => resumeTask(card) }, icon('play_arrow'), gettext('Resume'));
    card.log = h('button', {
      type: 'button', class: 'icon-button', 'aria-label': gettext('Detailed log'), 'data-tooltip': gettext('Detailed log'),
      onclick: () => openLog(card),
    }, icon('receipt_long'));
    card.toggle = h('button', {
      type: 'button', class: 'icon-button', 'aria-expanded': 'false', 'aria-label': gettext('Details'), 'data-tooltip': gettext('Details'),
      onclick: () => setOpen(card, !card.open),
    }, icon('expand_more'));
    card.bar = h('progress', { class: 'linear-progress', max: '1', 'aria-label': gettext('Copy progress') });
    card.line = h('p', { class: 'copy-task-line' });
    card.message = h('p', { class: 'copy-task-message', role: 'status', hidden: true });
    card.facts = h('dl', { class: 'facts copy-facts' });
    card.currentTitle = h('h3', { text: gettext('Being copied') });
    card.current = h('ul', { class: 'copy-files' });
    card.failuresTitle = h('h3', { text: gettext("Couldn't be copied") });
    card.failuresList = h('ul', { class: 'copy-failures' });
    card.failuresStatus = h('p', { class: 'muted small' });
    card.more = h('button', { type: 'button', class: 'btn btn-small btn-text copy-more', hidden: true, onclick: () => loadFailures(card) }, gettext('Load more'));
    card.details = h('div', { class: 'copy-task-details', hidden: true },
      card.facts, card.currentTitle, card.current, card.failuresTitle, card.failuresList, card.failuresStatus, card.more);
    card.el = h('article', { class: 'copy-task card card-outlined' },
      h('div', { class: 'copy-task-head' }, card.badge, card.route, card.when,
        h('div', { class: 'copy-task-actions' }, card.stop, card.resume, card.log, card.toggle)),
      card.bar, card.line, card.message, card.details);
    cards.set(id, card);
    return card;
  }

  function lineText(task) {
    const files = task.files;
    const finished = files.copied + files.skipped + files.failed;
    const parts = [ofTotal(task.bytes.done, task.bytes.total),
      interpolate(ngettext('%(done)s of %(total)s file', '%(done)s of %(total)s files', files.total), { done: fmtNum(finished), total: fmtNum(files.total) }, true)];
    if (files.failed) parts.push(interpolate(ngettext('%(count)s failed', '%(count)s failed', files.failed), { count: fmtNum(files.failed) }, true));
    if (live(task)) {
      if (task.scanning) parts.push(gettext('still listing the files'));
      parts.push(speed(task.speed));
      const left = task.bytes.total - task.bytes.done;
      if (!task.scanning && task.speed > 0 && left > 0) parts.push(interpolate(gettext('about %(time)s left'), { time: duration(left / task.speed) }, true));
    } else {
      parts.push(interpolate(gettext('took %(time)s'), { time: duration(task.seconds) }, true));
      if (task.average_speed) parts.push(interpolate(gettext('%(speed)s on average'), { speed: speed(task.average_speed) }, true));
    }
    return parts.join(SEP);
  }

  function update(card, task) {
    card.task = task;
    card.badge.className = `badge ${BADGES[task.status] || ''}`.trim();
    card.badge.textContent = task.status_label;
    const routeKey = JSON.stringify([task.source, task.source_path, task.target, task.target_path]);
    if (routeKey !== card.routeKey) {
      card.routeKey = routeKey;
      card.route.replaceChildren(place(task.source, task.source_path), icon('arrow_forward', 'copy-arrow'), place(task.target, task.target_path));
    }
    card.when.textContent = fmtDateTime(task.created_at);
    card.stop.hidden = !task.can_stop;
    card.resume.hidden = !task.can_resume;
    setBar(card.bar, task.bytes.done, task.bytes.total, { spinning: live(task) && task.scanning && !task.bytes.total, full: task.status === 'done' });
    card.line.textContent = lineText(task);
    card.message.textContent = task.message;
    card.message.hidden = !task.message;
    card.message.classList.toggle('copy-task-error', task.status === 'failed');
    if (card.open) renderDetails(card);
  }

  function counted(files, bytes) {
    return interpolate(ngettext('%(count)s file, %(size)s', '%(count)s files, %(size)s', files), { count: fmtNum(files), size: fmtSize(bytes) }, true);
  }

  function renderDetails(card) {
    const task = card.task;
    const failed = [interpolate(npgettext('copy task', '%(count)s file', '%(count)s files', task.files.failed), { count: fmtNum(task.files.failed) }, true)];
    if (task.folders_failed) failed.push(interpolate(ngettext('%(count)s folder', '%(count)s folders', task.folders_failed), { count: fmtNum(task.folders_failed) }, true));
    card.facts.replaceChildren(...nodes(
      fact(gettext('Created at'), [fmtDateTime(task.created_at), task.created_by && task.created_by.username].filter(Boolean).join(SEP)),
      fact(gettext('Started at'), [fmtDateTime(task.started_at), task.run > 1 ? interpolate(gettext('run %(run)s'), { run: task.run }, true) : ''].filter(Boolean).join(SEP)),
      task.finished_at ? fact(gettext('Ended at'), fmtDateTime(task.finished_at)) : null,
      fact(gettext('Threads'), fmtNum(task.threads)),
      fact(gettext('Speed limit'), task.speed_limit ? speed(task.speed_limit) : gettext('No limit')),
      fact(gettext('Same name and size'), POLICY[task.same_file] || task.same_file),
      fact(gettext('Same name, other size'), POLICY[task.same_name] || task.same_name),
      fact(gettext('Retries'), fmtNum(task.retries)),
      fact(pgettext('copy task', 'Copied'), counted(task.files.copied, task.bytes.copied)),
      fact(gettext('Skipped'), counted(task.files.skipped, task.bytes.skipped)),
      fact(pgettext('copy task', 'Failed'), failed.join(SEP)),
      fact(gettext('Read from the source'), fmtSize(task.transferred))));
    const current = (card.detail && live(task) && card.detail.current) || [];
    card.currentTitle.hidden = card.current.hidden = !live(task);
    card.current.replaceChildren(...(current.length ? current.map(currentItem)
      : [h('li', { class: 'muted small copy-none', text: task.scanning ? gettext('Listing the files…') : gettext('No file at the moment.') })]));
    renderFailures(card);
  }

  function currentItem(file) {
    const bar = h('progress', { class: 'linear-progress', max: '1', 'aria-label': interpolate(gettext('Progress of %(name)s'), { name: file.path }, true) });
    setBar(bar, file.done, file.size || 0, { spinning: file.size === null });
    const parts = [ofTotal(file.done, file.size || 0), speed(file.speed)];
    if (file.attempt > 1) parts.push(interpolate(gettext('try %(n)s'), { n: file.attempt }, true));
    return h('li', { class: 'copy-file' },
      h('code', { text: file.path }), bar,
      h('div', { class: 'muted small', text: parts.join(SEP) }),
      file.error ? h('div', { class: 'copy-error', text: interpolate(gettext('The try before failed: %(error)s'), { error: file.error }, true) }) : null);
  }

  function renderFailures(card) {
    const state = card.failures;
    const shownCount = state ? state.items.length : 0;
    const total = state ? state.count : 0;
    card.failuresList.replaceChildren(...(state ? state.items.map(failureItem) : []));
    card.failuresTitle.hidden = !total && !(state && state.error);
    card.failuresStatus.textContent = state && state.error ? interpolate(gettext('Failed to load: %(message)s'), { message: state.error }, true)
      : total > shownCount ? interpolate(gettext('%(shown)s of %(count)s shown'), { shown: fmtNum(shownCount), count: fmtNum(total) }, true) : '';
    card.failuresStatus.hidden = !card.failuresStatus.textContent;
    card.more.hidden = !(state && state.next && !state.error);
    card.more.disabled = Boolean(state && state.loading);
  }

  function failureItem(failure) {
    const facts = [];
    if (failure.size !== null) facts.push(fmtSize(failure.size));
    if (failure.attempts > 1) facts.push(interpolate(ngettext('tried %(n)s time', 'tried %(n)s times', failure.attempts), { n: failure.attempts }, true));
    return h('li', { class: 'copy-failure' },
      icon(failure.is_dir ? 'folder' : 'draft'),
      h('div', { class: 'copy-failure-text' },
        h('code', { text: failure.is_dir ? `${failure.path}/` : failure.path }),
        facts.length ? h('span', { class: 'muted small', text: ` ${facts.join(SEP)}` }) : null,
        h('div', { class: 'copy-error', text: failure.message })));
  }

  function setOpen(card, open) {
    card.open = open;
    card.toggle.setAttribute('aria-expanded', String(open));
    card.toggle.replaceChildren(icon(open ? 'expand_less' : 'expand_more'));
    card.details.hidden = !open;
    if (open) {
      renderDetails(card);
      loadDetail(card);
    }
  }

  async function loadDetail(card) {
    try {
      const detail = await api('GET', `${API}${card.id}/`);
      card.detail = detail;
      if (!card.failures || card.failures.count !== detail.failures_count) loadFailures(card, { fresh: true });
      update(card, detail);
    } catch (err) {
      card.failures = { items: [], count: 0, next: null, error: err.message };
      renderFailures(card);
    }
  }

  // The failures, a page at a time; `fresh` starts again from the first page (more of them now).
  async function loadFailures(card, { fresh = false } = {}) {
    let state = card.failures;
    if (fresh || !state || state.error) {
      state = { items: [], count: state && !state.error ? state.count : 0, next: 1, loading: false };
      card.failures = state;
    }
    if (state.loading || !state.next) return;
    state.loading = true;
    renderFailures(card);
    try {
      const data = await api('GET', `${API}${card.id}/failures/?page=${state.next}`);
      state.items.push(...data.results);
      state.count = data.count;
      state.next = data.next ? state.next + 1 : null;
    } catch (err) {
      state.error = err.message;
    } finally {
      state.loading = false;
      if (card.failures === state) renderFailures(card);
    }
  }

  // ------------------------------------------------------------ the detailed log

  const KINDS = {
    started: [gettext('Began'), 'badge-info'],
    copied: [pgettext('copy task', 'Copied'), 'badge-ok'],
    skipped: [gettext('Skipped'), ''],
    kept: [gettext('Skipped'), ''],
    retry: [gettext('Retrying'), 'badge-warning'],
    failed: [pgettext('copy task', 'Failed'), 'badge-error'],
  };

  // 0.4 s, 12.5 s, then as duration() says
  const took = (seconds) => (seconds < 60 ? interpolate(gettext('%(s)s s'), { s: seconds.toFixed(1) }, true) : duration(seconds));

  function logItem(line) {
    let [label, badge] = KINDS[line.kind] || [line.kind, ''];
    let what;
    let error = '';
    const facts = [];
    if (line.kind === 'started') {
      what = line.run > 1 ? interpolate(gettext('Run %(run)s: resumed.'), { run: line.run }, true) : gettext('Run 1: the task was made.');
    } else if (line.kind === 'ended') {
      [label, badge] = [line.status_label, BADGES[line.status] || ''];
      what = interpolate(gettext('Run %(run)s ended.'), { run: line.run }, true);
      if (line.status === 'failed') error = line.message;
      else if (line.message) facts.push(line.message);
    } else {
      what = h('code', { text: line.is_dir ? `${line.path}/` : line.path });
      if (line.size !== null) facts.push(fmtSize(line.size));
      if (line.kind === 'copied') {
        facts.push(interpolate(gettext('took %(time)s'), { time: took(line.seconds || 0) }, true));
        if (line.seconds > 0 && line.size) facts.push(speed(line.size / line.seconds));
        if (line.attempts > 1) facts.push(interpolate(gettext('on try %(n)s'), { n: line.attempts }, true));
      } else if (line.kind === 'skipped') {
        facts.push(gettext('the target has a file of the same name and size'));
      } else if (line.kind === 'kept') {
        facts.push(gettext('the target has a file of the same name and another size, kept as it is'));
      } else if (line.kind === 'retry') {
        facts.push(interpolate(gettext('try %(n)s failed, the next in %(time)s'), { n: line.attempts, time: duration(line.seconds || 0) }, true));
        error = line.message;
      } else if (line.kind === 'failed') {
        if (line.attempts > 1) facts.push(interpolate(ngettext('tried %(n)s time', 'tried %(n)s times', line.attempts), { n: line.attempts }, true));
        error = line.message;
      }
    }
    return h('li', { class: 'copy-log-line', 'data-id': String(line.id) },
      h('time', { class: 'copy-log-time', datetime: line.at, text: fmtDateTime(line.at, { seconds: true }) }),
      h('span', { class: `badge ${badge}`.trim(), text: label }),
      h('div', { class: 'copy-log-text' },
        what, facts.length ? h('span', { class: 'muted small', text: ` ${facts.join(SEP)}` }) : null,
        error ? h('div', { class: 'copy-error', text: error }) : null));
  }

  // The task's log in a dialog, newest first: the files being copied, then the lines. While its run goes on the
  // dialog asks for the task (its files being copied) and the lines after the newest it has every POLL_MS; it shows the
  // newest page again when more came than a page holds. "Load earlier ones" goes back a page at a time.
  function openLog(card) {
    const id = card.id;
    const titleId = `copy-log-${id}-${Math.random().toString(36).slice(2)}`;
    const badge = h('span', { class: 'badge' });
    const route = h('div', { class: 'copy-task-route' });
    const currentTitle = h('h3', { text: gettext('Being copied'), hidden: true });
    const current = h('ul', { class: 'copy-files', hidden: true });
    const list = h('ol', { class: 'copy-log', 'aria-label': gettext('Detailed log') });
    const status = h('p', { class: 'muted small copy-log-status', role: 'status' });
    const more = h('button', { type: 'button', class: 'btn btn-small btn-text copy-more', hidden: true, text: gettext('Load earlier ones') });
    const dialog = h('dialog', { class: 'dialog dialog-wide copy-log-dialog', 'aria-labelledby': titleId },
      h('div', { class: 'dialog-box' },
        h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: interpolate(gettext('Detailed log of task #%(id)s'), { id }, true) }),
          h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
        h('div', { class: 'dialog-body' }, h('div', { class: 'copy-log-head' }, badge, route), currentTitle, current, list, status, more)));
    let newest = 0; // the ids of the newest and the oldest line on screen
    let oldest = 0;
    let earlier = false; // earlier lines were asked for: they stay on screen
    let following = live(card.task);
    let timer = null;
    let closed = false;

    const showTask = (task) => {
      badge.className = `badge ${BADGES[task.status] || ''}`.trim();
      badge.textContent = task.status_label;
      route.replaceChildren(place(task.source, task.source_path), icon('arrow_forward', 'copy-arrow'), place(task.target, task.target_path));
      const files = (live(task) && task.current) || [];
      currentTitle.hidden = current.hidden = !files.length;
      current.replaceChildren(...files.map(currentItem));
    };
    const showStatus = () => {
      status.textContent = list.children.length ? '' : gettext('No log: this task ran before the site kept detailed logs. Its next run will have one.');
    };
    const newestPage = (data) => {
      list.replaceChildren(...data.results.map(logItem));
      newest = data.results.length ? data.results[0].id : 0;
      oldest = data.results.length ? data.results[data.results.length - 1].id : 0;
      earlier = false;
      more.hidden = !data.more;
    };
    // While following a run, the newest LOG_KEEP lines stay on screen.
    const trim = () => {
      if (earlier || list.children.length <= LOG_KEEP) return;
      while (list.children.length > LOG_KEEP) list.lastElementChild.remove();
      oldest = Number(list.lastElementChild.dataset.id);
      more.hidden = false;
    };

    async function poll() {
      timer = null;
      try {
        const task = await api('GET', `${API}${id}/`);
        if (closed) return;
        showTask(task);
        following = live(task);
        const data = await api('GET', `${API}${id}/log/${newest ? `?after=${newest}` : ''}`);
        if (closed) return;
        if (!newest || data.more) newestPage(data);
        else if (data.results.length) {
          list.prepend(...data.results.map(logItem));
          newest = data.results[0].id;
          trim();
        }
        showStatus();
      } catch (err) {
        if (closed) return;
        status.textContent = interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true);
      }
      if (following && !closed) timer = setTimeout(poll, POLL_MS);
    }

    more.addEventListener('click', async () => {
      more.disabled = true;
      try {
        const data = await api('GET', `${API}${id}/log/?before=${oldest}`);
        if (closed) return;
        list.append(...data.results.map(logItem));
        if (data.results.length) oldest = data.results[data.results.length - 1].id;
        earlier = true;
        more.hidden = !data.more;
        showStatus();
      } catch (err) {
        if (!closed) status.textContent = interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true);
      } finally {
        more.disabled = false;
      }
    });
    dialog.addEventListener('close', () => {
      closed = true;
      clearTimeout(timer);
      dialog.remove();
    });
    document.body.append(dialog);
    showTask(card.task);
    openDialog(dialog);
    poll();
  }

  // ------------------------------------------------------------ the list

  function renderPager() {
    const pages = Math.max(1, Math.ceil(count / PAGE_SIZE));
    pager.hidden = pages <= 1;
    if (pages <= 1) {
      pager.replaceChildren();
      return;
    }
    pager.replaceChildren(
      h('button', { type: 'button', class: 'btn btn-small', disabled: page <= 1, onclick: () => go(page - 1) }, icon('chevron_left'), gettext('Previous page')),
      h('span', { class: 'pager-text', text: interpolate(ngettext('Page %(page)s of %(pages)s', 'Page %(page)s of %(pages)s', pages), { page, pages }, true) }),
      h('button', { type: 'button', class: 'btn btn-small pager-next', disabled: page >= pages, onclick: () => go(page + 1) }, gettext('Next page'), icon('chevron_right')));
  }

  function go(number) {
    page = number;
    load();
  }

  function render(results) {
    summary.textContent = count ? interpolate(ngettext('%(count)s task', '%(count)s tasks', count), { count: fmtNum(count) }, true) : '';
    const seen = new Set();
    const elements = results.map((task) => {
      seen.add(task.id);
      const card = cards.get(task.id) || makeCard(task.id);
      update(card, task);
      return card.el;
    });
    for (const id of [...cards.keys()]) {
      if (!seen.has(id)) cards.delete(id);
    }
    if (!elements.length) {
      root.replaceChildren(h('p', { class: 'muted copy-empty', text: gettext('No copy tasks yet. "New copy task" makes one.') }));
    } else if (elements.length !== root.children.length || elements.some((el, i) => root.children[i] !== el)) {
      root.replaceChildren(...elements); // only when they changed: a button with the focus would lose it
    }
    renderPager();
  }

  function schedule() {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (shown && [...cards.values()].some((card) => card.task && live(card.task))) pollTimer = setTimeout(load, POLL_MS);
  }

  async function load() {
    clearTimeout(pollTimer);
    if (document.hidden) return; // visibilitychange loads it again
    const mine = ++generation;
    refresh.disabled = true;
    try {
      const data = await api('GET', `${API}?${new URLSearchParams({ page, page_size: PAGE_SIZE })}`);
      if (mine !== generation) return;
      count = data.count;
      render(data.results);
      // An open card's details again while it runs, and once more when it has ended (its last files, its failures).
      const stale = (card) => !card.detail || live(card.task) || card.detail.status !== card.task.status || card.detail.run !== card.task.run;
      await Promise.all([...cards.values()].filter((card) => card.open && card.task && stale(card)).map(loadDetail));
    } catch (err) {
      if (mine !== generation) return;
      if (err.status === 404 && page > 1) { // that page is gone
        page = 1;
        load();
        return;
      }
      summary.textContent = interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true);
    } finally {
      if (mine === generation) {
        refresh.disabled = false;
        schedule();
      }
    }
  }

  // ------------------------------------------------------------ stopping and resuming

  async function stopTask(card) {
    const ok = await confirmDialog({
      title: gettext('Stop this copy task?'),
      message: gettext('The files copied so far stay in the target; those being copied now stop halfway. It can be resumed later.'),
      confirmText: gettext('Stop'), danger: true,
    });
    if (!ok) return;
    try {
      update(card, await api('POST', `${API}${card.id}/stop/`));
      toast(gettext('Stopping…'));
    } catch (err) {
      toast(err.message, 'error');
    }
    load();
  }

  async function resumeTask(card) {
    const ok = await confirmDialog({
      title: gettext('Resume this copy task?'),
      message: gettext('The source folder is listed again. The files that are in the target with the same name and size are skipped; the others are copied as the task says.'),
      confirmText: gettext('Resume'),
    });
    if (!ok) return;
    try {
      const task = await api('POST', `${API}${card.id}/resume/`);
      card.failures = null;
      card.detail = task;
      update(card, task);
      if (card.open) loadFailures(card, { fresh: true });
      toast(gettext('Copying again.'));
    } catch (err) {
      toast(err.message, 'error');
    }
    load();
  }

  // ------------------------------------------------------------ a new task

  // Picks a folder of `storage`, from `start` (or the nearest folder above it that is there); its path, or null.
  function pickFolder(storage, start, title) {
    return new Promise((resolve) => {
      let current = normal(start);
      let chosen = null;
      const titleId = `copy-picker-${Math.random().toString(36).slice(2)}`;
      const crumbs = h('ol', { class: 'breadcrumbs copy-picker-crumbs' });
      const list = h('ul', { class: 'list copy-picker-list', role: 'listbox', 'aria-label': pgettext('list of them', 'Folders') });
      const status = h('p', { class: 'muted copy-picker-status', role: 'status' });
      const confirm = h('button', { type: 'button', class: 'btn btn-filled', text: gettext('Choose this folder') });
      const dialog = h('dialog', { class: 'dialog dialog-small copy-picker', 'aria-labelledby': titleId },
        h('div', { class: 'dialog-box' },
          h('header', { class: 'dialog-head' }, h('h2', { id: titleId, text: title }),
            h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close') }, icon('close'))),
          h('div', { class: 'dialog-body' },
            h('p', { class: 'muted copy-picker-storage', text: storage.name }),
            h('nav', { 'aria-label': gettext('Folder') }, crumbs), list, status,
            h('div', { class: 'dialog-foot' }, h('button', { type: 'button', class: 'btn btn-text', 'data-close': '', text: gettext('Cancel') }), confirm))));
      const show = async (path) => {
        status.textContent = gettext('Loading…');
        list.replaceChildren();
        confirm.disabled = true;
        let data;
        try {
          data = await api('GET', `${API}folders/?${new URLSearchParams({ storage: storage.id, path })}`);
        } catch (err) {
          if (err.status === 404 && path !== '/') return show(parentOf(path)); // not there (yet): the folder above
          status.textContent = err.message;
          return undefined;
        }
        current = data.path;
        const parts = current.split('/').filter(Boolean);
        const steps = [{ label: gettext('Root folder'), path: '/' }, ...parts.map((part, i) => ({ label: part, path: `/${parts.slice(0, i + 1).join('/')}` }))];
        crumbs.replaceChildren(...steps.map((step, i) => h('li', null, i === steps.length - 1
          ? h('span', { class: 'breadcrumb', 'aria-current': 'page', text: step.label })
          : h('button', { type: 'button', class: 'breadcrumb', text: step.label, onclick: () => show(step.path) }))));
        list.replaceChildren(...data.folders.map((name) => {
          const row = h('li', { class: 'list-item interactive', role: 'option', tabindex: '0' },
            h('span', { class: 'list-leading' }, icon('folder')), h('span', { class: 'list-text' }, h('span', { class: 'list-headline', text: name })));
          const enter = () => show(`${current === '/' ? '' : current}/${name}`);
          row.addEventListener('click', enter);
          row.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              enter();
            }
          });
          return row;
        }));
        status.textContent = data.folders.length ? '' : gettext('No folders');
        confirm.disabled = false;
        return undefined;
      };
      confirm.addEventListener('click', () => {
        chosen = current;
        dialog.close();
      });
      dialog.addEventListener('close', () => {
        dialog.remove();
        resolve(chosen);
      });
      document.body.append(dialog);
      openDialog(dialog);
      show(current);
    });
  }

  function field(name, label, control, hint, labelFor = control.id) {
    return h('div', { class: 'field', 'data-field': name },
      h('div', { class: 'label-row' }, h('label', { for: labelFor, text: label }), hint ? h('span', { class: 'hint label-hint', text: hint }) : null),
      control);
  }

  function storageSelect(id, name, storages) {
    return h('select', { id, name, required: true },
      ...storages.map((storage) => h('option', { value: String(storage.id), text: interpolate(gettext('%(name)s (%(type)s)'), { name: storage.name, type: storage.type_label }, true) })));
  }

  function folderInput(id, name, pick) {
    const input = h('input', { id, name, value: '/', autocomplete: 'off', spellcheck: 'false', maxlength: '4096' });
    const browse = h('button', { type: 'button', class: 'icon-button field-button', 'aria-label': gettext('Choose a folder'), 'data-tooltip': gettext('Choose a folder') }, icon('folder_open'));
    browse.addEventListener('click', async () => {
      const picked = await pick(input.value);
      if (picked !== null) {
        input.value = picked;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
    return { input, box: h('span', { class: 'input-box' }, input, browse) };
  }

  function policyField(name, legend, hint, chosen) {
    return h('fieldset', { class: 'copy-policy', 'data-field': name },
      h('legend', { text: legend }),
      h('p', { class: 'hint', text: hint }),
      h('div', { class: 'copy-policy-options' },
        ...['skip', 'overwrite'].map((value) => h('label', { class: 'check-line' },
          h('input', { type: 'radio', name, value, checked: value === chosen }), POLICY[value]))));
  }

  // The answer's errors next to their fields; true when each found its field.
  function showErrors(body, data) {
    body.querySelectorAll('.field-error.copy-field-error').forEach((el) => el.remove());
    body.querySelectorAll('[data-field].invalid').forEach((el) => el.classList.remove('invalid'));
    if (!data || typeof data !== 'object' || Array.isArray(data) || typeof data.detail === 'string') return false;
    let all = true;
    for (const [name, messages] of Object.entries(data)) {
      const box = body.querySelector(`[data-field="${name}"]`);
      if (!box) {
        all = false;
        continue;
      }
      box.classList.add('invalid');
      box.append(h('p', { class: 'field-error copy-field-error', text: [].concat(messages).join(' ') }));
    }
    return all;
  }

  async function newTask() {
    let storages;
    try {
      storages = await api('GET', `${API}storages/`);
    } catch (err) {
      toast(err.message, 'error');
      return;
    }
    const writable = storages.filter((storage) => storage.writable);
    if (!storages.length || !writable.length) {
      toast(storages.length ? gettext('No enabled storage source can take files to copy into.') : gettext('There is no enabled storage source.'), 'error');
      return;
    }
    const byId = (id) => storages.find((storage) => String(storage.id) === String(id));
    const source = storageSelect('copy-source', 'source', storages);
    const target = storageSelect('copy-target', 'target', writable);
    if (writable.length > 1 && writable[0].id === storages[0].id) target.value = String(writable[1].id);
    const sourcePath = folderInput('copy-source-path', 'source_path', (start) => pickFolder(byId(source.value), start, gettext('Copy from which folder')));
    const targetPath = folderInput('copy-target-path', 'target_path', (start) => pickFolder(byId(target.value), start, gettext('Copy into which folder')));
    const number = (id, name, attrs) => h('input', { id, name, type: 'number', inputmode: 'numeric', required: true, ...attrs });
    const body = h('div', { class: 'copy-form' },
      h('p', { class: 'muted', text: gettext('The source folder is copied into the target folder with its files and folders: copying /a into /b makes /b/a. From the root folder, what is in it goes straight into the target folder. A target folder that isn\'t there yet is made.') }),
      h('div', { class: 'form-grid' },
        field('source', gettext('Copy from'), source),
        field('source_path', gettext('Source folder'), sourcePath.box, '', sourcePath.input.id),
        field('target', gettext('Copy into'), target),
        field('target_path', gettext('Target folder'), targetPath.box, gettext("Made when it isn't there"), targetPath.input.id),
        field('threads', gettext('Threads'), number('copy-threads', 'threads', { min: '1', max: String(THREADS_MAX), step: '1', value: '4' }), gettext('Files copied at the same time')),
        field('speed_limit', gettext('Speed limit (MB/s)'), number('copy-speed', 'speed_limit', { min: '0', max: String(SPEED_MAX_MB), step: '0.1', value: '0', inputmode: 'decimal' }), gettext('All threads together; 0: no limit')),
        field('retries', gettext('Retries'), number('copy-retries', 'retries', { min: '0', max: String(RETRIES_MAX), step: '1', value: '3' }), gettext('For a file that failed'))),
      policyField('same_file', gettext('A file of the same name and size in the target'), gettext('Taken for the same file.'), 'skip'),
      policyField('same_name', gettext('A file of the same name and another size in the target'), gettext('An older or other version of it.'), 'overwrite'));
    const created = await editDialog({
      title: gettext('New copy task'),
      body,
      wide: true,
      submitText: gettext('Start copying'),
      submitIcon: 'file_copy',
      save: async (fields) => {
        const whole = (value, min, max) => (/^\d+$/.test(value.trim()) && Number(value) >= min && Number(value) <= max ? Number(value) : null);
        const threads = whole(fields.threads.value, 1, THREADS_MAX);
        const retries = whole(fields.retries.value, 0, RETRIES_MAX);
        // A number box gives '' for what isn't a number ("5MB"): that is an error, not "no limit".
        const mbps = fields.speed_limit.validity.badInput ? NaN : fields.speed_limit.value.trim() === '' ? 0 : Number(fields.speed_limit.value);
        const errors = {};
        if (threads === null) errors.threads = interpolate(gettext('A whole number from 1 to %(max)s.'), { max: THREADS_MAX }, true);
        if (retries === null) errors.retries = interpolate(gettext('A whole number from 0 to %(max)s.'), { max: RETRIES_MAX }, true);
        if (!(mbps >= 0 && mbps <= SPEED_MAX_MB)) errors.speed_limit = interpolate(gettext('A number from 0 (no limit) to %(max)s.'), { max: SPEED_MAX_MB }, true);
        if (Object.keys(errors).length) {
          showErrors(body, errors);
          return undefined;
        }
        showErrors(body, null);
        try {
          return await api('POST', API, {
            source: Number(fields.source.value), source_path: fields.source_path.value,
            target: Number(fields.target.value), target_path: fields.target_path.value,
            threads, retries, speed_limit: Math.round(mbps * MB),
            same_file: fields.same_file.value, same_name: fields.same_name.value,
          });
        } catch (err) {
          if (err.status === 400 && showErrors(body, err.data)) return undefined;
          throw new Error(err.data ? errorText(err.data, err.status) : err.message);
        }
      },
    });
    if (!created) return;
    toast(gettext('The copy task has started.'));
    page = 1;
    const card = cards.get(created.id) || makeCard(created.id);
    card.detail = created;
    update(card, created);
    setOpen(card, true);
    load();
  }

  // ------------------------------------------------------------ start

  refresh.addEventListener('click', () => load());
  document.getElementById('copy-new').addEventListener('click', newTask);
  document.addEventListener('visibilitychange', () => {
    if (shown && !document.hidden) load();
  });
  // Loads each time the section is shown (manage.js announces each one); asks no more once another one is.
  document.addEventListener('manage:show', (e) => {
    shown = e.detail === 'copy-tasks';
    if (shown) load();
    else clearTimeout(pollTimer);
  });
})();
