// Library radio — the page. No framework, no build step.
//
// Sources, all same-origin, all polled every 10 s:
//   stations.json        the catalogue (name, mount, era filter), static
//   now/stations.json    per-station track counts, written by the scanner
//   icecast-status       Icecast's status-json: which mounts are up, their
//                        current title, listener counts
//   now/<mount>.json     last played, written by Liquidsoap on each track
//   now/<mount>-next.json what the DJ has queued + its last break
//
// One <audio> element, one station at a time — it's a radio. No scrub bar:
// play always joins live (a cache-busting query on the stream URL makes the
// browser open a fresh connection rather than resume old buffer), stop tears
// the connection down so nothing keeps streaming. The visualiser is an
// AnalyserNode on the same element (same-origin stream, so Web Audio may
// read it).

const $ = (id) => document.getElementById(id);
const audio = $("audio");
const state = { stations: [], current: null, status: {}, quality: "", ctx: null, analyser: null, raf: 0 };

async function getJSON(url) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }   // a half-written file or a hiccup: keep what we had
}

function mountsOf(status) {
  // Icecast's quirk: `source` is an object with one mount, a list with several, absent with none.
  let src = status && status.icestats && status.icestats.source;
  if (!src) return {};
  if (!Array.isArray(src)) src = [src];
  const out = {};
  for (const s of src) {
    const m = (s.listenurl || "").split("/").pop().replace(/\.mp3$/, "");
    out[m] = { title: s.title || "", listeners: s.listeners | 0 };
  }
  return out;
}

function streamUrl(mount) { return `radio/${mount}${state.quality}.mp3`; }

function renderStations() {
  const ul = $("stations");
  ul.innerHTML = "";
  for (const s of state.stations) {
    const li = document.createElement("li");
    const base = state.status[s.mount] || {}, lo = state.status[s.mount + "-lo"] || {};
    const listeners = (base.listeners | 0) + (lo.listeners | 0);
    const up = !!(state.status[s.mount] || state.status[s.mount + "-lo"]);
    const count = s.tracks != null ? `${s.tracks.toLocaleString()} tracks` : "";
    const era = s.era ? s.era.join(" + ") : "";
    li.innerHTML = `<button class="btn ${state.current === s.mount ? "playing" : ""}" data-mount="${s.mount}" title="Play">${state.current === s.mount ? "■" : "▶"}</button>
      <div class="name"><b>${esc(s.name)}${era ? `<span class="badge">${esc(era)}</span>` : ""}</b>
        <small>${esc(count)}${up ? ` · <span class="live">on air</span>` : ""}${listeners ? ` · ${listeners} listening` : ""}</small></div>
      <div class="now">${up ? esc(base.title || lo.title || "") : ""}</div>`;
    ul.appendChild(li);
  }
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

async function refreshStatus() {
  const [ic, counts] = await Promise.all([getJSON("icecast-status"), getJSON("now/stations.json")]);
  if (ic) state.status = mountsOf(ic);
  if (counts) for (const s of state.stations) s.tracks = counts[s.mount] ? counts[s.mount].tracks : s.tracks;
  renderStations();
  if (state.current) await refreshPlayer();
}

async function refreshPlayer() {
  const s = state.stations.find(x => x.mount === state.current);
  if (!s) return;
  const st = state.status[s.mount + state.quality] || state.status[s.mount] || {};
  $("p-station").textContent = s.name;
  $("p-title").textContent = st.title || (audio.paused ? "—" : "tuning in…");
  const listeners = ((state.status[s.mount] || {}).listeners | 0) + ((state.status[s.mount + "-lo"] || {}).listeners | 0);
  $("p-meta").textContent = listeners ? `${listeners} listening` : "";
  const [hist, next] = await Promise.all([getJSON(`now/${s.mount}.json`), getJSON(`now/${s.mount}-next.json`)]);
  fillList($("p-history"), (hist || []).slice(1, 7));         // [0] is what's playing now
  fillList($("p-next"), next ? next.next : []);
  $("p-break").textContent = next && next.last_break ? `DJ: “${next.last_break}”` : "";
}

function fillList(ol, items) {
  ol.innerHTML = "";
  for (const it of items) {
    const li = document.createElement("li");
    if (it.kind === "break") { li.className = "break"; li.textContent = "station break"; }
    else li.textContent = it.artist ? `${it.artist} — ${it.title}` : it.title;
    ol.appendChild(li);
  }
  if (!items.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "—"; ol.appendChild(li); }
}

function play(mount) {
  state.current = mount;
  $("player").hidden = false;
  $("p-status").textContent = "tuning…";
  audio.src = `${streamUrl(mount)}?t=${Date.now()}`;   // always live: never resume old buffer
  audio.play().then(() => { $("p-status").textContent = ""; startViz(); })
             .catch(err => { $("p-status").textContent = `couldn't start (${err.message})`; });
  renderStations();
  refreshPlayer();
}

function stop() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();                       // drop the connection: no background streaming
  state.current = null;
  $("player").hidden = true;
  stopViz();
  renderStations();
}

// --- visualiser --------------------------------------------------------------
function startViz() {
  if (!state.ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    state.ctx = new AC();
    const src = state.ctx.createMediaElementSource(audio);
    state.analyser = state.ctx.createAnalyser();
    state.analyser.fftSize = 256;
    state.analyser.smoothingTimeConstant = 0.82;
    src.connect(state.analyser);
    state.analyser.connect(state.ctx.destination);
  }
  if (state.ctx.state === "suspended") state.ctx.resume();
  cancelAnimationFrame(state.raf);
  const canvas = $("viz"), g = canvas.getContext("2d");
  const data = new Uint8Array(state.analyser.frequencyBinCount);
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#b5542a";
  const draw = () => {
    state.raf = requestAnimationFrame(draw);
    state.analyser.getByteFrequencyData(data);
    const W = canvas.width, H = canvas.height, bars = 48, step = Math.floor(data.length * 0.75 / bars);
    g.clearRect(0, 0, W, H);
    g.fillStyle = accent;
    for (let i = 0; i < bars; i++) {
      let v = 0;
      for (let j = 0; j < step; j++) v = Math.max(v, data[i * step + j]);
      const h = (v / 255) * H;
      const w = W / bars;
      g.globalAlpha = 0.35 + 0.65 * (v / 255);
      g.fillRect(i * w + 1, H - h, w - 2, h);
    }
    g.globalAlpha = 1;
  };
  draw();
}
function stopViz() { cancelAnimationFrame(state.raf); const c = $("viz"); c.getContext("2d").clearRect(0, 0, c.width, c.height); }

// --- wiring ------------------------------------------------------------------
$("stations").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mount]");
  if (!b) return;
  if (state.current === b.dataset.mount) stop(); else play(b.dataset.mount);
});
$("stop").addEventListener("click", stop);
document.querySelectorAll('input[name="q"]').forEach(r => r.addEventListener("change", () => {
  state.quality = r.value;
  try { localStorage.setItem("radio.quality", state.quality); } catch (e) {}
  if (state.current) play(state.current);   // re-tune at the new bitrate
}));
audio.addEventListener("error", () => { $("p-status").textContent = "stream error — try again"; });
audio.addEventListener("waiting", () => { $("p-status").textContent = "buffering…"; });
audio.addEventListener("playing", () => { $("p-status").textContent = ""; });

(async function init() {
  try { state.quality = localStorage.getItem("radio.quality") || ""; } catch (e) {}
  const q = document.querySelector(`input[name="q"][value="${state.quality}"]`); if (q) q.checked = true;
  state.stations = (await getJSON("stations.json")) || [];
  renderStations();
  refreshStatus();
  setInterval(refreshStatus, 10000);
})();
