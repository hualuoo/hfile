/* The image viewer: an overlay with zoom, rotation, reset and dragging, and the folder's other images
 * (双击预览模式, the browser's ctx.prefs.previewMode: all of them, or with 'only' just the clicked one). Keys:
 * W/↑ zoom in, S/↓ zoom out, A/← previous, D/→ next, Q/E rotate, Space reset, Esc close. The wheel
 * zooms at the pointer, two fingers pinch, a swipe at normal size goes to the next or previous image.
 * The gallery's 点击放大预览 opens the same viewer. */
(function () {
  'use strict';

  const P = App.preview;
  const { h } = App;
  const MIN = 0.1;
  const MAX = 10;
  const STEP = 1.2;

  function open(ctx, item, siblings) {
    const mode = (ctx.prefs && ctx.prefs.previewMode) || 'full';
    let list = mode === 'only' ? [item] : P.ofKind(siblings, ['image']);
    let index = list.findIndex((other) => P.sameItem(other, item));
    if (index < 0) {
      list = [item];
      index = 0;
    }

    const view = { scale: 1, rotate: 0, x: 0, y: 0 };
    const img = h('img', { class: 'piv-img', alt: '', draggable: 'false' });
    const status = h('div', { class: 'piv-status' });
    const stage = h('div', { class: 'piv-stage' }, img, status);
    const name = h('span', { class: 'piv-name' });
    const count = h('span', { class: 'piv-count' });
    const zoomText = h('span', { class: 'piv-zoom', 'aria-live': 'polite' });
    const prevButton = h('button', { type: 'button', class: 'icon-button piv-side piv-prev', 'aria-label': pgettext('image', 'Previous'), 'data-tooltip': gettext('Previous (A)'), onclick: () => go(-1) }, App.icon('chevron_left'));
    const nextButton = h('button', { type: 'button', class: 'icon-button piv-side piv-next', 'aria-label': pgettext('image', 'Next'), 'data-tooltip': gettext('Next (D)'), onclick: () => go(1) }, App.icon('chevron_right'));
    const downloadButton = P.can(ctx, 'download')
      ? P.button(App.icon('download'), gettext('Download'), () => P.download(ctx, list[index]))
      : null;
    const bar = h('div', { class: 'piv-bar' },
      P.button(App.icon('zoom_out'), gettext('Zoom out (S)'), () => zoomBy(1 / STEP)),
      zoomText,
      P.button(App.icon('zoom_in'), gettext('Zoom in (W)'), () => zoomBy(STEP)),
      P.button(App.icon('rotate_left'), gettext('Rotate left (Q)'), () => rotate(-90)),
      P.button(App.icon('rotate_right'), gettext('Rotate right (E)'), () => rotate(90)),
      P.button(App.icon('refresh'), gettext('Reset (Space)'), reset),
      downloadButton);
    const dialog = h('dialog', { class: 'preview-image-viewer', 'aria-label': interpolate(gettext('Image preview: %(name)s'), { name: item.name }, true) },
      stage,
      h('div', { class: 'piv-top' }, h('div', { class: 'piv-title' }, name, count),
        h('button', { type: 'button', class: 'icon-button', 'data-close': '', 'aria-label': gettext('Close'), 'data-tooltip': gettext('Close (Esc)') }, App.icon('close'))),
      prevButton, nextButton, bar);

    function apply() {
      img.style.transform = `translate(${view.x}px, ${view.y}px) rotate(${view.rotate}deg) scale(${view.scale})`;
      zoomText.textContent = `${Math.round(view.scale * 100)}%`;
      stage.classList.toggle('zoomed', view.scale > 1.001);
    }
    function reset() {
      Object.assign(view, { scale: 1, rotate: 0, x: 0, y: 0 });
      apply();
    }
    // Zooms by `factor` keeping the point (px, py) of the stage (from its centre) where it is.
    function zoomBy(factor, px = 0, py = 0) {
      const scale = Math.min(MAX, Math.max(MIN, view.scale * factor));
      const k = scale / view.scale;
      view.x = px - k * (px - view.x);
      view.y = py - k * (py - view.y);
      view.scale = scale;
      if (scale <= 1) {
        view.x = 0;
        view.y = 0;
      }
      apply();
    }
    function rotate(degrees) {
      view.rotate += degrees;
      apply();
    }

    let loadSeq = 0;
    function show(at) {
      index = (at + list.length) % list.length;
      const current = list[index];
      const seq = ++loadSeq;
      reset();
      name.textContent = current.name;
      count.textContent = list.length > 1 ? `${index + 1} / ${list.length}` : '';
      dialog.setAttribute('aria-label', interpolate(gettext('Image preview: %(name)s'), { name: current.name }, true));
      prevButton.hidden = nextButton.hidden = list.length < 2;
      img.alt = current.name;
      img.classList.remove('loaded');
      if (!current.url) {
        img.removeAttribute('src');
        P.message(status, { icon: 'lock', text: gettext("You don't have permission to preview this image.") });
        return;
      }
      P.message(status, { busy: true, text: gettext('Loading') });
      img.onload = () => {
        if (seq !== loadSeq) return;
        status.replaceChildren();
        img.classList.add('loaded');
        // the next one, so that going on doesn't wait
        const next = list[(index + 1) % list.length];
        if (list.length > 1 && next.url) new Image().src = next.url;
      };
      img.onerror = () => {
        if (seq !== loadSeq) return;
        P.message(status, {
          icon: 'error', text: gettext("The image didn't load."), detail: gettext('The browser may not support this image format.'),
          actions: [P.downloadButton(ctx, current)].filter(Boolean),
        });
      };
      img.src = current.url;
    }
    function go(step) {
      if (list.length > 1) show(index + step);
    }

    // ---- pointers: drag to move (zoomed), swipe to go on (not zoomed), two fingers pinch
    const pointers = new Map();
    let gesture = null;
    let moved = false; // the last press moved: its click doesn't close the viewer
    stage.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      // A press on a control of the message (下载 when the picture didn't load) is that control's: captured,
      // its click would go to the stage, which closes the viewer.
      if (e.target.closest('button, a')) return;
      stage.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 1) moved = false;
      gesture = pointers.size === 2 ? pinchStart() : { kind: 'drag', x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
    });
    stage.addEventListener('pointermove', (e) => {
      if (!pointers.has(e.pointerId) || !gesture) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (gesture.kind !== 'drag' || Math.hypot(e.clientX - gesture.x, e.clientY - gesture.y) > 5) moved = true;
      if (gesture.kind === 'pinch' && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        const centre = centreOf((a.x + b.x) / 2, (a.y + b.y) / 2);
        zoomBy(distance / gesture.distance, centre.x, centre.y);
        gesture.distance = distance;
      } else if (gesture.kind === 'drag' && view.scale > 1.001) {
        view.x = gesture.vx + e.clientX - gesture.x;
        view.y = gesture.vy + e.clientY - gesture.y;
        apply();
      }
    });
    function endPointer(e) {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      if (gesture && gesture.kind === 'drag' && view.scale <= 1.001 && pointers.size === 0) {
        const dx = e.clientX - gesture.x;
        const dy = e.clientY - gesture.y;
        if (Math.abs(dx) > 60 && Math.abs(dx) > 2 * Math.abs(dy)) go(dx < 0 ? 1 : -1);
      }
      gesture = pointers.size ? { kind: 'none' } : null;
    }
    stage.addEventListener('pointerup', endPointer);
    stage.addEventListener('pointercancel', endPointer);
    function pinchStart() {
      const [a, b] = [...pointers.values()];
      return { kind: 'pinch', distance: Math.hypot(a.x - b.x, a.y - b.y) || 1 };
    }
    function centreOf(clientX, clientY) {
      const box = stage.getBoundingClientRect();
      return { x: clientX - (box.left + box.width / 2), y: clientY - (box.top + box.height / 2) };
    }
    stage.addEventListener('wheel', (e) => {
      e.preventDefault();
      const centre = centreOf(e.clientX, e.clientY);
      zoomBy(e.deltaY < 0 ? STEP : 1 / STEP, centre.x, centre.y);
    }, { passive: false });
    stage.addEventListener('dblclick', (e) => {
      if (view.scale > 1.001) {
        reset();
        return;
      }
      const centre = centreOf(e.clientX, e.clientY);
      zoomBy(2, centre.x, centre.y);
    });
    // A click on the dark space around the picture closes the viewer.
    stage.addEventListener('click', (e) => {
      if (e.target === stage && view.scale <= 1.001 && !moved) dialog.close();
    });

    // ---- keys
    dialog.addEventListener('keydown', (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
      const actions = {
        w: () => zoomBy(STEP), ArrowUp: () => zoomBy(STEP),
        s: () => zoomBy(1 / STEP), ArrowDown: () => zoomBy(1 / STEP),
        a: () => go(-1), ArrowLeft: () => go(-1),
        d: () => go(1), ArrowRight: () => go(1),
        q: () => rotate(-90), e: () => rotate(90),
        ' ': reset,
      };
      if (actions[key]) {
        e.preventDefault();
        actions[key]();
      }
    });

    dialog.addEventListener('close', () => {
      img.onload = img.onerror = null;
      img.removeAttribute('src');
      dialog.remove();
    });
    document.body.append(dialog);
    App.openDialog(dialog);
    show(index);
  }

  P.register(['image'], { open }, 'image');
})();
