// Library radio — the page, as a Vue 3 app (global build, no bundler).
// Data, all same-origin, polled every 10 s:
//   now/catalogue.json     the station list (written by the scanner)
//   now/stations.json      per-station track counts
//   icecast-status         mounts up, titles, listener counts (Icecast's status-json via nginx)
//   now/<mount>.json       last played (Liquidsoap writes it on every track)
//   now/<mount>-next.json  what the DJ queued, its last break, the active segment
// One <audio> element, one station at a time. No scrub bar: play joins live
// on a fresh connection, stop drops it. The visualiser is a Web Audio
// analyser on the element (same-origin stream).

const { createApp } = Vue;

async function getJSON(url) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    return r.ok ? await r.json() : null;
  } catch (e) { return null; }
}

function mountsOf(status) {
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

createApp({
  data() {
    let quality = "";
    try { quality = localStorage.getItem("radio.quality") || ""; } catch (e) {}
    return { stations: [], counts: {}, up: {}, histories: {}, nexts: {}, current: null, open: null,
             status: "", quality, scanning: false, ctx: null, analyser: null, raf: 0 };
  },
  computed: {
    groups() {
      const curated = this.stations.filter(s => s.kind !== "specialty");
      const specialty = this.stations.filter(s => s.kind === "specialty");
      const g = [{ kind: "curated", title: "Curated", stations: curated }];
      if (specialty.length) g.push({ kind: "specialty", title: "Specialty", stations: specialty });
      return g;
    },
    currentStation() { return this.stations.find(s => s.mount === this.current) || { name: "", mount: "" }; },
    nowTitle() { const m = this.current; return (this.up[m + this.quality] || this.up[m] || {}).title || ""; },
    curHistory() { return this.histories[this.current] || []; },
    curNext() { return this.nexts[this.current] || {}; },
  },
  methods: {
    label(it) { return it.kind === "break" ? "station break" : (it.artist ? `${it.artist} — ${it.title}` : it.title); },
    isUp(m) { return !!(this.up[m] || this.up[m + "-lo"]); },
    titleOf(m) { return (this.up[m] || this.up[m + "-lo"] || {}).title || ""; },
    listenersOf(m) { return ((this.up[m] || {}).listeners | 0) + ((this.up[m + "-lo"] || {}).listeners | 0); },
    countOf(m) { const c = this.counts[m]; return c ? `${c.tracks.toLocaleString()} tracks` : ""; },
    streamUrl(m) { return `radio/${m}${this.quality}.mp3?t=${Date.now()}`; },

    async refresh() {
      const [cat, counts, ic] = await Promise.all([getJSON("now/catalogue.json"), getJSON("now/stations.json"), getJSON("icecast-status")]);
      if (cat) { this.stations = cat; this.scanning = false; } else if (!this.stations.length) this.scanning = true;
      if (counts) this.counts = counts;
      if (ic) this.up = mountsOf(ic);
      const want = new Set([this.current, this.open].filter(Boolean));
      for (const m of want) {
        const [h, n] = await Promise.all([getJSON(`now/${m}.json`), getJSON(`now/${m}-next.json`)]);
        if (h) this.histories[m] = h;
        if (n) this.nexts[m] = n;
      }
    },
    toggle(m) { this.open = this.open === m ? null : m; if (this.open) this.refresh(); },
    play(m) {
      const audio = this.$refs.audio;
      this.current = m;
      this.status = "tuning…";
      audio.src = this.streamUrl(m);
      audio.play().then(() => { this.status = ""; this.$nextTick(() => this.startViz()); })
                  .catch(err => { this.status = `couldn't start (${err.message})`; });
      this.refresh();
    },
    stop() {
      const audio = this.$refs.audio;
      audio.pause(); audio.removeAttribute("src"); audio.load();
      this.current = null; this.stopViz();
    },
    retune() {
      try { localStorage.setItem("radio.quality", this.quality); } catch (e) {}
      if (this.current) this.play(this.current);
    },
    startViz() {
      const audio = this.$refs.audio, canvas = this.$refs.viz;
      if (!canvas) return;
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        this.ctx = new AC();
        const src = this.ctx.createMediaElementSource(audio);
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 256; this.analyser.smoothingTimeConstant = 0.82;
        src.connect(this.analyser); this.analyser.connect(this.ctx.destination);
      }
      if (this.ctx.state === "suspended") this.ctx.resume();
      cancelAnimationFrame(this.raf);
      const g = canvas.getContext("2d"), data = new Uint8Array(this.analyser.frequencyBinCount);
      const accent = getComputedStyle(document.documentElement).getPropertyValue("--pico-primary").trim() || "#b5542a";
      const draw = () => {
        this.raf = requestAnimationFrame(draw);
        const c = this.$refs.viz; if (!c) return;
        this.analyser.getByteFrequencyData(data);
        const W = c.width, H = c.height, bars = 48, step = Math.floor(data.length * 0.75 / bars);
        g.clearRect(0, 0, W, H); g.fillStyle = accent;
        for (let i = 0; i < bars; i++) {
          let v = 0; for (let j = 0; j < step; j++) v = Math.max(v, data[i * step + j]);
          const h = (v / 255) * H, w = W / bars;
          g.globalAlpha = 0.35 + 0.65 * (v / 255); g.fillRect(i * w + 1, H - h, w - 2, h);
        }
        g.globalAlpha = 1;
      };
      draw();
    },
    stopViz() { cancelAnimationFrame(this.raf); },
  },
  mounted() {
    const audio = this.$refs.audio;
    audio.addEventListener("error", () => { this.status = "stream error — try again"; });
    audio.addEventListener("waiting", () => { this.status = "buffering…"; });
    audio.addEventListener("playing", () => { this.status = ""; });
    this.refresh();
    setInterval(() => this.refresh(), 10000);
  },
}).mount("#app");
