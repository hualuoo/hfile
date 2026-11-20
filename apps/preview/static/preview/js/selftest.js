/* DEBUG only (/components/preview/selftest/): tests of the viewers' pure parts that only a browser can
 * run (TextDecoder, <template>, URL): .srt to WebVTT, the encoding guess, the check of highlight.js's
 * output, the Markdown viewer's link and picture rewriting. The result goes into #selftest-result
 * (data-status pass or fail), which the headless-Chrome check reads. */
(async function () {
  'use strict';

  const P = App.preview;
  const out = document.getElementById('selftest-result');
  const results = [];
  function check(name, ok, detail = '') {
    results.push(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !detail ? '' : `\n     ${detail}`}`);
  }
  function equal(name, actual, expected) {
    check(name, actual === expected, `got ${JSON.stringify(actual)}, expected ${JSON.stringify(expected)}`);
  }

  // ---- .srt -> WebVTT
  equal('srt: cues, commas, numbers, CRLF, BOM, positions, arrows',
    P.srtToVtt('\uFEFF1\r\n00:00:01,000 --> 00:00:04,000\r\n你好\r\n世界\r\n\r\n2\r\n00:00:05,5 --> 00:01:02,250 X1:100 X2:200\r\n<i>second</i> --> arrow\r\n'),
    'WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n你好\n世界\n\n00:00:05.500 --> 00:01:02.250\n<i>second</i> -> arrow\n');
  equal('srt: minutes only, dots, empty and broken blocks',
    P.srtToVtt('1\n01:02.003 --> 01:04.5\nshort\n\n\n\n2\nnot a timing\ntext\n\n3\n00:00:09,000 --> 00:00:10,000\n\n'),
    'WEBVTT\n\n00:01:02.003 --> 00:01:04.500\nshort\n');
  equal('srt: nothing', P.srtToVtt(''), 'WEBVTT\n\n\n');
  const vtt = P.srtToVtt('1\n00:00:00,000 --> 00:00:01,000\nWEBVTT -->\n');
  check('srt: no line of the text is read as a timing', !vtt.split('\n').slice(3).some((line) => line.includes('-->')), vtt);

  // ---- .vtt as cues only
  equal('vtt: STYLE, REGION, NOTE and odd cue settings go; minutes-only cues stay',
    P.cleanVtt('WEBVTT\n\nSTYLE\n::cue { background: url(https://evil.example/x.png) }\n\nREGION\nid:r1\n\nNOTE a note\n\n'
      + 'c1\n00:00:01.000 --> 00:00:02.000 region:r1 align:start position:10%\n<b>Hi</b>\n\n00:01.000 --> 00:02.000\nno hours\n'),
    'WEBVTT\n\n00:00:01.000 --> 00:00:02.000 align:start position:10%\n<b>Hi</b>\n\n00:01.000 --> 00:02.000\nno hours\n');

  // ---- subtitles beside a video
  const names = (list) => list.map((sub) => sub.item.name).join(',');
  const files = ['movie.srt', 'movie.mp4.vtt', 'movie.zh.srt', 'other.srt', 'movie2.vtt', 'movie.mp4'].map((name) => ({ name, kind: 'other' }));
  equal('subtitles: <base>, <name>, <base>.<label>; not other videos\'', names(P.subtitlesOf({ name: 'movie.mp4' }, files)), 'movie.srt,movie.mp4.vtt,movie.zh.srt');

  // ---- encodings
  const utf8 = new TextEncoder().encode('中文 text');
  const gbk = new Uint8Array([0xD6, 0xD0, 0xCE, 0xC4, 0x20, 0x61]); // "中文 a" in GBK
  equal('guess: valid UTF-8', P.guessEncoding(utf8).encoding, 'utf-8');
  equal('guess: else GB18030', P.guessEncoding(gbk).encoding, 'gb18030');
  equal('decode: GB18030', P.decodeText(gbk, 'gb18030'), '中文 a');
  equal('guess: UTF-8 BOM', JSON.stringify(P.guessEncoding(new Uint8Array([0xEF, 0xBB, 0xBF, 0x41]))), '{"encoding":"utf-8","bom":true}');
  equal('guess: UTF-16LE BOM', P.guessEncoding(new Uint8Array([0xFF, 0xFE, 0x41, 0x00])).encoding, 'utf-16le');
  equal('decode: the BOM goes', P.decodeText(new Uint8Array([0xFF, 0xFE, 0x41, 0x00]), 'utf-16le'), 'A');
  equal('decode: Big5', P.decodeText(new Uint8Array([0xA4, 0xA4, 0xA4, 0xE5]), 'big5'), '中文');

  // ---- highlight.js's output is checked before it goes in
  const fake = (html) => ({ highlight: () => ({ value: html }) });
  check('highlight: spans with hljs classes pass', P.highlight(fake('<span class="hljs-keyword">const</span> <span class="hljs-title function_">a</span>'), 'x', 'js') !== null);
  // (The check is the same for every attribute; on* and style ones would make Chrome report them while
  // parsing, blocked, and this page is also checked for a console without CSP reports.)
  equal('highlight: an <img> is refused', P.highlight(fake('<img src="x" alt="y">'), 'x', 'js'), null);
  equal('highlight: another attribute is refused', P.highlight(fake('<span class="hljs-string" data-x="1">a</span>'), 'x', 'js'), null);
  equal('highlight: an attribute without class is refused', P.highlight(fake('<span title="x">a</span>'), 'x', 'js'), null);
  equal('highlight: another class is refused', P.highlight(fake('<span class="dialog">a</span>'), 'x', 'js'), null);
  try {
    const hljs = await P.lib('hljs');
    const nodes = P.highlight(hljs, 'const a = "<img src=x onerror=alert(1)>"; // <b>', 'javascript');
    const box = document.createElement('div');
    box.append(nodes);
    check('highlight.js: markup in code stays text', box.querySelectorAll('img, b').length === 0 && box.textContent.includes('<img src=x'));
    check('highlight.js: the extra languages are there', ['dos', 'properties', 'dockerfile', 'nginx', 'powershell', 'python', 'xml'].every((name) => hljs.getLanguage(name)));
  } catch (err) {
    check('highlight.js loads', false, err.message);
  }

  // ---- Markdown: links and pictures map to the folder's own files, or go
  const root = P.inert('<p><img src="pic.png" alt="a"><img src="missing.png" alt="m"><img src="sub/x.png" alt="s">'
    + '<img src="https://evil.example/t.png" alt="e"><img src="//evil.example/t2.png">'
    + '<a href="other.md">o</a><a href="secret.md">s</a><a href="https://example.com/">x</a><a href="#top">t</a>'
    + '<a href="mailto:a@example.com">m</a><a href="pic%20two.png">p2</a>'
    + '<img src="data:image/png;base64,iVBORw0KGgo=" alt="d"><a href="https://x.example/"><img src="https://badge.example/b.svg" alt="badge"></a>'
    + '<img src="/pic.png" alt="root"></p><picture><source srcset="dark.png"><img src="pic.png" alt="p" srcset="big.png 2x"></picture>'
    + '<pre><code class="language-python">print(1)</code></pre><table><tr><td>1</td></tr></table>');
  const siblings = [
    { name: 'README.md', kind: 'markdown', url: '/u/README.md' },
    { name: 'pic.png', kind: 'image', url: '/u/pic.png' },
    { name: 'pic two.png', kind: 'image', url: '/u/pic%20two.png' },
    { name: 'other.md', kind: 'markdown', url: '/u/other.md' },
    { name: 'secret.md', kind: 'markdown', url: null },
    { name: 'sub', kind: null, is_dir: true, url: null },
  ];
  const opened = [];
  const blocks = P.rewriteMarkdown(root, { path: '/docs' }, { name: 'README.md' }, siblings, (item) => opened.push(item.name));
  const imgs = [...root.querySelectorAll('img')].map((img) => `${img.getAttribute('alt')}=${img.getAttribute('src')}`).join(' ');
  equal('markdown: pictures', imgs, 'a=/u/pic.png d=data:image/png;base64,iVBORw0KGgo= p=/u/pic.png');
  equal('markdown: no srcset or <source> left', root.querySelectorAll('source, [srcset]').length, 0);
  equal('markdown: missing pictures leave their alt text', [...root.querySelectorAll('.md-image-missing')].map((el) => el.textContent).join(' '), '[m] [s] [root]');
  const external = [...root.querySelectorAll('.md-image-link')];
  equal('markdown: pictures of other sites become links', external.map((el) => `${el.tagName}:${el.getAttribute('href')}`).join(' '),
    'A:https://evil.example/t.png A:https://evil.example/t2.png SPAN:null');
  const links = [...root.querySelectorAll('a:not(.md-image-link)')].map((a) => `${a.textContent}=${a.getAttribute('href')}|${a.getAttribute('target')}|${a.getAttribute('rel')}`).join(' ');
  equal('markdown: links', links,
    'o=/u/other.md|_blank|noopener noreferrer s=null|null|null x=https://example.com/|_blank|noopener noreferrer t=null|null|null '
    + 'm=mailto:a@example.com|_blank|noopener noreferrer p2=/u/pic%20two.png|_blank|noopener noreferrer badge=https://x.example/|_blank|noopener noreferrer');
  root.querySelector('a[href="/u/other.md"]').dispatchEvent(new MouseEvent('click', { cancelable: true }));
  equal('markdown: a link to another Markdown file opens it here', opened.join(), 'other.md');
  equal('markdown: code blocks to highlight', blocks.map(([code, language]) => `${language}:${code.textContent}`).join(), 'python:print(1)');
  equal('markdown: code blocks get a copy button, tables a scroller', `${root.querySelectorAll('.md-code .md-copy').length},${root.querySelectorAll('.md-table > table').length}`, '1,1');

  // ---- the 下载 button's link (download()), caught before the browser follows it
  const clicked = [];
  const catchClick = (e) => {
    if (e.target instanceof HTMLAnchorElement && e.target.classList.contains('sr-only')) {
      clicked.push(`${e.target.getAttribute('download')}|${e.target.getAttribute('target')}`);
      e.preventDefault();
    }
  };
  document.addEventListener('click', catchClick, true);
  const file = { name: 'a b.txt', url: '/pd/s/a%20b.txt?e=1&sign=x' };
  await P.download({ mode: 'browse' }, file);
  await P.download({ mode: 'browse', downloadUrl: () => 'https://bucket.example/a.txt?X-Amz-Signature=x' }, file);
  await P.download({ mode: 'share', downloadUrl: () => '/share/k/download?path=/a%20b.txt' }, file);
  document.removeEventListener('click', catchClick, true);
  equal('download: this site\'s links saved (also from storages whose links open in the browser), another site\'s and shares\' in a tab',
    clicked.join(' '), 'a b.txt|null null|_blank null|_blank');

  // ---- small helpers
  equal('suffix', ['.gitignore', 'a.TAR.GZ', 'noext', 'dir/x.md'].map(P.suffix).join(), 'gitignore,gz,,md');
  check('can: a list or an object', P.can({ permissions: ['preview'] }, 'preview') && P.can({ permissions: { download: true } }, 'download')
    && !P.can({ permissions: ['preview'] }, 'download') && !P.can({}, 'preview'));
  equal('pathOf', [P.pathOf({ path: '/a/' }, { name: 'b.txt' }), P.pathOf({ path: 'a' }, { name: 'b.txt' }), P.pathOf({ path: '' }, { name: 'b.txt' }),
    P.pathOf({ path: '/x' }, { name: 'b.txt', path: 'a/b.txt' })].join(), '/a/b.txt,/a/b.txt,/b.txt,/a/b.txt');
  equal('expiresAt: a signed link\'s e= in ms, Infinity without one',
    [P.expiresAt('/pd/s/a.txt?e=1794000000&d=i&sign=x'), P.expiresAt('https://bucket.example/a.txt?X-Amz-Expires=60'), P.expiresAt('/pd/s/a?e=x')].join(),
    '1794000000000,Infinity,Infinity');
  check('limits and kk: from the site\'s settings', (() => {
    const l = P.limits({ site: { text_preview_max_kb: 4096 } });
    const d = P.limits({});
    return l.text === 4194304 && l.markdown === 2097152 && l.edit === 2097152 && d.text === 524288 && d.markdown === 524288
      && P.kk({ site: { kkfileview_url: 'https://kk.example', kkfileview_open_mode: 'new_tab' } }).mode === 'new_tab'
      && P.kk({ site: { kkfileview_url: 'https://kk.example', kkfileview_open_mode: 'x' } }).mode === 'iframe'
      && !P.kk({ site: { kkfileview_url: '' } }).enabled && !P.kk({}).enabled;
  })());
  check('config: the vendored libraries next to preview.js', P.config.libs.hls.startsWith('/static/preview/vendor/hls.js-')
    && P.config.libs.pdfAssets.endsWith('/pdf.js-6.4.299/') && P.config.urls.save === '/api/v1/uploads/text/', JSON.stringify(P.config.libs));

  const failed = results.filter((line) => line.startsWith('FAIL')).length;
  out.textContent = `${results.join('\n')}\n\n${results.length - failed} passed, ${failed} failed`;
  out.dataset.status = failed ? 'fail' : 'pass';
})();
