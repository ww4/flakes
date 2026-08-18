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
def is_seeding:
  (.state | test("^(uploading|stalledUP|forcedUP|queuedUP)$"));

# Requirement satisfied either by seed time or, where the tracker allows it,
# by ratio. ratioAlt = 0 means "this tracker has no ratio shortcut".
def met($r):
  (.seeding_time >= $r.seedSeconds)
  or ($r.ratioAlt > 0 and .ratio >= $r.ratioAlt);

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

    per_tracker: [ $rules[] as $r
      | ([ $all[] | select((.tracker // "") != "" and (.tracker | test($r.match))) ]) as $ts
      | ([ $ts[] | select(met($r) | not) ]) as $unmet
      | {
          id:    $r.id,
          label: $r.label,
          total: ($ts | length),
          unmet: ($unmet | length),
          # THE signal: requirement still outstanding AND not currently earning
          # credit. This is what was true for 19 h on 2026-08-17 with nothing
          # watching it.
          not_seeding: ([ $unmet[] | select(is_seeding | not) ] | length),
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
