/* The video player (decision D21, inventory 7.Z.4 (b)): the browser's own <video>, with hls.js for .m3u8.
 *
 * - m3u8: the playlist is read from ctx.inlineUrl(item). hls.js resolves the playlist's relative
 *   entries against its URL; every request for a file of the same folder (segments, keys, other
 *   playlists) goes to that sibling's own URL instead (a same-origin one, else its inline URL, renewed
 *   when it is about to end), so a playlist plays from a storage whose links are signed one by one. Anything
 *   else a playlist names (another folder, another site, this site's other addresses) is refused: a
 *   stored playlist makes the visitor's browser fetch nothing but files of its own folder.
 * - a video whose signed URL ends while it plays gets a fresh one (once a minute) and goes on there;
 * - the folder's videos as a playlist, 上一个 / 下一个, 自动播放 and 自动播放下一个 (kept per browser);
 * - subtitles: siblings named <name>.vtt|srt, <base>.vtt|srt or <base>.<label>.vtt|srt; .srt becomes
 *   WebVTT here (srtToVtt), in a blob: URL; a selector with 关闭字幕;
 * - the external apps: 下载, 迅雷 and Motrix with the download permission; PotPlayer, IINA, VLC, nPlayer,
 *   MX Player. The bar can be hidden.
 * No flv, .ass or danmaku. */
(function () {
  'use strict';

  const P = App.preview;
  const { h } = App;
  const SUBTITLE_MAX = 5 * 1024 * 1024;
  const PLAYER_ICONS = { download: 'download', thunder: 'download', motrix: 'download', play: 'play_circle' };

  const isPlaylist = (item) => P.suffix(item.name) === 'm3u8';
  const stem = (name) => name.replace(/\.[^.]*$/, '');

  // ------------------------------------------------------------------ subtitles

  // A WebVTT file as its cues only: STYLE, REGION and NOTE blocks go (a STYLE block's CSS could load
  // images from anywhere), cue settings other than the layout ones too.
  function cleanVtt(text) {
    const timing = /^\s*((?:\d+:)?\d{2}:\d{2}\.\d{3})\s+-->\s+((?:\d+:)?\d{2}:\d{2}\.\d{3})(.*)$/;
    const cues = [];
    for (const block of String(text).replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').split(/\n{2,}/)) {
      const lines = block.split('\n');
      const at = lines.findIndex((line) => timing.test(line));
      if (at < 0) continue;
      const m = timing.exec(lines[at]);
      const settings = (m[3].match(/\b(?:vertical|line|position|size|align):[\w.%,-]+/g) || []).join(' ');
      const body = lines.slice(at + 1).map((line) => line.replace(/-->/g, '->')).filter((line) => line.trim() !== '');
      if (body.length) cues.push(`${m[1]} --> ${m[2]}${settings ? ` ${settings}` : ''}\n${body.join('\n')}`);
    }
    return `WEBVTT\n\n${cues.join('\n\n')}\n`;
  }

  // SubRip as WebVTT: "00:01:02,500 --> 00:01:04,000" becomes "00:01:02.500 --> 00:01:04.000"; the cue
  // numbers and SubRip's X1/Y1 positions go, and a "-->" in the text (which WebVTT would read as
  // timing) becomes "->". The cue text keeps SubRip's <i>, <b>, <u>: WebVTT reads them the same way.
  function srtToVtt(text) {
    const timing = /^\s*((?:\d+:)?\d{1,2}:\d{1,2})[,.](\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{1,2})[,.](\d{1,3})/;
    const time = (hms, ms) => {
      const parts = hms.split(':').map((part) => part.padStart(2, '0'));
      if (parts.length === 2) parts.unshift('00');
      return `${parts.join(':')}.${ms.padEnd(3, '0')}`;
    };
    const cues = [];
    const blocks = String(text).replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').split(/\n{2,}/);
    for (const block of blocks) {
      const lines = block.split('\n').filter((line, i, all) => !(i === all.length - 1 && line === ''));
      const at = lines.findIndex((line) => timing.test(line));
      if (at < 0) continue;
      const m = timing.exec(lines[at]);
      const body = lines.slice(at + 1).map((line) => line.replace(/-->/g, '->')).filter((line) => line.trim() !== '');
      if (!body.length) continue;
      cues.push(`${time(m[1], m[2])} --> ${time(m[3], m[4])}\n${body.join('\n')}`);
    }
    return `WEBVTT\n\n${cues.join('\n\n')}\n`;
  }

  // The subtitle files of a video among its siblings: [{item, label}].
  function subtitlesOf(video, siblings) {
    const name = video.name;
    const base = stem(name);
    const found = [];
    for (const sibling of siblings) {
      if (!P.isFile(sibling)) continue;
      const m = /^(.*)\.(vtt|srt)$/i.exec(sibling.name);
      if (!m) continue;
      if (m[1] === name || m[1] === base || m[1].startsWith(`${base}.`)) found.push({ item: sibling, label: sibling.name });
    }
    return found;
  }

  // ------------------------------------------------------------------ hls.js

  // A loader for hls.js: the playlist itself, and files of its folder at their own URLs; nothing else.
  // A sibling's link is asked for again a minute before it ends (a quarter of its time for short ones).
  const renewAt = (url) => {
    const ends = P.expiresAt(url);
    return ends - Math.min(60 * 1000, (ends - Date.now()) / 4);
  };
  function siblingLoader(Hls, ctx, siblings, source, onRefused) {
    const Base = Hls.DefaultConfig.loader;
    const byName = new Map(siblings.filter(P.isFile).map((item) => [item.name, item]));
    const resolved = new Map(); // name -> {url: Promise, renew: when to ask again}
    // The URLs handed to hls.js, without the _HLS_ parameters it may add (low-latency playlists).
    const key = (url) => {
      const parsed = new URL(url, location.href);
      [...parsed.searchParams.keys()].filter((name) => name.startsWith('_HLS_')).forEach((name) => parsed.searchParams.delete(name));
      return parsed.href;
    };
    const allowed = new Set([key(source)]);
    const sourcePath = new URL(source, location.href).pathname;
    const folder = sourcePath.slice(0, sourcePath.lastIndexOf('/') + 1); // as hls.js resolves against it
    const refused = (url) => Promise.reject(Object.assign(new Error(gettext('The m3u8 refers to addresses outside this folder.')), { refused: url }));
    function own(url) {
      let parsed;
      try {
        parsed = new URL(url, location.href);
        if (allowed.has(key(url))) return Promise.resolve(url); // the playlist, or a URL handed out below
      } catch (e) {
        return refused(url);
      }
      const slash = parsed.pathname.lastIndexOf('/');
      if (parsed.origin !== location.origin || parsed.search || parsed.pathname.slice(0, slash + 1) !== folder) return refused(url);
      let name;
      try {
        name = decodeURIComponent(parsed.pathname.slice(slash + 1));
      } catch (e) {
        return refused(url);
      }
      const sibling = byName.get(name);
      if (!sibling) return refused(url);
      let entry = resolved.get(name);
      if (!entry || entry.renew < Date.now()) {
        const listed = !entry && sibling.url && P.sameOrigin(sibling.url) && renewAt(sibling.url) > Date.now();
        const fresh = listed ? Promise.resolve(sibling.url) : ctx.inlineUrl(sibling);
        const current = { renew: Infinity, url: null };
        current.url = fresh.then((signed) => {
          allowed.add(key(signed));
          current.renew = renewAt(signed);
          return signed;
        }, (err) => {
          if (resolved.get(name) === current) resolved.delete(name); // asked again next time
          throw err;
        });
        resolved.set(name, current);
        entry = current;
      }
      return entry.url;
    }
    return class SiblingLoader extends Base {
      load(context, loaderConfig, callbacks) {
        this.hfileAborted = false;
        own(context.url).then((url) => {
          if (this.hfileAborted) return;
          context.url = url;
          super.load(context, loaderConfig, callbacks);
        }, (err) => {
          if (this.hfileAborted) return;
          // A refused address ends the playback at once (hls.js would retry it for half a minute).
          if (err.refused !== undefined) onRefused(String(err.refused));
          else callbacks.onError({ code: 403, text: err.message || String(err) }, context, null, this.stats);
        });
      }
      abort() {
        this.hfileAborted = true;
        super.abort();
      }
      destroy() {
        this.hfileAborted = true;
        super.destroy();
      }
    };
  }

  // ------------------------------------------------------------------ external apps

  function appLinks(ctx, video) {
    // The address another program opens: the site's own (ctx.absoluteUrl: [site] url when it is set),
    // as files' 复制下载链接 gives it.
    const url = typeof ctx.absoluteUrl === 'function' ? ctx.absoluteUrl(video.url) : P.absolute(video.url);
    const name = video.name;
    const links = [];
    if (P.can(ctx, 'download')) {
      links.push(
        { label: gettext('Download'), icon: 'download', run: () => P.download(ctx, video) },
        { label: gettext('Thunder'), icon: 'thunder', href: `thunder://${btoa(`AA${url}ZZ`)}` },
        { label: 'Motrix', icon: 'motrix', href: `motrix://new-task?uri=${encodeURIComponent(url)}&out=${encodeURIComponent(name)}` });
    }
    links.push(
      { label: 'PotPlayer', icon: 'play', href: `potplayer://${url}` },
      { label: 'IINA', icon: 'play', href: `iina://weblink?url=${encodeURIComponent(url)}` },
      { label: 'VLC', icon: 'play', href: `vlc://${url}` },
      { label: 'nPlayer', icon: 'play', href: `nplayer-${url}` },
      { label: 'MX Player', icon: 'play', href: `intent:${url}#Intent;package=com.mxtech.videoplayer.ad;S.title=${encodeURIComponent(name)};end` },
      { label: 'MX Player Pro', icon: 'play', href: `intent:${url}#Intent;package=com.mxtech.videoplayer.pro;S.title=${encodeURIComponent(name)};end` });
    return links.map((link) => {
      const content = [App.icon(PLAYER_ICONS[link.icon]), link.label];
      return link.href
        ? h('a', { class: 'chip', href: link.href, rel: 'noopener noreferrer' }, ...content)
        : h('button', { type: 'button', class: 'chip', onclick: link.run }, ...content);
    });
  }

  // ------------------------------------------------------------------ the player

  function open(ctx, item, siblings) {
    siblings = siblings || [];
    let list = P.ofKind(siblings, ['video']);
    if (!list.some((other) => P.sameItem(other, item))) list = [item];
    let index = list.findIndex((other) => P.sameItem(other, item));
    let hls = null;
    let trackUrls = [];
    let playSeq = 0;

    const video = h('video', { class: 'pv-video', controls: true, playsinline: true, preload: 'metadata' });
    const notice = h('div', { class: 'pv-notice', hidden: true });
    const player = h('div', { class: 'pv-player' }, video, notice);
    const autoplay = h('input', { type: 'checkbox', role: 'switch', class: 'switch' });
    const autonext = h('input', { type: 'checkbox', role: 'switch', class: 'switch' });
    autoplay.checked = P.store.get('video-autoplay', false);
    autonext.checked = P.store.get('video-autoplay-next', false);
    autoplay.addEventListener('change', () => P.store.set('video-autoplay', autoplay.checked));
    autonext.addEventListener('change', () => P.store.set('video-autoplay-next', autonext.checked));
    const subtitleSelect = h('select', { class: 'pv-subtitles', 'aria-label': gettext('Subtitles') });
    const subtitleBox = h('label', { class: 'pv-subtitle-box', hidden: true }, App.icon('subtitles'), subtitleSelect);
    const prevButton = P.button(App.icon('skip_previous'), gettext('Previous video'), () => play(index - 1));
    const nextButton = P.button(App.icon('skip_next'), gettext('Next video'), () => play(index + 1));
    const apps = h('div', { class: 'chips pv-apps' });
    const appsHidden = P.store.get('video-apps-hidden', false);
    const appsToggle = h('button', { type: 'button', class: 'btn btn-text btn-small pv-apps-toggle', 'aria-expanded': String(!appsHidden) });
    const appsBox = h('div', { class: 'pv-apps-box' }, apps,
      h('p', { class: 'small muted pv-tip', text: gettext('Tip: click an app above to play it in a local player, which decodes better.') }));
    function showApps(shown) {
      appsBox.hidden = !shown;
      appsToggle.setAttribute('aria-expanded', String(shown));
      appsToggle.replaceChildren(App.icon(shown ? 'expand_less' : 'expand_more'), shown ? gettext('Hide external players') : gettext('External players'));
      P.store.set('video-apps-hidden', !shown);
    }
    appsToggle.addEventListener('click', () => showApps(appsBox.hidden));
    showApps(!appsHidden);

    const controls = h('div', { class: 'pv-controls' },
      prevButton, nextButton,
      h('label', { class: 'pv-switch' }, autoplay, gettext('Autoplay')),
      h('label', { class: 'pv-switch' }, autonext, gettext('Play the next one automatically')),
      subtitleBox,
      h('span', { class: 'pv-spacer' }),
      appsToggle);
    const listBox = h('ol', { class: 'list pv-list', 'aria-label': gettext('Playlist') });
    const aside = h('aside', { class: 'pv-aside' }, h('h3', { class: 'pv-list-title', text: interpolate(gettext('Playlist (%(count)s)'), { count: list.length }, true) }), listBox);
    const layout = h('div', { class: 'pv-layout' }, h('div', { class: 'pv-main' }, player, controls, appsBox), list.length > 1 ? aside : null);

    const f = P.frame({
      title: item.name, className: 'preview-video', body: layout, label: gettext('Video player'),
      actions: [P.downloadAction(ctx, item)], onClose: cleanup,
    });
    const headDownload = f.head.querySelector('.preview-actions');

    function say(text, actions = []) {
      notice.hidden = !text;
      notice.replaceChildren(...(text ? App.nodes(App.icon('error'), h('p', { text }), actions.length ? h('div', { class: 'btn-row' }, ...actions) : null) : []));
    }

    function cleanup() {
      playSeq += 1;
      if (hls) {
        hls.destroy();
        hls = null;
      }
      video.pause();
      video.removeAttribute('src');
      video.querySelectorAll('track').forEach((track) => track.remove());
      video.load();
      trackUrls.forEach((url) => URL.revokeObjectURL(url));
      trackUrls = [];
    }

    function drawList() {
      listBox.replaceChildren(...list.map((entry, i) => h('li', null, h('button', {
        type: 'button',
        class: `list-item interactive pv-item${i === index ? ' selected' : ''}`,
        'aria-current': i === index ? 'true' : null,
        onclick: () => play(i, { auto: true }),
      }, h('span', { class: 'list-leading' }, App.icon(i === index ? 'play_arrow' : 'movie')),
      h('span', { class: 'list-text' }, h('span', { class: 'list-headline', text: entry.name }),
        entry.size !== null && entry.size !== undefined ? h('span', { class: 'list-supporting', text: App.fmtSize(entry.size) }) : null)))));
      const current = listBox.children[index];
      if (current && current.scrollIntoView) current.scrollIntoView({ block: 'nearest' });
    }

    async function loadSubtitles(entry, seq) {
      const found = subtitlesOf(entry, siblings);
      subtitleBox.hidden = !found.length;
      subtitleSelect.replaceChildren(h('option', { value: '', text: gettext('Subtitles off') }),
        ...found.map((sub, i) => h('option', { value: String(i), text: sub.label })));
      if (!found.length) return;
      const tracks = []; // option value -> its <track>
      for (const [i, sub] of found.entries()) {
        try {
          const bytes = await P.fetchBytes(await ctx.inlineUrl(sub.item), { limit: SUBTITLE_MAX });
          if (seq !== playSeq) return;
          const text = P.decodeText(bytes, P.guessEncoding(bytes).encoding);
          const vtt = /\.srt$/i.test(sub.item.name) ? srtToVtt(text) : cleanVtt(text);
          const url = URL.createObjectURL(new Blob([vtt], { type: 'text/vtt' }));
          trackUrls.push(url);
          const track = h('track', { kind: 'subtitles', label: sub.label, src: url, srclang: 'zh' });
          video.append(track);
          // The first one that loads shows. The others are hidden, not disabled: Chrome's automatic track
          // selection turns a disabled <track> just added back on, and two subtitles showed at once.
          track.track.mode = tracks.some(Boolean) ? 'hidden' : 'showing';
          tracks[i] = track;
        } catch (err) {
          if (seq === playSeq) App.toast(interpolate(gettext("The subtitles %(name)s didn't load: %(message)s"), { name: sub.item.name, message: err.message }, true), 'error');
        }
      }
      const choose = (value) => {
        const chosen = value === '' ? null : tracks[Number(value)];
        video.querySelectorAll('track').forEach((track) => { track.track.mode = track === chosen ? 'showing' : 'hidden'; });
      };
      subtitleSelect.onchange = () => choose(subtitleSelect.value);
      const first = tracks.findIndex(Boolean);
      subtitleSelect.value = first < 0 ? '' : String(first);
      choose(subtitleSelect.value);
    }

    function unplayable(entry) {
      say(gettext("This video can't play in the browser (its format or codec isn't supported). You can download it, or open it in one of the external players below."),
        [P.downloadButton(ctx, entry)].filter(Boolean));
      showApps(true);
    }

    async function play(at, { auto = true } = {}) {
      cleanup();
      const seq = playSeq;
      index = (at + list.length) % list.length;
      const entry = list[index];
      f.setTitle(entry.name);
      if (headDownload) headDownload.replaceChildren(...[P.downloadAction(ctx, entry)].filter(Boolean));
      prevButton.disabled = nextButton.disabled = list.length < 2;
      say('');
      apps.replaceChildren(...(entry.url ? appLinks(ctx, entry) : []));
      drawList();
      if (!entry.url) {
        say(gettext("You don't have permission to play this video."));
        return;
      }
      // It played, then failed: most likely its signed URL ended (30 minutes by default). A fresh one, at most
      // once a minute, from where it was.
      let started = false;
      let renewed = 0;
      video.onplaying = () => { started = true; };
      video.onerror = async () => {
        if (seq !== playSeq || hls) return;
        if (started && Date.now() - renewed > 60_000) {
          renewed = Date.now();
          const at = video.currentTime;
          try {
            const fresh = await ctx.inlineUrl(entry, { fresh: true });
            if (seq !== playSeq) return;
            video.src = fresh;
            video.currentTime = at;
            video.play().catch(() => {});
            return;
          } catch (err) {
            // as below
          }
        }
        unplayable(entry);
      };
      try {
        if (isPlaylist(entry)) {
          const Hls = await P.lib('hls');
          if (seq !== playSeq) return;
          const source = await ctx.inlineUrl(entry);
          if (seq !== playSeq) return;
          if (Hls.isSupported()) {
            const refuse = (address) => {
              if (seq !== playSeq || !hls) return;
              video.onerror = null;
              hls.destroy();
              hls = null;
              say(interpolate(gettext("This m3u8 refers to an address outside its folder, which isn't loaded for safety: %(address)s"), { address: address.slice(0, 200) }, true),
                [P.downloadButton(ctx, entry)].filter(Boolean));
            };
            hls = new Hls({ workerPath: P.config.libs.hlsWorker, loader: siblingLoader(Hls, ctx, siblings, source, refuse) });
            hls.on(Hls.Events.ERROR, (event, data) => {
              if (!data.fatal || seq !== playSeq) return;
              say(interpolate(gettext("The m3u8 didn't play (%(error)s). Its segments must be in the same folder as the m3u8."), { error: data.details || data.type }, true),
                [P.downloadButton(ctx, entry)].filter(Boolean));
            });
            hls.loadSource(source);
            hls.attachMedia(video);
          } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = source; // Safari's own HLS: the playlist's entries must be reachable from its URL
          } else {
            say(gettext("This browser can't play m3u8."));
            return;
          }
        } else {
          video.src = entry.url;
        }
      } catch (err) {
        if (seq === playSeq) say(interpolate(gettext("The video didn't load: %(message)s"), { message: err.message }, true));
        return;
      }
      loadSubtitles(entry, seq);
      if (auto) video.play().catch(() => {}); // the browser may want a click first
    }

    video.addEventListener('ended', () => {
      if (autonext.checked && list.length > 1 && index < list.length - 1) play(index + 1);
    });
    // Only one thing plays at a time: the audio bar stops while a video plays.
    video.addEventListener('play', () => document.dispatchEvent(new CustomEvent('hfile:media-play', { detail: { source: 'video' } })));
    play(index, { auto: autoplay.checked });
  }

  App.preview.srtToVtt = srtToVtt;
  App.preview.cleanVtt = cleanVtt;
  App.preview.subtitlesOf = subtitlesOf;
  P.register(['video'], { open }, 'video');
})();
