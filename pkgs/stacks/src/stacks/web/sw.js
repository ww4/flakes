/* Service worker: keep the app shell available with no network.
 *
 * Only the shell is cached here. The catalog itself lives in IndexedDB, which
 * suits it better — it is large, it is queried rather than fetched, and it must
 * survive independently of any cache eviction that clears the shell.
 */
'use strict';

// Bump on every shell change. Without this the browser keeps serving the old
// app.js, so a verdict-semantics fix silently does not reach the phone.
const CACHE = 'stacks-shell-v24';
const COVERS = 'stacks-covers-v1';
const SHELL = ['./', 'index.html', 'app.js', 'card.js', 'edit.js', 'shared-ui.js',
               'app.css', 'browse.html', 'browse.js',
               'shelf.html', 'shelf.js', 'cleanup.html', 'cleanup.js', 'logs.html', 'logs.js', 'book.html', 'book.js', 'labels.html', 'labels.js', 'select.js',
               'manifest.webmanifest', 'icon.svg'];

// Only these ever enter the shell cache. Before this filter, every
// same-origin GET was put() — including book.html?id=N for each of 3,000+
// works — so the "shell" cache grew without bound and without value.
const SHELL_NAMES = new Set(SHELL.map((p) => p.replace('./', '')));

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE && k !== COVERS).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // API calls must never be served stale: a cached verdict is a wrong verdict
  // the moment a book is scanned or a shelf is swept.
  if (url.pathname.includes('/api/')) return;

  // Cover images are immutable and worth keeping: cache-first, in their own
  // bucket so evicting art never takes the app shell with it. The page stays
  // perfectly usable without them — tiles fall back to the title.
  if (url.pathname.startsWith('/covers/')) {
    e.respondWith(
      caches.open(COVERS).then((c) =>
        c.match(e.request).then((hit) =>
          hit || fetch(e.request).then((res) => {
            if (res.ok) c.put(e.request, res.clone());
            return res;
          }).catch(() => Response.error())
        )
      )
    );
    return;
  }

  // App shell: NETWORK-FIRST, cache as the fallback.
  //
  // Cache-first was wrong and produced a genuinely baffling bug: navigating
  // between pages served a previously cached app.js, so a scan could return a
  // verdict computed by yesterday's logic, and only a hard refresh — which
  // bypasses the worker — showed the truth. Freshness matters more than the
  // few milliseconds cache-first saves, and the cache still makes the whole app
  // work with no signal at all, which is the only thing it was ever for.
  const basename = url.pathname.split('/').pop() || 'index.html';
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && url.origin === self.location.origin
            && url.search === '' && SHELL_NAMES.has(basename)) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => {
        // Offline. Match ignoring the query so book.html?id=N finds the
        // book.html shell cached at install. The index fallback applies to
        // NAVIGATIONS only: handing an HTML body to a missed subresource
        // (an icon, a script) used to poison it, and serving the scanner
        // page at a book URL was the confusing version of a 404.
        const nav = e.request.mode === 'navigate';
        return caches.match(e.request, { ignoreSearch: nav }).then((hit) =>
          hit || (nav ? caches.match('index.html') : Response.error()));
      })
  );
});
