/* The catalog's own to-do list. */
'use strict';

async function loadCleanup() {
  try {
    const res = await fetch('api/cleanup');
    if (!res.ok) throw new Error(res.status);
    const groups = await res.json();
    const host = document.getElementById('groups');
    host.innerHTML = '';

    if (!groups.length) {
      host.innerHTML = '<p class="note">Nothing to clean up. Suspicious, but nice.</p>';
    }

    groups.forEach((g) => {
      const sec = document.createElement('section');
      sec.className = 'shelf';
      sec.innerHTML =
        `<div class="shelf-head" style="padding-left:0">
           <h2 class="shelf-title">${esc(g.title)}</h2>
           <span class="shelf-sub">${g.total}</span>
         </div>
         <p class="note" style="margin:.2rem 0">${esc(g.why)}</p>
         <p class="note" style="margin:.2rem 0; color:var(--accent)">${esc(g.fix)}</p>`;

      const list = document.createElement('div');
      list.style.cssText = 'display:flex; flex-direction:column; gap:.3rem; margin-top:.6rem';
      g.items.forEach((it) => {
        const b = document.createElement('button');
        b.className = 'hit';
        b.innerHTML =
          `<span class="hit-body"><span class="hit-title">${esc(it.title)}</span>` +
          `<span class="hit-sub">${esc(it.author || 'unknown author')}` +
          `${it.detail ? ' · ' + esc(it.detail) : ''}</span></span>`;
        b.onclick = () => openSheet(it.work_id);
        list.appendChild(b);
      });
      sec.appendChild(list);

      if (g.total > g.items.length) {
        const more = document.createElement('p');
        more.className = 'note';
        more.textContent = `…and ${g.total - g.items.length} more`;
        sec.appendChild(more);
      }
      host.appendChild(sec);
    });
    document.getElementById('cleanup-loading').hidden = true;
  } catch {
    document.getElementById('cleanup-loading').textContent =
      'Could not run the checks. This page needs a connection.';
  }
}

wireSheet();
loadCleanup();
