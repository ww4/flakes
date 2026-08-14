/* Selecting a handful of books and labelling them.
 *
 * The labels page already does whole groups — a Libib collection, a shelf, an
 * author. This is the other half: the arbitrary handful you can only pick out
 * by eye. "These eleven, and not the other four on the same shelf."
 *
 * Deliberately a MODE rather than always-on checkboxes. Tapping a book to open
 * it is the common action by a wide margin, and putting a checkbox beside
 * three thousand covers to serve the rarer one would tax every visit. Selection
 * mode is entered on purpose and leaves on its own once the work is applied.
 *
 * shared-ui.js calls decorateSelectable() on every book it renders, if this
 * file is loaded. Pages that do not want selection simply do not include it,
 * and nothing in shared-ui needs to know the difference.
 */
'use strict';

let selectMode = false;
const selected = new Set();
/* Everything currently on screen, so "select all" means what it says. */
let visibleItems = [];

function selectionActive() { return selectMode; }

function rememberVisible(items) { visibleItems = items || []; }

/* Called by renderBooks for each rendered book. */
function decorateSelectable(node, item) {
  if (!selectMode) return;
  const id = item.work_id;
  node.classList.add('selectable');
  if (selected.has(id)) node.classList.add('picked');
  /* Replace navigation wholesale — a half-second press opening a book page
     mid-selection loses the whole selection. */
  node.onclick = (e) => {
    e.preventDefault();
    if (selected.has(id)) { selected.delete(id); node.classList.remove('picked'); }
    else { selected.add(id); node.classList.add('picked'); }
    paintBar();
  };
}

/* ------------------------------------------------------------------- bar */

function bar() {
  let b = document.getElementById('selbar');
  if (!b) {
    b = document.createElement('div');
    b.id = 'selbar';
    b.className = 'selbar';
    document.body.appendChild(b);
  }
  return b;
}

function paintBar() {
  const b = bar();
  if (!selectMode) { b.hidden = true; return; }
  b.hidden = false;
  const n = selected.size;
  b.innerHTML =
    `<span class="selcount">${n} selected</span>` +
    `<button id="sel-all" class="link">${
      n && n >= visibleItems.length ? 'None' : 'All'}</button>` +
    `<button id="sel-place" class="primary"${n ? '' : ' disabled'}>Place…</button>` +
    `<button id="sel-tag"${n ? '' : ' disabled'}>Tag…</button>` +
    '<button id="sel-done" class="link">Done</button>';

  document.getElementById('sel-all').onclick = () => {
    if (selected.size >= visibleItems.length) selected.clear();
    else visibleItems.forEach((i) => selected.add(i.work_id));
    repaintSelection();
  };
  document.getElementById('sel-place').onclick = () => askLabel('place');
  document.getElementById('sel-tag').onclick = () => askLabel('tag');
  document.getElementById('sel-done').onclick = exitSelect;
}

/* Repaint whatever list is on screen. Each page owns its own painter. */
function repaintSelection() {
  if (typeof paint === 'function') paint();
  paintBar();
}

function enterSelect() {
  selectMode = true;
  selected.clear();
  repaintSelection();
}

function exitSelect() {
  selectMode = false;
  selected.clear();
  repaintSelection();
}

function selectToggleButton() {
  const b = document.createElement('button');
  b.className = 'link';
  b.id = 'sel-enter';
  b.textContent = 'Select';
  b.onclick = () => (selectMode ? exitSelect() : enterSelect());
  return b;
}

/* ----------------------------------------------------------------- apply */

/* A datalist of what already exists, so the second book onto a shelf costs a
   couple of keystrokes rather than remembering exactly how it was spelled. */
function labelOptions(kind) {
  return fetch(`api/labels/${kind}`)
    .then((r) => r.json())
    .then((d) => d.nodes.map((n) => n.path))
    .catch(() => []);
}

function askLabel(kind) {
  const n = selected.size;
  if (!n) return;
  labelOptions(kind).then((paths) => {
    const wrap = document.createElement('div');
    wrap.className = 'selask';
    wrap.innerHTML =
      `<div class="selask-box">` +
        `<h2>${kind === 'place' ? 'Put' : 'Tag'} ${n} book${n === 1 ? '' : 's'}</h2>` +
        `<p class="note">${kind === 'place'
          ? 'This takes them out of wherever they are now.'
          : 'Added alongside any tags they already carry.'}</p>` +
        `<input id="selask-path" list="selask-list" autocapitalize="words" ` +
               `placeholder="${kind === 'place' ? 'Frankfort / science shelf'
                                                : 'Sonlight / Core B'}">` +
        `<datalist id="selask-list">${
          paths.map((p) => `<option value="${esc(p)}">`).join('')}</datalist>` +
        `<div class="selask-row">` +
          `<button id="selask-cancel" class="link">Cancel</button>` +
          `<button id="selask-go" class="primary">Apply</button>` +
        `</div>` +
        `<p class="note" id="selask-msg"></p>` +
      `</div>`;
    document.body.appendChild(wrap);
    const input = document.getElementById('selask-path');
    input.focus();

    const close = () => wrap.remove();
    document.getElementById('selask-cancel').onclick = close;
    wrap.onclick = (e) => { if (e.target === wrap) close(); };

    const go = () => {
      const path = input.value.trim();
      if (!path) { input.focus(); return; }
      document.getElementById('selask-msg').textContent = 'Working…';
      fetch(`api/bulk/${kind}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ work_ids: Array.from(selected), path })
      }).then((r) => r.json().then((body) => {
        if (!r.ok) throw new Error(body.detail || r.status);
        close();
        exitSelect();
        toast(kind === 'place'
          ? `${body.works} book${body.works === 1 ? '' : 's'} → ${body.place}`
          : `${body.changed} tagged ${body.tag}`);
      })).catch((e) => {
        document.getElementById('selask-msg').textContent = 'Failed: ' + e.message;
      });
    };
    document.getElementById('selask-go').onclick = go;
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  });
}

function toast(text) {
  const t = document.createElement('div');
  t.className = 'seltoast';
  t.textContent = text;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}
