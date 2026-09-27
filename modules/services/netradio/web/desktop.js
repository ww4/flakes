// Library radio — the page, as a Vue 3 app (global build, no bundler), and
// the phone remote for the living-room receiver. All same-origin:
//   now/catalogue.json     the station list (written by the scanner)
//   now/quick-picks.json   the internet stations in the receiver's menu
//   now/stations.json      per-station track counts
//   icecast-status         mounts up, titles, listener counts
//   now/<mount>.json       last played;  now/<mount>-next.json  what the DJ queued
//   receiver/*             the receiver's JSON API (yamaha-ync-api via nginx)
//   admin/api/dj/*, admin/api/dislike, admin/api/search   listener feedback (the DJ's inbox)
// Three targets: "here" plays the stream in this browser (one <audio>, live,
// no scrub bar); "room" drives the receiver — a station tap walks its
// NET RADIO menu to My Stations → <category> → <station>; "local" drives this
// machine's own sound card through speaker/* (netradio-speaker).
// The phone page grew "local" first and this one did not, so a wide screen —
// which index.html redirects here automatically — had no way to reach the
// speakers at all (2026-09-26).

const { createApp } = Vue;

async function getJSON(url) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    return r.ok ? await r.json() : null;
  } catch (e) { return null; }
}

async function call(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((data && (data.error || data.hint)) || `${method} ${url}: ${r.status}`);
  return data;
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
    let quality = "", target = "here";
    try { quality = localStorage.getItem("radio.quality") || ""; target = localStorage.getItem("radio.target") || "here"; } catch (e) {}
    return { stations: [], quick: [], counts: {}, up: {}, histories: {}, nexts: {}, current: null, open: null,
             status: "", quality, scanning: false, ctx: null, analyser: null, raf: 0,
             target, receiver: { name: "", on: false, input: "", volume: 0, mute: false, error: "" }, inputs: [], presets: [],
             volumeDraft: 0, busy: "", toast: "", requesting: false, query: "", results: [], searchTimer: 0, roomPoll: 0,
             site: { title: "Radio", localName: "These speakers", roomName: "Living room", localMount: "", hasLocal: false, hasRoom: true },
             speaker: { playing: false, mount: "", volume: null, muted: false, error: "" },
             speakerDraft: 0, speakerDragging: false, speakerPoll: 0 };
  },
  computed: {
    groups() {
      // a fixed station (Rain, Rainy Mood) lists as a specialty, exactly as
      // on the phone page — it is not a curated genre programme
      const curated = this.stations.filter(s => !["specialty", "fixed"].includes(s.kind));
      const specialty = this.stations.filter(s => ["specialty", "fixed"].includes(s.kind));
      const g = [{ kind: "curated", title: "Curated", stations: curated }];
      if (specialty.length) g.push({ kind: "specialty", title: "Specialty", stations: specialty });
      if (this.target === "room" && this.quick.length)
        g.push({ kind: "quick", title: "Quick Picks", stations: this.quick.map(q => ({ mount: "", name: q.name, kind: "quick" })) });
      return g;
    },
    currentStation() { return this.stations.find(s => s.mount === this.current) || { name: "", mount: "" }; },
    nowTitle() { const m = this.current; return (this.up[m + this.quality] || this.up[m] || {}).title || ""; },
    curNext() { return this.nexts[this.current] || {}; },
    roomTitle() {
      const np = this.receiver.now_playing;
      if (!np) return "";
      const t = [np.artist, np.track].filter(Boolean).join(" — ") || np.station || "";
      return np.playback === "Play" ? t : (np.playback ? `${np.playback.toLowerCase()} · ${t}` : t);
    },
    // the library station being listened to on the chosen target, for feedback
    roomMount() {
      const np = this.receiver.now_playing;
      if (!this.receiver.on || this.receiver.input !== "NET RADIO" || !np || !np.station) return null;
      const s = this.stations.find(s => s.name === np.station);
      return s ? s.mount : null;
    },
    feedbackMount() { return this.target === "room" ? this.roomMount : this.target === "local" ? this.speaker.mount : this.current; },
    feedbackStation() { return this.stations.find(s => s.mount === this.feedbackMount) || { name: "" }; },
    feedbackTitle() { return this.target === "room" ? this.roomTitle : this.nowTitle; },
    feedbackHistory() { return this.histories[this.feedbackMount] || []; },
    feedbackNext() { return this.nexts[this.feedbackMount] || {}; },
  },
  watch: {
    target(t) {
      try { localStorage.setItem("radio.target", t); } catch (e) {}
      if (t !== "room") clearInterval(this.roomPoll);
      if (t !== "local") clearInterval(this.speakerPoll);
      if (t === "local") this.pollSpeaker();
    },
    "receiver.volume"(v) { this.volumeDraft = v; },
  },
  methods: {
    label(it) { return it.kind === "break" ? "station break" : (it.artist ? `${it.artist} — ${it.title}` : it.title); },
    isUp(m) { return !!(this.up[m] || this.up[m + "-lo"]); },
    titleOf(m) { return (this.up[m] || this.up[m + "-lo"] || {}).title || ""; },
    listenersOf(m) { return ((this.up[m] || {}).listeners | 0) + ((this.up[m + "-lo"] || {}).listeners | 0); },
    countOf(m) { const c = this.counts[m]; return c ? `${c.tracks.toLocaleString()} tracks${c.fringe ? " +" + c.fringe.toLocaleString() + " fringe" : ""}` : ""; },
    streamUrl(m) { return `radio/${m}${this.quality}.mp3?t=${Date.now()}`; },
    isPlaying(s) {
      if (this.target === "room") return this.receiver.on && this.receiver.now_playing && this.receiver.now_playing.station === s.name;
      if (this.target === "local") return !!s.mount && this.speaker.mount === s.mount;
      return this.current === s.mount;
    },
    say(msg) { this.toast = msg; clearTimeout(this._toastT); this._toastT = setTimeout(() => { this.toast = ""; }, 4000); },

    async refresh() {
      const [cat, quick, counts, ic] = await Promise.all([getJSON("now/catalogue.json"), getJSON("now/quick-picks.json"), getJSON("now/stations.json"), getJSON("icecast-status")]);
      if (cat) { this.stations = cat; this.scanning = false; } else if (!this.stations.length) this.scanning = true;
      if (quick) this.quick = quick;
      if (counts) this.counts = counts;
      if (ic) this.up = mountsOf(ic);
      const want = new Set([this.current, this.open, this.feedbackMount].filter(Boolean));
      for (const m of want) {
        const [h, n] = await Promise.all([getJSON(`now/${m}.json`), getJSON(`now/${m}-next.json`)]);
        if (h) this.histories[m] = h;
        if (n) this.nexts[m] = n;
      }
    },
    toggle(m) { if (!m) return; this.open = this.open === m ? null : m; if (this.open) this.refresh(); },

    // ---- this phone ----------------------------------------------------------
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
    playTarget(s) {
      if (this.target === "room") return this.playOnReceiver(s);
      if (this.target === "local") return s.mount ? this.speakerPlay(s.mount) : undefined;
      return this.play(s.mount);
    },
    stopTarget() {
      if (this.target === "room") return this.receiverAction("stopping…", () => call("POST", "receiver/playback", { action: "Stop" }));
      if (this.target === "local") return this.speakerAction("stopping…", () => call("POST", "speaker/stop", {}));
      return this.stop();
    },

    // ---- this machine's own audio output -------------------------------------
    async pollSpeaker() {
      const st = await getJSON("speaker/state");
      this.speaker = st ? { ...st, error: st.error || "" } : { ...this.speaker, error: "unreachable" };
      if (st && !this.speakerDragging) this.speakerDraft = st.volume ?? 0;
      clearInterval(this.speakerPoll);
      this.speakerPoll = setInterval(async () => {
        if (this.target !== "local" || this.busy) return;
        const s2 = await getJSON("speaker/state");
        if (s2) { this.speaker = { ...s2, error: s2.error || "" }; if (!this.speakerDragging) this.speakerDraft = s2.volume ?? this.speakerDraft; }
      }, 5000);
    },
    async speakerAction(label, fn) {
      this.busy = label;
      try { const st = await fn(); if (st) this.speaker = { ...st, error: st.error || "" }; }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },
    speakerPlay(mount) { return this.speakerAction("starting…", () => call("POST", "speaker/play", { mount })); },
    speakerSet(level) {
      this.speakerDraft = Math.max(0, Math.min(100, Math.round(level)));
      return this.speakerAction("", () => call("POST", "speaker/volume", { level: this.speakerDraft }));
    },
    speakerVolume(step) { return this.speakerSet((this.speaker.volume ?? this.speakerDraft) + step); },
    speakerMute(on) { return this.speakerAction("", () => call("POST", "speaker/mute", { on })); },
    // what Play starts when nothing is selected: the station the box is tuned
    // to by default (site.json), else the first one in the list
    last_local_mount() { return this.site.localMount || (this.stations[0] || {}).mount || ""; },

    // ---- the receiver --------------------------------------------------------
    async pollReceiver() {
      const st = await getJSON("receiver/status");
      if (!st) { this.receiver = { ...this.receiver, error: "unreachable" }; return; }
      this.receiver = { ...st, error: "" };
      if (!this.inputs.length) this.inputs = (await getJSON("receiver/inputs")) || [];
      if (st.input === "TUNER" && !this.presets.length) this.presets = (await getJSON("receiver/tuner/presets")) || [];
      clearInterval(this.roomPoll);
      this.roomPoll = setInterval(() => { if (this.target === "room" && !this.busy) this.pollReceiver(); }, 5000);
    },
    async receiverAction(label, fn) {
      this.busy = label;
      try { const r = await fn(); if (r && "on" in r) this.receiver = { ...this.receiver, ...r }; await this.pollReceiver(); }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },
    power(on) { return this.receiverAction(on ? "powering on…" : "standby…", () => call("POST", "receiver/power", { on })); },
    mute(on) { return this.receiverAction("", () => call("POST", "receiver/mute", { on })); },
    setVolume(level) { return this.receiverAction("", () => call("POST", "receiver/volume", { level })); },
    volumeStep(step) { this.volumeDraft = Math.max(0, Math.min(this.receiver.volume_max || 100, this.volumeDraft + step)); return this.setVolume(this.volumeDraft); },
    selectInput(name) { return this.receiverAction(`switching to ${name}…`, () => call("POST", "receiver/input", { name })); },
    tunerPreset(n) { return this.receiverAction("tuning…", () => call("POST", "receiver/tuner", { preset: n })); },
    playOnReceiver(s) {
      const category = s.kind === "quick" ? "Quick Picks" : s.kind === "specialty" ? "Specialty" : "Curated";
      return this.receiverAction(`tuning the ${this.receiver.name || "receiver"} to ${s.name}…`,
        () => call("POST", "receiver/menu/path", { path: ["My Stations", category, s.name] }));
    },

    // ---- feedback (either target) ---------------------------------------------
    nowTrack() {
      // what is playing on the feedback station, from the DJ's history (path, artist, title)
      const h = this.feedbackHistory;
      return (h && h[0] && h[0].kind !== "break") ? h[0] : null;
    },
    async skip() {
      if (!this.feedbackMount) return;
      try { await call("POST", `admin/api/dj/${this.feedbackMount}/skip`); this.say("skipping…"); setTimeout(() => this.refresh(), 2500); }
      catch (e) { this.say(e.message); }
    },
    async dislike(scope) {
      const t = this.nowTrack();
      if (!t || !this.feedbackMount) { this.say("nothing to rate yet"); return; }
      try {
        await call("POST", "admin/api/dislike", { scope, path: t.path, artist: t.artist, title: t.title, mount: this.feedbackMount });
        this.say(scope === "artist" ? `less ${t.artist} from now on` : `never again: ${t.title}`);
        setTimeout(() => this.refresh(), 2500);
      } catch (e) { this.say(e.message); }
    },
    searchDebounced() {
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(async () => {
        this.results = this.query.trim().length > 1 ? ((await getJSON(`admin/api/search?q=${encodeURIComponent(this.query)}`)) || []) : [];
      }, 250);
    },
    async request(r) {
      if (!this.feedbackMount) return;
      try {
        await call("POST", `admin/api/dj/${this.feedbackMount}/request`, { path: r.path });
        this.say(`next on ${this.feedbackStation.name}: ${r.title}`);
        this.requesting = false; this.query = ""; this.results = [];
        setTimeout(() => this.refresh(), 3000);
      } catch (e) { this.say(e.message); }
    },

    // ---- visualiser -----------------------------------------------------------
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
    getJSON("site.json").then(s => { if (s) { this.site = { ...this.site, ...s }; document.title = this.site.title; } });
    this.refresh();
    setInterval(() => this.refresh(), 10000);
    if (this.target === "room") this.pollReceiver();
    if (this.target === "local") this.pollSpeaker();
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  },
}).mount("#app");
