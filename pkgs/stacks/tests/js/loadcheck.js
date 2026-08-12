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

const context = vm.createContext({
  document: {
    getElementById: () => makeEl(),
    querySelector: () => makeEl(),
    querySelectorAll: () => [],
    createElement: () => makeEl(),
    addEventListener() {},
  },
  window: {},
  navigator: { onLine: false, serviceWorker: { register: () => Promise.resolve() } },
  localStorage: { getItem: () => null, setItem() {} },
  indexedDB: { open: () => ({}) },
  fetch: () => Promise.reject(new Error('offline in test')),
  URLSearchParams,
  console: { log() {}, warn() {}, error() {} },
  setTimeout, clearTimeout, requestAnimationFrame: () => {},
  location: { reload() {} },
  alert() {}, confirm: () => false, prompt: () => null,
});

// Order matters and mirrors the <script> tags in the two pages.
const PAGES = {
  'index.html': ['card.js', 'edit.js', 'shared-ui.js', 'app.js'],
  'browse.html': ['card.js', 'edit.js', 'shared-ui.js', 'browse.js'],
  'shelf.html': ['card.js', 'edit.js', 'shared-ui.js', 'shelf.js'],
  'cleanup.html': ['card.js', 'edit.js', 'shared-ui.js', 'cleanup.js'],
};

let failed = false;
for (const [page, scripts] of Object.entries(PAGES)) {
  // Fresh globals per page, exactly as a real navigation would give.
  const ctx = vm.createContext({ ...context });
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
    'index.html': ['renderBookCard', 'mountEditor', 'renderCard', 'check'],
    'browse.html': ['renderBookCard', 'mountEditor', 'bookTile', 'openSheet', 'renderSearch'],
    'shelf.html': ['renderBookCard', 'mountEditor', 'renderBooks', 'openSheet', 'viewToggle'],
    'cleanup.html': ['renderBookCard', 'mountEditor', 'openSheet'],
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
