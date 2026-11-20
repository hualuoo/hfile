/* /components/uploads/ (DEBUG): a ctx for the upload panel, as the file browser will give it (contract
 * W4): the storage and folder chosen here, the caller's permissions on it (GET /api/v1/storages/). */
(function () {
  'use strict';
  const { api, h, toast } = window.App;
  const select = document.getElementById('try-storage');
  const pathInput = document.getElementById('try-path');
  const permissionsText = document.getElementById('try-permissions');
  const log = document.getElementById('try-log');
  let storages = [];

  function ctx() {
    const storage = storages.find((item) => item.key === select.value);
    if (!storage) throw new Error('先选一个存储源。');
    return {
      mode: 'browse',
      storage: { key: storage.key, name: storage.name, type: storage.type },
      path: pathInput.value || '/',
      permissions: storage.permissions,
      capabilities: storage.capabilities,
      refresh() {},
      toast: (text, kind) => toast(text, kind),
    };
  }

  function showPermissions() {
    const storage = storages.find((item) => item.key === select.value);
    permissionsText.textContent = storage ? `权限：${storage.permissions.join('、') || '（没有）'}` : '';
  }

  async function load() {
    storages = await api('GET', '/api/v1/storages/');
    select.replaceChildren(...storages.map((item) => h('option', { value: item.key, text: `${item.name}（${item.key}，${item.type_label}）` })));
    if (!storages.length) select.append(h('option', { value: '', text: '没有能用的存储源' }));
    showPermissions();
  }

  select.addEventListener('change', showPermissions);
  document.getElementById('try-files').addEventListener('click', () => App.uploads.pick(ctx(), { folder: false }));
  document.getElementById('try-folder').addEventListener('click', () => App.uploads.pick(ctx(), { folder: true }));
  document.getElementById('try-show').addEventListener('click', () => App.uploads.show());
  const count = document.getElementById('try-count');
  App.uploads.onCount((n) => { count.textContent = String(n); });

  const zone = document.getElementById('try-drop');
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('over');
    App.uploads.drop(ctx(), e.dataTransfer);
  });

  document.addEventListener('hfile:files-changed', (e) => {
    const { storageKey, path } = e.detail;
    log.prepend(h('li', { class: 'list-item' }, h('span', { class: 'list-text' },
      h('span', { class: 'list-headline', text: `${storageKey} ${path}` }),
      h('span', { class: 'list-supporting', text: new Date().toLocaleTimeString() }))));
  });

  load().catch((error) => toast(error.message, 'error'));
})();
