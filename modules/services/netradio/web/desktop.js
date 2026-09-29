// Library radio — the wide-screen page. Vue 3 global build, no bundler.
//
// Same-origin data, all of it written by the scanner or the DJ:
//   now/catalogue.json      the station list        now/stations.json   track counts
//   now/internet-radio.json the receiver's streams  now/tiles.json      cover art per station
//   now/<mount>.json        last played             now/<mount>-next.json  the DJ's queue
//   icecast-status          which mounts are up, their titles and listener counts
//   receiver/*              the receiver's JSON API (yamaha-ync-api via nginx)
//   speaker/*               this box's own sound card (netradio-speaker)
//   admin/api/*             feedback, hearts, search, resume
//
// Three targets, unchanged: "here" plays the stream in this browser, "room"
// drives the receiver by walking its NET RADIO menu, "local" drives this
// machine's sound card.
//
// Two surfaces: the WALL (every station and what it last played) and the
// STATION screen (one station's artwork, queue and actions), plus SOURCES for
// the receiver's own inputs and menus. The transport bar is common to all
// three and is a grid row, so nothing ever scrolls underneath it.
//
// This page was a Pico-CSS list of station names until 2026-09-28, and had
// never gained the heart, the artwork, the remote or resume that the phone
// page grew — a wide screen is redirected here automatically, so it lost them.

const { createApp } = Vue;
// ~5 minutes of trying at the 8 s ceiling. A deploy takes about a minute;
// beyond this the station is probably gone rather than restarting.
const RECONNECT_GIVE_UP = 40;

createApp({
  data() {
    let quality = "", target = "here", view = "wall", openMount = "", hereVol = 100, bedVol = 35, bedOn = false;
    try {
      hereVol = Math.max(0, Math.min(100, parseInt(localStorage.getItem("radio.volume"), 10) || 100));
      bedVol = Math.max(0, Math.min(100, parseInt(localStorage.getItem("radio.bedVolume"), 10) || 35));
      bedOn = localStorage.getItem("radio.bed") === "1";
      quality = localStorage.getItem("radio.quality") || "";
      target = localStorage.getItem("radio.target") || "here";
      view = localStorage.getItem("radio.desktopView") || "wall";
      openMount = localStorage.getItem("radio.openMount") || "";
    } catch (e) {}
    if (view === "sources") view = "wall";          // needs a live receiver; never land on it
    return {
      view, qtab: "played", openMount, target, quality,
      stations: [], quick: [], counts: {}, up: {}, tiles: {}, histories: {}, nexts: {}, hearts: {},
      current: null, status: "", scanning: false, busy: "", toast: "", tick: 0,
      query: "", results: [], searchTimer: 0,
      site: { title: "Radio", localName: "These speakers", roomName: "Living room", localMount: "", hasLocal: false, hasRoom: true, bedMount: "" },
      receiver: { name: "", on: false, input: "", volume: 0, volume_max: 100, mute: false, error: "" },
      inputs: [], presets: [], menu: { lines: [], layer: 0, max_line: 0, current_line: 1, status: "", name: "" }, menuSource: "",
      speaker: { playing: false, mount: "", volume: null, muted: false, error: "" },
      remote: false, volDraft: 0, volDragging: false,
      // This browser's own output. The receiver and the sound card have a
      // volume; the tab had none, so the only way down was the OS mixer
      // (Chris, 2026-09-29). Kept in localStorage so a reload is not a
      // surprise at full volume.
      hereVol, hereMuted: false,
      // The rain bed: a SECOND stream, mixed here rather than at the station.
      // Mixing it into the broadcast would put rain under everyone who tuned
      // in — including a receiver that cannot turn it off — and one level
      // cannot suit both a soft track and a loud one (Chris, 2026-09-29).
      bedOn, bedVol,
      roomPoll: 0, speakerPoll: 0, ctx: null, analyser: null, raf: 0,
      // Whether each output has actually answered yet. The rail shows all
      // of them at once, and until 2026-09-29 it rendered the ones that
      // were not selected straight from these defaults — so a speaker that
      // was playing read "stopped" until you clicked it. A status nobody
      // asked for is a guess, and it should not look like a fact.
      speakerSeen: false, receiverSeen: false,
      // covers that 404'd, by path. Plenty of tracks have no embedded picture
      // and no folder cover, and an <img> that fails renders the browser's
      // broken-image glyph — worse than the coloured monogram it should fall
      // back to. Retried after 30 s so a slow art service is not written off.
      badArt: {},
      // matches index.html's own wide-screen test, so the two pages agree
      // about what counts as a phone
      narrow: !window.matchMedia("(min-width: 900px)").matches,
    };
  },

  computed: {
    // ---- the rail's zone list -------------------------------------------
    zones() {
      const z = [{ id: "here", name: "This browser", live: !!this.current,
                   sub: this.current ? `playing ${this.currentStation.name}` : (this.status || "idle"), dead: false }];
      if (this.site.hasRoom) z.push({
        id: "room", name: this.receiver.name || this.site.roomName,
        live: this.receiverSeen && this.receiver.on && this.playbackState === "Play",
        sub: !this.receiverSeen ? "checking…"
           : this.receiver.error ? this.receiver.error
           : this.receiver.on ? this.receiver.input : "standby",
        dead: this.receiver.error === "unreachable" });
      if (this.site.hasLocal) z.push({
        id: "local", name: this.site.localName, live: this.speakerSeen && !!this.speaker.playing,
        sub: !this.speakerSeen ? "checking…"
           : this.speaker.error || (this.speaker.playing ? "playing" : "stopped"),
        dead: this.speaker.error === "unreachable" });
      return z;
    },
    targetName() { return (this.zones.find(z => z.id === this.target) || {}).name || ""; },

    groups() {
      // a fixed station (Rain, Rainy Mood) lists as a specialty: it is a loop,
      // not a curated genre programme
      const curated = this.stations.filter(s => !["specialty", "fixed"].includes(s.kind));
      const specialty = this.stations.filter(s => ["specialty", "fixed"].includes(s.kind));
      const g = [{ kind: "curated", title: "Curated", stations: curated }];
      if (specialty.length) g.push({ kind: "specialty", title: "Specialty", stations: specialty });
      // the internet streams live in the receiver's own menu, so they are only
      // reachable when the receiver is the target
      if (this.target === "room" && this.quick.length)
        g.push({ kind: "quick", title: "Internet Radio", stations: this.quick.map(q => ({ mount: "", name: q.name, kind: "quick" })) });
      return g;
    },

    // ---- what is PLAYING on the chosen target ----------------------------
    playbackState() { return ((this.receiver.now_playing || {}).playback) || ""; },
    currentStation() { return this.stations.find(s => s.mount === this.current) || { name: "", mount: "" }; },
    isFixed() { return (m) => (this.stations.find(s => s.mount === m) || {}).kind === "fixed"; },
    roomMount() {
      const np = this.receiver.now_playing;
      if (!this.receiver.on || this.receiver.input !== "NET RADIO" || !np || !np.station) return null;
      const s = this.stations.find(s => s.name === np.station);
      return s ? s.mount : null;
    },
    playingMount() {
      return this.target === "room" ? this.roomMount
           : this.target === "local" ? (this.speaker.mount || null)
           : this.current;
    },
    // a fixed station has no DJ: nothing to skip to, request or rate
    feedbackMount() { const m = this.playingMount; return m && !this.isFixed(m) ? m : null; },
    feedbackStation() { return this.stations.find(s => s.mount === this.feedbackMount) || { name: "" }; },
    feedbackHistory() { return this.histories[this.feedbackMount] || []; },
    feedbackNext() { return this.nexts[this.feedbackMount] || {}; },
    canDJ() { return !!this.feedbackMount; },
    // the actions on the station screen act on the PLAYING track, so they are
    // only offered when the station being LOOKED at is the one being heard
    canActHere() { return this.canDJ && this.openMount === this.feedbackMount; },
    anythingPlaying() {
      return this.target === "room" ? (this.receiver.on && this.playbackState === "Play")
           : this.target === "local" ? !!this.speaker.playing
           : !!this.current;
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
    barSub() {
      const bits = [];
      if (this.nowStation) bits.push(this.nowStation);
      if (this.anythingPlaying && this.playingMount && this.isUp(this.playingMount)) bits.push("on air");
      const n = this.playingMount ? this.listenersOf(this.playingMount) : 0;
      if (n) bits.push(`${n} listening`);
      if (!bits.length) bits.push(this.targetName);
      return bits.join(" · ");
    },
    playingTrack() {
      const h = this.feedbackHistory;
      return (h && h[0] && h[0].kind !== "break") ? h[0] : null;
    },
    hearted() { const t = this.playingTrack; return !!t && !!this.hearts[t.path]; },
    art() {
      // sized, not the original: covers in this library run to 3 MB, and the
      // hero is 340 CSS px. Asking for the full file made the one image on the
      // page the biggest thing on it.
      const t = this.playingTrack;
      return t && t.path ? `admin/api/art?path=${encodeURIComponent(t.path)}&size=700` : "";
    },
    barCovers() {
      const t = this.playingTrack;
      const own = this.goodArt(t && t.path ? [t.path] : []);
      if (own.length) return own;
      const m = this.playingMount;
      return this.goodArt(m && this.tiles[m] ? (this.tiles[m].covers || []) : []);
    },

    // ---- what is being LOOKED at (the station screen) ---------------------
    openStationObj() { return this.stations.find(s => s.mount === this.openMount) || { name: "", mount: "" }; },
    openStationName() { return this.openStationObj.name; },
    viewTrack() {
      const h = this.histories[this.openMount] || [];
      return (h[0] && h[0].kind !== "break") ? h[0] : null;
    },
    viewTitle() {
      const t = this.viewTrack;
      if (t) return t.title;
      return (this.up[this.openMount] || this.up[this.openMount + "-lo"] || {}).title || "";
    },
    viewArtist() { const t = this.viewTrack; return t ? (t.artist || "") : ""; },
    // the track's own cover if it has one, else the station's mosaic
    heroCovers() {
      const t = this.viewTrack;
      const own = this.goodArt(t && t.path ? [t.path] : []);
      if (own.length) return own;
      return this.goodArt(this.openMount && this.tiles[this.openMount] ? (this.tiles[this.openMount].covers || []) : []);
    },
    // The right column follows the station being LOOKED at, not the one being
    // heard: opening a station you are not listening to and being shown some
    // other station's queue is simply wrong.
    viewHistory() { return this.histories[this.openMount] || []; },
    viewNext() { return this.nexts[this.openMount] || {}; },
    // Keyed to the STATION, not the track: on a radio the station is the
    // long-lived identity, so its colour holds steady while songs change
    // inside it (Plexamp's UltraBlur, applied to the thing that persists).
    washStyle() {
      const c = this.openMount && this.tiles[this.openMount] ? (this.tiles[this.openMount].covers || []) : [];
      return c.length ? { backgroundImage: `url("${this.thumb(c[0], 500)}")` } : {};
    },
    techLine() {
      const bits = [];
      if (this.target === "here") bits.push(this.quality === "-lo" ? "MP3 96 kbps" : "MP3 192 kbps");
      const t = this.openMount === this.feedbackMount ? this.playingTrack : this.viewTrack;
      if (t && t.at) {
        const secs = Math.max(0, Math.floor(this.tickNow / 1000 - t.at));
        if (secs < 3600) bits.push(`${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")} into this track`);
      }
      return bits.join(" · ");
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

    // ---- the transport bar's volume, whichever target is chosen ----------
    volumeMax() { return this.target === "room" ? (this.receiver.volume_max || 100) : 100; },
    isMuted() {
      return this.target === "room" ? !!this.receiver.mute
           : this.target === "local" ? !!this.speaker.muted
           : this.hereMuted;
    },
    freqText() {
      const t = this.receiver.tuner; if (!t) return "";
      return t.band === "FM" ? (t.fm.val / 100).toFixed(1) : String(t.am.val);
    },
  },

  watch: {
    target(t) {
      try { localStorage.setItem("radio.target", t); } catch (e) {}
      clearInterval(this.roomPoll); clearInterval(this.speakerPoll);
      if (t !== "room" && this.view === "sources") this.view = "wall";
      if (t === "room") this.pollReceiver();
      if (t === "local") this.pollSpeaker();
      this.syncVolume();
    },
    view(v) { try { localStorage.setItem("radio.desktopView", v); } catch (e) {} },
    openMount(m) { try { localStorage.setItem("radio.openMount", m); } catch (e) {} if (m) this.refresh(); },
    "receiver.volume"() { this.syncVolume(); },
    "receiver.input"() { if (this.view === "sources") this.loadMenu(); },
    "speaker.volume"() { this.syncVolume(); },
  },

  methods: {
    // ---- small helpers the template uses ---------------------------------
    thumb(path, size = 200) { return `admin/api/art?path=${encodeURIComponent(path)}&size=${size}`; },
    // Hide just the tile that failed and remember the path. Hiding keeps a
    // four-up mosaic four-up: dropping the cover from the list instead would
    // re-lay the whole card out as a single image the moment one 404'd.
    imgError(path, ev) {
      if (ev && ev.target) ev.target.style.visibility = "hidden";
      this.badArt = { ...this.badArt, [path]: Date.now() };
    },
    goodArt(paths) {
      const now = this.tickNow;
      return (paths || []).filter(p => !this.badArt[p] || now - this.badArt[p] > 30000);
    },
    initials(name) { return (name || "").split(/[\s&]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join(""); },
    // NOT filtered through goodArt: see imgError. The hero and the bar, which
    // show ONE image, do filter — there a failure has to fall through to the
    // station's monogram rather than leave a blank square.
    tileCovers(s) { return s.mount && this.tiles[s.mount] ? (this.tiles[s.mount].covers || []) : []; },
    // In a two-line list row the artist has its own line, so the headline is
    // the title alone, with the artist beneath it. A break's own
    // title is the useful part ("Krüger Brothers spotlight"), not the word
    // "break", so it keeps it.
    rowTitle(it) { return it.kind === "break" ? (it.title || "station break") : it.title; },
    rowSub(it) { return it.kind === "break" ? "station break" : (it.artist || ""); },
    isUp(m) { return !!m && !!(this.up[m] || this.up[m + "-lo"]); },
    listenersOf(m) { return ((this.up[m] || {}).listeners | 0) + ((this.up[m + "-lo"] || {}).listeners | 0); },
    countOf(m) { const c = this.counts[m]; return c ? `${c.tracks.toLocaleString()} tracks${c.fringe ? " +" + c.fringe.toLocaleString() + " fringe" : ""}` : ""; },
    shortCount(m) { const c = this.counts[m]; return c ? (c.tracks >= 1000 ? (c.tracks / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(c.tracks)) : ""; },
    // what this station last played, for its card on the wall
    lastOf(m) { const h = this.histories[m]; return (h && h[0] && h[0].kind !== "break") ? h[0] : null; },
    streamUrl(m) { return `radio/${m}${this.quality}.mp3?t=${Date.now()}`; },
    artError() { this.artFailed = this.art; this.artFailedAt = Date.now(); },
    // a cover that failed is retried after 30 s, not written off until the song changes
    artOk() { return !!this.art && (this.artFailed !== this.art || this.tickNow - this.artFailedAt > 30000); },
    say(msg) { this.toast = msg; clearTimeout(this._toastT); this._toastT = setTimeout(() => { this.toast = ""; }, 4000); },
    isPlaying(s) {
      if (this.target === "room") return this.receiver.on && !!this.receiver.now_playing && this.receiver.now_playing.station === s.name;
      if (this.target === "local") return !!s.mount && this.speaker.mount === s.mount;
      return !!s.mount && this.current === s.mount;
    },
    focusSearch() { const el = document.querySelector(".search input"); if (el) el.focus(); },

    openStation(s) {
      // an entry with no mount of our own (an internet stream in the
      // receiver's menu) has no station screen to show: just tune it
      if (!s.mount) return this.playTarget(s);
      this.openMount = s.mount;
      this.view = "station";
    },

    // ---- loading ---------------------------------------------------------
    async refresh() {
      const [cat, quick, counts, ic] = await Promise.all([
        getJSON("now/catalogue.json"), getJSON("now/internet-radio.json"),
        getJSON("now/stations.json"), getJSON("icecast-status")]);
      if (cat) { this.stations = cat; this.scanning = false; } else if (!this.stations.length) this.scanning = true;
      if (quick) this.quick = quick;
      if (counts) this.counts = counts;
      if (ic) this.up = mountsOf(ic);
      // the cover manifest changes only when the library is rescanned
      if (!this._tilesAt || Date.now() - this._tilesAt > 600000) {
        const t = await getJSON("now/tiles.json");
        if (t) { this.tiles = t; this._tilesAt = Date.now(); }
      }
      // the station being heard and the one being looked at, every time
      const want = new Set([this.feedbackMount, this.openMount].filter(Boolean));
      for (const m of want) {
        const [h, n] = await Promise.all([getJSON(`now/${m}.json`), getJSON(`now/${m}-next.json`)]);
        if (h) this.histories[m] = h;
        if (n) this.nexts[m] = n;
      }
      // …and every station's last track, for the wall. One small file each and
      // they are static on the same nginx, but 28 of them is not something to
      // do on the 10 s tick, so it runs at most once a minute.
      if (this.view === "wall" && (!this._wallAt || Date.now() - this._wallAt > 60000)) {
        this._wallAt = Date.now();
        const mounts = this.stations.map(s => s.mount).filter(m => m && !want.has(m));
        const got = await Promise.all(mounts.map(m => getJSON(`now/${m}.json`)));
        const merged = { ...this.histories };
        mounts.forEach((m, i) => { if (got[i]) merged[m] = got[i]; });
        this.histories = merged;
      }
    },

    // ---- playing ---------------------------------------------------------
    play(m) {
      const audio = this.$refs.audio;
      this.current = m; this.status = "tuning…"; this._reTries = 0; clearTimeout(this._reTimer);
      audio.src = this.streamUrl(m);
      this.applyHereVolume();
      this.$nextTick(() => this.applyBed());
      audio.play().then(() => { this.status = ""; this.$nextTick(() => this.startViz()); })
                  .catch(err => { this.status = `couldn't start (${err.message})`; });
      this.refresh();
    },
    stop() { const a = this.$refs.audio; a.pause(); a.removeAttribute("src"); a.load(); this.current = null; clearTimeout(this._reTimer); this._reTries = 0; this.stopViz(); this.applyBed(); this.mediaSession(); },
    retune() { try { localStorage.setItem("radio.quality", this.quality); } catch (e) {} if (this.current) this.play(this.current); },
    playTarget(s) {
      if (this.target === "room") return this.playOnReceiver(s);
      if (this.target === "local") return s.mount ? this.speakerPlay(s.mount) : undefined;
      return s.mount ? this.play(s.mount) : undefined;
    },
    stopTarget() {
      if (this.target === "room") return this.receiverAction("stopping…", () => call("POST", "receiver/playback", { action: "Stop" }));
      if (this.target === "local") return this.speakerAction("stopping…", () => call("POST", "speaker/stop", {}));
      return this.stop();
    },
    // the bar's play button with nothing playing: start the station being
    // looked at, else this box's default, else the first in the list
    resumePlay() {
      const s = this.openStationObj.mount ? this.openStationObj
              : this.stations.find(x => x.mount === this.site.localMount) || this.stations[0];
      if (s) return this.playTarget(s);
    },
    playOnReceiver(s) {
      const category = s.kind === "quick" ? "Internet Radio" : s.kind === "specialty" ? "Specialty" : "Curated";
      return this.receiverAction(`tuning the ${this.receiver.name || "receiver"} to ${s.name}…`,
        () => call("POST", "receiver/menu/path", { path: ["My Stations", category, s.name] }));
    },

    // ---- volume, for whichever target is chosen --------------------------
    syncVolume() {
      if (this.volDragging) return;          // never drag the slider out from under a finger
      this.volDraft = this.target === "room" ? Math.round(this.receiver.volume || 0)
                    : this.target === "local" ? (this.speaker.volume ?? 0)
                    : this.hereVol;
    },
    setLevel(level) {
      if (this.target === "room") return this.setVolume(level);
      if (this.target === "local") return this.speakerSet(level);
      return this.hereSet(level);
    },
    toggleMute() {
      if (this.target === "room") return this.mute(!this.receiver.mute);
      if (this.target === "local") return this.speakerMute(!this.speaker.muted);
      this.hereMuted = !this.hereMuted;
      this.applyHereVolume();
    },
    // ---- this browser's own volume ---------------------------------------
    hereSet(level) {
      this.hereVol = Math.max(0, Math.min(100, Math.round(level)));
      this.volDraft = this.hereVol;
      if (this.hereVol > 0) this.hereMuted = false;   // moving the slider up means unmute
      try { localStorage.setItem("radio.volume", String(this.hereVol)); } catch (e) {}
      this.applyHereVolume();
    },
    applyHereVolume() {
      const a = this.$refs.audio;
      if (!a) return;
      // HTMLMediaElement.volume is 0..1 and is NOT affected by the OS mixer,
      // so this is the tab's own level rather than the machine's.
      a.volume = this.hereMuted ? 0 : this.hereVol / 100;
      a.muted = this.hereMuted;
    },
    shownVolume() { return this.target === "local" ? (this.speaker.volume ?? 0) : Math.round(this.receiver.volume || 0); },

    // ---- this box's own sound card ---------------------------------------
    async pollSpeaker() {
      const st = await getJSON("speaker/state");
      this.speaker = st ? { ...st, error: st.error || "" } : { ...this.speaker, error: "unreachable" };
      this.speakerSeen = true;
      this.syncVolume();
      clearInterval(this.speakerPoll);
      this.speakerPoll = setInterval(async () => {
        if (this.target !== "local" || this.busy) return;
        const s2 = await getJSON("speaker/state");
        if (s2) { this.speaker = { ...s2, error: s2.error || "" }; this.syncVolume(); }
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
      this.volDraft = Math.max(0, Math.min(100, Math.round(level)));
      return this.speakerAction("", () => call("POST", "speaker/volume", { level: this.volDraft }));
    },
    speakerVolume(step) { return this.speakerSet((this.speaker.volume ?? this.volDraft) + step); },
    speakerMute(on) { return this.speakerAction("", () => call("POST", "speaker/mute", { on })); },

    // ---- the outputs nobody has selected ---------------------------------
    // The rail lists every output at once, so every one of them needs a status,
    // not just the one being driven. The selected target keeps its own 5 s poll;
    // these ride the 10 s tick, which is plenty for "is it on".
    // Read-only: status endpoints only, nothing here changes what anything is
    // doing.
    async refreshZones() {
      if (this.busy) return;
      const jobs = [];
      if (this.site.hasLocal && this.target !== "local") jobs.push((async () => {
        const st = await getJSON("speaker/state");
        this.speaker = st ? { ...st, error: st.error || "" } : { ...this.speaker, error: "unreachable" };
        this.speakerSeen = true;
      })());
      if (this.site.hasRoom && this.target !== "room") jobs.push((async () => {
        const st = await getJSON("receiver/status");
        this.receiver = st ? { ...st, error: "" } : { ...this.receiver, error: "unreachable" };
        this.receiverSeen = true;
      })());
      await Promise.all(jobs);
    },

    // ---- the receiver ----------------------------------------------------
    async pollReceiver() {
      const st = await getJSON("receiver/status");
      this.receiverSeen = true;
      if (!st) { this.receiver = { ...this.receiver, error: "unreachable" }; return; }
      this.receiver = { ...st, error: "" };
      this.syncVolume();
      if (!this.inputs.length) this.inputs = (await getJSON("receiver/inputs")) || [];
      if (st.on && !this.presets.length) this.presets = (await getJSON("receiver/tuner/presets")) || [];
      clearInterval(this.roomPoll);
      this.roomPoll = setInterval(() => { if (this.target === "room" && !this.busy) this.pollReceiver(); }, 5000);
    },
    async receiverAction(label, fn) {
      this.busy = label;
      try { const r = await fn(); if (r && "on" in r) this.receiver = { ...this.receiver, ...r }; await this.pollReceiver(); await this.refresh(); }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },
    power(on) { return this.receiverAction(on ? "powering on…" : "standby…", () => call("POST", "receiver/power", { on })); },
    mute(on) { return this.receiverAction("", () => call("POST", "receiver/mute", { on })); },
    setVolume(level) { return this.receiverAction("", () => call("POST", "receiver/volume", { level })); },
    volumeStep(step) {
      if (this.target === "local") return this.speakerVolume(step);
      this.volDraft = Math.max(0, Math.min(this.volumeMax, this.volDraft + step));
      return this.setVolume(this.volDraft);
    },
    selectInput(name) { return this.receiverAction(`switching to ${name}…`, () => call("POST", "receiver/input", { name })); },
    playback(action) { return this.receiverAction("", () => call("POST", "receiver/playback", { action })); },
    feedback(up) { return this.receiverAction("", async () => { await call("POST", "receiver/feedback", { thumbs_up: up, source: "Pandora" }); this.say(up ? "thumbs up" : "thumbs down"); }); },
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
    // offered only when there is something to fix: on, on net radio, stopped
    canResume() {
      if (this.target !== "room") return false;
      const np = this.receiver.now_playing || {};
      return this.receiver.on && this.receiver.input === "NET RADIO" && np.playback !== "Play";
    },

    // ---- the tuner -------------------------------------------------------
    band(b) {
      const t = this.receiver.tuner || {};
      const f = b === "FM" ? ((t.fm && t.fm.val) || 9310) / 100 : ((t.am && t.am.val) || 1300);
      return this.receiverAction("", () => call("POST", "receiver/tuner", { band: b, frequency: f }));
    },
    tuneStep(dir) {
      const t = this.receiver.tuner; if (!t) return;
      const fm = t.band === "FM";
      const f = fm ? Math.round((t.fm.val / 100 + dir * 0.2) * 10) / 10 : t.am.val + dir * 10;
      return this.receiverAction("", () => call("POST", "receiver/tuner", { band: t.band, frequency: f }));
    },
    seek(up) { return this.receiverAction(up ? "seeking up…" : "seeking down…", () => call("POST", "receiver/tuner/seek", { up })); },
    tunerPreset(n) { return this.receiverAction("tuning…", () => call("POST", "receiver/tuner", { preset: n })); },
    presetStep(up) { return this.receiverAction("", () => call("POST", "receiver/tuner", { preset: up ? "Up" : "Down" })); },

    // ---- the remote, and the receiver's own menus ------------------------
    openRemote() {
      this.remote = true;
      if (this.target !== "room") this.target = "room";
      this.pollReceiver();
      if (!this.inputs.length) getJSON("receiver/inputs").then(i => { if (i) this.inputs = i; });
      if (!this.presets.length) getJSON("receiver/tuner/presets").then(p => { if (p) this.presets = p; });
    },
    sourceOf(input) { return ({ "NET RADIO": "NET_RADIO", Pandora: "Pandora", SERVER: "SERVER", Spotify: "Spotify", AirPlay: "AirPlay", TUNER: "Tuner" })[input] || ""; },
    cursor(action) {
      const src = this.sourceOf(this.receiver.input);
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: src, action }); });
    },
    remotePage(down) {
      const src = this.sourceOf(this.receiver.input);
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: src, down }); });
    },
    async loadMenu() {
      this.menuSource = ["SERVER", "Pandora", "NET RADIO"].includes(this.receiver.input) ? this.sourceOf(this.receiver.input) : "";
      if (!this.menuSource) return;
      const m = await getJSON(`receiver/menu?source=${this.menuSource}`);
      if (m) this.menu = m;
    },
    menuSelect(line) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/select", { source: this.menuSource, line }); }); },
    menuBack() { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: this.menuSource, action: "Return" }); }); },
    menuPage(down) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: this.menuSource, down }); }); },

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
    // asked once per track, then cached
    async refreshHeart() {
      const t = this.playingTrack;
      if (!t || t.path in this.hearts) return;
      const r = await getJSON(`admin/api/heart?path=${encodeURIComponent(t.path)}`);
      if (r) this.hearts = { ...this.hearts, [t.path]: !!r.heart };
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
    searchDebounced() {
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(async () => {
        this.results = this.query.trim().length > 1 ? ((await getJSON(`admin/api/search?q=${encodeURIComponent(this.query)}`)) || []) : [];
      }, 250);
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

    // ---- the OS's own media controls -------------------------------------
    // Desktop browsers put the same metadata on the system media keys and the
    // notification shade. Same closed action list as on the phone: skip maps
    // to `nexttrack`, and there is no heart action to map one to.
    mediaSession() {
      if (!("mediaSession" in navigator)) return;
      const ms = navigator.mediaSession;
      if (this.target !== "here" || !this.current) { ms.metadata = null; ms.playbackState = "none"; return; }
      const t = this.playingTrack;
      const cover = this.artOk() ? new URL(this.art, location.href).href : "";
      const title = (t && t.title) || this.nowLine1 || this.nowStation || "Library radio";
      const key = [title, t && t.artist, cover].join("\u0000");
      if (key === this._msKey) { ms.playbackState = "playing"; return; }
      this._msKey = key;
      try {
        ms.metadata = new MediaMetadata({
          title,
          artist: (t && t.artist) || "",
          album: this.nowStation || this.site.title,
          artwork: cover ? [96, 192, 384, 512].map(px => ({ src: cover, sizes: `${px}x${px}`, type: "image/jpeg" })) : [],
        });
        ms.playbackState = "playing";
      } catch (e) { /* older browsers manage without */ }
    },
    setMediaHandlers() {
      if (!("mediaSession" in navigator)) return;
      const set = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch (e) {} };
      set("play", () => this.resumePlay());
      set("pause", () => this.stop());        // a live stream has no resume point
      set("stop", () => this.stop());
      set("nexttrack", () => this.skip());
      set("previoustrack", null);
    },

    // ---- the rain bed ----------------------------------------------------
    // A second <audio> on its own mount, with its own level. It follows the
    // music: it plays while something is playing in this browser and stops
    // when that stops, so it never becomes rain on its own by accident.
    // Requesting the mount also wakes its encoder, the same as any other.
    bedToggle() {
      this.bedOn = !this.bedOn;
      try { localStorage.setItem("radio.bed", this.bedOn ? "1" : "0"); } catch (e) {}
      this.applyBed();
    },
    bedSet(level) {
      this.bedVol = Math.max(0, Math.min(100, Math.round(level)));
      try { localStorage.setItem("radio.bedVolume", String(this.bedVol)); } catch (e) {}
      this.applyBed();
    },
    applyBed() {
      const b = this.$refs.bed;
      if (!b || !this.site.bedMount) return;
      const wanted = this.bedOn && this.target === "here" && !!this.current;
      b.volume = this.bedVol / 100;
      if (wanted) {
        if (!b.src) b.src = `radio/${this.site.bedMount}.mp3?t=${Date.now()}`;
        if (b.paused) b.play().catch(() => {});      // a wake can take a moment; the retry below covers it
      } else if (b.src) {
        b.pause(); b.removeAttribute("src"); b.load();
      }
    },

    // ---- keeping the stream alive across a restart -----------------------
    // Every deploy restarts Liquidsoap, which drops every listener. The page
    // used to set "stream error — try again" and stop, so a browser left
    // playing went quiet and somebody had to find the tab and press play
    // (Chris, 2026-09-29). It reconnects itself now.
    //
    // Re-requesting the mount also WAKES it: nginx fires the wake on
    // /radio/<mount>.mp3, so a retry both restarts the encoder and reattaches
    // to it. Backs off 1, 2, 4, 8 s and then every 8 s, because the encoder is
    // genuinely absent for a few seconds and hammering it helps nobody.
    reconnect() {
      const m = this.current;
      if (!m) return;                       // stopped on purpose: stay stopped
      clearTimeout(this._reTimer);
      if (this._reTries > RECONNECT_GIVE_UP) {
        this.status = "stream lost — press play";
        return;
      }
      const audio = this.$refs.audio;
      audio.src = this.streamUrl(m);        // a fresh URL, so nothing is cached
      this.applyHereVolume();
      this.$nextTick(() => this.applyBed());
      audio.play()
        .then(() => { this.status = ""; this._reTries = 0; this.$nextTick(() => this.startViz()); })
        .catch(() => this.scheduleReconnect());
    },
    scheduleReconnect() {
      if (!this.current) return;
      clearTimeout(this._reTimer);
      this._reTries = (this._reTries || 0) + 1;
      if (this._reTries > RECONNECT_GIVE_UP) { this.status = "stream lost — press play"; return; }
      const wait = Math.min(1000 * 2 ** (this._reTries - 1), 8000);
      this.status = "reconnecting…";
      this._reTimer = setTimeout(() => this.reconnect(), wait);
    },

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
    stopViz() { cancelAnimationFrame(this.raf); },
  },

  mounted() {
    const audio = this.$refs.audio;
    // "ended" is what a clean Liquidsoap shutdown looks like to the element;
    // "error" is what a mid-flight drop looks like. Both mean reconnect.
    audio.addEventListener("error", () => this.scheduleReconnect());
    audio.addEventListener("ended", () => this.scheduleReconnect());
    const bed = this.$refs.bed;
    if (bed) {
      const again = () => { if (this.bedOn && this.current) setTimeout(() => { bed.removeAttribute("src"); this.applyBed(); }, 2000); };
      bed.addEventListener("error", again);
      bed.addEventListener("ended", again);
    }
    audio.addEventListener("waiting", () => { this.status = "buffering…"; });
    audio.addEventListener("playing", () => { this.status = ""; this._reTries = 0; });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") this.remote = false; });
    window.addEventListener("resize", () => { this.narrow = !window.matchMedia("(min-width: 900px)").matches; });
    // who this box is, written at build time from the module's options
    getJSON("site.json").then(s => {
      if (s) { this.site = { ...this.site, ...s }; document.title = this.site.title; }
      this.applyBed();
      this.refreshZones();          // site.json is what says which outputs exist
    });
    this.applyHereVolume();
    this.setMediaHandlers();
    this.refresh().then(() => { this.refreshHeart(); this.mediaSession(); });
    setInterval(() => { this.tick = Date.now(); this.refreshZones(); this.refresh().then(() => { this.refreshHeart(); this.mediaSession(); }); }, 10000);
    if (this.target === "room") this.pollReceiver();
    if (this.target === "local") this.pollSpeaker();
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  },
}).mount("#app");
