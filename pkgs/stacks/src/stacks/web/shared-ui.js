/* Pieces every page that shows books needs.
 *
 * Loaded after card.js (which owns `esc` and `tagHtml`) and before the
 * page script. Kept separate so browse, shelf and cleanup cannot drift into
 * three slightly different tiles.
 */
'use strict';

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
  if (!items.length) {
    host.innerHTML = '<p class="note" style="padding:0 1rem">Nothing here.</p>';
    return;
  }
  if (viewMode() === 'grid') {
    const grid = document.createElement('div');
    grid.className = 'grid-results';
    items.forEach((i) => grid.appendChild(bookTile(i, onOpen)));
    host.appendChild(grid);
  } else {
    const list = document.createElement('div');
    list.style.cssText = 'padding:0 1rem; display:flex; flex-direction:column; gap:.4rem';
    items.forEach((i) => list.appendChild(bookRow(i, onOpen)));
    host.appendChild(list);
  }
}

/* Open a book in the sheet. Shared so every page behaves identically, and the
   sheet opens in a `finally` — a render failure may be ugly but never
   invisible. */
async function openSheet(workId) {
  const sheet = document.getElementById('sheet');
  try {
    let card;
    try {
      const res = await fetch(`api/work/${workId}`);
      if (!res.ok) throw new Error(res.status);
      card = await res.json();
    } catch {
      card = { title: 'Could not load this book', verdict: 'UNKNOWN',
               status: 'UNREADABLE', recommendation: 'The server did not answer.' };
    }
    renderBookCard(card, null);
  } catch (err) {
    console.error('render failed', err);
  } finally {
    sheet.classList.add('show');
    const inner = sheet.querySelector('.sheet-inner');
    if (inner) inner.scrollTop = 0;
  }
}

function wireSheet() {
  const sheet = document.getElementById('sheet');
  if (!sheet) return;
  sheet.addEventListener('click', (e) => {
    if (e.target === sheet) sheet.classList.remove('show');
  });
  const close = document.getElementById('sheet-close');
  if (close) close.onclick = () => sheet.classList.remove('show');
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') sheet.classList.remove('show');
  });
}
