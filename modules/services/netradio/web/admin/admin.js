// Library radio — admin, as a Vue 3 app. Talks to /admin/api (netradio admin).
const { createApp } = Vue;
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

async function api(method, path, body) {
  const r = await fetch(`api${path}`, { method, headers: { "Content-Type": "application/json" },
                                        body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${r.status}`);
  return j;
}

// A tag field: chips + an input with suggestions (datalist). Enter or comma
// adds; only suggested values are accepted when suggestions are given.
const TagInput = {
  props: { modelValue: { type: Array, default: () => [] }, suggestions: { type: Array, default: () => [] }, placeholder: { type: String, default: "add…" } },
  emits: ["update:modelValue"],
  data() { return { text: "", id: "dl" + Math.random().toString(36).slice(2) }; },
  methods: {
    add() {
      const v = this.text.trim().replace(/,$/, "").toLowerCase();
      this.text = "";
      if (!v || this.modelValue.includes(v)) return;
      if (this.suggestions.length && !this.suggestions.includes(v)) return;
      this.$emit("update:modelValue", [...this.modelValue, v]);
    },
    remove(v) { this.$emit("update:modelValue", this.modelValue.filter(x => x !== v)); },
    key(e) { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); this.add(); } else if (e.key === "Backspace" && !this.text && this.modelValue.length) this.remove(this.modelValue[this.modelValue.length - 1]); },
  },
  template: `<span class="tags">
    <span v-for="t in modelValue" class="tag">{{ t }}<button type="button" @click="remove(t)" title="remove">✕</button></span>
    <input :list="id" v-model="text" :placeholder="placeholder" @keydown="key" @change="add" @blur="add">
    <datalist :id="id"><option v-for="s in suggestions.filter(s => !modelValue.includes(s))" :value="s"></option></datalist>
  </span>`,
};

createApp({
  components: { "tag-input": TagInput },
  data() {
    return { state: { feeds: {}, stations: [], schedule: [], artists: {}, families: [], today: {}, pending_requests: [] },
             options: {}, tab: "feeds", openId: null, edit: {}, add: { title: "", description: "", family: [], listenable: true },
             slots: {}, days: DAYS, flash: "", flashErr: false, scheduleMsg: "",
             sel: null,
             tabs: [{ id: "feeds", title: "Feeds" }, { id: "stations", title: "Stations" }] };
  },
  computed: {
    feedList() { return Object.entries(this.state.feeds).sort((a, b) => a[1].title.localeCompare(b[1].title)); },
    curated() { return this.state.stations.filter(s => s.kind !== "specialty"); },
    specialty() { return this.state.stations.filter(s => s.kind === "specialty"); },
    selected() { return this.state.stations.find(s => s.mount === this.sel) || null; },
  },
  methods: {
    say(msg, err) { this.flash = msg; this.flashErr = !!err; clearTimeout(this._t); this._t = setTimeout(() => this.flash = "", 7000); },
    async load() {
      this.state = await api("GET", "/state");
      this.slots = {};
      for (const s of this.state.schedule) {
        const custom = Array.isArray(s.days);
        (this.slots[s.station] ||= []).push({ ...s, _k: s.id || Math.random(), dayspec: custom ? "custom" : (s.days || "daily"),
                                               customDays: custom ? [...s.days] : [], minutes: s.minutes || 60, like: s.like || "artist" });
      }
    },
    // -- feeds
    openFeed(id) { const f = this.state.feeds[id]; this.edit = { title: f.title, description: f.description, family: [...(f.family || [])], listenable: !!f.listenable, rule: JSON.stringify(f.rule || {}, null, 1), _rule0: JSON.stringify(f.rule || {}, null, 1) }; },
    async saveFeed(id) {
      try {
        const body = { title: this.edit.title, description: this.edit.description, family: this.edit.family, listenable: this.edit.listenable };
        if (this.edit.rule.trim() !== this.edit._rule0.trim()) body.rule = JSON.parse(this.edit.rule);
        const r = await api("PUT", `/feeds/${id}`, body);
        this.say(r.recompile ? "Saved — the description changed, so the rule will be rebuilt." : "Saved; apply requested.");
        await this.load(); this.openFeed(id);
      } catch (e) { this.say(e.message, true); }
    },
    async compileFeed(id) { try { await api("POST", `/feeds/${id}/compile`); this.say("Rebuilding the rule from the description — a minute or two."); await this.load(); } catch (e) { this.say(e.message, true); } },
    async deleteFeed(id) {
      if (!confirm(`Delete feed "${this.state.feeds[id].title}"?`)) return;
      try { await api("DELETE", `/feeds/${id}`); this.openId = null; this.say("Feed removed; apply requested."); await this.load(); } catch (e) { this.say(e.message, true); }
    },
    async addFeed() {
      try {
        const r = await api("POST", "/feeds", this.add);
        this.say(`Feed added as ${r.id}; the agent is building its rule.`);
        this.add = { title: "", description: "", family: [], listenable: true }; this.openId = r.id;
        await this.load(); this.openFeed(r.id);
      } catch (e) { this.say(e.message, true); }
    },
    // -- stations (with their schedule)
    async select(st) {
      this.sel = st.mount;
      this.edit = { name: st.name, family: [...(st.family || [])], base: JSON.stringify(st.base || {}, null, 1),
                    breaks_every: st.breaks_every === undefined || st.breaks_every === null ? "" : String(st.breaks_every) };
      if (st.kind !== "specialty" && !this.options[st.mount])
        this.options[st.mount] = await api("GET", `/options?station=${encodeURIComponent(st.mount)}`);
    },
    slotsOf(mount) { return this.slots[mount] ||= []; },
    slotCount(mount) { return this.slotsOf(mount).length; },
    addSlot(mount) {
      const o = this.options[mount] || { feeds: [], artists: [] };
      this.slotsOf(mount).push({ _k: Math.random(), station: mount, name: "", kind: "feed", feed: (o.feeds[0] || {}).id, artist: (o.artists[0] || {}).name,
                                 like: "artist", dayspec: "daily", customDays: [], start: "20:00", minutes: 60 });
    },
    removeSlot(mount, i) { this.slotsOf(mount).splice(i, 1); },
    allSlots() {
      const out = [];
      for (const [mount, list] of Object.entries(this.slots)) {
        for (const s of list) {
          const slot = { id: s.id, station: mount, name: s.name, kind: s.kind, days: s.dayspec === "custom" ? s.customDays : s.dayspec, start: s.start, minutes: s.minutes };
          if (s.kind === "feed") slot.feed = s.feed;
          if (s.kind === "artist") slot.artist = s.artist;
          if (s.kind === "auto") slot.like = s.like;
          out.push(slot);
        }
      }
      return out;
    },
    async saveStation() {
      const st = this.selected;
      try {
        const body = { name: this.edit.name };
        if (this.edit.breaks_every !== "") body.breaks_every = /^\d+-\d+$/.test(this.edit.breaks_every) ? this.edit.breaks_every : Number(this.edit.breaks_every);
        if (st.kind !== "specialty") {
          body.family = this.edit.family;
          body.base = JSON.parse(this.edit.base || "{}");
          const r = await api("PUT", "/schedule", this.allSlots());
          this.scheduleMsg = `schedule: ${r.count} slot(s)`;
        }
        await api("PUT", `/stations/${st.mount}`, body);
        this.say("Saved; apply requested."); this.options = {};
        await this.load(); await this.select(this.selected);
      } catch (e) { this.say(e.message, true); }
    },
    async apply() { try { await api("POST", "/apply"); this.say("Apply requested: rescan, and a Liquidsoap restart if the station list changed."); await this.load(); } catch (e) { this.say(e.message, true); } },
  },
  watch: {
    openId(id) { if (id && this.tab === "feeds" && this.state.feeds[id]) this.openFeed(id); },
  },
  mounted() {
    this.load().then(() => { if (this.curated.length) this.select(this.curated[0]); }).catch(e => this.say(e.message, true));
    setInterval(() => { if (!this.openId && this.tab === "feeds") this.load().catch(() => {}); }, 30000);   // don't clobber an open edit
  },
}).mount("#app");
