/* The book card, shared by the scanner and the browse page.
 *
 * One renderer for every way a book can be reached — scanned, searched,
 * tapped on a shelf — so the two pages can never drift into disagreeing about
 * what a book is or what to do about it.
 */
'use strict';

/* Recommendation tone by verdict. Owning a book is good news; only the action
   is coloured, and never alarmingly. */
const REC_CLASS = {
  BUY_WANTED: 'buy', BUY_REPLACE: 'lost', BUY_MORE: 'buy',
  CAUTION_UNVERIFIED: 'warn', SKIP_HAVE: 'skip',
  NOT_IN_CATALOG: 'skip', UNKNOWN: 'skip',
};

const STATUS_OF = {
  SKIP_HAVE: 'HAVE', BUY_MORE: 'HAVE', CAUTION_UNVERIFIED: 'UNCONFIRMED',
  BUY_REPLACE: 'LOST', BUY_WANTED: 'WANTED', NOT_IN_CATALOG: 'NOT OWNED',
  UNKNOWN: 'UNREADABLE',
};

/* Mirrors stacks.match.status_for. "REPLACED" and "UNCONFIRMED" carry the same
   confidence but very different stories: one was deliberately bought again,
   the other sat in a 2023 export nobody has checked since. */
function statusOf(verdict, w) {
  if (verdict === 'CAUTION_UNVERIFIED' && w && (w.r || 0) > 0) return 'REPLACED';
  return STATUS_OF[verdict] || 'NOT OWNED';
}

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function tagHtml(status) {
  // esc() the class token too: badges include user-assigned tag names,
  // and a quote in one used to break out of the attribute (audit M1).
  return `<span class="tag ${esc(String(status || '').replace(/\s+/g, ''))}">${esc(status)}</span>`;
}

const $ = (id) => document.getElementById(id);

/* Fill the card elements. The title is the heading; the status tag sits beside
   it; the recommendation is its own line below. */
function renderBookCard(c, code) {
  // The title is a link to the book's own page — the full record, the editor,
  // and a URL that can be bookmarked or sent to someone. On that page itself
  // it stays plain text rather than linking to where you already are.
  const heading = esc(c.title || code || 'Unknown book') +
                  (c.subtitle ? ': ' + esc(c.subtitle) : '');
  const onBookPage = location.pathname.endsWith('book.html');
  $('c-title').innerHTML =
    (c.work_id && !onBookPage
      ? `<a href="book.html?id=${c.work_id}" style="color:inherit">${heading}</a>`
      : `<span>${heading}</span>`) +
    tagHtml(c.status || STATUS_OF[c.verdict] || 'NOT OWNED');

  $('c-by').textContent = [
    c.author,
    c.series ? `${c.series}${c.series_position ? ' #' + c.series_position : ''}` : null,
    c.publisher, c.year,
    c.editions_known ? `${c.editions_known} edition${c.editions_known === 1 ? '' : 's'} known` : null,
  ].filter(Boolean).join(' · ');

  const img = $('c-cover');
  if (c.cover) {
    img.src = c.cover; img.hidden = false;
    img.onerror = () => { img.hidden = true; };
  } else img.hidden = true;

  const rec = $('c-rec');
  rec.className = 'rec ' + (REC_CLASS[c.verdict] || 'skip');
  rec.textContent = c.recommendation || '';

  // A different printing on the shelf is the one edition fact worth saying out
  // loud — it is the difference between a duplicate and an upgrade.
  $('c-ednote').textContent = c.edition_note || '';
  $('c-ednote').hidden = !c.edition_note;

  $('c-facts').innerHTML =
    (c.detail || []).map((d) => `<li>${esc(d)}</li>`).join('') +
    (c.wants || []).map((w) => `<li class="want">${esc(w)}</li>`).join('');

  $('c-desc').textContent = c.description || '';

  $('c-copies').innerHTML = (c.copies && c.copies.length)
    ? '<h3>Your copies</h3><table><tr><th>state</th><th>ISBN</th>' +
      '<th>collection</th><th>note</th></tr>' +
      c.copies.map((x) => {
        const isbn = x.isbn13
          ? esc(x.isbn13) + (x.matches_scan ? ' <span class="ok" title="same printing">✓</span>' : '')
          : '<span class="faint">not recorded</span>';
        // Collections are the Libib grouping; provenance is the fallback when a
        // copy came from somewhere else (the flood record, a re-purchase).
        const from = (x.collections || []).length
          ? esc(x.collections.join(', '))
          : `<span class="faint">${esc(x.provenance)}</span>`;
        return `<tr><td class="st-${x.status}">${esc(x.status)}</td>` +
               `<td class="mono">${isbn}</td><td>${from}</td>` +
               `<td>${esc((x.notes || '').slice(0, 90))}</td></tr>`;
      }).join('') + '</table>'
    : '';

  // Printings are rarely interesting; keep them one tap away. The label says
  // "all", because with a single edition "other editions (1)" is a small lie.
  const nEd = c.editions_known || (c.editions || []).length;
  const edLabel = nEd > 1 ? `All editions (${nEd})`
                : nEd === 1 ? 'Only one edition known'
                : 'No editions recorded';
  $('c-editions').innerHTML = (c.editions && c.editions.length)
    ? `<details><summary>${edLabel}</summary>` +
      '<table><tr><th>ISBN</th><th>publisher</th><th>year</th><th>binding</th></tr>' +
      c.editions.map((e) => {
        const mark = e.is_scanned ? ' <span class="ok">scanned</span>'
          : e.is_owned ? ' <span class="ok">yours</span>' : '';
        return `<tr><td class="mono">${esc(e.isbn13 || '—')}${mark}</td>` +
               `<td>${esc(e.publisher || '')}</td><td>${e.year || ''}</td>` +
               `<td>${esc(e.binding || '')}</td></tr>`;
      }).join('') + '</table></details>'
    : '';

  $('c-meta').textContent = [
    code || null, c.offline ? 'offline' : 'online',
    c.tier || null, c.source ? 'via ' + c.source : null,
    c.cover_reason ? 'cover: ' + c.cover_reason : null,
  ].filter(Boolean).join(' · ');

  // Editing is server-only; offline the panel would offer writes it cannot make.
  if (typeof mountEditor === 'function') {
    mountEditor(c.offline ? { work_id: null } : c,
                (updated) => renderBookCard({ ...updated, offline: false }, code));
  }
}
