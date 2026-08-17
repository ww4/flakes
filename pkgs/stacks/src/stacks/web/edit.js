/* Editing a book — shared by the scanner and the browse sheet.
 *
 * Much of this catalog came out of hand-written documents and a crowd-edited
 * database, so correcting it is not an admin afterthought: it is how the data
 * gets good. Every surface that shows a book can fix it.
 *
 * Server-only, deliberately. These are writes, and a write queued on a phone
 * that may never reconnect is worse than a control that admits it needs a
 * connection.
 */
'use strict';

const COPY_STATES = ['present', 'unverified', 'lost_flood', 'loaned', 'missing', 'discarded'];

let editWorkId = null;
let onSaved = null;

function editHost() {
  return document.getElementById('c-edit');
}

/* Build the edit panel for a card. Called by the renderer each time. */
function mountEditor(card, refresh) {
  const host = editHost();
  if (!host) return;
  editWorkId = card.work_id || null;
  onSaved = refresh;

  if (!editWorkId) {
    host.innerHTML = '';
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.innerHTML = `
    <details id="edit-details">
      <summary>Edit this book</summary>
      <div class="edit">
        <label for="e-title">Title</label>
        <input id="e-title" value="${escAttr(card.title)}">
        <div class="edit-row">
          <div>
            <label for="e-author">Author</label>
            <input id="e-author" value="${escAttr(card.author)}">
          </div>
          <div>
            <label for="e-series">Series</label>
            <input id="e-series" value="${escAttr(card.series)}">
          </div>
        </div>
        <div class="edit-row">
          <div>
            <label for="e-pos">Position in series</label>
            <input id="e-pos" type="number" step="0.5" value="${card.series_position ?? ''}">
          </div>
          <div>
            <label for="e-want">Copies wanted</label>
            <input id="e-want" type="number" min="0" value="${card.desired_copies ?? 1}">
          </div>
        </div>
        <label for="e-isbn">Add an ISBN</label>
        <div class="edit-row">
          <input id="e-isbn" inputmode="numeric" placeholder="978…">
          <button id="e-addisbn" style="flex:0 0 auto">Add</button>
        </div>
        <p class="note" id="e-isbnmsg" style="margin:.3rem 0 0"></p>
        <div id="e-isbnlist">${isbnRows(card)}</div>

        <label for="e-desc">Description</label>
        <textarea id="e-desc">${esc(card.description || '')}</textarea>
        <div class="edit-row" style="margin-top:.8rem">
          <button class="primary" id="e-save">Save</button>
          <button id="e-covers">Choose cover</button>
        </div>
        <div id="e-covergrid"></div>

        <label style="margin-top:1rem">Copies</label>
        <div id="e-copies">${copyRows(card)}</div>

        <div class="edit-row" style="margin-top:1.2rem">
          <button class="danger" id="e-delete">Delete this book entirely</button>
        </div>
      </div>
    </details>`;

  document.getElementById('e-save').onclick = saveWork;
  document.getElementById('e-addisbn').onclick = addIsbn;
  wireIsbnRows();
  document.getElementById('e-covers').onclick = loadCovers;
  document.getElementById('e-delete').onclick = () => deleteWork(card.title);
  wireCopyRows();
}

// `esc` comes from card.js, which always loads first. Redeclaring it here was
// a SyntaxError that killed this whole file at load — while leaving
// `mountEditor` hoisted onto the global object, so callers' typeof guard still
// passed and they walked into uninitialised `let` bindings.
const escAttr = (s) => esc(s == null ? '' : s);

/* An ISBN is what makes a book scannable. Around 300 flood losses were
   destroyed before anyone catalogued them and have none at all, so this is the
   edit that turns a bare title into a book the scanner can recognise. */
function isbnRows(card) {
  const eds = (card.editions || []).filter((e) => e.isbn13);
  if (!eds.length) {
    return '<p class="note">No ISBN recorded — this book cannot be scanned yet.</p>';
  }
  return '<div style="margin-top:.4rem">' + eds.map((e) => `
    <div class="copy-edit">
      <span class="mono" style="flex:1">${esc(e.isbn13)}</span>
      <span class="faint" style="flex:1">${esc([e.publisher, e.year].filter(Boolean).join(' · '))}</span>
      <button class="danger" data-isbn="${esc(e.isbn13)}" title="Remove this printing">✕</button>
    </div>`).join('') + '</div>';
}

function wireIsbnRows() {
  document.querySelectorAll('#e-isbnlist [data-isbn]').forEach((b) => {
    b.onclick = () => removeIsbn(b.dataset.isbn);
  });
}

async function addIsbn() {
  const input = document.getElementById('e-isbn');
  const msg = document.getElementById('e-isbnmsg');
  const raw = input.value.trim();
  if (!raw) return;
  msg.textContent = 'checking…';
  try {
    const card = await apiJSON(`api/work/${editWorkId}/isbn`, 'POST', { isbn13: raw });
    input.value = '';
    msg.textContent = 'Added — this book can be scanned now.';
    if (onSaved) onSaved(card);
  } catch (err) {
    // The server explains WHY: a failed check digit, or the ISBN already
    // belonging to another book. Both are worth reading rather than "error".
    let detail = String(err.message || '');
    try { detail = JSON.parse(detail).detail || detail; } catch { /* plain text */ }
    msg.textContent = detail;
  }
}

async function removeIsbn(isbn) {
  if (!confirm(`Remove ISBN ${isbn} from this book?`)) return;
  const msg = document.getElementById('e-isbnmsg');
  try {
    const eds = await apiJSON(`api/work/${editWorkId}/edition-ids`, 'GET');
    const id = eds[isbn];
    if (!id) return;
    const card = await apiJSON(`api/edition/${id}`, 'DELETE');
    if (onSaved) onSaved(card);
  } catch {
    // Unguarded, this rejection vanished: the user confirmed the dialog,
    // nothing happened, and nothing said why.
    if (msg) msg.textContent = 'Could not remove — needs a connection.';
  }
}

function copyRows(card) {
  if (!card.copies || !card.copies.length) return '<p class="note">No copies recorded.</p>';
  return card.copies.map((c, i) => `
    <div class="copy-edit" data-i="${i}">
      <select data-f="status">
        ${COPY_STATES.map((st) =>
          `<option value="${st}"${st === c.status ? ' selected' : ''}>${st}</option>`).join('')}
      </select>
      <input data-f="collections" value="${escAttr((c.collections || []).join(', '))}"
             placeholder="collections">
      <button data-act="save">Save</button>
      <button class="danger" data-act="del" title="Remove this record">✕</button>
    </div>`).join('');
}

/* Copy ids are not in the card payload, so rows are addressed by position and
   resolved server-side on save. Keeping the card lean is worth one extra
   round trip on an action nobody performs in bulk. */
function wireCopyRows() {
  document.querySelectorAll('#e-copies .copy-edit').forEach((row) => {
    row.querySelector('[data-act="save"]').onclick = () => saveCopy(row);
    row.querySelector('[data-act="del"]').onclick = () => deleteCopy(row);
  });
}

async function apiJSON(url, method, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function saveWork() {
  const btn = document.getElementById('e-save');
  btn.disabled = true; btn.textContent = 'saving…';
  try {
    const card = await apiJSON(`api/work/${editWorkId}`, 'PATCH', {
      title: document.getElementById('e-title').value,
      author: document.getElementById('e-author').value,
      series: document.getElementById('e-series').value,
      series_position: parseFloat(document.getElementById('e-pos').value) || null,
      desired_copies: parseInt(document.getElementById('e-want').value, 10),
      description: document.getElementById('e-desc').value,
    });
    if (onSaved) onSaved(card);
  } catch (err) {
    btn.textContent = 'failed — needs a connection';
    setTimeout(() => { btn.textContent = 'Save'; btn.disabled = false; }, 2500);
    return;
  }
  btn.disabled = false; btn.textContent = 'Save';
}

async function loadCovers() {
  const host = document.getElementById('e-covergrid');
  host.innerHTML = '<p class="note">loading covers…</p>';
  try {
    const opts = await apiJSON(`api/work/${editWorkId}/covers`, 'GET');
    if (!opts.length) {
      host.innerHTML = '<p class="note">No alternative covers known for this book.</p>';
      return;
    }
    host.innerHTML = `<div class="covergrid">${opts.map((o) => `
      <button data-ed="${o.edition_id}"
              class="${o.is_chosen ? 'chosen' : ''} ${o.is_owned ? 'owned' : ''}"
              title="${escAttr([o.publisher, o.year, o.isbn13].filter(Boolean).join(' · '))}">
        <img loading="lazy" src="${escAttr(o.url)}" alt="">
      </button>`).join('')}</div>
      <button id="e-clearcover" style="margin-top:.5rem">Use the automatic choice</button>`;
    host.querySelectorAll('.covergrid button').forEach((b) => {
      b.onclick = () => pickCover(parseInt(b.dataset.ed, 10));
    });
    document.getElementById('e-clearcover').onclick = () => pickCover(null);
  } catch {
    host.innerHTML = '<p class="note">Could not load covers.</p>';
  }
}

async function pickCover(editionId) {
  try {
    const card = await apiJSON(`api/work/${editWorkId}`, 'PATCH',
      editionId === null ? { clear_cover_choice: true } : { cover_edition_id: editionId });
    if (onSaved) onSaved(card);
  } catch {
    alert('Cover not changed — needs a connection.');
  }
}

async function saveCopy(row) {
  const i = parseInt(row.dataset.i, 10);
  const status = row.querySelector('[data-f="status"]').value;
  const collections = row.querySelector('[data-f="collections"]').value
    .split(',').map((x) => x.trim()).filter(Boolean);
  const btn = row.querySelector('[data-act="save"]');
  btn.disabled = true; btn.textContent = '…';
  try {
    const ids = await apiJSON(`api/work/${editWorkId}/copy-ids`, 'GET');
    const card = await apiJSON(`api/copy/${ids[i]}`, 'PATCH', { status, collections });
    if (onSaved) onSaved(card);
  } catch {
    btn.textContent = '✕';
  }
  btn.disabled = false; btn.textContent = 'Save';
}

async function deleteCopy(row) {
  const i = parseInt(row.dataset.i, 10);
  if (!confirm('Remove this copy record? Use "discarded" instead if the book left the house.'))
    return;
  try {
    const ids = await apiJSON(`api/work/${editWorkId}/copy-ids`, 'GET');
    const card = await apiJSON(`api/copy/${ids[i]}`, 'DELETE');
    if (onSaved) onSaved(card);
  } catch {
    alert('Copy not removed — needs a connection.');
  }
}

async function deleteWork(title) {
  const typed = prompt(
    `This removes the book, its copies and its loss record.\n\nType the title to confirm:`);
  if (typed == null) return;
  try {
    await apiJSON(`api/work/${editWorkId}?confirm_title=${encodeURIComponent(typed)}`, 'DELETE');
    location.reload();
  } catch {
    alert('Not deleted — the title did not match exactly.');
  }
}
