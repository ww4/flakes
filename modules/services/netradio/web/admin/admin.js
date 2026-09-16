// Library radio — admin. Talks to /admin/api (netradio admin). No framework.
const $ = (id) => document.getElementById(id);
const S = { state: null, options: {} };
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

async function api(method, path, body) {
  const r = await fetch(`api${path}`, { method, headers: { "Content-Type": "application/json" },
                                        body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${r.status}`);
  return j;
}
function flash(msg, isErr) { const f = $("flash"); f.textContent = msg; f.hidden = false; f.style.borderLeftColor = isErr ? "#b53a2a" : ""; clearTimeout(f._t); f._t = setTimeout(() => f.hidden = true, 6000); }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function chips(name, chosen, fams) {
  return `<span class="chips">${fams.map(f => `<label><input type="checkbox" name="${name}" value="${f}" ${chosen.includes(f) ? "checked" : ""}> ${f}</label>`).join("")}</span>`;
}
function chosen(container, name) { return [...container.querySelectorAll(`input[name="${name}"]:checked`)].map(i => i.value); }

async function load() {
  S.state = await api("GET", "/state");
  $("now").textContent = S.state.now;
  $("pending").textContent = S.state.pending_requests.length ? `${S.state.pending_requests.length} request(s) waiting for the box` : "";
  renderFeeds(); await renderSchedule(); renderStations();
}

// --- feeds ---------------------------------------------------------------------
function renderFeeds() {
  const tb = $("feeds").querySelector("tbody");
  tb.innerHTML = "";
  const fams = S.state.families;
  for (const [id, f] of Object.entries(S.state.feeds).sort((a, b) => a[1].title.localeCompare(b[1].title))) {
    const tr = document.createElement("tr");
    tr.dataset.id = id;
    tr.innerHTML = `
      <td><input type="text" name="title" value="${esc(f.title)}"><br><small class="muted">${id}</small><br>
          <label><input type="checkbox" name="listenable" ${f.listenable ? "checked" : ""}> listenable</label></td>
      <td><textarea name="description">${esc(f.description)}</textarea>
          <details><summary class="muted">rule</summary><textarea name="rule" spellcheck="false">${esc(JSON.stringify(f.rule || {}, null, 1))}</textarea></details></td>
      <td>${chips("family", f.family || [], fams)}</td>
      <td><span class="status-${f.status}">${f.status}</span> · ${f.count ?? 0} tracks<br><small class="muted">${esc(f.note || "")}</small></td>
      <td><button class="small" data-act="save">Save</button> <button class="small" data-act="compile">Recompile</button> <button class="small" data-act="delete">Delete</button></td>`;
    tb.appendChild(tr);
  }
}
$("feeds").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const tr = b.closest("tr"), id = tr.dataset.id;
  try {
    if (b.dataset.act === "delete") { if (!confirm(`Delete feed "${S.state.feeds[id].title}"?`)) return; await api("DELETE", `/feeds/${id}`); flash("Feed removed; apply requested."); }
    else if (b.dataset.act === "compile") { await api("POST", `/feeds/${id}/compile`); flash("Compile requested — the agent will build the rule from the description (a minute or two)."); }
    else {
      const body = { title: tr.querySelector('[name=title]').value, description: tr.querySelector('[name=description]').value,
                     family: chosen(tr, "family"), listenable: tr.querySelector('[name=listenable]').checked };
      const ruleText = tr.querySelector('[name=rule]').value.trim();
      const before = JSON.stringify(S.state.feeds[id].rule || {}, null, 1);
      if (ruleText && ruleText !== before) body.rule = JSON.parse(ruleText);
      const r = await api("PUT", `/feeds/${id}`, body);
      flash(r.recompile ? "Saved — description changed, so the rule will be rebuilt." : "Saved; apply requested.");
    }
    await load();
  } catch (err) { flash(err.message, true); }
});
$("add-feed").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const r = await api("POST", "/feeds", { title: f.title.value, description: f.description.value, listenable: f.listenable.checked });
    flash(`Feed "${f.title.value}" added as ${r.id}; the agent is building its rule.`);
    f.reset(); f.listenable.checked = true;
    await load();
  } catch (err) { flash(err.message, true); }
});

// --- schedule ------------------------------------------------------------------
async function renderSchedule() {
  const root = $("schedule");
  root.innerHTML = "";
  const curated = S.state.stations.filter(s => s.kind !== "specialty");
  for (const st of curated) {
    if (!S.options[st.mount]) S.options[st.mount] = await api("GET", `/options?station=${encodeURIComponent(st.mount)}`);
    const block = document.createElement("div");
    block.className = "station-block"; block.dataset.mount = st.mount;
    const today = (S.state.today[st.mount] || []).map(t => `${t.start}–${t.end} ${esc(t.name || "")}${t.kind === "artist" ? " (spotlight)" : ""}`).join(" · ");
    block.innerHTML = `<h3>${esc(st.name)} <small class="muted">${(st.family || []).join(", ")}</small></h3>
      <div class="today">Today: ${today || "no segments — the base all day"}</div>
      <div class="slots"></div>
      <button class="small" data-act="add">Add slot</button>`;
    const slots = block.querySelector(".slots");
    for (const s of S.state.schedule.filter(s => s.station === st.mount)) slots.appendChild(slotRow(st, s));
    root.appendChild(block);
  }
}
function slotRow(st, s) {
  const o = S.options[st.mount];
  const row = document.createElement("div");
  row.className = "slot";
  const daysSpec = s.days || "daily";
  const custom = Array.isArray(daysSpec);
  row.innerHTML = `
    <input type="text" name="name" placeholder="Name (as announced)" value="${esc(s.name || "")}">
    <select name="kind"><option value="feed">feed</option><option value="artist">artist spotlight</option><option value="auto">auto (DJ picks)</option></select>
    <span class="what">
      <select name="feed">${o.feeds.map(f => `<option value="${f.id}">${esc(f.title)} (${f.count})</option>`).join("")}</select>
      <select name="artist">${o.artists.map(a => `<option value="${esc(a.name)}">${esc(a.name)} (${a.tracks})</option>`).join("")}</select>
      <select name="like"><option value="artist">a different artist each time</option><option value="feed">a different feed each time</option></select>
    </span>
    <span class="days">
      <select name="dayspec"><option value="daily">daily</option><option value="weekdays">weekdays</option><option value="weekends">weekends</option><option value="custom">days…</option></select>
      <span class="custom">${DAYS.map(d => `<label><input type="checkbox" name="day" value="${d}" ${custom && daysSpec.includes(d) ? "checked" : ""}>${d}</label>`).join("")}</span>
    </span>
    <input type="time" name="start" value="${esc(s.start || "20:00")}">
    <input type="number" name="minutes" min="15" max="720" step="15" value="${s.minutes || 60}" title="minutes">
    <button class="small" data-act="remove" title="Remove">✕</button>`;
  row.dataset.id = s.id || "";
  row.querySelector('[name=kind]').value = s.kind || "feed";
  if (s.feed) row.querySelector('[name=feed]').value = s.feed;
  if (s.artist) row.querySelector('[name=artist]').value = s.artist;
  row.querySelector('[name=like]').value = s.like || "artist";
  row.querySelector('[name=dayspec]').value = custom ? "custom" : daysSpec;
  const sync = () => {
    const k = row.querySelector('[name=kind]').value;
    row.querySelector('[name=feed]').hidden = k !== "feed";
    row.querySelector('[name=artist]').hidden = k !== "artist";
    row.querySelector('[name=like]').hidden = k !== "auto";
    row.querySelector(".custom").hidden = row.querySelector('[name=dayspec]').value !== "custom";
  };
  row.querySelector('[name=kind]').addEventListener("change", sync);
  row.querySelector('[name=dayspec]').addEventListener("change", sync);
  sync();
  return row;
}
$("schedule").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const block = b.closest(".station-block");
  const st = S.state.stations.find(s => s.mount === block.dataset.mount);
  if (b.dataset.act === "add") block.querySelector(".slots").appendChild(slotRow(st, { kind: "feed", days: "daily", start: "20:00", minutes: 60 }));
  if (b.dataset.act === "remove") b.closest(".slot").remove();
});
$("save-schedule").addEventListener("click", async () => {
  const slots = [];
  for (const block of $("schedule").querySelectorAll(".station-block")) {
    for (const row of block.querySelectorAll(".slot")) {
      const v = (n) => row.querySelector(`[name=${n}]`).value;
      const dayspec = v("dayspec");
      const s = { id: row.dataset.id || undefined, station: block.dataset.mount, name: v("name"), kind: v("kind"),
                  days: dayspec === "custom" ? chosen(row, "day") : dayspec, start: v("start"), minutes: Number(v("minutes")) };
      if (s.kind === "feed") s.feed = v("feed");
      if (s.kind === "artist") s.artist = v("artist");
      if (s.kind === "auto") s.like = v("like");
      slots.push(s);
    }
  }
  try { const r = await api("PUT", "/schedule", slots); $("schedule-msg").textContent = `saved ${r.count} slot(s)`; await load(); }
  catch (err) { flash(err.message, true); }
});

// --- stations ------------------------------------------------------------------
function renderStations() {
  const tb = $("stations").querySelector("tbody");
  tb.innerHTML = "";
  for (const st of S.state.stations) {
    const tr = document.createElement("tr"); tr.dataset.mount = st.mount;
    const spec = st.kind === "specialty";
    tr.innerHTML = `<td><input type="text" name="name" value="${esc(st.name)}"><br><small class="muted">${st.mount}</small></td>
      <td>${spec ? `specialty<br><small class="muted">feed ${esc(st.feed)}</small>` : "curated"}</td>
      <td>${spec ? (st.family || []).join(", ") : chips("family", st.family || [], S.state.families)}</td>
      <td>${spec ? "" : `<textarea name="base" spellcheck="false">${esc(JSON.stringify(st.base || {}, null, 1))}</textarea>`}</td>
      <td><button class="small" data-act="save">Save</button></td>`;
    tb.appendChild(tr);
  }
}
$("stations").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const tr = b.closest("tr"), st = S.state.stations.find(s => s.mount === tr.dataset.mount);
  try {
    const body = { name: tr.querySelector('[name=name]').value };
    if (st.kind !== "specialty") { body.family = chosen(tr, "family"); body.base = JSON.parse(tr.querySelector('[name=base]').value || "{}"); }
    await api("PUT", `/stations/${st.mount}`, body);
    flash("Station saved; apply requested."); S.options = {}; await load();
  } catch (err) { flash(err.message, true); }
});

$("apply").addEventListener("click", async () => { try { await api("POST", "/apply"); flash("Apply requested: rescan, and a Liquidsoap restart if the station list changed."); await load(); } catch (err) { flash(err.message, true); } });

load().catch(err => flash(err.message, true));
setInterval(() => load().catch(() => {}), 30000);
