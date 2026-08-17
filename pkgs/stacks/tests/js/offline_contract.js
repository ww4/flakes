/* Run the real lookupOffline against a real catalog payload.
 *
 * This exists because the offline lookup shipped broken for its entire life:
 * catalog.py emits `isbns` as {isbn13: work_id} — a bare integer — while
 * app.js read `hit.w`, so every OWNED book answered "nothing cached for this
 * code" at the no-signal book sale the app was built for, and unknown books
 * answered correctly. The schema-version handshake could not catch it: it
 * guards the version NUMBER, not the shape. Only feeding the genuine
 * catalog.build() output to the genuine client code does.
 *
 * Usage: node offline_contract.js <payload.json> <checks.json>
 *   payload.json — exactly what /api/catalog would return (catalog.build()).
 *   checks.json  — [{isbn, expect: "hit", title} | {isbn, expect: "miss"}]
 * Exits non-zero with one line per failed expectation.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { WEB, idsIn, makeContext, PAGES } = require('./loadcheck.js');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const checks = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// Load index.html's scripts exactly as the page does; lookupOffline and its
// helpers live in that shared scope.
const ctx = makeContext(idsIn('index.html'));
for (const name of PAGES['index.html']) {
  vm.runInContext(fs.readFileSync(path.join(WEB, name), 'utf8'), ctx, { filename: name });
}

const failures = [];
for (const c of checks) {
  const r = ctx.lookupOffline(payload, c.isbn);
  if (c.expect === 'hit') {
    if (!r) {
      failures.push(`${c.isbn}: expected a card, got null — the isbns map and the client disagree on shape`);
    } else if (r.verdict === 'NOT_IN_CATALOG') {
      failures.push(`${c.isbn}: owned book answered NOT_IN_CATALOG offline (verdict=${r.verdict})`);
    } else if (c.title && r.title !== c.title) {
      failures.push(`${c.isbn}: resolved to "${r.title}", expected "${c.title}"`);
    } else if (r.scanned_isbn !== c.isbn) {
      failures.push(`${c.isbn}: card lost the scanned isbn (got ${r.scanned_isbn}) — cardFromCatalog was not reached with the work id`);
    }
  } else if (c.expect === 'miss') {
    if (!r || r.verdict !== 'NOT_IN_CATALOG') {
      failures.push(`${c.isbn}: unknown isbn should be NOT_IN_CATALOG, got ${r && r.verdict}`);
    }
  } else {
    failures.push(`${c.isbn}: unknown expectation ${c.expect}`);
  }
}

if (failures.length) {
  for (const f of failures) console.error(`FAIL ${f}`);
  process.exit(1);
}
console.log(`offline contract holds for ${checks.length} checks`);
