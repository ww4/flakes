/* Load the real browser scripts the way a browser does: as separate classic
 * scripts sharing ONE global lexical scope.
 *
 * This exists because a duplicate top-level `esc` — a `const` in card.js and a
 * `function` in edit.js — was a redeclaration SyntaxError that killed edit.js
 * entirely at load. Each file passed `node --check` on its own, and the Python
 * suite could not see it at all. Worse, `mountEditor` was still hoisted onto
 * the global object, so callers' `typeof mountEditor === 'function'` guard
 * passed and walked straight into uninitialised `let` bindings, which threw
 * inside the renderer and left tapping a book doing visibly nothing.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.join(__dirname, '..', '..', 'src', 'stacks', 'web');

// A DOM stub generous enough that scripts can wire themselves up. Every lookup
// returns an element, because the point is to catch load-order and scope bugs,
// not to assert on markup.
function makeEl() {
  const el = {
    style: {}, dataset: {}, hidden: false, textContent: '', innerHTML: '', value: '',
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild(x) { return x; }, append() {}, prepend() {}, remove() {},
    insertAdjacentElement() {}, insertAdjacentHTML() {},
    addEventListener() {}, removeEventListener() {}, scrollIntoView() {},
    querySelector: () => makeEl(), querySelectorAll: () => [],
    setAttribute() {}, focus() {}, select() {}, children: [], lastChild: null,
  };
  return el;
}

/* The ids a page actually declares.
 *
 * Handing back an element for every id — as this harness first did — makes it
 * impossible to catch a script wiring up an element its page does not have.
 * That is precisely how the "I have this" button shipped broken: the markup was
 * dropped in a rewrite, app.js kept calling
 * el('btn-have').addEventListener(...), and every test stayed green.
 */
function idsIn(page) {
  const html = fs.readFileSync(path.join(WEB, page), 'utf8');
  return new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
}

function makeContext(ids) {
  return vm.createContext({
  document: {
    getElementById: (id) => (ids.has(id) ? makeEl() : null),
    querySelector: () => makeEl(),
    querySelectorAll: () => [],
    createElement: () => makeEl(),
    addEventListener() {},
  },
  // Scripts register global error handlers here; a bare object made that a
  // TypeError and failed the page for a harness shortcoming, not a bug.
  window: { addEventListener() {}, removeEventListener() {} },
  navigator: { onLine: false, serviceWorker: { register: () => Promise.resolve() } },
  localStorage: { getItem: () => null, setItem() {} },
  indexedDB: { open: () => ({}) },
  fetch: () => Promise.reject(new Error('offline in test')),
  location: { search: '', pathname: '/', href: '', reload() {} },
  URLSearchParams,
  console: { log() {}, warn() {}, error() {} },
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: () => {},
  alert() {}, confirm: () => false, prompt: () => null,
  });
}

// Order matters and mirrors the <script> tags in the two pages.
const PAGES = {
  'index.html': ['card.js', 'edit.js', 'app.js'],
  'browse.html': ['shared-ui.js', 'select.js', 'browse.js'],
  'shelf.html': ['shared-ui.js', 'select.js', 'shelf.js'],
  'cleanup.html': ['shared-ui.js', 'cleanup.js'],
  'logs.html': ['logs.js'],
  'book.html': ['card.js', 'edit.js', 'book.js'],
  'labels.html': ['shared-ui.js', 'labels.js'],
};

let failed = false;
for (const [page, scripts] of Object.entries(PAGES)) {
  // Fresh globals per page, with only the ids that page really declares.
  const ctx = makeContext(idsIn(page));
  for (const name of scripts) {
    const file = path.join(WEB, name);
    try {
      vm.runInContext(fs.readFileSync(file, 'utf8'), ctx, { filename: name });
    } catch (err) {
      failed = true;
      console.error(`FAIL ${page} -> ${name}: ${err.constructor.name}: ${err.message}`);
    }
  }
  // The functions each page depends on must actually be callable.
  const NEEDED = {
    'index.html': ['renderBookCard', 'mountEditor', 'renderCard', 'check',
                   'startScan', 'pauseScan', 'resumeScan', 'stopScan'],
    'browse.html': ['bookTile', 'openSheet', 'renderSearch', 'renderBooks', 'enterSelect', 'decorateSelectable', 'paint'],
    'shelf.html': ['renderBooks', 'openSheet', 'viewToggle', 'enterSelect', 'decorateSelectable', 'selectToggleButton'],
    'cleanup.html': ['openSheet', 'loadCleanup'],
    'logs.html': ['load'],
    'book.html': ['renderBookCard', 'mountEditor', 'paint', 'wireActions'],
    'labels.html': ['renderTree', 'load', 'create', 'applyBulk', 'setKind'],
  };
  const needed = NEEDED[page];
  for (const fn of needed) {
    if (typeof ctx[fn] !== 'function') {
      failed = true;
      console.error(`FAIL ${page}: ${fn} is ${typeof ctx[fn]}, expected function`);
    }
  }
}

if (failed) process.exit(1);
console.log('all page scripts load cleanly in a shared global scope');
