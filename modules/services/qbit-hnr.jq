# qbit-hnr.jq — classify qBittorrent torrents against per-tracker hit-and-run rules.
#
# input: the array returned by qBittorrent's /api/v2/torrents/info
# args:  $rules — array of H&R rule objects (see qbit-seed-guard.nix)
#        $now   — epoch seconds
#
# Kept as a separate file rather than inlined in the module so the jq stays
# readable and is not fighting Nix '' string escaping.

# A torrent is earning seed credit only in these states. Notably `checkingUP`
# is NOT included: mid-recheck it is not connected to the tracker, and counting
# it as healthy would hide exactly the outage this module exists to catch.
#
# ⚠️ `queuedUP` was in this list until 2026-11-02 and must never go back. A
# queued torrent is NOT started: qBittorrent sends a `stopped` event when it
# parks one, and the tracker then reports the peer as gone — DarkPeers answered
# `"Stopped peer doesn't exist."` for an obligation it was simultaneously
# issuing a pre-warning on. Its `uploaded` stayed at 0 and its `last_activity`
# and `seeding_time` froze at the moment it was queued, so it could never
# satisfy the rule either. It is exactly the `checkingUP` case: present in the
# client, invisible to the tracker.
#
# The failure it caused: with `queuedUP` counted as active, every queued torrent
# read as healthy, so `not_seeding` was 0 for ALL SIX trackers,
# `grace_used_ratio{darkpeers}` read 0.0000 where the true figure was ~248% of
# the 24 h grace, and no alert could fire. `unmet` was correct throughout (15,
# independently recounted) — the guard saw the obligations and believed they
# were being served, which is the worst direction for a watchdog to fail.
#
# Why it lay dormant: `queuedUP` is only reachable once the library exceeds
# qBittorrent's active-torrent cap. On 2026-11-02 there were 353 non-stopped
# torrents against `max_active_torrents: 300`, parking exactly 53 — and queue
# position favours the oldest, so the newest grab loses, which is precisely
# when an H&R obligation is freshest.
def is_seeding:
  (.state | test("^(uploading|stalledUP|forcedUP)$"));

# A torrent the tracker can see: seeding, OR still downloading. A download
# in flight announces the whole time — it cannot be a hit-and-run — but it
# is not `is_seeding`, and on 2026-09-16 a slow 45-minute DarkPeers grab
# (minProgress 0, so the obligation attached at byte one) sat in at_risk
# from the moment it started and fired QbitHnRNotSeeding ("has stopped
# seeding") for a torrent that had never seeded yet. At-risk means
# INACTIVE: paused, stopped, errored, missing files, mid-recheck, moving.
def is_active:
  is_seeding or (.state | test("^(downloading|stalledDL|forcedDL|queuedDL|metaDL|forcedMetaDL)$"));

# Requirement satisfied either by seed time or, where the tracker allows it,
# by ratio. ratioAlt = 0 means "this tracker has no ratio shortcut".
def met($r):
  (.seeding_time >= $r.seedSeconds)
  or ($r.ratioAlt > 0 and .ratio >= $r.ratioAlt);

# Does an H&R obligation attach to this torrent at all?
#
# An H&R is recorded against a SNATCH, not against a registration. A cross-seed
# — the same data announced to a second tracker, added against files already on
# disk and verified by recheck — pulls zero bytes off the wire, so the tracker
# records no snatch and no obligation exists.
#
# This gate used to read `.progress >= minProgress`, but .progress is COMPLETION,
# which is 1.0 for a cross-seed the moment its recheck passes — so every
# cross-seed was counted as owing the full seed requirement it does not owe. The
# rules table describes downloaded VOLUME ("torrents where you have downloaded
# less than 10% are exempt"), so measure that instead.
#
# The `tags` clause is the conservative half. downloaded==0 alone would also
# exempt a genuine snatch whose counter was lost (re-add, discarded resume data),
# which would UNDER-report a real obligation — the failure that actually costs
# ratio. So only a torrent we ourselves tagged `cross-seed` AND that has pulled
# nothing is treated as obligation-free; if it ever does download bytes, it
# starts counting again on its own.
def has_obligation($r):
  ((.size // 0) > 0)
  and (((.tags // "") | test("(^|,) *cross-seed *(,|$)")) and ((.downloaded // 0) == 0) | not)
  and (((.downloaded // 0) / .size) >= $r.minProgress);

# --- retirement ------------------------------------------------------------
# qBittorrent's active-torrent cap is a hard ceiling and gromit is memory-tight,
# so the answer to a full queue is to RETIRE old releases, not to raise the cap.
# "Retire" means STOP: the data stays on disk and a resume undoes it. Nothing
# here deletes anything.
#
# This exists because on 2026-11-02 the library crossed the cap (353 non-stopped
# against max_active_torrents 300) and parked 53 torrents in `queuedUP`, where
# they announce nothing. Queue position favours the oldest, so the newest grab
# loses — and DarkPeers issued an H&R pre-warning on a torrent that had been
# queued, and therefore silent, for 32 h.
#
# Owing anything on any monitored tracker is disqualifying, full stop.
#
# `private` is libtorrent's own flag from the .torrent, deliberately NOT a
# hostname allowlist: checked against a hostname classification on 2026-11-02 it
# agreed on all 345 classifiable non-stopped torrents with zero disagreements,
# and unlike a list it keeps working when a tracker we have never seen turns up.
# `null` means metadata has not arrived, so the torrent cannot be classified and
# is left alone.
def owes:
  . as $t
  | any($rules[]; . as $r
        | (($t.tracker // "") != "")
          and ($t.tracker | test($r.match))
          and ($t | has_obligation($r))
          and (($t | met($r)) | not));

# Lower tier retires first; null means never retire.
def retire_tier:
  if ((.progress // 0) < 1) then null        # never stop an unfinished download
  elif (.private == null)   then null        # metadata not in yet: unclassifiable
  elif owes                 then null        # outstanding obligation: protect
  elif (.private == false)  then 1           # public: owes nobody anything
  else 2 end;                                # private, requirement already satisfied

# Hours left to bank the requirement, for trackers that impose a deadline
# window measured from download completion (RetroToon's "within 10 days").
# null when the tracker has no such window.
def deadline_hours($r):
  if $r.withinDays > 0 and .completion_on > 0
  then (($r.withinDays * 86400) - ($now - .completion_on)) / 3600
  else null
  end;

. as $all
| {
    # Torrents qBittorrent has given up on. Recovery candidates.
    missing: [ $all[]
               | select(.state == "missingFiles")
               | { hash: .hash, name: .name, content_path: .content_path } ],

    # Hashes on any monitored (private) tracker — the only ones worth spending
    # a per-torrent /trackers API call on to check announce health.
    # NOTE: bind the rule to $r before piping the URL. Writing
    # `$u | test(.match)` rebinds `.` to the URL string, so `.match` then tries
    # to index a string and the whole program dies at runtime (not at parse
    # time — only running it against real data catches this).
    private: [ $all[]
               | select((.tracker // "") != "")
               | select( .tracker as $u | any($rules[]; . as $r | $u | test($r.match)) )
               | .hash ],

    # Retirement view. `protect` is published so the caller can assert it never
    # stops a protected hash even if the ordering changes later; `candidates` is
    # pre-sorted so the caller just takes the first N.
    retire: {
      protect: [ $all[] | select(retire_tier == null) | .hash ],
      candidates: [ $all[]
                    | select((.state | startswith("stopped")) | not)
                    | select(retire_tier != null)
                    | { hash: .hash,
                        name: .name,
                        tier: retire_tier,
                        private: .private,
                        age: (if (.completion_on // 0) > 0
                              then .completion_on else (.added_on // 0) end) } ]
                  | sort_by(.tier, .age),
      non_stopped: ([ $all[] | select((.state | startswith("stopped")) | not) ] | length),
    },

    per_tracker: [ $rules[] as $r
      # has_obligation gates whether an obligation attaches at all. DigitalCore's
      # rule reads "a torrent that you have started to download 10% or more",
      # so a barely-started torrent carries NO obligation and must not be
      # reported at risk. minProgress 0 means "every torrent you snatched on
      # this tracker counts" — it does not sweep in cross-seeds.
      | ([ $all[]
           | select((.tracker // "") != "" and (.tracker | test($r.match)))
           | select(has_obligation($r)) ]) as $ts
      | ([ $ts[] | select(met($r) | not) ]) as $unmet
      | {
          id:    $r.id,
          label: $r.label,
          # Carried through so the caller does not have to re-read the rules
          # table to time each tracker's grace.
          grace_seconds: $r.graceSeconds,
          cure_days:     $r.cureDays,
          total: ($ts | length),
          unmet: ($unmet | length),
          # Hashes carrying an outstanding obligation that are not earning
          # credit right now. The caller times how long each has been in this
          # set, which is what turns "not seeding" into "N seconds of a 1-hour
          # grace consumed".
          at_risk: [ $unmet[] | select(is_active | not) | .hash ],
          # THE signal: requirement still outstanding AND the tracker cannot
          # see the torrent. This is what was true for 19 h on 2026-08-17 with
          # nothing watching it. (A download in flight is not this.)
          not_seeding: ([ $unmet[] | select(is_active | not) ] | length),
          # Deadline already passed with the requirement unmet.
          breached: ([ $unmet[]
                       | select(deadline_hours($r) != null and deadline_hours($r) < 0) ]
                     | length),
          # Soonest deadline among outstanding torrents; -1 = no deadline applies.
          min_hours_to_deadline:
            ([ $unmet[] | deadline_hours($r) | select(. != null and . >= 0) ]
             | if length > 0 then min else -1 end)
        } ],

    totals: {
      missing_files: ([ $all[] | select(.state == "missingFiles") ] | length),
      torrents:      ($all | length)
    }
  }
