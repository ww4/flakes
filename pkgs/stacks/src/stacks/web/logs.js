/* The scan log, built to be read on the phone that produced it.
 *
 * A phone has no console, so "the scanner didn't work" is not a debuggable
 * report. This shows what the camera did, what was decoded, what the server
 * answered, and — the useful one — where the device displayed something
 * different from what the server computed.
 */
'use strict';

// Local, because this page deliberately does not load card.js: that renderer
// needs card markup this page does not have, and pulling a whole renderer in
// for one helper is how a page ends up wired to elements it lacks.
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const KIND_STYLE = {
  scan: 'HAVE',
  decode: 'NOTOWNED',
  camera_started: 'HAVE',
  camera_failed: 'LOST',
  camera_unsupported: 'LOST',
  sync_ok: 'HAVE',
  sync_failed: 'LOST',
};

let timer = null;

function line(l) {
  const div = document.createElement('div');
  div.className = 'copy-edit';
  div.style.alignItems = 'flex-start';

  const when = l.at.replace('T', ' ').slice(5, 19);
  const tag = `<span class="tag ${KIND_STYLE[l.kind] || 'NOTOWNED'}">${esc(l.kind)}</span>`;

  let body;
  if (l.kind === 'scan') {
    body =
      `<span class="mono">${esc(l.code || '')}</span> ` +
      `<b>${esc(l.title || 'not in catalog')}</b>` +
      `<br><span class="faint">${esc(l.verdict || '')}` +
      `${l.source ? ' · ' + esc(l.source) : ''}` +
      `${l.detail ? ' · ' + esc(l.detail) : ''}</span>`;
    // The one line worth shouting about: the device and the server disagreed,
    // which means the cached catalog answered and answered wrongly.
    if (l.disagreed) {
      body += `<br><span style="color:var(--unconfirmed);font-weight:700">` +
              `device showed ${esc(l.client_verdict)} — cached catalog was stale</span>`;
    }
  } else {
    body = `<span class="faint">${esc(l.detail || '')}</span>`;
  }

  div.innerHTML =
    `<span class="faint mono" style="flex:0 0 6.5rem">${esc(when)}</span>` +
    tag +
    `<span style="flex:1; min-width:0">${body}</span>` +
    `<span class="faint" style="flex:0 0 auto">${esc(l.device || '')}</span>`;
  return div;
}

async function load() {
  try {
    const res = await fetch('api/logs?limit=150');
    if (!res.ok) throw new Error(res.status);
    const lines = await res.json();
    const host = document.getElementById('lines');
    host.innerHTML = '';
    lines.forEach((l) => host.appendChild(line(l)));

    const scans = lines.filter((l) => l.kind === 'scan');
    const bad = lines.filter((l) => l.disagreed).length;
    const failures = lines.filter((l) => String(l.kind).includes('failed')
                                      || String(l.kind).includes('unsupported')).length;
    document.getElementById('summary').textContent =
      `${scans.length} scans · ${failures} device failures · ${bad} stale-cache disagreements`;
    document.getElementById('loading').hidden = true;
  } catch {
    document.getElementById('loading').textContent = 'Could not load logs.';
  }
}

document.getElementById('refresh').onclick = load;
document.getElementById('autorefresh').onclick = (e) => {
  if (timer) {
    clearInterval(timer); timer = null; e.target.textContent = 'Auto: off';
  } else {
    // Slow on purpose: this is meant to be left open on a laptop while someone
    // scans in another room, not to hammer the box.
    timer = setInterval(load, 5000);
    e.target.textContent = 'Auto: on';
  }
};

load();
