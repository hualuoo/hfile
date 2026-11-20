/* The text and code viewer (decision D21, inventory 7.Z.5 (b)): highlight.js for reading, a <textarea>
 * for editing.
 *
 * - The file is read from ctx.inlineUrl(item) as bytes and decoded with TextDecoder: by its byte-order
 *   mark, else UTF-8 when the bytes are valid UTF-8, else GB18030; the 编码 list decodes it again.
 * - Toolbar: 语言 (from the suffix), 编码, 只读 (when the visitor may edit), 自动换行, 保存.
 * - Footer: lines, length, the cursor's line and column.
 * - Saving (Ctrl/⌘+S or 保存) sends the text, the encoding (utf-8-bom when the file had one) and the file's
 *   line breaks (a textarea only has \n) to the uploads app's PUT /api/v1/uploads/text/, which encodes it
 *   on the server, with `expected` (the modified time and size the editor opened) so that a file changed
 *   meanwhile isn't overwritten without asking (409 changed). Only with 上传 and 删除 (D17a: it overwrites),
 *   never in a share. Text that decoded with replacement characters (a wrong encoding, a binary file)
 *   asks before it can be edited: saving would write the replacement characters. So does text with NUL
 *   (U+0000), which the server doesn't save (FILE-03).
 * - Size guards (7.Z.8): previewed up to the site's 文本预览的最大文件, edited up to 2 MB, highlighted
 *   up to 512 KB; bigger files offer the download. */
(function () {
  'use strict';

  const P = App.preview;
  const { h } = App;

  const LANGUAGES = [
    ['plaintext', gettext('Plain text')], ['bash', 'Bash / Shell'], ['c', 'C'], ['cpp', 'C++'], ['csharp', 'C#'], ['css', 'CSS'],
    ['diff', 'Diff'], ['dockerfile', 'Dockerfile'], ['dos', gettext('Batch (bat)')], ['go', 'Go'], ['graphql', 'GraphQL'],
    ['ini', 'INI / TOML'], ['java', 'Java'], ['javascript', 'JavaScript'], ['json', 'JSON'], ['kotlin', 'Kotlin'],
    ['less', 'Less'], ['lua', 'Lua'], ['makefile', 'Makefile'], ['markdown', 'Markdown'], ['nginx', 'Nginx'],
    ['objectivec', 'Objective-C'], ['perl', 'Perl'], ['php', 'PHP'], ['powershell', 'PowerShell'],
    ['properties', 'Properties'], ['python', 'Python'], ['r', 'R'], ['ruby', 'Ruby'], ['rust', 'Rust'],
    ['scss', 'SCSS / Sass'], ['sql', 'SQL'], ['swift', 'Swift'], ['typescript', 'TypeScript'], ['vbnet', 'VB.NET'],
    ['xml', 'HTML / XML'], ['yaml', 'YAML'],
  ];
  const BY_SUFFIX = {
    sh: 'bash', bash: 'bash', zsh: 'bash', gitignore: 'bash', script: 'bash', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp',
    cxx: 'cpp', hpp: 'cpp', cs: 'csharp', css: 'css', diff: 'diff', patch: 'diff', dockerfile: 'dockerfile', bat: 'dos',
    cmd: 'dos', go: 'go', graphql: 'graphql', gql: 'graphql', ini: 'ini', toml: 'ini', conf: 'ini', cfg: 'ini',
    java: 'java', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript', json: 'json', kt: 'kotlin',
    kts: 'kotlin', less: 'less', lua: 'lua', mk: 'makefile', md: 'markdown', markdown: 'markdown', m: 'objectivec',
    pl: 'perl', pm: 'perl', php: 'php', ps1: 'powershell', properties: 'properties', py: 'python', r: 'r', rb: 'ruby',
    rs: 'rust', rust: 'rust', scss: 'scss', sass: 'scss', sql: 'sql', swift: 'swift', ts: 'typescript', tsx: 'typescript',
    vb: 'vbnet', xml: 'xml', html: 'xml', htm: 'xml', svg: 'xml', vue: 'xml', xhtml: 'xml', yml: 'yaml', yaml: 'yaml',
  };
  const BY_NAME = { makefile: 'makefile', dockerfile: 'dockerfile', 'nginx.conf': 'nginx' };
  const ENCODINGS = [
    ['utf-8', 'UTF-8'], ['gb18030', 'GB18030'], ['gbk', 'GBK'], ['big5', 'Big5'], ['shift_jis', 'Shift_JIS'],
    ['euc-kr', 'EUC-KR'], ['utf-16le', 'UTF-16LE'], ['utf-16be', 'UTF-16BE'],
    ['windows-1250', gettext('Windows-1250 (Central European)')], ['windows-1251', gettext('Windows-1251 (Cyrillic)')], ['windows-1252', gettext('Windows-1252 (Western European)')],
    ['windows-1253', gettext('Windows-1253 (Greek)')], ['windows-1254', gettext('Windows-1254 (Turkish)')], ['windows-1255', gettext('Windows-1255 (Hebrew)')],
    ['windows-1256', gettext('Windows-1256 (Arabic)')], ['windows-1257', gettext('Windows-1257 (Baltic)')], ['windows-1258', gettext('Windows-1258 (Vietnamese)')],
  ];
  const HIGHLIGHT_MAX = 512 * 1024;

  function languageOf(name) {
    const lower = String(name).toLowerCase();
    return BY_NAME[lower] || BY_SUFFIX[P.suffix(lower)] || 'plaintext';
  }

  function select(options, value, label) {
    const box = h('select', { 'aria-label': label }, ...options.map(([key, text]) => h('option', { value: key, text })));
    box.value = value;
    return box;
  }

  function open(ctx, item, siblings) {
    const limits = P.limits(ctx);
    const textLimit = limits.text;
    const editLimit = limits.edit;
    const mayEdit = !P.isShare(ctx) && P.can(ctx, 'upload') && P.can(ctx, 'delete') && Boolean(P.config.urls.save);
    const state = {
      bytes: null, text: '', encoding: 'utf-8', bom: false, guessed: true, language: languageOf(item.name),
      newline: 'lf', readOnly: true, dirty: false, saved: false, modified: item.modified || null, size: item.size,
      wrap: P.store.get('text-wrap', true),
    };

    const area = h('div', { class: 'pt-area' });
    const code = h('code', { class: 'hljs' });
    const pre = h('pre', { class: 'pt-code', tabindex: '0' }, code);
    const editor = h('textarea', { class: 'pt-editor', spellcheck: 'false', autocapitalize: 'off', autocomplete: 'off', 'aria-label': interpolate(gettext('Edit %(name)s'), { name: item.name }, true) });
    const foot = h('div', { class: 'pt-foot small muted', 'aria-live': 'polite' });
    const languageSelect = select(LANGUAGES, state.language, gettext('Language'));
    const encodingSelect = select(ENCODINGS, 'utf-8', gettext('Encoding'));
    const readOnlySwitch = h('input', { type: 'checkbox', role: 'switch', class: 'switch', checked: true });
    const wrapSwitch = h('input', { type: 'checkbox', role: 'switch', class: 'switch' });
    wrapSwitch.checked = state.wrap;
    const saveButton = h('button', { type: 'button', class: 'btn btn-filled btn-small', hidden: true, 'data-tooltip': 'Ctrl/⌘ + S', onclick: () => save() },
      App.icon('save'), gettext('Save'));
    const readOnlyBox = mayEdit ? h('label', { class: 'pt-switch' }, readOnlySwitch, gettext('Read-only')) : null;
    const toolbar = h('div', { class: 'pt-toolbar' },
      h('label', { class: 'pt-select' }, h('span', { text: gettext('Language') }), languageSelect),
      h('label', { class: 'pt-select' }, h('span', { text: gettext('Encoding') }), encodingSelect),
      readOnlyBox,
      h('label', { class: 'pt-switch' }, wrapSwitch, gettext('Wrap lines')),
      h('span', { class: 'pt-spacer' }),
      saveButton);
    const box = h('div', { class: 'pt-box' }, toolbar, area, foot);
    const f = P.frame({ title: item.name, className: 'preview-text', body: box, actions: [P.downloadAction(ctx, item)] });

    function lines(text) {
      let count = 1;
      for (let i = text.indexOf('\n'); i !== -1; i = text.indexOf('\n', i + 1)) count += 1;
      return count;
    }
    let footPending = false;
    function updateFoot() {
      if (footPending) return;
      footPending = true;
      requestAnimationFrame(() => {
        footPending = false;
        const text = state.readOnly ? state.text : editor.value;
        const parts = [interpolate(gettext('Lines %(lines)s'), { lines: App.fmtNum(lines(text)) }, true), interpolate(gettext('Length %(length)s'), { length: App.fmtNum(text.length) }, true)];
        if (!state.readOnly) {
          const before = editor.value.slice(0, editor.selectionStart);
          const line = lines(before);
          const column = before.length - before.lastIndexOf('\n');
          parts.push(interpolate(gettext('Cursor %(line)s:%(column)s'), { line, column }, true));
          const selected = editor.selectionEnd - editor.selectionStart;
          if (selected) parts.push(interpolate(gettext('Selected %(selected)s'), { selected: App.fmtNum(selected) }, true));
        }
        const name = (ENCODINGS.find(([key]) => key === state.encoding) || [state.encoding, state.encoding])[1];
        parts.push(interpolate(gettext('Encoding %(name)s'), { name }, true) + (state.bom ? gettext(' (with BOM)') : '') + (state.guessed ? gettext(' (detected)') : ''));
        if (state.dirty) parts.push(gettext('Unsaved changes'));
        foot.textContent = parts.join(' · ');
      });
    }

    let drawSeq = 0;
    async function drawRead() {
      const seq = ++drawSeq;
      pre.classList.toggle('wrap', state.wrap);
      code.textContent = state.text;
      area.replaceChildren(pre);
      if (state.language === 'plaintext' || state.text.length > HIGHLIGHT_MAX) return;
      try {
        const hljs = await P.lib('hljs');
        if (seq !== drawSeq) return;
        const nodes = P.highlight(hljs, state.text, state.language);
        if (nodes) code.replaceChildren(nodes);
      } catch (err) {
        // plain text it stays
      }
    }
    function drawEdit() {
      drawSeq += 1;
      editor.value = state.text;
      editor.classList.toggle('wrap', state.wrap);
      editor.wrap = state.wrap ? 'soft' : 'off';
      area.replaceChildren(editor);
      editor.focus();
      editor.setSelectionRange(0, 0);
    }
    function draw() {
      saveButton.hidden = state.readOnly;
      if (state.readOnly) drawRead();
      else drawEdit();
      updateFoot();
    }

    function decode() {
      state.text = P.decodeText(state.bytes, state.encoding);
      state.newline = state.text.includes('\r\n') ? 'crlf' : state.text.includes('\r') ? 'cr' : 'lf';
      state.dirty = false;
    }

    async function load() {
      if (state.size !== null && state.size !== undefined && state.size > textLimit) {
        P.tooLarge(area, ctx, item, textLimit);
        return false;
      }
      P.message(area, { busy: true, text: gettext('Loading') });
      try {
        const url = await ctx.inlineUrl(item);
        state.bytes = await P.fetchBytes(url, { limit: textLimit, fresh: state.saved });
      } catch (err) {
        if (err instanceof P.TooLarge) {
          P.tooLarge(area, ctx, { ...item, size: err.size }, textLimit);
        } else {
          P.message(area, {
            icon: 'error', text: gettext("The text file didn't load."), detail: err.message,
            actions: [P.downloadButton(ctx, item)].filter(Boolean),
          });
        }
        return false;
      }
      state.size = state.bytes.length;
      return true;
    }

    async function start() {
      if (!(await load())) {
        toolbar.hidden = true;
        return;
      }
      const guess = P.guessEncoding(state.bytes);
      state.encoding = guess.encoding;
      state.bom = guess.bom;
      encodingSelect.value = state.encoding;
      decode();
      if (readOnlyBox && state.size > editLimit) {
        readOnlySwitch.disabled = true;
        readOnlyBox.dataset.tooltip = interpolate(gettext('Files over %(limit)s can only be viewed'), { limit: App.fmtSize(editLimit) }, true);
      }
      draw();
    }

    async function save() {
      if (state.readOnly) {
        App.toast(gettext("Read-only: the file can't be saved"), 'error');
        return;
      }
      const text = editor.value;
      const body = {
        storage: ctx.storage && ctx.storage.key, path: P.pathOf(ctx, item), content: text,
        encoding: state.encoding === 'utf-8' && state.bom ? 'utf-8-bom' : state.encoding, newline: state.newline,
      };
      if (state.modified && state.size !== null && state.size !== undefined) body.expected = { modified: state.modified, size: state.size };
      saveButton.disabled = true;
      try {
        let result;
        try {
          result = await App.api('PUT', P.config.urls.save, body);
        } catch (err) {
          if (err.status !== 409) throw err;
          const overwrite = await App.confirmDialog({
            title: gettext('The file has changed'), danger: true, confirmText: gettext('Overwrite'),
            message: gettext('This file was changed after you opened it. Overwrite it with what you have now?'),
          });
          if (!overwrite) return;
          delete body.expected;
          result = await App.api('PUT', P.config.urls.save, body);
        }
        const saved = (result && (result.item || result)) || {};
        state.modified = saved.modified || null;
        state.size = saved.size === undefined ? null : saved.size;
        state.text = text;
        state.bytes = null; // what is stored now is the server's encoding of the text
        state.saved = true; // read again from the storage, not from the browser's cache
        state.dirty = false;
        App.toast(gettext('Saved'));
        document.dispatchEvent(new CustomEvent('hfile:files-changed', {
          detail: { storageKey: ctx.storage && ctx.storage.key, path: ctx.path || '' },
        }));
      } catch (err) {
        App.toast(interpolate(gettext('Saving failed: %(message)s'), { message: err.message }, true), 'error');
      } finally {
        saveButton.disabled = false;
        updateFoot();
      }
    }

    languageSelect.addEventListener('change', () => {
      state.language = languageSelect.value;
      if (state.readOnly) drawRead();
    });
    encodingSelect.addEventListener('change', async () => {
      const chosen = encodingSelect.value;
      if (state.dirty && !(await App.confirmDialog({
        title: gettext('Change the encoding'), confirmText: gettext('Change'), danger: true,
        message: gettext('Changing the encoding reads the file again in the new one; unsaved changes are lost.'),
      }))) {
        encodingSelect.value = state.encoding;
        return;
      }
      if (!state.bytes && !(await load())) return;
      state.encoding = chosen;
      state.bom = false;
      state.guessed = false;
      decode();
      draw();
    });
    readOnlySwitch.addEventListener('change', async () => {
      if (!readOnlySwitch.checked) {
        if (state.text.includes('\uFFFD') && !(await App.confirmDialog({
          title: gettext('Unreadable characters'), confirmText: gettext('Edit anyway'), danger: true,
          message: gettext("In the current encoding this file has characters that can't be read (shown as �). Try another encoding; if you edit and save it as it is, those characters are replaced."),
        }))) {
          readOnlySwitch.checked = true;
          return;
        }
        if (state.text.includes('\x00') && !(await App.confirmDialog({
          title: gettext('NUL characters'), confirmText: gettext('Edit anyway'), danger: true,
          message: gettext("This file has NUL characters (U+0000), and the online editor can't save text with them. Most likely the encoding is wrong (UTF-16 without a BOM, say), so try another one; to edit it anyway, delete those characters before saving."),
        }))) {
          readOnlySwitch.checked = true;
          return;
        }
        state.readOnly = false;
      } else {
        state.text = editor.value;
        state.readOnly = true;
      }
      draw();
    });
    wrapSwitch.addEventListener('change', () => {
      state.wrap = wrapSwitch.checked;
      P.store.set('text-wrap', state.wrap);
      pre.classList.toggle('wrap', state.wrap);
      editor.classList.toggle('wrap', state.wrap);
      editor.wrap = state.wrap ? 'soft' : 'off';
    });
    editor.addEventListener('input', () => {
      state.dirty = true;
      updateFoot();
    });
    for (const type of ['keyup', 'click', 'select']) editor.addEventListener(type, updateFoot);
    f.dialog.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') {
        e.preventDefault(); // not the browser's 保存网页
        if (mayEdit) save();
        else App.toast(gettext("Read-only: the file can't be saved"), 'error');
      }
    });
    f.dialog.addEventListener('cancel', async (e) => {
      if (!state.dirty) return; // also edits made before switching back to 只读
      e.preventDefault();
      if (await App.confirmDialog({ title: gettext('Close'), message: gettext('There are unsaved changes. Close without saving?'), confirmText: gettext("Don't save"), danger: true })) {
        state.dirty = false;
        f.close();
      }
    });
    start();
  }

  P.register(['text'], { open }, 'text');
})();
