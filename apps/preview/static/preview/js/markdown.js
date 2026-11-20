/* The Markdown viewer (.md, .markdown). The page never renders Markdown itself (decision D22): the
 * server's POST /api/v1/preview/markdown/ reads the file behind the viewer's signed URL and answers what
 * apps.core.markdown.render() (untrusted) makes of it. That HTML is parsed in a document of its own (P.inert:
 * nothing loads there), then:
 *
 * - relative links and pictures are looked up among the folder's own files (`siblings`, what the
 *   visitor already sees): a picture gets that file's URL, a link opens it (another Markdown file in
 *   this viewer). Anything else relative is dropped, so the text grants nothing the visitor lacks;
 * - pictures from other sites become links (no request leaves for them: the CSP, and the visitor's IP
 *   stays with this site); links to other sites open in a new tab, rel="noopener noreferrer";
 * - code blocks get highlight.js and a copy button.
 * 查看源文件 opens the text viewer (and its editor). */
(function () {
  'use strict';

  const P = App.preview;
  const { h } = App;
  const BASE = 'https://markdown.invalid';

  const encodePath = (path) => path.split('/').map((part) => encodeURIComponent(part)).join('/');

  function rewrite(fragment, ctx, item, siblings, openSibling) {
    const path = P.pathOf(ctx, item);
    const folder = path.slice(0, path.lastIndexOf('/') + 1); // "/docs/", "/"
    const base = new URL(BASE + encodePath(folder));
    const byPath = new Map(siblings.filter(P.isFile).map((sibling) => [folder + sibling.name, sibling]));

    function target(value) {
      let url;
      try {
        url = new URL(value, base);
      } catch (e) {
        return { kind: 'drop' };
      }
      if (url.origin === base.origin) {
        let found = null;
        try {
          found = byPath.get(decodeURIComponent(url.pathname)) || null;
        } catch (e) {
          // a broken %-escape
        }
        return found && found.url ? { kind: 'sibling', sibling: found } : { kind: 'drop' };
      }
      if (url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:') return { kind: 'external', href: url.href };
      return { kind: 'drop' };
    }

    // <picture>'s other sources and srcset: only the one src, which is checked below.
    fragment.querySelectorAll('source').forEach((source) => source.remove());
    fragment.querySelectorAll('[srcset]').forEach((el) => el.removeAttribute('srcset'));

    for (const img of [...fragment.querySelectorAll('img')]) {
      const src = img.getAttribute('src') || '';
      if (/^data:image\//i.test(src.trim())) continue; // PNG, GIF, JPEG or WebP: the sanitizer allows only these
      const found = target(src);
      const alt = img.getAttribute('alt') || '';
      if (found.kind === 'sibling') {
        img.setAttribute('src', found.sibling.url);
        img.setAttribute('loading', 'lazy');
        continue;
      }
      if (found.kind === 'external') {
        const label = [App.icon('image'), alt || gettext('External image')];
        img.replaceWith(img.closest('a')
          ? h('span', { class: 'md-image-link', title: found.href }, ...label)
          : h('a', { class: 'md-image-link', href: found.href, target: '_blank', rel: 'noopener noreferrer', title: found.href }, ...label));
        continue;
      }
      img.replaceWith(alt ? h('span', { class: 'md-image-missing', text: `[${alt}]` }) : '');
    }

    for (const link of [...fragment.querySelectorAll('a[href]')]) {
      if (link.classList.contains('md-image-link')) continue;
      const found = target(link.getAttribute('href') || '');
      if (found.kind === 'external') {
        link.setAttribute('target', '_blank');
        link.setAttribute('rel', 'noopener noreferrer');
      } else if (found.kind === 'sibling') {
        link.setAttribute('href', found.sibling.url);
        link.setAttribute('target', '_blank');
        link.setAttribute('rel', 'noopener noreferrer');
        if (found.sibling.kind === 'markdown') {
          link.addEventListener('click', (e) => {
            // a link inside this one (HTML allows it in a table cell) is that link's click
            if (e.ctrlKey || e.metaKey || e.shiftKey || e.target.closest('a') !== link) return;
            e.preventDefault();
            openSibling(found.sibling);
          });
        }
      } else {
        link.removeAttribute('href');
        link.classList.add('md-link-dropped');
      }
    }

    for (const table of [...fragment.querySelectorAll('table')]) {
      const wrap = h('div', { class: 'md-table' });
      table.replaceWith(wrap);
      wrap.append(table);
    }

    const blocks = [];
    for (const pre of [...fragment.querySelectorAll('pre')]) {
      const code = pre.querySelector('code') || pre;
      const copy = h('button', { type: 'button', class: 'icon-button small md-copy', 'aria-label': gettext('Copy code'), 'data-tooltip': gettext('Copy') }, App.icon('content_copy'));
      copy.addEventListener('click', async () => {
        App.toast((await App.copyText(code.textContent)) ? gettext('Copied') : gettext('Copy failed'), '');
      });
      const wrap = h('div', { class: 'md-code' });
      pre.replaceWith(wrap);
      wrap.append(pre, copy);
      const language = [...code.classList].find((name) => name.startsWith('language-'));
      if (language) blocks.push([code, language.slice('language-'.length).toLowerCase()]);
    }
    return blocks;
  }

  // At most this much code of a document is coloured (as text.js's own limit): the page stays responsive.
  const HIGHLIGHT_MAX = 512 * 1024;

  async function highlightBlocks(blocks) {
    if (!blocks.length) return;
    let hljs;
    try {
      hljs = await P.lib('hljs');
    } catch (e) {
      return; // the code stays plain
    }
    let budget = HIGHLIGHT_MAX;
    for (const [code, language] of blocks) {
      const length = code.textContent.length;
      if (!hljs.getLanguage(language) || length > budget) continue;
      budget -= length;
      const nodes = P.highlight(hljs, code.textContent, language);
      if (nodes) {
        code.replaceChildren(nodes);
        code.classList.add('hljs');
      }
    }
  }

  function open(ctx, item, siblings) {
    siblings = siblings || [];
    const article = h('article', { class: 'md-body' });
    const asText = () => {
      f.close();
      P.viewers.text.open(ctx, item, siblings);
    };
    const f = P.frame({
      title: item.name, className: 'preview-markdown', body: article,
      actions: [P.viewers.text ? P.button(App.icon('code'), gettext('View source'), asText) : null, P.downloadAction(ctx, item)],
    });
    const asTextButton = () => (P.viewers.text ? h('button', { type: 'button', class: 'btn btn-tonal', onclick: asText }, App.icon('article'), gettext('View as text')) : null);
    const limit = P.limits(ctx).markdown;
    if (item.size !== null && item.size !== undefined && item.size > limit) {
      P.tooLarge(article, ctx, item, limit, [asTextButton()].filter(Boolean));
      return;
    }
    P.message(article, { busy: true, text: gettext('Loading') });
    (async () => {
      try {
        const url = await ctx.inlineUrl(item);
        const data = await App.api('POST', P.config.urls.markdown, { url: App.pageUrl(url) });
        const content = P.inert(data.html); // apps.core.markdown.render() on the server, rewritten before it shows
        const blocks = rewrite(content, ctx, item, siblings, (sibling) => {
          f.close();
          open(ctx, sibling, siblings);
        });
        article.replaceChildren(P.adopt(content));
        if (!article.childNodes.length) P.message(article, { icon: 'description', text: gettext('This file is empty.') });
        highlightBlocks(blocks);
      } catch (err) {
        if (err.data && err.data.code === 'too_large') {
          P.tooLarge(article, ctx, { ...item, size: err.data.size }, err.data.limit, [asTextButton()].filter(Boolean));
          return;
        }
        P.message(article, {
          icon: 'error', text: gettext("The Markdown didn't load."), detail: err.message,
          actions: [asTextButton(), P.downloadButton(ctx, item)].filter(Boolean),
        });
      }
    })();
  }

  App.preview.rewriteMarkdown = rewrite;
  P.register(['markdown'], { open }, 'markdown');
})();
