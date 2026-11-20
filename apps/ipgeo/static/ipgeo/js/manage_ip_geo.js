/* /manage/#ip-geo: IP 归属地 (GET and PATCH /api/v1/manage/ip-geo/). Turning it on, downloading or updating the
 * database, the city one or the country one (POST download/ {edition}), looking one IP up (check/), and 补全:
 * filling in the locations of IPs stored before (POST fill/). Downloads and 补全 run on the server in the background; the page asks job/ every second until
 * the job ends. What changes the locations the lists show (turning it on or off, a download, a 补全) is
 * announced as "hfile:ip-locations": the user list and the logs load again when next shown. */
(function () {
  'use strict';
  const { api, h, toast, fmtNum, fmtSize, fmtDate, fmtDateTime, handleSubmit } = window.App;

  const API = '/api/v1/manage/ip-geo/';
  const POLL_MS = 1000;
  const RETRY_MS = 5000;

  const root = document.getElementById('ip-geo');
  if (!root) return;
  const errorBox = document.getElementById('ip-geo-error');
  const enabledBox = document.getElementById('ip-geo-enabled');
  const downloadForm = document.getElementById('ip-geo-download');
  const downloadButton = downloadForm.querySelector('[type="submit"]');
  const editionsBox = document.getElementById('ip-geo-editions');
  const switchNote = document.getElementById('ip-geo-switch');
  const facts = document.getElementById('ip-geo-facts');
  const missing = document.getElementById('ip-geo-missing');
  const checkForm = document.getElementById('ip-geo-check-form');
  const checkResult = document.getElementById('ip-geo-check-result');
  const fillForm = document.getElementById('ip-geo-fill-form');
  const unfilledText = document.getElementById('ip-geo-unfilled');
  const progressBox = { download: document.getElementById('ip-geo-download-progress'), fill: document.getElementById('ip-geo-fill-progress') };
  const resultBox = { download: document.getElementById('ip-geo-download-result'), fill: document.getElementById('ip-geo-fill-result') };

  let data = null; // the answer of GET manage/ip-geo/
  let loading = null;
  let watching = null; // the job this page started, or found running: its result is shown when it ends
  let pollTimer = null;
  let picked = null; // the database chosen here (sources.EDITIONS); until then the one in use, or the city one

  const fact = (label, ...value) => h('div', {}, h('dt', { text: label }), h('dd', {}, ...value));
  const wideFact = (label, ...value) => h('div', { class: 'wide' }, h('dt', { text: label }), h('dd', {}, ...value));
  const running = () => Boolean(data && data.job && data.job.state === 'running');
  const usable = () => Boolean(data && data.enabled && data.database && !data.database.missing);
  const showError = (message) => { errorBox.textContent = message || ''; errorBox.hidden = !message; };
  const locationsChanged = () => document.dispatchEvent(new CustomEvent('hfile:ip-locations'));

  // ------------------------------------------------------------ showing the state

  function renderFacts() {
    const db = data.database;
    if (!db) {
      facts.replaceChildren(fact(gettext('Database in use'), h('span', { class: 'muted', text: gettext('None downloaded yet') })));
    } else {
      facts.replaceChildren(
        fact(gettext('Database in use'), db.file),
        fact(gettext('Data date'), db.built_at ? fmtDate(db.built_at) : pgettext('date', 'unknown')),
        fact(gettext('File size'), db.size ? fmtSize(db.size) : pgettext('size', 'unknown')),
        fact(gettext('Downloaded at'), fmtDateTime(db.downloaded_at)),
        wideFact(gettext('Folder'), h('code', { class: 'backup-path', text: data.folder })));
    }
    missing.textContent = db && db.missing ? interpolate(gettext("The database file is gone (it should be in %(folder)s). Download it again."), { folder: data.folder }, true) : '';
    missing.hidden = !missing.textContent;
  }

  // The database to download: the button updates the one in use, or downloads the other one in its place.
  const chosen = () => downloadForm.elements.edition.value;

  function renderEdition() {
    const db = data.database;
    const inUse = db && !db.missing ? db.edition : null;
    downloadForm.elements.edition.value = picked || (db && db.edition) || 'city';
    switchNote.hidden = !inUse || chosen() === inUse;
    downloadButton.querySelector('span').textContent = chosen() === inUse ? gettext('Update the database') : gettext('Download the database');
  }

  const PARTS = {
    users: (n) => interpolate(ngettext('the last login IP of %(count)s user', 'the last login IPs of %(count)s users', n), { count: fmtNum(n) }, true),
    activity: (n) => interpolate(ngettext('%(count)s activity log entry', '%(count)s activity log entries', n), { count: fmtNum(n) }, true),
    downloads: (n) => interpolate(ngettext('%(count)s download log entry', '%(count)s download log entries', n), { count: fmtNum(n) }, true),
  };

  function renderUnfilled() {
    const { total, parts } = data.unfilled;
    if (!total) {
      unfilledText.textContent = gettext('Every IP recorded has a location.');
      return;
    }
    const counts = parts.filter((part) => part.count).map((part) => (PARTS[part.key] ? PARTS[part.key](part.count) : `${part.label} ${fmtNum(part.count)}`));
    unfilledText.textContent = interpolate(ngettext('%(count)s record has no location yet: %(parts)s.', '%(count)s records have no location yet: %(parts)s.', total),
      { count: fmtNum(total), parts: counts.join(gettext(', ')) }, true);
  }

  function progressText(job) {
    if (job.kind === 'download') {
      if (!job.done) return gettext('Connecting to the download address…');
      return job.total
        ? interpolate(gettext('Downloading: %(done)s of %(total)s'), { done: fmtSize(job.done), total: fmtSize(job.total) }, true)
        : interpolate(gettext('Downloading: %(done)s'), { done: fmtSize(job.done) }, true);
    }
    return job.total
      ? interpolate(gettext('Filling in: %(done)s of %(total)s IPs looked up'), { done: fmtNum(job.done), total: fmtNum(job.total) }, true)
      : gettext('Finding the records without a location…');
  }

  function renderJob() {
    const job = data.job;
    for (const [kind, box] of Object.entries(progressBox)) {
      const active = Boolean(job && job.state === 'running' && job.kind === kind);
      box.hidden = !active;
      if (!active) continue;
      const bar = box.querySelector('progress');
      if (job.total) {
        bar.max = job.total;
        bar.value = Math.min(job.done, job.total);
      } else {
        bar.removeAttribute('value'); // a bar that moves along: how much is left isn't known
      }
      box.querySelector('.hint').textContent = progressText(job);
    }
  }

  function render() {
    const busy = running();
    enabledBox.checked = data.enabled;
    enabledBox.disabled = false;
    renderFacts();
    downloadButton.disabled = !data.enabled || busy;
    editionsBox.disabled = !data.enabled || busy;
    renderEdition();
    checkForm.elements.ip.disabled = !usable();
    checkForm.querySelector('[type="submit"]').disabled = !usable();
    fillForm.elements.redo.disabled = !usable() || busy;
    fillForm.querySelector('[type="submit"]').disabled = !usable() || busy;
    renderUnfilled();
    renderJob();
  }

  function showResult(job, { when = false } = {}) {
    const box = resultBox[job.kind];
    if (!box) return;
    const prefix = when ? interpolate(gettext('%(job)s failed (%(time)s): '), { job: job.kind_label, time: fmtDateTime(job.finished_at || job.started_at) }, true) : '';
    box.className = job.state === 'done' ? 'notice notice-ok' : 'notice notice-error';
    box.textContent = prefix + job.message;
    box.hidden = false;
  }

  function clearResults() {
    for (const box of Object.values(resultBox)) box.hidden = true;
  }

  // ------------------------------------------------------------ following a job

  function poll(delay = POLL_MS) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      let job;
      try {
        ({ job } = await api('GET', `${API}job/`));
      } catch (err) {
        poll(RETRY_MS); // the server may be restarting: keep asking, more slowly
        return;
      }
      if (job && job.state === 'running') {
        data.job = job;
        renderJob();
        poll();
        return;
      }
      await ended(job);
    }, delay);
  }

  // The job ended: load everything it may have changed, then say how it went.
  async function ended(job) {
    try {
      data = await api('GET', API);
    } catch (err) {
      poll(RETRY_MS);
      return;
    }
    render();
    if (job && job.id === watching) showResult(job);
    watching = null;
    locationsChanged();
  }

  function follow() {
    watching = data.job && data.job.id;
    render();
    poll();
  }

  // ------------------------------------------------------------ actions

  enabledBox.addEventListener('change', async () => {
    const want = enabledBox.checked;
    enabledBox.disabled = true;
    showError('');
    try {
      data = await api('PATCH', API, { enabled: want });
      render();
      locationsChanged();
      if (!want) toast(gettext('IP location is off'));
      else toast(data.database ? gettext('IP location is on') : gettext('IP location is on; now download a database'));
    } catch (err) {
      enabledBox.checked = !want;
      showError(err.message);
    } finally {
      enabledBox.disabled = false;
    }
  });

  editionsBox.addEventListener('change', () => {
    picked = chosen();
    renderEdition();
  });

  handleSubmit(downloadForm, async () => {
    clearResults();
    data = await api('POST', `${API}download/`, { edition: chosen() });
    follow();
    return true; // render() decides when the button comes back
  });

  handleSubmit(checkForm, async (f) => {
    checkResult.hidden = true;
    const ip = f.ip.value.trim();
    if (!ip) throw new Error(gettext('Write an IP address first.'));
    const answer = await api('GET', `${API}check/?ip=${encodeURIComponent(ip)}`);
    checkResult.textContent = interpolate(gettext('%(ip)s: %(location)s'), { ip: answer.ip, location: answer.location }, true);
    checkResult.hidden = false;
  });

  handleSubmit(fillForm, async (f) => {
    clearResults();
    data = await api('POST', `${API}fill/`, { redo: f.redo.checked });
    follow();
    return true;
  });

  // ------------------------------------------------------------ loading

  let first = true;

  async function load() {
    try {
      data = await api('GET', API);
    } catch (err) {
      showError(interpolate(gettext('Failed to load: %(message)s'), { message: err.message }, true));
      return;
    }
    showError('');
    if (first) {
      first = false;
      if (data.job && data.job.state === 'failed') showResult(data.job, { when: true }); // nobody may have seen it
    }
    if (running()) follow();
    else render();
  }

  // Loads each time the section is shown (manage.js announces each one), unless a job is being followed
  // (that keeps it up to date).
  document.addEventListener('manage:show', (e) => {
    if (e.detail !== 'ip-geo' || loading || pollTimer) return;
    loading = load().finally(() => { loading = null; });
  });
})();
