/* Pieces every page that shows books needs.
 *
 * Loaded after card.js (which owns `esc` and `tagHtml`) and before the
 * page script. Kept separate so browse, shelf and cleanup cannot drift into
 * three slightly different tiles.
 */
'use strict';

/* These live here rather than in card.js because the two files are never
   loaded on the same page: card.js serves the scan card and the book page,
   shared-ui.js serves the browsing pages. Sharing them through a third file
   would only add a script tag to every page to save eight lines. */
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function tagHtml(status) {
  return `<span class="tag ${String(status || '').replace(/\s+/g, '')}">${esc(status)}</span>`;
}

/* A book tile. `badges[0]` is the strongest — LOST outranks WANTED, because
   what happened to a book outranks what you would like to happen next. */
function bookTile(item, onOpen) {
  const b = document.createElement('button');
  b.className = 'tile';
  b.title = `${item.title}${item.author ? ' — ' + item.author : ''}`;

  const src = item.cover_id ? `covers/id/${item.cover_id}?size=M`
            : item.isbn13 ? `covers/${encodeURIComponent(item.isbn13)}?size=M` : null;
  const art = src
    ? `<img loading="lazy" decoding="async" src="${src}" alt="" onerror="this.remove()">`
    : '';
  const primary = (item.badges && item.badges[0]) || item.status;
  const corner = primary && primary !== 'UNCONFIRMED'
    ? `<span class="corner tag ${primary.replace(/\s+/g, '')}">${esc(primary)}</span>` : '';

  b.innerHTML =
    `<span class="tile-art">${art}` +
    `<span class="fallback">${esc(item.title)}</span>${corner}</span>` +
    `<span class="tile-title">${esc(item.title)}</span>` +
    `<span class="tile-author">${esc(item.author || '')}</span>`;
  b.onclick = () => onOpen(item.work_id);
  return b;
}

function bookRow(item, onOpen) {
  const b = document.createElement('button');
  b.className = 'hit';
  const src = item.cover_id ? `covers/id/${item.cover_id}?size=M`
            : item.isbn13 ? `covers/${encodeURIComponent(item.isbn13)}?size=M` : null;
  const badges = (item.badges && item.badges.length ? item.badges : [item.status])
    .filter(Boolean).map(tagHtml).join('');
  b.innerHTML =
    (src ? `<img class="hit-cover" loading="lazy" src="${src}" alt=""
                 onerror="this.style.visibility='hidden'">`
         : '<span class="hit-cover"></span>') +
    `<span class="hit-body"><span class="hit-title">${esc(item.title)}</span>` +
    `<span class="hit-sub">${esc([item.author || 'unknown', item.series, item.year]
      .filter(Boolean).join(' · '))}</span></span>` + badges;
  b.onclick = () => onOpen(item.work_id);
  return b;
}

/* Grid or list. Genuinely different jobs: grid to recognise a cover, list to
   scan titles and statuses quickly. Remembered across pages and visits. */
function viewMode() { return localStorage.getItem('stacks-view') || 'grid'; }
function setViewMode(v) { localStorage.setItem('stacks-view', v); }

function viewToggle(onChange) {
  const div = document.createElement('div');
  div.className = 'viewtoggle';
  const cur = viewMode();
  div.innerHTML =
    `<button data-v="grid" class="${cur === 'grid' ? 'on' : ''}">▦ Grid</button>` +
    `<button data-v="list" class="${cur === 'list' ? 'on' : ''}">☰ List</button>`;
  div.querySelectorAll('button').forEach((b) => {
    b.onclick = () => { setViewMode(b.dataset.v); onChange(); };
  });
  return div;
}

function renderBooks(host, items, onOpen) {
  host.innerHTML = '';
  /* select.js, when the page includes it, turns these into checkboxes-in-
     spirit. Pages that do not want selection simply do not load it, and
     nothing here needs to know the difference. */
  if (typeof rememberVisible === 'function') rememberVisible(items);
  if (!items.length) {
    host.innerHTML = '<p class="note" style="padding:0 1rem">Nothing here.</p>';
    return;
  }
  if (viewMode() === 'grid') {
    const grid = document.createElement('div');
    grid.className = 'grid-results';
    items.forEach((i) => {
      const node = bookTile(i, onOpen);
      if (typeof decorateSelectable === 'function') decorateSelectable(node, i);
      grid.appendChild(node);
    });
    host.appendChild(grid);
  } else {
    const list = document.createElement('div');
    list.style.cssText = 'padding:0 1rem; display:flex; flex-direction:column; gap:.4rem';
    items.forEach((i) => {
      const node = bookRow(i, onOpen);
      if (typeof decorateSelectable === 'function') decorateSelectable(node, i);
      list.appendChild(node);
    });
    host.appendChild(list);
  }
}

/* Open a book. One destination, always: its own page.
 *
 * This used to open a modal sheet whose card markup was duplicated into three
 * HTML files and which had no room for the editor. A book now has exactly one
 * home, with a URL that can be bookmarked, shared, or reloaded.
 */
function openSheet(workId) {
  if (!workId) return;
  location.href = `book.html?id=${encodeURIComponent(workId)}`;
}

function wireSheet() { /* nothing to wire — books get their own page now */ }
