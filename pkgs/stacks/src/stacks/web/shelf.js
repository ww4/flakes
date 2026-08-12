/* One shelf, in full, on its own page. */
'use strict';

const params = new URLSearchParams(location.search);
const shelfKey = params.get('key') || 'lost';
let shelfItems = [];

async function loadShelf() {
  try {
    const res = await fetch(`api/shelf/${shelfKey}`);
    if (!res.ok) throw new Error(res.status);
    const sh = await res.json();
    document.getElementById('shelf-title').textContent = sh.title;
    document.getElementById('shelf-sub').textContent =
      `${sh.total} book${sh.total === 1 ? '' : 's'}${sh.subtitle ? ' · ' + sh.subtitle : ''}`;
    document.title = `stacks — ${sh.title}`;
    shelfItems = sh.items;

    const tog = document.getElementById('shelf-view');
    tog.innerHTML = '';
    tog.appendChild(viewToggle(paint));
    paint();
    document.getElementById('shelf-loading').hidden = true;
  } catch {
    document.getElementById('shelf-loading').textContent =
      'Could not load this shelf. Browsing needs a connection.';
  }
}

function paint() {
  renderBooks(document.getElementById('shelf-body'), shelfItems, openSheet);
}

wireSheet();
loadShelf();
