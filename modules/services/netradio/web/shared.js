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
    currentStation() { return this.stations.find(s => s.mount === this.current) || { name: "", mount: "" }; },
    feedbackHistory() { return this.histories[this.feedbackMount] || []; },
    feedbackStation() { return this.stations.find(s => s.mount === this.feedbackMount) || { name: "" }; },
    isFixed() { return (m) => (this.stations.find(s => s.mount === m) || {}).kind === "fixed"; },
  },

  methods: {
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
