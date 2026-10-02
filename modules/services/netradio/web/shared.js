// Library radio — what the two pages agree on, to the byte.
//
// app.js (the phone) and desktop.js (the wide player) grew side by side and
// drifted: 82 method names in common, and a `SpeakerTargetParity` test class
// that exists only to catch that drift. Every change this month — reconnect,
// the view handover, mute reaching the rain bed, the OS transport — had to be
// written twice, in two files, and reviewed twice.
//
// This is the half of that duplication with NO judgement in it: members whose
// bodies are byte-identical in both files today. They move here and both pages
// mix this in. Vue's merge gives a component's OWN options priority, so a page
// that genuinely differs simply keeps its version and wins — which is why only
// the identical ones are here, and why moving them cannot change behaviour.
//
// The 45 members that differ stay where they are, deliberately. Some of those
// differences are real features rather than drift (the phone's playTarget
// drives tuner presets and Pandora stations; the desktop's does neither), and
// reconciling them is a decision per member, not a refactor.
//
// Loads after radio.js — whose getJSON/call/mountsOf these use — and before
// the page's own script.

// ~5 minutes of trying at the 8 s ceiling. A deploy takes about a minute;
// beyond this the station is probably gone rather than restarting.
const RECONNECT_GIVE_UP = 40;

window.RadioShared = {
  computed: {
    art() {
      // sized, not the original: covers in this library run to 3 MB, and the
      // hero is 340 CSS px. Asking for the full file made the one image on the
      // page the biggest thing on it.
      const t = this.playingTrack;
      if (t && t.path) return `admin/api/art?path=${encodeURIComponent(t.path)}&size=700`;
      // Pandora and the streaming inputs hand the receiver a cover of their
      // own. Proxied unless it is already an absolute URL, because some of
      // them are served off the receiver's own address.
      const np = this.np;
      if (!np || !np.album_art_url) return "";
      return /^https?:/.test(np.album_art_url) ? np.album_art_url
           : `receiver/art?url=${encodeURIComponent(np.album_art_url)}`;
    },
    canDJ() { return !!this.feedbackMount; },
    // a fixed station has no DJ: nothing to skip to, request or rate
    feedbackMount() { const m = this.playingMount; return m && !this.isFixed(m) ? m : null; },
    feedbackNext() { return this.nexts[this.feedbackMount] || {}; },
    freqText() {
      const t = this.receiver.tuner; if (!t) return "";
      return t.band === "FM" ? (t.fm.val / 100).toFixed(1) : String(t.am.val);
    },
    groups() {
      // a fixed station (Rain, Rainy Mood) lists as a specialty: it is a loop,
      // not a curated genre programme
      const curated = this.stations.filter(s => !["specialty", "fixed"].includes(s.kind));
      const specialty = this.stations.filter(s => ["specialty", "fixed"].includes(s.kind));
      const g = [{ kind: "curated", title: "Curated", stations: curated }];
      if (specialty.length) g.push({ kind: "specialty", title: "Specialty", stations: specialty });
      // the internet streams live in the receiver's own menu, so they are only
      // reachable when the receiver is the target
      if (this.target === "room") {
        if (this.quick.length)
          g.push({ kind: "quick", title: "Internet Radio", stations: this.quick.map(q => ({ mount: "", name: q.name, kind: "quick" })) });
        // The receiver's own: its Pandora stations and its tuner presets. The
        // phone has offered both all along; this page simply never listed them.
        if (this.pandora.length)
          g.push({ kind: "pandora", title: "Pandora", stations: this.pandora.map(n => ({ mount: "", name: n, kind: "pandora" })) });
        if (this.presets.length)
          g.push({ kind: "preset", title: "Radio presets",
                   stations: this.presets.map(x => ({ mount: "", name: x.text.replace(/\s+/g, " ").trim(), kind: "preset", number: x.number })) });
      }
      return g;
    },
    hearted() { const t = this.playingTrack; return !!t && !!this.hearts[t.path]; },
    nowKind() {
      if (this.target === "local") return !this.speaker.mount ? "" : this.isFixed(this.speaker.mount) ? "ambient" : "library";
      if (this.target === "here") return !this.current ? "" : this.isFixed(this.current) ? "ambient" : "library";
      if (!this.receiver.on) return "";
      if (this.receiver.input === "TUNER") return "tuner";
      if (this.receiver.input === "Pandora") return "pandora";
      if (this.roomMount) return "library";
      if (["NET RADIO", "Spotify", "SERVER", "AirPlay"].includes(this.receiver.input)) return "stream";
      return "";
    },
    nowLine1() {
      if (this.target === "room") {
        const np = this.receiver.now_playing || {};
        return [np.artist, np.track].filter(Boolean).join(" — ") || np.station || "";
      }
      const t = this.playingTrack;
      if (t) return t.artist ? `${t.artist} — ${t.title}` : t.title;
      const m = this.playingMount;
      return m ? ((this.up[m] || this.up[m + "-lo"] || {}).title || "") : "";
    },
    nowStation() {
      const m = this.playingMount;
      if (m) return (this.stations.find(s => s.mount === m) || {}).name || "";
      if (this.target === "room" && this.receiver.on) {
        const np = this.receiver.now_playing || {};
        return np.station || this.receiver.input || "";
      }
      return "";
    },
    // What the RECEIVER is doing, and what kind of thing it is. This page only
    // ever knew about library mounts, so a receiver sitting on its tuner or on
    // Pandora showed as nothing at all (2026-09-29).
    np() { return (this.target === "room" && this.receiver.now_playing) || null; },
    roomMount() {
      const np = this.receiver.now_playing;
      if (!this.receiver.on || this.receiver.input !== "NET RADIO" || !np || !np.station) return null;
      const s = this.stations.find(s => s.name === np.station);
      return s ? s.mount : null;
    },
    // WHAT IS PLAYING, asked once. Both pages had their own spelling of
    // this — the phone inlined it in four places — and that divergence was
    // what kept ten otherwise-identical members apart (2026-09-29).
    playingMount() {
      return this.target === "room" ? this.roomMount
           : this.target === "local" ? (this.speaker.mount || null)
           : this.current;
    },
    playingTrack() {
      const h = this.feedbackHistory;
      return (h && h[0] && h[0].kind !== "break") ? h[0] : null;
    },
    tickNow() { return this.tick || Date.now(); },
    stationFacts() {
      const out = [];
      const c = this.counts[this.openMount];
      if (c) {
        out.push({ head: `${c.tracks.toLocaleString()} tracks`, sub: "in this station's own pool" });
        if (c.fringe) out.push({ head: `+ ${c.fringe.toLocaleString()} fringe`, sub: "neighbouring genres, now and then" });
      }
      const seg = this.feedbackNext.segment;
      if (seg && seg.name) out.push({ head: seg.name, sub: "the segment on air now" });
      return out;
    },
    currentStation() { return this.stations.find(s => s.mount === this.current) || { name: "", mount: "" }; },
    feedbackHistory() { return this.histories[this.feedbackMount] || []; },
    feedbackStation() { return this.stations.find(s => s.mount === this.feedbackMount) || { name: "" }; },
    isFixed() { return (m) => (this.stations.find(s => s.mount === m) || {}).kind === "fixed"; },
  },

  methods: {
    // a cover that failed is retried after 30 s, not written off until the song changes
    artOk() { return !!this.art && (this.artFailed !== this.art || this.tickNow - this.artFailedAt > 30000); },
    // ---- the tuner -------------------------------------------------------
    band(b) {
      const t = this.receiver.tuner || {};
      const f = b === "FM" ? ((t.fm && t.fm.val) || 9310) / 100 : ((t.am && t.am.val) || 1300);
      return this.receiverAction("", () => call("POST", "receiver/tuner", { band: b, frequency: f }));
    },
    // offered only when there is something to fix: on, on net radio, stopped
    canResume() {
      if (this.target !== "room") return false;
      const np = this.receiver.now_playing || {};
      return this.receiver.on && this.receiver.input === "NET RADIO" && np.playback !== "Play";
    },
    cursor(action) {
      const src = this.sourceOf(this.receiver.input);
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: src, action }); });
    },
    async dislike(scope) {
      const t = this.playingTrack;
      if (!t || !this.feedbackMount) { this.say("nothing to rate yet"); return; }
      try {
        await call("POST", "admin/api/dislike", { scope, path: t.path, artist: t.artist, title: t.title, mount: this.feedbackMount });
        this.say(scope === "artist" ? `less ${t.artist} from now on` : `never again: ${t.title}`);
        setTimeout(() => this.refresh(), 2500);
      } catch (e) { this.say(e.message); }
    },
    isPlaying(s) {
      if (this.target === "room") {
        const np = this.np;
        if (s.kind === "preset") return this.receiver.input === "TUNER" && !!this.receiver.tuner
                                      && String(this.receiver.tuner.preset) === String(s.number);
        if (s.kind === "pandora") return this.receiver.input === "Pandora" && !!np && np.station === s.name;
        return this.receiver.on && !!np && np.station === s.name;
      }
      if (this.target === "local") return !!s.mount && this.speaker.mount === s.mount;
      return !!s.mount && this.current === s.mount;
    },
    // ---- the OS's own media controls -------------------------------------
    // Desktop browsers put the same metadata on the system media keys and the
    // notification shade. Same closed action list as on the phone: skip maps
    // to `nexttrack`, and there is no heart action to map one to.
    mediaSession() {
      if (!("mediaSession" in navigator)) return;
      const ms = navigator.mediaSession;
      if (this.target !== "here" || !this.current) { ms.metadata = null; ms.playbackState = "none"; return; }
      const playing = this.paused ? "paused" : "playing";
      const t = this.playingTrack;
      const cover = this.artOk() ? new URL(this.art, location.href).href : "";
      const title = (t && t.title) || this.nowLine1 || this.nowStation || "Library radio";
      const key = [title, t && t.artist, cover].join("\u0000");
      if (key === this._msKey) { ms.playbackState = playing; return; }
      this._msKey = key;
      try {
        ms.metadata = new MediaMetadata({
          title,
          artist: (t && t.artist) || "",
          album: this.nowStation || this.site.title,
          artwork: cover ? [96, 192, 384, 512].map(px => ({ src: cover, sizes: `${px}x${px}`, type: "image/jpeg" })) : [],
        });
        ms.playbackState = playing;
      } catch (e) { /* older browsers manage without */ }
    },
    // asked once per track, then cached
    async refreshHeart() {
      const t = this.playingTrack;
      if (!t || t.path in this.hearts) return;
      const r = await getJSON(`admin/api/heart?path=${encodeURIComponent(t.path)}`);
      if (r) this.hearts = { ...this.hearts, [t.path]: !!r.heart };
    },
    remotePage(down) {
      const src = this.sourceOf(this.receiver.input);
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: src, down }); });
    },
    async request(r) {
      if (!this.feedbackMount) { this.say("start a library station first"); return; }
      try {
        await call("POST", `admin/api/dj/${this.feedbackMount}/request`, { path: r.path });
        this.say(`next on ${this.feedbackStation.name}: ${r.title}`);
        this.query = ""; this.results = [];
        setTimeout(() => this.refresh(), 3000);
      } catch (e) { this.say(e.message); }
    },
    // The receiver stops dead when an encoder restarts and does not come back
    // by itself; netradio-resume does this after Liquidsoap starts, and this is
    // the same thing on a finger for when it stopped some other way.
    resumeRoom() {
      return this.receiverAction("resuming…", async () => {
        const r = await call("POST", "admin/api/resume", {});
        this.say((r && r.message) || "asked the receiver to resume");
        return null;
      });
    },
    say(msg) { this.toast = msg; clearTimeout(this._toastT); this._toastT = setTimeout(() => { this.toast = ""; }, 4000); },
    shownVolume() { return this.target === "local" ? (this.speaker.volume ?? 0) : Math.round(this.receiver.volume || 0); },
    // ---- feedback --------------------------------------------------------
    // Skip and "less of this" are ONE press: the station moves on and the track
    // loses ground. Four skips and it stops coming back, without anything
    // having to be declared "never" (Chris, 2026-09-27).
    async skip() {
      if (!this.feedbackMount) return;
      const t = this.playingTrack;
      try {
        if (!t) { await call("POST", `admin/api/dj/${this.feedbackMount}/skip`); this.say("skipping…"); }
        else {
          const r = await call("POST", "admin/api/feedback",
                               { kind: "skip", path: t.path, artist: t.artist, title: t.title, mount: this.feedbackMount });
          this.say(r && r.out_of_rotation ? `skipped — that's enough of ${t.title}` : `skipping — less of ${t.title}`);
        }
        setTimeout(() => this.refresh(), 2500);
      } catch (e) { this.say(e.message); }
    },
    sourceOf(input) { return ({ "NET RADIO": "NET_RADIO", Pandora: "Pandora", SERVER: "SERVER", Spotify: "Spotify", AirPlay: "AirPlay", TUNER: "Tuner" })[input] || ""; },
    async speakerAction(label, fn) {
      this.busy = label;
      try { const st = await fn(); if (st) this.speaker = { ...st, error: st.error || "" }; }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },
    stopTarget() {
      if (this.target === "room") return this.receiverAction("stopping…", () => call("POST", "receiver/playback", { action: "Stop" }));
      if (this.target === "local") return this.speakerAction("stopping…", () => call("POST", "speaker/stop", {}));
      return this.stop();
    },
    stopViz() { cancelAnimationFrame(this.raf); },
    takeHandover() {
      let h = null;
      try {
        h = JSON.parse(localStorage.getItem("radio.handover") || "null");
        localStorage.removeItem("radio.handover");     // one-shot; pagehide re-arms it
      } catch (e) {}
      if (!h || !h.mount || !(Date.now() - (h.at || 0) < HANDOVER_MS)) return;
      if (this.target !== "here") return;              // the speakers never stopped
      this.play(h.mount);
    },
    // A toggle, not a tally: stored locally always, and mirrored to Jellyfin
    // when one is configured.
    async toggleHeart() {
      const t = this.playingTrack;
      if (!t) { this.say("nothing to rate yet"); return; }
      const want = !this.hearted;
      try {
        const r = await call("POST", "admin/api/feedback",
                             { kind: "heart", on: want, path: t.path, artist: t.artist, title: t.title });
        this.hearts = { ...this.hearts, [t.path]: !!(r && r.heart) };
        this.say((r && r.message) || (r && r.heart ? "hearted" : "heart removed"));
      } catch (e) { this.say(e.message); }
    },
    tuneStep(dir) {
      const t = this.receiver.tuner; if (!t) return;
      const fm = t.band === "FM";
      const f = fm ? Math.round((t.fm.val / 100 + dir * 0.2) * 10) / 10 : t.am.val + dir * 10;
      return this.receiverAction("", () => call("POST", "receiver/tuner", { band: t.band, frequency: f }));
    },
    async loadPandora() {
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
    artError() { this.artFailed = this.art; this.artFailedAt = Date.now(); },
    countOf(m) { const c = this.counts[m]; return c ? `${c.tracks.toLocaleString()} tracks${c.fringe ? " +" + c.fringe.toLocaleString() + " fringe" : ""}` : ""; },
    feedback(up) { return this.receiverAction("", async () => { await call("POST", "receiver/feedback", { thumbs_up: up, source: "Pandora" }); this.say(up ? "thumbs up" : "thumbs down"); }); },
    forgetHandover() { try { localStorage.removeItem("radio.handover"); } catch (e) {} },
    initials(name) { return (name || "").split(/[\s&]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join(""); },
    isUp(m) { return !!m && !!(this.up[m] || this.up[m + "-lo"]); },
    listenersOf(m) { return ((this.up[m] || {}).listeners | 0) + ((this.up[m + "-lo"] || {}).listeners | 0); },
    async loadMenu() {
      this.menuSource = ["SERVER", "Pandora", "NET RADIO"].includes(this.receiver.input) ? this.sourceOf(this.receiver.input) : "";
      if (!this.menuSource) return;
      const m = await getJSON(`receiver/menu?source=${this.menuSource}`);
      if (m) this.menu = m;
    },
    menuBack() { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: this.menuSource, action: "Return" }); }); },
    menuPage(down) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: this.menuSource, down }); }); },
    menuSelect(line) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/select", { source: this.menuSource, line }); }); },
    mute(on) { return this.receiverAction("", () => call("POST", "receiver/mute", { on })); },
    // ---- the view switch -------------------------------------------------
    noteHandover() {
      try {
        if (this.current && this.target === "here")
          localStorage.setItem("radio.handover", JSON.stringify({ mount: this.current, at: Date.now() }));
        else localStorage.removeItem("radio.handover");
      } catch (e) {}
    },
    // ---- the remote, and the receiver's own menus ------------------------
    openRemote() {
      this.remote = true;
      if (this.target !== "room") this.target = "room";
      this.pollReceiver();
      if (!this.inputs.length) getJSON("receiver/inputs").then(i => { if (i) this.inputs = i; });
      if (!this.presets.length) getJSON("receiver/tuner/presets").then(p => { if (p) this.presets = p; });
    },
    playback(action) { return this.receiverAction("", () => call("POST", "receiver/playback", { action })); },
    power(on) { return this.receiverAction(on ? "powering on…" : "standby…", () => call("POST", "receiver/power", { on })); },
    presetStep(up) { return this.receiverAction("", () => call("POST", "receiver/tuner", { preset: up ? "Up" : "Down" })); },
    async receiverAction(label, fn) {
      this.busy = label;
      try { const r = await fn(); if (r && "on" in r) this.receiver = { ...this.receiver, ...r }; await this.pollReceiver(); await this.refresh(); }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },
    retune() { try { localStorage.setItem("radio.quality", this.quality); } catch (e) {} if (this.current) this.play(this.current); },
    scheduleReconnect() {
      if (!this.current) return;
      clearTimeout(this._reTimer);
      this._reTries = (this._reTries || 0) + 1;
      if (this._reTries > RECONNECT_GIVE_UP) { this.status = "stream lost — press play"; return; }
      const wait = Math.min(1000 * 2 ** (this._reTries - 1), 8000);
      this.status = "reconnecting…";
      this._reTimer = setTimeout(() => this.reconnect(), wait);
    },
    searchDebounced() {
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(async () => {
        this.results = this.query.trim().length > 1 ? ((await getJSON(`admin/api/search?q=${encodeURIComponent(this.query)}`)) || []) : [];
      }, 250);
    },
    seek(up) { return this.receiverAction(up ? "seeking up…" : "seeking down…", () => call("POST", "receiver/tuner/seek", { up })); },
    selectInput(name) { return this.receiverAction(`switching to ${name}…`, () => call("POST", "receiver/input", { name })); },
    setMediaHandlers() {
      if (!("mediaSession" in navigator)) return;
      const set = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch (e) {} };
      set("play", () => this.resumeHere());
      set("pause", () => this.pauseHere());   // pause, NOT stop: stop ends the session
      set("stop", () => this.stop());
      set("nexttrack", () => this.skip());
      set("previoustrack", null);
    },
    setVolume(level) { return this.receiverAction("", () => call("POST", "receiver/volume", { level })); },
    speakerMute(on) { return this.speakerAction("", () => call("POST", "speaker/mute", { on })); },
    speakerPlay(mount) { return this.speakerAction("starting…", () => call("POST", "speaker/play", { mount })); },
    // ---- the spectrum in the bar (this browser only) ---------------------
    startViz() {
      const audio = this.$refs.audio, canvas = this.$refs.viz;
      if (!canvas) return;
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
        this.ctx = new AC();
        const src = this.ctx.createMediaElementSource(audio);
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 256; this.analyser.smoothingTimeConstant = 0.82;
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
        for (let i = 0; i < bars; i++) {
          let v = 0; for (let j = 0; j < step; j++) v = Math.max(v, data[i * step + j]);
          const h = (v / 255) * H, w = W / bars;
          g.globalAlpha = 0.35 + 0.65 * (v / 255);
          g.fillRect(i * w + 1, H - h, w - 2, h);
        }
        g.globalAlpha = 1;
      };
      draw();
    },
    streamUrl(m) { return `radio/${m}${this.quality}.mp3?t=${Date.now()}`; },
    // ---- small helpers the template uses ---------------------------------
    thumb(path, size = 200) { return `admin/api/art?path=${encodeURIComponent(path)}&size=${size}`; },
    // NOT filtered through goodArt: see imgError. The hero and the bar, which
    // show ONE image, do filter — there a failure has to fall through to the
    // station's monogram rather than leave a blank square.
    tileCovers(s) { return s.mount && this.tiles[s.mount] ? (this.tiles[s.mount].covers || []) : []; },
    tunerPreset(n) { return this.receiverAction("tuning…", () => call("POST", "receiver/tuner", { preset: n })); },
  },
};
