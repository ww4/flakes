/* One book, on its own page.
 *
 * The scan card is a DECISION surface — what is this, do I own it, buy or not.
 * Everything else about a book belongs here, where there is room for it: the
 * full editor, every copy, every printing, and a stable URL that can be
 * bookmarked or sent to someone.
 */
'use strict';

const workId = new URLSearchParams(location.search).get('id');
let current = null;

/* A phone has no console. app.js reports errors home; this page did not, so
 * an editor rejection here died in silence. Best-effort and throttled by the
 * browser's own coalescing; never awaited. */
window.addEventListener('unhandledrejection', (e) => {
  try {
    fetch('api/client-event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'js_rejection', page: 'book',
                             detail: { message: String(e.reason && e.reason.message || e.reason).slice(0, 300) } }),
    }).catch(() => {});
  } catch { /* reporting must never break the page */ }
});
window.addEventListener('error', (e) => {
  try {
    fetch('api/client-event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'js_error', page: 'book',
                             detail: { message: String(e.message || '').slice(0, 300) } }),
    }).catch(() => {});
  } catch { /* ditto */ }
});

function paint(card) {
  current = card;
  document.getElementById('page-title').textContent = card.title || 'Book';
  document.title = `stacks — ${card.title || 'book'}`;
  renderBookCard(card, null);
  wireActions(card);
  document.getElementById('loading').hidden = true;
  // The editor is the point of this page, so it starts open rather than as a
  // collapsed summary nobody finds.
  const details = document.querySelector('#c-edit details');
  if (details) details.open = true;
}

/* One label that never changes, and pressing it is idempotent.
 *
 * A button whose text flips with state is ambiguous — "Not on the shelf" reads
 * as either a statement of fact or an instruction, and those imply opposite
 * outcomes. Marking something missing is a deliberate act and lives in the copy
 * editor below, as an explicit field.
 */
function wireActions(card) {
  const have = document.getElementById('btn-have');
  const another = document.getElementById('btn-another');

  have.textContent = '✓ I have this';
  have.className = 'primary';
  have.onclick = () => act('confirm', have);

  another.hidden = !card.work_id;
  another.textContent = '+ Another copy';
  another.onclick = () => act('add', another);
}

async function act(action, btn) {
  const isbn = (current && current.scanned_isbn)
    || (current && (current.editions || []).map((e) => e.isbn13).filter(Boolean)[0]);
  if (!isbn) {
    const was = btn.textContent;
    btn.textContent = 'needs an ISBN first';
    setTimeout(() => { btn.textContent = was; }, 2500);
    return;
  }
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = 'saving…';
  try {
    const res = await fetch(`api/confirm/${encodeURIComponent(isbn)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    if (!res.ok) throw new Error(res.status);
    const j = await res.json();
    paint(j.card);
    flash(j.message);
    // paint() -> wireActions() resets the label and class but a disabled
    // property survives it — "+ Another copy" (whose whole purpose is
    // entering a STACK of sale purchases) used to work exactly once per
    // page load.
    btn.disabled = false;
  } catch {
    btn.textContent = 'failed — needs a connection';
    setTimeout(() => { btn.textContent = was; btn.disabled = false; }, 2500);
  }
}

function flash(message) {
  const p = document.createElement('p');
  p.className = 'flash';
  p.textContent = message;
  document.getElementById('c-actions').insertAdjacentElement('beforebegin', p);
  setTimeout(() => p.remove(), 4000);
}

async function load() {
  if (!workId) {
    document.getElementById('loading').textContent = 'No book specified.';
    return;
  }
  try {
    const res = await fetch(`api/work/${encodeURIComponent(workId)}`);
    if (!res.ok) throw new Error(res.status);
    paint(await res.json());
  } catch {
    document.getElementById('loading').textContent =
      'Could not load this book. This page needs a connection.';
  }
}

load();

// The book page is reachable directly (bookmarks, shared links) — it must
// also plant the offline shell, not just benefit from it.
if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
