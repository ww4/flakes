// Library radio — the remote (Vue 3 global build, no bundler). Same-origin data:
//   now/catalogue.json, now/quick-picks.json, now/stations.json, icecast-status
//   now/<mount>.json (last played), now/<mount>-next.json (the DJ's queue)
//   receiver/*      the receiver's JSON API (yamaha-ync-api via nginx)
//   admin/api/dj/*, admin/api/dislike, admin/api/search   listener feedback
// Two targets: "here" plays the stream in this browser; "room" drives the
// receiver — a station tile walks its NET RADIO menu, a Pandora tile its
// Pandora list, a preset tile the tuner.

const { createApp } = Vue;

async function getJSON(url) {
  try { const r = await fetch(url, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch (e) { return null; }
}
async function call(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null; try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((data && (data.error || data.hint)) || `${method} ${url}: ${r.status}`);
  return data;
}
function mountsOf(status) {
  let src = status && status.icestats && status.icestats.source;
  if (!src) return {};
  if (!Array.isArray(src)) src = [src];
  const out = {};
  for (const s of src) { const m = (s.listenurl || "").split("/").pop().replace(/\.mp3$/, ""); out[m] = { title: s.title || "", listeners: s.listeners | 0 }; }
  return out;
}
// a stable colour per name, for tiles and the hero
function hue(name) { let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }

createApp({
  data() {
    let quality = "", target = "here", tab = "now", last = { here: "", room: null, gromit: "rain" };
    try { quality = localStorage.getItem("radio.quality") || ""; target = localStorage.getItem("radio.target") || "here"; tab = localStorage.getItem("radio.tab") || "now"; last = { ...last, ...JSON.parse(localStorage.getItem("radio.last") || "{}") }; } catch (e) {}
    return { tiles: {}, stations: [], quick: [], counts: {}, up: {}, histories: {}, nexts: {}, current: null, status: "", quality, scanning: false, last,
             ctx: null, analyser: null, raf: 0, wide: window.innerWidth > 640,
             target, tab, receiver: { name: "", on: false, input: "", volume: 0, mute: false, error: "" }, inputs: [], presets: [],
             speaker: { playing: false, mount: "", volume: null, muted: false, error: "" }, speakerPoll: 0,
             pandora: JSON.parse((() => { try { return localStorage.getItem("radio.pandora") || "[]"; } catch (e) { return "[]"; } })()), menu: { lines: [], layer: 0, max_line: 0, current_line: 1, status: "", name: "" }, menuSource: "",
             remote: false, volumeDraft: 0, freqDraft: "", busy: "", toast: "", query: "", results: [], searchTimer: 0, roomPoll: 0, artFailed: "", artFailedAt: 0, tick: 0 };
  },
  computed: {
    groups() {
      // a fixed station (Rain, Rainy Mood) is a specialty as far as the
      // list is concerned — it is not a curated genre programme
      const curated = this.stations.filter(s => !["specialty", "fixed"].includes(s.kind));
      const specialty = this.stations.filter(s => ["specialty", "fixed"].includes(s.kind));
      const g = [{ kind: "curated", title: "Curated", stations: curated }];
      if (specialty.length) g.push({ kind: "specialty", title: "Specialty", stations: specialty });
      if (this.target === "room") {
        if (this.quick.length) g.push({ kind: "quick", title: "Quick Picks", stations: this.quick.map(q => ({ mount: "", name: q.name, kind: "quick" })) });
        if (this.pandora.length) g.push({ kind: "pandora", title: "Pandora", stations: this.pandora.map(n => ({ mount: "", name: n, kind: "pandora" })) });
        if (this.presets.length) g.push({ kind: "preset", title: "Radio presets", stations: this.presets.map(p => ({ mount: "", name: p.text.replace(/^\d+\s*:\s*/, ""), kind: "preset", number: p.number, sub: `preset ${p.number}` })) });
      }
      return g;
    },
    currentStation() { return this.stations.find(s => s.mount === this.current) || { name: "", mount: "" }; },
    nowTitle() { const m = this.current; return (this.up[m + this.quality] || this.up[m] || {}).title || ""; },
    // --- what "Now" shows, per target
    np() { return (this.target === "room" && this.receiver.now_playing) || null; },
    // an ambient station (rain) has no DJ, no queue and nothing to skip:
    // its transport is the play/stop on the bottom bar, nothing else
    isFixed() { return (m) => (this.stations.find(s => s.mount === m) || {}).kind === "fixed"; },
    nowKind() {
      if (this.target === "gromit") return !this.speaker.mount ? "" : this.isFixed(this.speaker.mount) ? "ambient" : "library";
      if (this.target === "here") return !this.current ? "" : this.isFixed(this.current) ? "ambient" : "library";
      if (!this.receiver.on) return "";
      if (this.receiver.input === "TUNER") return "tuner";
      if (this.receiver.input === "Pandora") return "pandora";
      if (this.roomMount) return "library";
      if (["NET RADIO", "Spotify", "SERVER", "AirPlay"].includes(this.receiver.input)) return "stream";
      return "";
    },
    roomMount() {
      const np = this.np;
      if (!this.receiver.on || this.receiver.input !== "NET RADIO" || !np || !np.station) return null;
      const s = this.stations.find(s => s.name === np.station); return s ? s.mount : null;
    },
    nowStation() {
      if (this.target === "gromit") return (this.stations.find(s => s.mount === this.speaker.mount) || {}).name || "";
      if (this.target === "here") return this.currentStation.name;
      const np = this.np;
      if (this.nowKind === "tuner") return `${this.receiver.tuner.band} radio`;
      if (this.nowKind === "pandora") return np && np.station ? np.station : "Pandora";
      return np && np.station ? np.station : (this.receiver.on ? this.receiver.input : "");
    },
    nowLine1() {
      if (this.target === "gromit") { const m = this.speaker.mount; return (this.up[m] || {}).title || ""; }
      if (this.target === "here") return this.nowTitle;
      if (this.nowKind === "tuner") return this.freqText + (this.receiver.tuner.band === "FM" ? " MHz" : " kHz");
      const np = this.np; if (!np) return "";
      return this.nowKind === "pandora" ? (np.track || "") : (np.track || np.station || "");
    },
    nowLine2() {
      if (this.target === "gromit") return this.speaker.error || (this.speaker.playing ? "on gromit's speakers" : "stopped");
      if (this.nowKind === "ambient") return "on a loop";
      if (this.target === "here") { const n = this.listenersOf(this.current); return n ? `${n} listening` : ""; }
      if (this.nowKind === "tuner") return this.receiver.tuner.tuned ? (this.receiver.tuner.stereo ? "stereo" : "mono") : "no signal";
      const np = this.np; if (!np) return "";
      return this.nowKind === "pandora" ? [np.artist, np.album].filter(Boolean).join(" · ") : (np.artist || "");
    },
    art() {
      // a library track: its embedded picture / folder cover via the admin API;
      // Pandora & co: the unit's own album art, relayed by ync-api. The <img>'s
      // onerror falls back to the coloured tile.
      if (this.nowKind === "library") {
        const t = this.nowTrack();
        return t && t.path ? `admin/api/art?path=${encodeURIComponent(t.path)}` : "";
      }
      const np = this.np;
      if (!np || !np.album_art_url) return "";
      if (/^https?:/.test(np.album_art_url)) return np.album_art_url;
      return `receiver/art?url=${encodeURIComponent(np.album_art_url)}`;
    },
    segment() { const seg = this.feedbackNext.segment; return seg && seg.name ? seg : null; },
    segmentLine() {
      const seg = this.segment; if (!seg) return "";
      return seg.kind === "artist" ? `${seg.name.replace(/ spotlight$/i, "")} spotlight` : seg.name;
    },
    heroStyle() { const h = hue(this.nowStation || "radio"); return { background: `linear-gradient(160deg, hsl(${h} 45% 34%), hsl(${(h + 40) % 360} 55% 18%))` }; },
    freqText() { const t = this.receiver.tuner; if (!t) return ""; return t.band === "FM" ? (t.fm.val / 100).toFixed(1) : String(t.am.val); },
    // --- nothing chosen yet: the play button starts what played last on this target
    idle() {
      if (this.target === "gromit") return !this.speaker.mount;
      if (this.target === "here") return !this.current;
      const np = this.np;
      return this.receiver.on && this.receiver.input === "NET RADIO" && !!np && np.playback === "Stop" && !np.station;
    },
    lastStation() {
      if (this.target === "gromit") return this.stations.find(s => s.mount === (this.last.gromit || "rain")) || null;
      if (this.target === "here") return this.stations.find(s => s.mount === this.last.here) || null;
      const l = this.last.room; if (!l) return null;
      for (const g of this.groups) { const s = g.stations.find(s => s.kind === l.kind && s.name === l.name); if (s) return s; }
      return l.kind ? l : null;   // not in today's lists (a Pandora station since removed, say): still worth a try
    },
    // a fixed station has no DJ behind it: nothing to skip to, nothing to
    // request, no history to dislike. The buttons go rather than lie.
    canDJ() { const m = this.target === "room" ? this.roomMount : this.target === "gromit" ? this.speaker.mount : this.current;
              return !!m && !this.isFixed(m); },
    feedbackMount() {
      const m = this.target === "room" ? this.roomMount : this.target === "gromit" ? this.speaker.mount : this.current;
      return this.isFixed(m) ? null : m;      // no history, no requests, no dislikes on a rain loop
    },
    feedbackStation() { return this.stations.find(s => s.mount === this.feedbackMount) || { name: "" }; },
    feedbackHistory() { return this.histories[this.feedbackMount] || []; },
    feedbackNext() { return this.nexts[this.feedbackMount] || {}; },
  },
  watch: {
    target(t) {
      try { localStorage.setItem("radio.target", t); } catch (e) {}
      clearInterval(this.roomPoll); clearInterval(this.speakerPoll);
      if (t !== "room" && this.tab === "sources") this.tab = "now";
      if (t === "room") this.pollReceiver();
      if (t === "gromit") this.pollSpeaker();
    },
    tab(t) { try { localStorage.setItem("radio.tab", t); } catch (e) {} if (t === "sources") this.loadMenu(); },
    "receiver.volume"(v) { this.volumeDraft = v; },
    "receiver.input"(i) { if (i === "TUNER" && this.receiver.tuner) this.freqDraft = this.freqText; if (this.tab === "sources") this.loadMenu(); },
  },
  methods: {
    tileCovers(s) { return s.mount && this.tiles[s.mount] ? (this.tiles[s.mount].covers || []) : []; },
    tileIcon(s) { return s.mount && this.tiles[s.mount] ? (this.tiles[s.mount].icon || "") : ""; },
    thumb(path, size = 200) { return `admin/api/art?path=${encodeURIComponent(path)}&size=${size}`; },
    initials(name) { return (name || "").split(/[\s&]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join(""); },
    tileStyle(s) { const h = hue(s.name); return { background: `linear-gradient(160deg, hsl(${h} 40% 30%), hsl(${(h + 40) % 360} 50% 17%))` }; },
    label(it) { return it.kind === "break" ? "station break" : (it.artist ? `${it.artist} — ${it.title}` : it.title); },
    isUp(m) { return !!m && !!(this.up[m] || this.up[m + "-lo"]); },
    listenersOf(m) { return ((this.up[m] || {}).listeners | 0) + ((this.up[m + "-lo"] || {}).listeners | 0); },
    countOf(m) { const c = this.counts[m]; return c ? `${c.tracks.toLocaleString()} tracks${c.fringe ? " +" + c.fringe.toLocaleString() + " fringe" : ""}` : ""; },
    streamUrl(m) { return `radio/${m}${this.quality}.mp3?t=${Date.now()}`; },
    artError() { this.artFailed = this.art; this.artFailedAt = Date.now(); },
    artOk() { return !!this.art && (this.artFailed !== this.art || this.tick - this.artFailedAt > 30000); },   // a failed cover is retried after 30 s, not written off until the next song
    say(msg) { this.toast = msg; clearTimeout(this._toastT); this._toastT = setTimeout(() => { this.toast = ""; }, 3500); },
    // here → the living room → gromit's own speakers → back
    pickTarget() {
      const order = ["here", "room", "gromit"];
      this.target = order[(order.indexOf(this.target) + 1) % order.length];
      this.say({ here: "playing on this phone", room: `controlling the ${this.receiver.name || "receiver"}`,
                 gromit: "controlling gromit's speakers" }[this.target]);
    },
    targetName() { return { here: "This phone", room: this.receiver.name || "Living room", gromit: "Gromit speakers" }[this.target]; },

    // ---- gromit's own audio output ------------------------------------------
    async pollSpeaker() {
      const st = await getJSON("speaker/state");
      this.speaker = st ? { ...st, error: st.error || "" } : { ...this.speaker, error: "unreachable" };
      clearInterval(this.speakerPoll);
      this.speakerPoll = setInterval(async () => {
        const s = await getJSON("speaker/state");
        if (s) this.speaker = { ...s, error: s.error || "" };
      }, 10000);
    },
    async speakerAction(msg, fn) {
      this.busy = msg;
      try { const st = await fn(); if (st) this.speaker = { ...st, error: st.error || "" }; }
      catch (e) { this.say(e.message, true); }
      finally { this.busy = ""; }
    },
    speakerPlay(mount) { return this.speakerAction("starting…", () => call("POST", "speaker/play", { mount })); },
    speakerVolume(step) { return this.speakerAction("", () => call("POST", "speaker/volume", { step })); },
    speakerMute(on) { return this.speakerAction("", () => call("POST", "speaker/mute", { on })); },
    isPlaying(s) {
      if (this.target === "gromit") return !!s.mount && this.speaker.mount === s.mount;
      if (this.target === "here") return !!s.mount && this.current === s.mount;
      const np = this.np;
      if (s.kind === "preset") return this.receiver.input === "TUNER" && this.receiver.tuner && String(this.receiver.tuner.preset) === String(s.number);
      if (s.kind === "pandora") return this.receiver.input === "Pandora" && !!np && np.station === s.name;
      return this.receiver.on && !!np && np.station === s.name;
    },

    async refresh() {
      const [cat, quick, counts, ic] = await Promise.all([getJSON("now/catalogue.json"), getJSON("now/quick-picks.json"), getJSON("now/stations.json"), getJSON("icecast-status")]);
      if (cat) { this.stations = cat; this.scanning = false; } else if (!this.stations.length) this.scanning = true;
      if (quick) this.quick = quick;
      if (!this._tilesAt || Date.now() - this._tilesAt > 600000) { const t = await getJSON("now/tiles.json"); if (t) { this.tiles = t; this._tilesAt = Date.now(); } }
      if (counts) this.counts = counts;
      if (ic) this.up = mountsOf(ic);
      const m = this.feedbackMount;
      if (m) {
        const [h, n] = await Promise.all([getJSON(`now/${m}.json`), getJSON(`now/${m}-next.json`)]);
        if (h) this.histories[m] = h;
        if (n) this.nexts[m] = n;
      }
    },

    // ---- this phone ----------------------------------------------------------
    remember(s) {
      this.last = { ...this.last, [this.target]: this.target === "room" ? { kind: s.kind, name: s.name, mount: s.mount, number: s.number } : s.mount };
      try { localStorage.setItem("radio.last", JSON.stringify(this.last)); } catch (e) {}
    },
    resume() { const s = this.lastStation; if (s) this.playTarget(s); else this.tab = "stations"; },
    play(m) {
      const audio = this.$refs.audio;
      const s = this.stations.find(s => s.mount === m); if (s) this.remember(s);
      this.current = m; this.status = "tuning…"; this.tab = "now";
      audio.src = this.streamUrl(m);
      audio.play().then(() => { this.status = ""; this.$nextTick(() => this.startViz()); }).catch(err => { this.status = `couldn't start (${err.message})`; });
      this.refresh();
    },
    stop() { const a = this.$refs.audio; a.pause(); a.removeAttribute("src"); a.load(); this.current = null; this.stopViz(); },
    retune() { try { localStorage.setItem("radio.quality", this.quality); } catch (e) {} if (this.current) this.play(this.current); },
    playTarget(s) {
      this.tab = "now";              // always show the switch happen
      if (this.target === "gromit") { if (!s.mount) return; this.remember(s); return this.speakerPlay(s.mount); }
      if (this.target !== "room") return this.play(s.mount);
      this.remember(s);
      if (s.kind === "preset") return this.receiverAction("tuning…", async () => { if (this.receiver.input !== "TUNER") await call("POST", "receiver/input", { name: "TUNER" }); return call("POST", "receiver/tuner", { preset: s.number }); });
      if (s.kind === "pandora") return this.receiverAction(`starting ${s.name}…`, () => call("POST", "receiver/menu/path", { source: "Pandora", path: [s.name] }));
      const category = s.kind === "quick" ? "Quick Picks" : s.kind === "specialty" ? "Specialty" : "Curated";
      return this.receiverAction(`tuning to ${s.name}…`, () => call("POST", "receiver/menu/path", { path: ["My Stations", category, s.name] }));
    },
    stopTarget() {
      if (this.target === "gromit") return this.speakerAction("stopping…", () => call("POST", "speaker/stop", {}));
      return this.target === "room" ? this.receiverAction("stopping…", () => call("POST", "receiver/playback", { action: "Stop" })) : this.stop();
    },

    // ---- the receiver --------------------------------------------------------
    async pollReceiver() {
      const st = await getJSON("receiver/status");
      if (!st) { this.receiver = { ...this.receiver, error: "unreachable" }; return; }
      this.receiver = { ...st, error: "" };
      // the station just became known (cold load): fetch its history now,
      // don't wait for the 10 s refresh — the cover comes from it
      const m = this.feedbackMount;
      if (m && !this.histories[m]) this.refresh();
      if (!this.inputs.length) this.inputs = (await getJSON("receiver/inputs")) || [];
      if (st.on && !this.presets.length) this.presets = (await getJSON("receiver/tuner/presets")) || [];
      if (st.on && st.input === "Pandora" && !this._pandoraFresh) { this._pandoraFresh = true; await this.loadPandora(); }
      clearInterval(this.roomPoll);
      this.roomPoll = setInterval(() => { if (this.target === "room" && !this.busy) this.pollReceiver(); }, 5000);
    },
    async receiverAction(label, fn) {
      this.busy = label;
      try { const r = await fn(); if (r && "on" in r) this.receiver = { ...this.receiver, ...r }; await this.pollReceiver(); await this.refresh(); }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },
    // ---- the remote ----------------------------------------------------------
    openRemote() {
      this.remote = true;
      if (this.target !== "room") this.target = "room";
      this.pollReceiver();
      if (!this.inputs.length) getJSON("receiver/inputs").then(i => { if (i) this.inputs = i; });
      if (!this.presets.length) getJSON("receiver/tuner/presets").then(p => { if (p) this.presets = p; });
    },
    remoteSource() {
      const map = { "NET RADIO": "NET_RADIO", "Pandora": "Pandora", "Spotify": "Spotify",
                    "SERVER": "SERVER", "AirPlay": "AirPlay", "TUNER": "Tuner" };
      return map[this.receiver.input] || "";
    },
    cursor(action) {
      const src = this.remoteSource();
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: src, action }); });
    },
    remotePage(down) {
      const src = this.remoteSource();
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: src, down }); });
    },
    presetStep(up) { return this.receiverAction("", () => call("POST", "receiver/tuner", { preset: up ? "Up" : "Down" })); },
    band(b) { return this.receiverAction("", () => call("POST", "receiver/tuner", { band: b, frequency: b === "FM" ? 93.1 : 1300 })); },
    power(on) { return this.receiverAction(on ? "powering on…" : "standby…", () => call("POST", "receiver/power", { on })); },
    mute(on) { return this.receiverAction("", () => call("POST", "receiver/mute", { on })); },
    setVolume(level) { return this.receiverAction("", () => call("POST", "receiver/volume", { level })); },
    volumeStep(step) { this.volumeDraft = Math.max(0, Math.min(this.receiver.volume_max || 100, this.volumeDraft + step)); return this.setVolume(this.volumeDraft); },
    selectInput(name) { return this.receiverAction(`switching to ${name}…`, () => call("POST", "receiver/input", { name })); },
    playback(action) { return this.receiverAction("", () => call("POST", "receiver/playback", { action })); },
    feedback(up) { return this.receiverAction("", async () => { await call("POST", "receiver/feedback", { thumbs_up: up, source: "Pandora" }); this.say(up ? "thumbs up" : "thumbs down"); }); },
    // tuner
    band(b) { return this.receiverAction("", () => call("POST", "receiver/tuner", { band: b, frequency: b === "FM" ? this.receiver.tuner.fm.val / 100 : this.receiver.tuner.am.val })); },
    tuneStep(dir) { const t = this.receiver.tuner; const fm = t.band === "FM"; const f = fm ? Math.round((t.fm.val / 100 + dir * 0.2) * 10) / 10 : t.am.val + dir * 10; return this.receiverAction("", () => call("POST", "receiver/tuner", { band: t.band, frequency: f })); },
    tuneTo() { const t = this.receiver.tuner; return this.receiverAction("tuning…", () => call("POST", "receiver/tuner", { band: t.band, frequency: parseFloat(this.freqDraft) })); },
    seek(up) { return this.receiverAction(up ? "seeking up…" : "seeking down…", () => call("POST", "receiver/tuner/seek", { up })); },
    tunerPreset(n) { return this.receiverAction("tuning…", () => call("POST", "receiver/tuner", { preset: n })); },
    // menus (Pandora list, media server)
    sourceOf(input) { return ({ "NET RADIO": "NET_RADIO", Pandora: "Pandora", SERVER: "SERVER", Spotify: "Spotify", AirPlay: "AirPlay" })[input] || ""; },
    async loadMenu() {
      this.menuSource = ["SERVER", "Pandora", "NET RADIO"].includes(this.receiver.input) ? this.sourceOf(this.receiver.input) : "";
      if (!this.menuSource) return;
      const m = await getJSON(`receiver/menu?source=${this.menuSource}`);
      if (m) this.menu = m;
    },
    async loadPandora() {
      // the Pandora station list, from its menu's first pages
      try {
        let m = await call("POST", "receiver/menu/cursor", { source: "Pandora", action: "Return to Home" });
        const names = [];
        for (let i = 0; i < 6 && m; i++) {
          for (const l of m.lines) if (l.attribute !== "Unselectable" && l.text !== "Shuffle") names.push(l.text);
          if (m.current_line + 8 > m.max_line) break;
          m = await call("POST", "receiver/menu/page", { source: "Pandora", down: true });
        }
        this.pandora = names;
        try { localStorage.setItem("radio.pandora", JSON.stringify(names)); } catch (e) {}
      } catch (e) {}
    },
    menuSelect(line) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/select", { source: this.menuSource, line }); }); },
    menuBack() { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: this.menuSource, action: "Return" }); }); },
    menuPage(down) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: this.menuSource, down }); }); },

    // ---- feedback on a library station ---------------------------------------
    nowTrack() { const h = this.feedbackHistory; return (h && h[0] && h[0].kind !== "break") ? h[0] : null; },
    async skip() {
      if (!this.feedbackMount) return;
      try { await call("POST", `admin/api/dj/${this.feedbackMount}/skip`); this.say("skipping…"); setTimeout(() => this.refresh(), 2500); } catch (e) { this.say(e.message); }
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
      this.searchTimer = setTimeout(async () => { this.results = this.query.trim().length > 1 ? ((await getJSON(`admin/api/search?q=${encodeURIComponent(this.query)}`)) || []) : []; }, 250);
    },
    async request(r) {
      if (!this.feedbackMount) { this.say("start a library station first"); return; }
      try {
        await call("POST", `admin/api/dj/${this.feedbackMount}/request`, { path: r.path });
        this.say(`next on ${this.feedbackStation.name}: ${r.title}`);
        this.query = ""; this.results = []; this.tab = "now";
        setTimeout(() => this.refresh(), 3000);
      } catch (e) { this.say(e.message); }
    },

    // ---- visualiser (this phone) ----------------------------------------------
    startViz() {
      const audio = this.$refs.audio, canvas = this.$refs.viz;
      if (!canvas) return;
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
        this.ctx = new AC(); const src = this.ctx.createMediaElementSource(audio);
        this.analyser = this.ctx.createAnalyser(); this.analyser.fftSize = 256; this.analyser.smoothingTimeConstant = 0.82;
        src.connect(this.analyser); this.analyser.connect(this.ctx.destination);
      }
      if (this.ctx.state === "suspended") this.ctx.resume();
      cancelAnimationFrame(this.raf);
      const g = canvas.getContext("2d"), data = new Uint8Array(this.analyser.frequencyBinCount);
      const draw = () => {
        this.raf = requestAnimationFrame(draw);
        const c = this.$refs.viz; if (!c) return;
        this.analyser.getByteFrequencyData(data);
        const W = c.width, H = c.height, bars = 40, step = Math.floor(data.length * 0.75 / bars);
        g.clearRect(0, 0, W, H); g.fillStyle = "#d9743f";
        for (let i = 0; i < bars; i++) { let v = 0; for (let j = 0; j < step; j++) v = Math.max(v, data[i * step + j]); const h = (v / 255) * H, w = W / bars; g.globalAlpha = 0.35 + 0.65 * (v / 255); g.fillRect(i * w + 1, H - h, w - 2, h); }
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
    window.addEventListener("resize", () => { this.wide = window.innerWidth > 640; });
    this.refresh();
    setInterval(() => { this.tick = Date.now(); this.refresh(); }, 10000);
    if (this.target === "room") this.pollReceiver();
    if (this.target === "gromit") this.pollSpeaker();
    if (this.tab === "sources" && this.target !== "room") this.tab = "now";
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  },
}).mount("#app");
