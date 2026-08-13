/* stacks — sale-day scanner.
 *
 * The offline path is the primary one, not a fallback. Book sales happen in
 * basements and gyms with no signal, so the catalog is cached in IndexedDB and
 * every verdict is computed on the device. The network is consulted to enrich
 * what the cache cannot answer, and its absence is never fatal.
 *
 * One card renders every result — a scan, a search hit, a log entry. The title
 * is the heading, what we hold is a tag, what to do is a separate line.
 */
'use strict';

const DB_NAME = 'stacks';
const STORE = 'catalog';
const KEY = 'catalog';
const SCHEMA = 4;

// ---------------------------------------------------------------- storage

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbGet(key) {
  const db = await openDB();
  return new Promise((res, rej) => {
    const t = db.transaction(STORE, 'readonly').objectStore(STORE).get(key);
    t.onsuccess = () => res(t.result); t.onerror = () => rej(t.error);
  });
}
async function idbPut(key, value) {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = res; tx.onerror = () => rej(tx.error);
  });
}

// ---------------------------------------------------------------- ISBN

/* Mirrors stacks.normalize.to_isbn13. A book printed before 2007 carries an
   ISBN-10 and its barcode may be an EAN-13 wrapping the same number; both must
   fold to one key or the offline lookup misses. */
function toIsbn13(raw, repair) {
  if (!raw) return null;
  const s = String(raw).replace(/[^0-9Xx]/g, '').toUpperCase();
  if (s.length === 13 && /^\d+$/.test(s)) {
    if (check13(s.slice(0, 12)) === s[12]) return s;
    // Repairing a failed check digit is right for a spreadsheet, where the
    // last digit gets mangled by a text import — and WRONG for a scan, where
    // the check digit is the barcode's own integrity check. Repairing turns a
    // misread into a plausible wrong book: 108 distinct valid-looking ISBNs
    // are reachable from a single misread digit.
    if (!repair) return null;
    return (s.startsWith('978') || s.startsWith('979'))
      ? s.slice(0, 12) + check13(s.slice(0, 12)) : null;
  }
  if (s.length === 10) {
    let sum = 0;
    for (let i = 0; i < 9; i++) sum += (10 - i) * Number(s[i]);
    const rem = (11 - (sum % 11)) % 11;
    if ((rem === 10 ? 'X' : String(rem)) !== s[9]) return null;
    const body = '978' + s.slice(0, 9);
    return body + check13(body);
  }
  return null;
}
function check13(b) {
  let sum = 0;
  for (let i = 0; i < 12; i++) sum += Number(b[i]) * (i % 2 === 0 ? 1 : 3);
  return String((10 - (sum % 10)) % 10);
}

// ---------------------------------------------------------------- verdict

/* Mirrors stacks.match._decide, including the two rules that matter most:
 *  - An `unverified` holding never produces a confident SKIP: the Libib export
 *    predates the flood, so it says what was owned, not what survived.
 *  - A book we simply do not own is not a buy. Most books at a sale are
 *    unknown, and recommending all of them drowns the signals that count.
 * Facts are not repeated in the recommendation — saying "unconfirmed since the
 * flood" twice reads as the system fretting.
 */
function decide(w) {
  const confirmed = (w.p || 0) + (w.loaned || 0);
  const unverified = w.u || 0;
  const lost = w.l || 0;
  // Copies deliberately bought again after the flood. Checked before the loss
  // branch below: a re-bought book that still said "not replaced yet" would
  // send someone to buy a second one.
  const reacquired = w.r || 0;
  const desired = w.d == null ? 1 : w.d;
  const facts = [];
  let verdict, rec, spent = '';

  if (desired <= 0) {
    verdict = 'SKIP_HAVE';
    rec = confirmed ? `You have ${confirmed} — not collecting more` : 'Marked as not wanted';
  } else if (confirmed >= desired) {
    verdict = 'SKIP_HAVE'; rec = `You have ${confirmed} — confirmed`;
  } else if (confirmed > 0) {
    verdict = 'BUY_MORE'; rec = `You have ${confirmed} of ${desired} you want`;
  } else if (reacquired) {
    verdict = 'CAUTION_UNVERIFIED'; rec = 'Replaced after the flood — not scanned yet';
    spent = 'both';
  } else if (lost) {
    verdict = 'BUY_REPLACE'; rec = 'The flood took this — not replaced yet'; spent = 'lost';
  } else if (unverified) {
    verdict = 'CAUTION_UNVERIFIED';
    rec = `Probably yours — ${unverified} in the catalog, but not seen since the flood`;
    spent = 'unverified';
  } else {
    verdict = 'NOT_IN_CATALOG'; rec = 'No copies recorded';
  }

  if (lost && spent !== 'lost' && spent !== 'both') facts.push(`${lost} lost in the flood`);
  if (reacquired && spent !== 'both') facts.push(`${reacquired} bought again after the flood`);
  const plain = unverified - reacquired;
  if (plain > 0 && spent !== 'unverified' && spent !== 'both') {
    facts.push(`${plain} unconfirmed since the flood`);
  }
  return { verdict, recommendation: rec, detail: facts,
           present: w.p || 0, unverified, lost_flood: lost };
}

function lookupOffline(cat, code) {
  const isbn = toIsbn13(code);
  if (!isbn || !cat) return null;
  const hit = cat.isbns[isbn];
  if (!hit) {
    return { verdict: 'NOT_IN_CATALOG', status: 'NOT OWNED',
             recommendation: 'Not in your catalog', detail: [], wants: [], offline: true };
  }
  const w = cat.works[String(hit.w)];
  if (!w) return null;
  const d = decide(w);
  const wants = [];
  if (w.a && cat.want_authors.includes(w.a)) wants.push(`author on your want list: ${w.a}`);
  if (wants.length && d.verdict === 'NOT_IN_CATALOG') {
    d.verdict = 'BUY_WANTED'; d.recommendation = 'On your want list';
  }
  return { ...cardFromCatalog(cat, hit.w, isbn), ...d,
           status: statusOf(d.verdict, w), wants, offline: true };
}

/* Build a full card from the cached catalog — the whole point of shipping
   1.2 MB instead of a summary. With no signal you still get the description,
   every printing, and what you own, rather than "needs a connection". */
function cardFromCatalog(cat, workId, scannedIsbn) {
  const key = String(workId);
  const w = cat.works[key];
  if (!w) return {};
  const eds = (cat.editions[key] || []);
  const shown = (scannedIsbn && eds.find((e) => e.n === scannedIsbn)) ||
                eds.find((e) => e.y) || eds[0] || {};
  const mine = (cat.copies[key] || []);
  const ownedIsbns = new Set(mine.map((c) => c.n).filter(Boolean));

  return {
    work_id: workId, title: w.t, author: w.a, series: w.s, series_position: w.n,
    description: w.x, publisher: shown.p, year: shown.y,
    cover: shown.c ? `covers/id/${shown.c}?size=M`
         : shown.n ? `covers/${shown.n}?size=M` : null,
    scanned_isbn: scannedIsbn || null,
    editions_known: eds.length,
    editions: eds.slice()
      .sort((x, y) => (x.n === scannedIsbn ? -1 : y.n === scannedIsbn ? 1 : 0) ||
                      ((y.y || 0) - (x.y || 0)))
      .slice(0, 20)
      .map((e) => ({ isbn13: e.n, publisher: e.p, year: e.y, binding: e.b,
                     is_scanned: e.n === scannedIsbn, is_owned: ownedIsbns.has(e.n) })),
    copies: mine.map((c) => ({ status: c.s, provenance: c.v, collections: c.c || [],
                               notes: c.o, isbn13: c.n,
                               matches_scan: !!(scannedIsbn && c.n === scannedIsbn) })),
  };
}

function searchOffline(cat, query) {
  if (!cat) return [];
  const q = query.toLowerCase().trim();
  if (q.length < 2) return [];
  const out = [];
  for (const [id, w] of Object.entries(cat.works)) {
    const t = (w.t || '').toLowerCase();
    const a = (w.a || '').toLowerCase();
    if (t.includes(q) || a.includes(q)) {
      const d = decide(w);
      out.push({ work_id: Number(id), title: w.t, author: w.a || null, year: null,
                 cover: null, status: statusOf(d.verdict, w), verdict: d.verdict,
                 recommendation: d.recommendation, present: w.p || 0,
                 unverified: w.u || 0, lost_flood: w.l || 0,
                 _exact: t.startsWith(q) });
    }
  }
  out.sort((a, b) => (b._exact - a._exact) || a.title.localeCompare(b.title));
  return out.slice(0, 30);
}

/* Tell the server what happened here.
 *
 * A phone has no console. Camera permission refused, no BarcodeDetector, a
 * decode that never resolves — all invisible server-side, and all the likeliest
 * failures. Best-effort and never awaited: diagnostics must not be able to
 * break the thing they are diagnosing.
 */
const _reportedAt = Object.create(null);

function report(kind, detail, minGapMs) {
  // Throttled per kind. These are called from the detect loop, where an
  // oscillating barcode or two codes in frame produced a POST on EVERY frame —
  // enough traffic to stall the main thread on a phone and make the Stop
  // button stop responding.
  const gap = minGapMs == null ? 1500 : minGapMs;
  const now = Date.now();
  if (_reportedAt[kind] && now - _reportedAt[kind] < gap) return;
  _reportedAt[kind] = now;
  try {
    fetch('api/client-event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, detail: detail || {}, page: 'scan' }),
      keepalive: true,
    }).catch(() => {});
  } catch { /* diagnostics must never throw */ }
}

/* Uncaught failures are the ones that look like "it just stopped working".
   Send them where they can be read: the Logs tab. */
window.addEventListener('error', (e) => {
  report('js_error', { message: String(e.message || ''),
                       at: `${e.filename || '?'}:${e.lineno || 0}` }, 0);
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason;
  report('js_rejection', { message: String((r && r.message) || r || '') }, 0);
});

// ---------------------------------------------------------------- UI

const el = (id) => document.getElementById(id);
let catalog = null, detector = null, stream = null;
let lastCode = null, lastAt = 0;

/* A decoded barcode is not believed until it repeats.
 *
 * EAN-13's check digit catches every single-digit error but not every
 * two-digit one, so a blurred frame can produce a DIFFERENT VALID ISBN and
 * sail straight through. That is not hypothetical: scanning "How a Seed Grows"
 * (9780062381880) yielded 9780062381750 — "The Demon Crown", an unrelated
 * thriller — because two digits flipped in a way that still checksummed.
 *
 * A misread comes from one bad frame and rarely repeats identically; the true
 * code repeats every frame. Requiring agreement across consecutive detections
 * costs about 50ms and removes the whole class.
 */
const STABLE_FRAMES = 3;
let pendingCode = null, pendingCount = 0;

function setStatus(text, cls) {
  el('status').innerHTML = text;
  el('dot').className = 'dot' + (cls ? ' ' + cls : '');
}
function hide(id) { el(id).classList.remove('show'); }

/* Scanner wrapper around the shared renderer: show the card, then the two
   things only the scanner does — haptic feedback and the session log. */
let lastScanned = null;

function renderCard(c, code) {
  hide('results');
  el('card').classList.add('show');
  renderBookCard(c, code);

  // Confirming needs a barcode: it records that *this printing* is in hand.
  lastScanned = c.scanned_isbn || (code && toIsbn13(code)) || null;
  const acts = el('c-actions');
  if (acts) {
    acts.hidden = !lastScanned || !!c.offline;
    // ONE action, ONE label, always safe to press.
    //
    // A label that changes with state reads as either an indicator ("this is
    // not on the shelf") or an input ("click to mark it missing"), and those
    // two readings imply opposite outcomes. Pressing it is now idempotent —
    // "I have this" said twice is still just "I have this" — so the label
    // never has to change and can never be misread.
    //
    // Recording that something is NOT there is a different, deliberate act.
    // It belongs on the book page's copy editor, where it is an explicit
    // labelled field rather than a button that flipped meaning underneath you.
    const have = el('btn-have');
    have.textContent = '✓ I have this';
    have.className = 'primary';

    // The other slot opens the full record: everything that does not belong on
    // a decision surface lives there.
    const open = el('btn-open');
    open.hidden = !c.work_id;
    open.onclick = () => { location.href = `book.html?id=${c.work_id}`; };
  }

  if (navigator.vibrate) {
    navigator.vibrate(c.verdict === 'SKIP_HAVE' ? [120]
      : c.verdict === 'NOT_IN_CATALOG' || c.verdict === 'UNKNOWN' ? [25] : [40, 60, 40]);
  }
  addLog(c, code);
}

function addLog(c, code) {
  const li = document.createElement('li');
  li.innerHTML = tagHtml(c.status || STATUS_OF[c.verdict] || 'NOT OWNED') +
    `<span class="t">${esc(c.title || code)}</span>`;
  if (c.work_id) li.onclick = () => showWork(c.work_id);
  const list = el('log-list');
  list.prepend(li);
  while (list.children.length > 40) list.lastChild.remove();
}

function renderResults(hits, query) {
  hide('card');
  const list = el('results-list');
  el('results-head').textContent =
    `${hits.length} match${hits.length === 1 ? '' : 'es'} for “${query}”`;
  list.innerHTML = '';
  if (!hits.length) {
    list.innerHTML = '<p class="note">Nothing found. Try fewer words.</p>';
  }
  hits.forEach((h) => {
    const b = document.createElement('button');
    b.className = 'hit';
    b.innerHTML =
      (h.cover ? `<img class="hit-cover" src="${esc(h.cover)}" alt=""
                       onerror="this.style.visibility='hidden'">`
               : '<span class="hit-cover"></span>') +
      `<span class="hit-body"><span class="hit-title">${esc(h.title)}</span>` +
      `<span class="hit-sub">${esc([h.author || 'unknown', h.series, h.year]
        .filter(Boolean).join(' · '))}</span></span>` +
      tagHtml(h.status);
    b.onclick = () => showWork(h.work_id);
    list.appendChild(b);
  });
  el('results').classList.add('show');
}

// ---------------------------------------------------------------- actions

async function showWork(workId) {
  if (!workId) return;
  try {
    const res = await fetch(`api/work/${workId}`);
    if (!res.ok) throw new Error(res.status);
    renderCard(await res.json(), null);
    el('card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch {
    // Offline: the cached payload holds counts and titles but no full record.
    const w = catalog && catalog.works[String(workId)];
    if (!w) return;
    const d = decide(w);
    renderCard({ ...cardFromCatalog(catalog, workId, null), ...d,
                 status: statusOf(d.verdict, w), offline: true }, null);
  }
}

async function check(code, source) {
  const off = lookupOffline(catalog, code);
  // A cached hit is authoritative and instant; show it at once, then quietly
  // upgrade to the full record when there is a network.
  if (off && off.verdict !== 'NOT_IN_CATALOG') renderCard(off, code);
  try {
    // `shown` is what the DEVICE displayed. When it differs from the server's
    // answer the cached catalog was stale — and that divergence is exactly the
    // bug a log recording only the server's opinion could never show.
    const q = new URLSearchParams({ source: source || 'unknown' });
    if (off) q.set('shown', off.verdict);
    const res = await fetch(`api/scan/${encodeURIComponent(code)}?${q}`);
    if (!res.ok) throw new Error(res.status);
    renderCard({ ...(await res.json()), offline: false }, code);
  } catch {
    if (off) {
      // Say so. Leaving the cached card up unannounced is how a stale verdict
      // passes for a current one.
      renderCard({ ...off,
                   detail: [...(off.detail || []), 'Server did not answer — this is cached data'],
                 }, code);
    } else {
      renderCard({ verdict: 'UNKNOWN', status: 'UNREADABLE',
                   recommendation: 'No network, and nothing cached for this code',
                   detail: ['Sync the catalog while you have signal.'],
                   offline: true }, code);
    }
  }
}

async function doSearch(query) {
  try {
    const res = await fetch(`api/search?q=${encodeURIComponent(query)}`);
    if (!res.ok) throw new Error(res.status);
    const hits = await res.json();
    // A single unambiguous match is what you meant — open it.
    if (hits.length === 1) return showWork(hits[0].work_id);
    renderResults(hits, query);
  } catch {
    const hits = searchOffline(catalog, query);
    if (hits.length === 1) return showWork(hits[0].work_id);
    renderResults(hits, query);
  }
}

// ---------------------------------------------------------------- camera

/* Scanning is a back-and-forth, not a firehose.
 *
 * Every book that resolves needs a decision — confirm it, add it, or move on —
 * and nothing useful happens while someone is reading a card. So a hit PAUSES
 * the camera and shows the result; the next scan starts when they say so. This
 * also removes the layout problem entirely: the viewfinder and the card are
 * never on screen at the same time, so neither can push the other away.
 *
 * The MediaStream is kept alive across a pause. Releasing it would make every
 * book cost a camera warm-up, and on some browsers a fresh permission prompt.
 */
const IDLE = 'idle', SCANNING = 'scanning', PAUSED = 'paused';
let scanState = IDLE;
let starting = false;
let scanned = 0;

function setScanState(next) {
  scanState = next;
  const wrap = el('video-wrap');
  const scan = el('btn-scan');
  const sync = el('btn-sync');
  const done = el('btn-done');

  wrap.classList.toggle('on', next === SCANNING);
  document.body.classList.toggle('scanning', next === SCANNING);
  el('btn-stop-overlay').hidden = next !== SCANNING;

  // Each label says what the button does in this state. No mode guessing.
  scan.textContent =
    next === SCANNING ? 'Stop'
    : next === PAUSED ? `Scan another${scanned ? ` (${scanned})` : ''}`
    : 'Scan';

  // "Done" replaces Sync only while paused, where ending the session is the
  // other thing someone plausibly wants.
  sync.hidden = next === PAUSED;
  done.hidden = next !== PAUSED;
}

async function startScan() {
  if (scanState === SCANNING) return stopScan();
  if (scanState === PAUSED) return resumeScan();
  // getUserMedia can take a second on a phone; a second tap meanwhile must not
  // open a competing stream that the first stopScan cannot see to release.
  if (starting) return;
  starting = true;
  try {
    await beginCamera();
  } finally {
    starting = false;
  }
}

async function beginCamera() {
  if (!('BarcodeDetector' in window)) {
    report('camera_unsupported', { ua: navigator.userAgent }, 0);
    el('cam-note').hidden = false; el('manual-input').focus(); return;
  }
  try {
    detector = detector || new BarcodeDetector({ formats: ['ean_13', 'ean_8', 'upc_a', 'upc_e'] });
    if (!stream) {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    }
    const v = el('video');
    v.srcObject = stream;
    await v.play();
    scanned = 0;
    setScanState(SCANNING);
    report('camera_started', {}, 0);
    tick();
  } catch (err) {
    report('camera_failed', { message: String((err && err.message) || err),
                              name: String((err && err.name) || '') }, 0);
    el('cam-note').hidden = false;
    el('cam-note').textContent = 'Camera unavailable: ' + err.message;
    stopScan();
  }
}

/* A book resolved. Stop looking and let them decide about it. */
function pauseScan() {
  if (tickTimer) { clearTimeout(tickTimer); tickTimer = null; }
  pendingCode = null; pendingCount = 0;
  el('video').pause();
  setScanState(PAUSED);
}

/* Put the card away and go back to the viewfinder. */
async function resumeScan() {
  el('card').classList.remove('show');
  el('c-actions').hidden = true;
  hide('results');
  lastCode = null; lastAt = 0;

  if (!stream) return beginCamera();
  try {
    await el('video').play();
  } catch {
    // The stream died while paused (backgrounded tab, camera taken by another
    // app). Acquire a fresh one rather than showing a frozen frame.
    stream = null;
    return beginCamera();
  }
  setScanState(SCANNING);
  tick();
}

function stopScan() {
  if (tickTimer) { clearTimeout(tickTimer); tickTimer = null; }
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  pendingCode = null; pendingCount = 0;
  el('video').srcObject = null;
  setScanState(IDLE);
}

/* Detect on a timer, not every animation frame.
 *
 * BarcodeDetector.detect is expensive. Running it at ~60fps pegged the phone's
 * main thread, and a saturated main thread does not process touch events.
 * Eight looks a second is far more than enough to read a barcode someone is
 * holding still, and leaves the UI responsive.
 */
const DETECT_INTERVAL_MS = 120;
let tickTimer = null;

function scheduleTick() {
  if (scanState !== SCANNING) return;
  tickTimer = setTimeout(tick, DETECT_INTERVAL_MS);
}

async function tick() {
  tickTimer = null;
  if (scanState !== SCANNING) return;
  try {
    const codes = await detector.detect(el('video'));
    if (!codes.length) {
      pendingCode = null; pendingCount = 0;
    } else {
      const values = [...new Set(codes.map((c) => c.rawValue))];
      if (values.length > 1) {
        // Two barcodes in view. Guessing which one someone meant is worse than
        // asking them to isolate the book.
        pendingCode = null; pendingCount = 0;
        report('multiple_codes', { values });
        setStatus('two barcodes in view — show one', 'stale');
        return scheduleTick();
      }

      const code = values[0];
      if (code === pendingCode) {
        pendingCount++;
      } else {
        if (pendingCode) report('unstable_decode', { was: pendingCode, now: code });
        pendingCode = code; pendingCount = 1;
      }

      if (pendingCount >= STABLE_FRAMES) {
        scanned += 1;
        if (navigator.vibrate) navigator.vibrate(35);
        report('decode', { code, frames: pendingCount }, 0);
        pauseScan();
        check(code, 'camera');
        return;
      }
    }
  } catch { /* transient decode failure — keep going */ }
  scheduleTick();
}

// ---------------------------------------------------------------- sync

async function sync() {
  el('btn-sync').disabled = true;
  setStatus('syncing…');
  try {
    const res = await fetch('api/catalog', { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    // Refuse a payload we do not understand rather than misread it.
    if (data.version !== SCHEMA) throw new Error(`schema ${data.version}`);
    catalog = data;
    await idbPut(KEY, catalog);
    reportReady();
  } catch {
    setStatus('sync failed — using cached copy', catalog ? 'stale' : '');
  } finally { el('btn-sync').disabled = false; }
}

function reportReady() {
  if (!catalog) return setStatus('no catalog — <b>sync</b> while online', '');
  const w = Object.keys(catalog.works || {}).length.toLocaleString();
  const n = Object.keys(catalog.isbns || {}).length.toLocaleString();
  setStatus(`<b>${w}</b> books · ${n} ISBNs`, 'ready');
}

// ---------------------------------------------------------------- boot

el('btn-scan').addEventListener('click', startScan);
el('btn-stop-overlay').addEventListener('click', stopScan);
el('btn-done').addEventListener('click', stopScan);
el('btn-sync').addEventListener('click', sync);
el('manual-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const raw = el('manual-input').value.trim();
  if (!raw) return;
  // A valid ISBN is a scan; anything else is a search, which is the only route
  // to the pre-ISBN books making up much of what the flood destroyed.
  const asIsbn = toIsbn13(raw);
  if (asIsbn) {
    check(raw, 'manual');
  } else if (/^[0-9Xx\s-]{10,17}$/.test(raw)) {
    // Looks like an ISBN but failed its check digit. Searching for the digits
    // as a title would be nonsense; say what is actually wrong.
    renderCard({ verdict: 'UNKNOWN', status: 'UNREADABLE',
                 recommendation: 'That ISBN fails its check digit',
                 detail: ['A digit is wrong somewhere — retype or rescan it.'],
                 offline: true }, raw);
  } else {
    doSearch(raw);
  }
  el('manual-input').select();
});

(async function boot() {
  try { catalog = await idbGet(KEY); } catch { /* first run */ }
  reportReady();
  // Re-sync every load when online. Confirming a book, or editing one on the
  // browse page, leaves this copy behind, and a stale catalog is exactly what
  // produces a confident wrong answer.
  if (navigator.onLine) sync();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
})();


/* Confirm a book is physically in hand.
 *
 * The same action does two jobs: it promotes a pre-flood catalogue entry to
 * `present` (the sweep — the only thing that turns "probably yours" into
 * "yours"), and it creates the book outright when we have never seen it, so a
 * stack of sale purchases can be entered by scanning them on the way home.
 *
 * Server-only on purpose. This writes, and a write queued on a phone that may
 * never reconnect is worse than a button that admits it needs a connection.
 */
async function confirmInHand(action) {
  if (!lastScanned) return;
  const btn = el('btn-have');
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = 'saving…';
  try {
    const res = await fetch(`api/confirm/${encodeURIComponent(lastScanned)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    if (!res.ok) throw new Error(res.status);
    const j = await res.json();
    renderCard({ ...j.card, offline: false }, lastScanned);
    const flash = document.createElement('p');
    flash.className = 'flash';
    flash.textContent = j.message;
    el('c-actions').insertAdjacentElement('beforebegin', flash);
    setTimeout(() => flash.remove(), 4000);
    // The cached catalog is now behind; a re-sync is cheap at 1.2 MB.
    if (navigator.onLine) sync();
  } catch {
    btn.textContent = 'needs a connection';
    setTimeout(() => { btn.textContent = was; btn.disabled = false; }, 2000);
    return;
  }
  btn.disabled = false; btn.textContent = was;
}

el('btn-have').addEventListener('click', () => confirmInHand('confirm'));
