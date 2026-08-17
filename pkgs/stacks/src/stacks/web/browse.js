/* Browse — horizontal shelves over the whole library.
 *
 * Three thousand books is unusable as a list. The rows are the questions this
 * library actually raises: what the flood took, what has been replaced, what is
 * still being collected.
 *
 * No carousel library. CSS scroll-snap does the scrolling and Chrome's
 * ::scroll-button() adds arrows where it exists; both cost nothing and neither
 * needs a build step or a CDN the offline shell could not reach.
 */
'use strict';

const el = (id) => document.getElementById(id);

function shelfNode(shelf) {
  const sec = document.createElement('section');
  sec.className = 'shelf';

  const head = document.createElement('div');
  head.className = 'shelf-head';
  // The heading is a link: a shelf worth a row is worth a page of its own.
  const href = `shelf.html?key=${encodeURIComponent(shelf.key)}`;
  head.innerHTML =
    `<a class="shelf-title" href="${href}" style="color:inherit;text-decoration:none">` +
    `${esc(shelf.title)} \u203a</a>` +
    (shelf.subtitle ? `<span class="shelf-sub">${esc(shelf.subtitle)}</span>` : '') +
    (shelf.total > shelf.items.length
      ? `<a class="shelf-more" href="${href}" style="text-decoration:none">` +
        `all ${shelf.total} \u203a</a>` : '');

  const rail = document.createElement('div');
  rail.className = 'rail';
  shelf.items.forEach((it) => rail.appendChild(bookTile(it, openSheet)));

  sec.append(head, rail);
  return sec;
}

async function load() {
  try {
    const res = await fetch('api/browse');
    if (!res.ok) throw new Error(res.status);
    const shelves = await res.json();
    const host = el('shelves');
    host.innerHTML = '';
    shelves.forEach((s) => host.appendChild(shelfNode(s)));
    el('loading').hidden = true;
    if (!shelves.length) {
      el('loading').hidden = false;
      el('loading').textContent = 'No shelves yet — import a catalog first.';
    }
  } catch (err) {
    el('loading').textContent =
      'Could not load shelves. Browsing needs a connection; the scanner works offline.';
  }
}

/* The last search, so selection can repaint without re-querying. */
let lastHits = null;
let lastQ = '';

/* select.js repaints through this name on every page that has a list. */
function paint() {
  if (lastHits) renderSearch(lastHits, lastQ);
}

function renderSearch(hits, q) {
  lastHits = hits;
  lastQ = q;
  const host = el('shelves');
  host.innerHTML = '';

  const head = document.createElement('div');
  head.className = 'shelf-head';
  head.innerHTML =
    `<h2 class="shelf-title">${hits.length} match${hits.length === 1 ? '' : 'es'}</h2>` +
    `<span class="shelf-sub">for \u201c${esc(q)}\u201d</span>`;
  head.appendChild(selectToggleButton());
  head.appendChild(viewToggle(() => renderSearch(hits, q)));
  host.appendChild(head);

  const body = document.createElement('div');
  host.appendChild(body);
  renderBooks(body, hits, openSheet);

  const back = document.createElement('button');
  back.className = 'shelf-more';
  back.style.margin = '1rem';
  back.textContent = '\u2190 back to shelves';
  back.onclick = () => { el('browse-q').value = ''; load(); };
  host.appendChild(back);
}

el('browse-search').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = el('browse-q').value.trim();
  if (q.length < 2) return;
  try {
    const res = await fetch(`api/search?q=${encodeURIComponent(q)}`);
    // A 500 used to flow into res.json()/renderSearch and be misreported
    // as "Search needs a connection." while fully online.
    if (!res.ok) throw new Error(res.status);
    const hits = await res.json();
    if (hits.length === 1) return openSheet(hits[0].work_id);
    renderSearch(hits, q);
  } catch {
    el('loading').hidden = false;
    el('loading').textContent = 'Search needs a connection.';
  }
});

wireSheet();
load();
