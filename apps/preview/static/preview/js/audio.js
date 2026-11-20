/* The audio player: a bar at the bottom of the page that stays while one browses on. Its
 * playlist is the folder's audio files, starting at the clicked one, and is replaced when a file of
 * another folder is played. 播放顺序: in turn or random; 循环模式: all, one or none (kept per browser).
 * The browser's own <audio>, preload="none"; the system's media keys through the Media Session API. */
(function () {
  'use strict';

  const P = App.preview;
  const { h } = App;
  const ORDER_ICONS = { list: 'repeat', random: 'shuffle' };
  const REPEAT_ICONS = { all: 'repeat', one: 'repeat_one', none: 'arrow_forward' };
  const ORDERS = { list: gettext('In order'), random: gettext('Shuffle') };
  const REPEATS = { all: gettext('Repeat all'), one: gettext('Repeat one'), none: gettext('No repeat') };

  let bar = null;

  function time(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const s = Math.floor(seconds % 60);
    const m = Math.floor(seconds / 60) % 60;
    const hours = Math.floor(seconds / 3600);
    return `${hours ? `${hours}:${String(m).padStart(2, '0')}` : m}:${String(s).padStart(2, '0')}`;
  }

  function build() {
    const audio = h('audio', { preload: 'none' });
    const title = h('span', { class: 'ab-title' });
    const clock = h('span', { class: 'ab-time' });
    const seek = h('input', { type: 'range', class: 'ab-seek', min: '0', max: '1000', value: '0', step: '1', 'aria-label': gettext('Playback position') });
    const playButton = P.button(App.icon('play_arrow'), gettext('Play'), () => (audio.paused ? audio.play().catch(failed) : audio.pause()));
    const orderButton = P.button(App.icon('repeat'), '', () => setOrder(state.order === 'list' ? 'random' : 'list'));
    const repeatButton = P.button(App.icon('repeat'), '', () => setRepeat({ all: 'one', one: 'none', none: 'all' }[state.repeat]));
    const listButton = P.button(App.icon('queue_music'), gettext('Playlist'), () => toggleList(), { 'aria-expanded': 'false' });
    const list = h('ol', { class: 'list ab-list', 'aria-label': gettext('Playlist') });
    const listPanel = h('div', { class: 'ab-panel', hidden: true }, h('h3', { class: 'ab-panel-title', text: gettext('Playlist') }), list);
    const root = h('section', { class: 'audio-bar', 'aria-label': gettext('Music player') },
      audio,
      listPanel,
      h('div', { class: 'ab-row' },
        h('span', { class: 'ab-art', 'aria-hidden': 'true' }, App.icon('music_note')),
        h('div', { class: 'ab-info' }, title, h('div', { class: 'ab-progress' }, seek, clock)),
        h('div', { class: 'ab-buttons' },
          P.button(App.icon('skip_previous'), pgettext('track', 'Previous'), () => step(-1, false)),
          playButton,
          P.button(App.icon('skip_next'), pgettext('track', 'Next'), () => step(1, false)),
          orderButton, repeatButton, listButton,
          P.button(App.icon('close'), gettext('Close the player'), () => close()))));
    let sizer = null;
    const state = {
      list: [], index: 0, ctx: null, folder: '', started: false, renewed: 0,
      order: P.store.get('audio-order', 'list'),
      repeat: P.store.get('audio-repeat', 'all'),
      seeking: false,
    };
    if (!ORDERS[state.order]) state.order = 'list';
    if (!REPEATS[state.repeat]) state.repeat = 'all';

    function failed() {
      // play() was refused (no click yet) or the file can't play: the error event says the latter
    }
    function setOrder(order) {
      state.order = order;
      P.store.set('audio-order', order);
      orderButton.replaceChildren(App.icon(ORDER_ICONS[order]));
      orderButton.setAttribute('aria-label', interpolate(gettext('Play order: %(order)s'), { order: ORDERS[order] }, true));
      orderButton.dataset.tooltip = interpolate(gettext('Play order: %(order)s'), { order: ORDERS[order] }, true);
      orderButton.setAttribute('aria-pressed', String(order === 'random'));
    }
    function setRepeat(repeat) {
      state.repeat = repeat;
      P.store.set('audio-repeat', repeat);
      repeatButton.replaceChildren(App.icon(REPEAT_ICONS[repeat]));
      repeatButton.setAttribute('aria-label', interpolate(gettext('Repeat: %(mode)s'), { mode: REPEATS[repeat] }, true));
      repeatButton.dataset.tooltip = interpolate(gettext('Repeat: %(mode)s'), { mode: REPEATS[repeat] }, true);
      repeatButton.setAttribute('aria-pressed', String(repeat !== 'none'));
    }
    function toggleList(shown = listPanel.hidden) {
      listPanel.hidden = !shown;
      listButton.setAttribute('aria-expanded', String(shown));
      if (shown) {
        const current = list.children[state.index];
        if (current) current.scrollIntoView({ block: 'nearest' });
      }
    }
    function drawList() {
      list.replaceChildren(...state.list.map((entry, i) => h('li', null, h('button', {
        type: 'button', class: `list-item interactive ab-item${i === state.index ? ' selected' : ''}`,
        'aria-current': i === state.index ? 'true' : null, onclick: () => start(i),
      }, h('span', { class: 'list-leading' }, App.icon(i === state.index ? 'play_arrow' : 'music_note')),
      h('span', { class: 'list-text' }, h('span', { class: 'list-headline', text: entry.name }))))));
    }
    function start(i) {
      state.index = i;
      const entry = state.list[i];
      title.textContent = entry.name;
      title.title = entry.name;
      root.classList.remove('ab-failed');
      drawList();
      if (!entry.url) {
        audio.pause();
        audio.removeAttribute('src');
        title.textContent = interpolate(gettext('%(name)s (no permission to play)'), { name: entry.name }, true);
        return;
      }
      state.started = false;
      audio.src = entry.url;
      audio.play().catch(failed);
      if ('mediaSession' in navigator && window.MediaMetadata) {
        navigator.mediaSession.metadata = new MediaMetadata({ title: entry.name, album: state.folder || (state.ctx.storage && state.ctx.storage.name) || '' });
      }
    }
    // The next one: by the order; at the end of the list per the repeat mode (auto: it ended by itself).
    function step(direction, auto) {
      const count = state.list.length;
      if (!count) return;
      if (auto && state.repeat === 'one') {
        audio.currentTime = 0;
        audio.play().catch(failed);
        return;
      }
      if (direction < 0 && audio.currentTime > 3) {
        audio.currentTime = 0;
        return;
      }
      let next;
      if (state.order === 'random' && count > 1) {
        do next = Math.floor(Math.random() * count); while (next === state.index);
      } else {
        next = state.index + direction;
        if (next >= count || next < 0) {
          if (auto && state.repeat === 'none') {
            audio.pause();
            return;
          }
          next = (next + count) % count;
        }
      }
      start(next);
    }
    function close() {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
      if (sizer) sizer.disconnect();
      document.documentElement.style.setProperty('--audio-bar-height', '0px');
      root.remove();
      document.body.classList.remove('has-audio-bar');
      bar = null;
    }

    audio.addEventListener('play', () => {
      playButton.replaceChildren(App.icon('pause'));
      playButton.setAttribute('aria-label', gettext('Pause'));
      playButton.dataset.tooltip = gettext('Pause');
      document.dispatchEvent(new CustomEvent('hfile:media-play', { detail: { source: 'audio' } }));
    });
    audio.addEventListener('pause', () => {
      playButton.replaceChildren(App.icon('play_arrow'));
      playButton.setAttribute('aria-label', gettext('Play'));
      playButton.dataset.tooltip = gettext('Play');
    });
    audio.addEventListener('timeupdate', () => {
      if (!state.seeking && audio.duration) seek.value = String(Math.round((audio.currentTime / audio.duration) * 1000));
      clock.textContent = `${time(audio.currentTime)} / ${time(audio.duration)}`;
    });
    audio.addEventListener('loadedmetadata', () => { clock.textContent = `0:00 / ${time(audio.duration)}`; });
    audio.addEventListener('ended', () => step(1, true));
    audio.addEventListener('playing', () => { state.started = true; });
    audio.addEventListener('error', async () => {
      if (!audio.getAttribute('src')) return;
      // It played, then failed: most likely its signed URL ended. A fresh one, once a minute at most.
      if (state.started && Date.now() - state.renewed > 60_000) {
        state.renewed = Date.now();
        const entry = state.list[state.index];
        const at = audio.currentTime;
        try {
          const fresh = await state.ctx.inlineUrl(entry, { fresh: true });
          if (state.list[state.index] !== entry) return;
          audio.src = fresh;
          audio.currentTime = at;
          audio.play().catch(failed);
          return;
        } catch (err) {
          // as below
        }
      }
      root.classList.add('ab-failed');
      App.toast(interpolate(gettext("%(name)s can't be played: the browser may not support this audio format."), { name: state.list[state.index].name }, true), 'error');
    });
    seek.addEventListener('input', () => {
      state.seeking = true;
      if (audio.duration) clock.textContent = `${time((seek.value / 1000) * audio.duration)} / ${time(audio.duration)}`;
    });
    seek.addEventListener('change', () => {
      state.seeking = false;
      if (audio.duration) audio.currentTime = (seek.value / 1000) * audio.duration;
    });
    document.addEventListener('hfile:media-play', (e) => {
      if (e.detail && e.detail.source !== 'audio') audio.pause();
    });
    if ('mediaSession' in navigator) {
      const handlers = { play: () => audio.play().catch(failed), pause: () => audio.pause(), previoustrack: () => step(-1, false), nexttrack: () => step(1, false) };
      for (const [name, handler] of Object.entries(handlers)) {
        try {
          navigator.mediaSession.setActionHandler(name, handler);
        } catch (e) {
          // not supported here
        }
      }
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !listPanel.hidden && root.contains(document.activeElement)) toggleList(false);
    });
    setOrder(state.order);
    setRepeat(state.repeat);
    document.body.append(root);
    document.body.classList.add('has-audio-bar');
    // The upload panel and the snackbars go above the bar (uploads.css, preview.css): they need its height.
    sizer = window.ResizeObserver ? new ResizeObserver(() => {
      document.documentElement.style.setProperty('--audio-bar-height', `${root.offsetHeight}px`);
    }) : null;
    if (sizer) sizer.observe(root);
    return {
      play(ctx, item, siblings) {
        let entries = P.ofKind(siblings, ['audio']);
        if (!entries.some((other) => P.sameItem(other, item))) entries = [item];
        state.list = entries;
        state.ctx = ctx;
        state.folder = String(ctx.path || '').split('/').filter(Boolean).pop() || '';
        start(entries.findIndex((other) => P.sameItem(other, item)));
      },
    };
  }

  function open(ctx, item, siblings) {
    if (!bar) bar = build();
    bar.play(ctx, item, siblings || []);
  }

  P.register(['audio'], { open }, 'audio');
})();
