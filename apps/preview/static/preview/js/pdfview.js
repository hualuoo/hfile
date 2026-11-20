/* The PDF viewer (inventory 7.Z.6): the vendored pdf.js (its prebuilt generic build, the legacy one for
 * older browsers, with its worker from /static/). One page or all of them ("❮ n / N ❯", 显示所有页), zoom
 * ("缩放比例", 适合宽度), selectable text, the keys ←/→ (page), +/− (zoom), Home/End. The file comes from
 * ctx.inlineUrl(item) with range requests, so only the pages looked at are fetched; in the all-pages
 * view pages are drawn as they come into view and dropped again far from it. A page exists in the page
 * only when shown: one at a time, or all of them for documents up to ALL_PAGES_MAX pages (a crafted PDF
 * can claim millions). "加载 X%" while the start
 * of the file arrives, "PDF 加载失败" when it can't be read; password-protected PDFs ask for it. */
(function () {
  'use strict';

  const P = App.preview;
  const { h } = App;
  const PDF_TO_CSS = 96 / 72;
  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 4;
  const STEP = 0.1;
  const MAX_CANVAS_PIXELS = 16 * 1024 * 1024; // per page: a 4096 × 4096 canvas
  const MAX_IMAGE_PIXELS = 100_000_000; // an image in a PDF larger than this isn't drawn (pdf.js's maxImageSize)
  const ALL_PAGES_MAX = 1000;

  function askPassword(wrong) {
    const input = h('input', { type: 'password', name: 'password', autocomplete: 'off', required: true, 'data-autofocus': '' });
    return App.editDialog({
      title: gettext('This PDF has a password'),
      submitText: gettext('Open'), submitIcon: 'lock_open',
      body: h('div', null,
        wrong ? h('p', { class: 'form-error', text: gettext('Wrong password. Please try again.') }) : null,
        h('div', { class: 'field' }, h('label', { text: gettext('Password') }), input)),
      save: (fields) => fields.password.value || undefined,
    });
  }

  function open(ctx, item) {
    let pdfjs = null;
    let task = null;
    let doc = null;
    let zoom = 1;
    let current = 1;
    let allPages = P.store.get('pdf-all-pages', true);
    let slots = []; // page number - 1 -> its slot, made when first needed (slotAt)
    let count = 0;
    let observer = null;
    let closed = false;
    const visible = new Set();

    const pages = h('div', { class: 'pdf-pages', tabindex: '0', 'aria-label': gettext('PDF pages') });
    const pageInput = h('input', { type: 'number', class: 'pdf-page-input', min: '1', value: '1', inputmode: 'numeric', 'aria-label': gettext('Page') });
    const total = h('span', { class: 'pdf-total', text: '/ -' });
    const prevButton = P.button(App.icon('chevron_left'), gettext('Previous page (←)'), () => goTo(current - 1));
    const nextButton = P.button(App.icon('chevron_right'), gettext('Next page (→)'), () => goTo(current + 1));
    const allSwitch = h('input', { type: 'checkbox', role: 'switch', class: 'switch' });
    allSwitch.checked = allPages;
    const zoomText = h('span', { class: 'pdf-zoom', 'aria-live': 'polite' });
    const toolbar = h('div', { class: 'pdf-toolbar' },
      h('div', { class: 'pdf-group' }, prevButton, pageInput, total, nextButton),
      h('label', { class: 'pt-switch' }, allSwitch, gettext('Show all pages')),
      h('div', { class: 'pdf-group' },
        P.button(App.icon('zoom_out'), gettext('Zoom out (-)'), () => setZoom(zoom - STEP)),
        zoomText,
        P.button(App.icon('zoom_in'), gettext('Zoom in (+)'), () => setZoom(zoom + STEP)),
        P.button(App.icon('fit_width'), gettext('Fit width'), () => setZoom(fitZoom()))));
    const status = h('div', { class: 'pdf-status' });
    const box = h('div', { class: 'pdf-box' }, toolbar, h('div', { class: 'pdf-stage' }, pages, status));
    const f = P.frame({ title: item.name, className: 'preview-pdf', body: box, actions: [P.downloadAction(ctx, item)], onClose: cleanup });
    toolbar.hidden = true;

    function cleanup() {
      closed = true;
      if (observer) observer.disconnect();
      slots.forEach((slot) => { if (slot.task) slot.task.cancel(); });
      if (task) task.destroy();
    }
    function fail(err) {
      if (closed) return;
      toolbar.hidden = true;
      pages.replaceChildren();
      const invalid = err && err.name === 'InvalidPDFException';
      P.message(status, {
        icon: 'error', text: gettext("The PDF didn't load"), detail: invalid ? gettext("This file isn't a valid PDF.") : (err && err.message) || '',
        actions: [P.downloadButton(ctx, item)].filter(Boolean),
      });
    }

    // ---- pages: a slot per page, sized before it is drawn
    function slotAt(number) {
      if (!slots[number - 1]) {
        const canvas = h('canvas', { class: 'pdf-canvas' });
        const text = h('div', { class: 'textLayer' });
        const div = h('div', { class: 'pdf-page', 'data-page': String(number), 'aria-label': interpolate(gettext('Page %(number)s'), { number }, true) }, canvas, text);
        div.style.setProperty('--scale-round-x', '1px');
        div.style.setProperty('--scale-round-y', '1px');
        slots[number - 1] = { number, div, canvas, text, page: null, base: null, task: null, drawn: 0, drawing: 0 };
      }
      return slots[number - 1];
    }
    function slotSize(slot) {
      const base = slot.base || slots[0].base;
      return { width: base.width * zoom, height: base.height * zoom };
    }
    function sizeSlot(slot) {
      const size = slotSize(slot);
      slot.div.style.width = `${Math.floor(size.width)}px`;
      slot.div.style.height = `${Math.floor(size.height)}px`;
    }
    function free(slot) {
      if (slot.task) slot.task.cancel();
      slot.task = null;
      slot.drawn = 0;
      slot.canvas.width = slot.canvas.height = 0;
      slot.text.replaceChildren();
    }
    async function draw(slot) {
      if (slot.drawn === zoom || slot.drawing === zoom || closed) return;
      const want = zoom;
      slot.drawing = want;
      try {
        if (!slot.page) {
          slot.page = await doc.getPage(slot.number);
          const unscaled = slot.page.getViewport({ scale: PDF_TO_CSS });
          slot.base = { width: unscaled.width, height: unscaled.height };
          sizeSlot(slot);
        }
        // Gone from the view (or from the zoom) while its page was read: drawn when it comes back.
        if (want !== zoom || closed || (allPages && !visible.has(slot))) return;
        const viewport = slot.page.getViewport({ scale: want * PDF_TO_CSS });
        const ratio = Math.min(window.devicePixelRatio || 1, Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height)));
        const canvas = slot.canvas;
        if (slot.task) slot.task.cancel();
        canvas.width = Math.floor(viewport.width * ratio);
        canvas.height = Math.floor(viewport.height * ratio);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        slot.task = slot.page.render({
          canvasContext: canvas.getContext('2d'), viewport, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null,
        });
        await slot.task.promise;
        slot.task = null;
        if (want !== zoom || closed) return;
        // Selectable text over the picture (pdf.js's text layer, styles in preview.css).
        slot.div.style.setProperty('--total-scale-factor', String(viewport.scale));
        slot.text.replaceChildren();
        slot.text.style.width = canvas.style.width;
        slot.text.style.height = canvas.style.height;
        const layer = new pdfjs.TextLayer({ textContentSource: slot.page.streamTextContent(), container: slot.text, viewport });
        await layer.render();
        slot.drawn = want;
      } catch (err) {
        if (err && err.name === 'RenderingCancelledException') return;
        slot.div.classList.add('pdf-page-failed');
      } finally {
        if (slot.drawing === want) slot.drawing = 0;
      }
    }
    function layout() {
      if (observer) observer.disconnect();
      visible.clear();
      slots.forEach((slot) => {
        free(slot);
        sizeSlot(slot);
      });
      if (allPages) {
        for (let number = 1; number <= count; number += 1) sizeSlot(slotAt(number));
        pages.replaceChildren(...slots.map((slot) => slot.div));
        observer = new IntersectionObserver((entries) => {
          for (const entry of entries) {
            const slot = slots[Number(entry.target.dataset.page) - 1];
            if (entry.isIntersecting) {
              visible.add(slot);
              draw(slot);
            } else {
              visible.delete(slot);
              free(slot);
            }
          }
        }, { root: pages, rootMargin: '150% 0px' });
        slots.forEach((slot) => observer.observe(slot.div));
        slots[current - 1].div.scrollIntoView({ block: 'start' });
      } else {
        observer = null;
        const slot = slotAt(current);
        sizeSlot(slot);
        pages.replaceChildren(slot.div);
        pages.scrollTop = 0;
        draw(slot);
      }
      showPage();
    }
    function showPage() {
      pageInput.value = String(current);
      prevButton.disabled = current <= 1;
      nextButton.disabled = current >= count;
    }
    function goTo(number) {
      if (!count) return;
      const target = Math.min(count, Math.max(1, Math.round(number) || 1));
      if (allPages) {
        slots[target - 1].div.scrollIntoView({ block: 'start' });
        current = target;
        showPage();
      } else if (target !== current) {
        free(slotAt(current));
        current = target;
        layout();
      } else {
        showPage();
      }
    }
    // In the all-pages view the current page is the one at the top third of the view.
    let scrollPending = false;
    pages.addEventListener('scroll', () => {
      if (!allPages || scrollPending) return;
      scrollPending = true;
      requestAnimationFrame(() => {
        scrollPending = false;
        const mark = pages.scrollTop + pages.clientHeight / 3;
        let low = 0;
        let high = slots.length - 1;
        while (low < high) {
          const mid = Math.ceil((low + high) / 2);
          if (slots[mid].div.offsetTop <= mark) low = mid;
          else high = mid - 1;
        }
        if (current !== low + 1) {
          current = low + 1;
          showPage();
        }
      });
    });

    // The page as wide as the view: its padding and, in the all-pages view, the scroll bar it will get.
    function fitZoom() {
      const base = (slots[0] && slots[0].base) || { width: 816 };
      const room = pages.clientWidth - (pages.offsetWidth > 640 ? 32 : 16) - (allPages && pages.scrollHeight <= pages.clientHeight ? 16 : 0);
      return Math.max(MIN_ZOOM, Math.min(2, Math.floor((room / base.width) * 100) / 100));
    }
    function setZoom(value) {
      const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(value * 100) / 100));
      zoomText.textContent = `${Math.round(next * 100)}%`;
      if (next === zoom || !count) return;
      zoom = next;
      const keep = current;
      if (allPages) {
        slots.forEach((slot) => {
          if (slot.task) slot.task.cancel();
          slot.drawn = 0;
          sizeSlot(slot);
        });
        slots[keep - 1].div.scrollIntoView({ block: 'start' });
        visible.forEach(draw);
      } else {
        sizeSlot(slotAt(current));
        draw(slotAt(current));
      }
    }

    pageInput.addEventListener('change', () => goTo(Number(pageInput.value)));
    pageInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') goTo(Number(pageInput.value)); });
    allSwitch.addEventListener('change', () => {
      allPages = allSwitch.checked;
      P.store.set('pdf-all-pages', allPages);
      layout();
    });
    f.dialog.addEventListener('keydown', (e) => {
      if (e.target === pageInput || e.ctrlKey || e.metaKey || e.altKey || !count) return;
      const keys = {
        ArrowLeft: () => goTo(current - 1), ArrowRight: () => goTo(current + 1),
        Home: () => goTo(1), End: () => goTo(count),
        '+': () => setZoom(zoom + STEP), '=': () => setZoom(zoom + STEP), '-': () => setZoom(zoom - STEP),
      };
      if (keys[e.key] && !(e.target.matches && e.target.matches('input[type="checkbox"]') && e.key.startsWith('Arrow'))) {
        e.preventDefault();
        keys[e.key]();
      }
    });

    (async () => {
      P.message(status, { busy: true, text: gettext('Loading') });
      try {
        pdfjs = await P.lib('pdf');
        const url = await ctx.inlineUrl(item);
        if (closed) return;
        const assets = P.config.libs.pdfAssets || '';
        task = pdfjs.getDocument({
          url: P.absolute(url),
          cMapUrl: `${assets}cmaps/`, cMapPacked: true,
          standardFontDataUrl: `${assets}standard_fonts/`,
          wasmUrl: `${assets}wasm/`,
          iccUrl: `${assets}iccs/`,
          disableAutoFetch: true,
          enableXfa: false,
          maxImageSize: MAX_IMAGE_PIXELS,
          verbosity: pdfjs.VerbosityLevel.ERRORS, // pdf.js's warnings about a broken file aren't the visitor's
        });
        task.onProgress = ({ loaded, total: size }) => {
          if (size) P.message(status, { busy: true, text: interpolate(gettext('Loading %(percent)s%'), { percent: Math.min(100, Math.round((loaded / size) * 100)) }, true) });
        };
        task.onPassword = async (update, reason) => {
          const password = await askPassword(reason === pdfjs.PasswordResponses.INCORRECT_PASSWORD);
          if (password) update(password);
          else fail(new Error(gettext("Without the password this PDF can't be opened.")));
        };
        doc = await task.promise;
        if (closed) return;
        count = doc.numPages;
        slots = [];
        if (count > ALL_PAGES_MAX) {
          allPages = false;
          allSwitch.checked = false;
          allSwitch.disabled = true;
          allSwitch.closest('label').dataset.tooltip = interpolate(ngettext('PDFs over %(limit)s page can only be viewed one page at a time', 'PDFs over %(limit)s pages can only be viewed one page at a time', ALL_PAGES_MAX), { limit: ALL_PAGES_MAX }, true);
        }
        const first = slotAt(1);
        first.page = await doc.getPage(1);
        const unscaled = first.page.getViewport({ scale: PDF_TO_CSS });
        first.base = { width: unscaled.width, height: unscaled.height };
        status.replaceChildren();
        toolbar.hidden = false;
        total.textContent = `/ ${doc.numPages}`;
        pageInput.max = String(doc.numPages);
        zoom = Math.min(1.5, fitZoom());
        zoomText.textContent = `${Math.round(zoom * 100)}%`;
        layout();
        pages.focus({ preventScroll: true });
      } catch (err) {
        fail(err);
      }
    })();
  }

  P.register(['pdf'], { open }, 'pdf');
})();
