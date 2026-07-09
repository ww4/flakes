# Daybook — the twice-daily scheduling-assistant bookends (decided 2026-07-09,
# docs repo: docs/scheduling-assistant-research.md).
#
#   09:00  morning run — read the SilverBullet space (tasks/notes SoT), the
#          calendar vdir (pim.nix) and homelab state -> write today's plan into
#          Journal/Day/<date>.md + a short ntfy.
#   20:00  evening run — review what happened (space git diff + task states),
#          close out the day, stage tomorrow -> append Review/Tomorrow + ntfy.
#
# Same headless-claude pattern as digest.nix: runs `claude -p` as the claude
# user on the subscription OAuth, CLAUDE_AUTONOMOUS=1 (reflection hook no-ops),
# WorkingDirectory = the docs repo so the agent's memory loads. The prompts
# grant FULL agency inside the space (Chris, 2026-07-09: create/edit/delete/
# arrange/link/schedule — "update any and everything"); the existing guardrails
# (PreToolUse main-branch guard, scoped sudo, gated flakes PRs) still bound
# everything outside it. The space git autosave (silverbullet.nix) is the undo
# log; each run also commits its own changes.
#
# Quiet hours (22:00-07:00) are respected by construction: 09:00 and 20:00.
{ config, lib, pkgs, ... }:

let
  spaceDir = "/var/lib/silverbullet";
  notesUrl = "https://notes.rosemaryacres.com";

  commonRules = ''
    Ground rules:
    - The SilverBullet space at ${spaceDir} is the shared task/notes source of
      truth — plain markdown, edited by BOTH Chris (web UI) and you (files).
      You have full agency in it: create, edit, complete, reschedule, delete,
      reorganize, and link pages and tasks. Keep it always understandable to a
      human reading the raw files.
    - Read ${spaceDir}/CONVENTIONS.md first if it exists and follow it (task
      syntax, page layout, where things live). Prefer editing a task where it
      lives over duplicating it; keep [[wiki-links]] coherent.
    - Calendar: run `pim-vdirsyncer sync 2>/dev/null || true` first, then
      `agenda` (next 7 days) / `agenda list <when>` (khal). If sync legs are
      not set up yet, say so in one line and move on — never treat it as a
      failure.
    - Homelab: skim the open-loops memory board and the last-24h sentinel
      incidents (the current month's ${spaceDir}/System/Sentinel/ page, or
      /var/lib/sentinel/incidents/); surface only what genuinely needs
      Chris — no noise. Weekly digests live in ${spaceDir}/System/Digest/.
    - Do NOT do repo/PR work from this run; it is a planning run. Writing in
      the space and reading anywhere is in scope.
    - Your final message: a one-paragraph summary for a phone notification,
      ending with a line "TLDR: <one sentence>".
  '';

  amPrompt = pkgs.writeText "daybook-am.md" ''
    Daybook — MORNING run (~09:00). Plan Chris's day.

    ${commonRules}

    Morning specifics:
    1. Triage ${spaceDir}/Inbox.md: file new captures onto the right pages
       (create pages if needed), leaving Inbox empty or near-empty.
    2. Grocery upkeep: ${spaceDir}/"Grocery List.md" is the SHARED store list (fixed
       page — Chris and Mary use it at the store), organized STORE-FIRST:
       Walmart / Kroger / Frankfort house (storage at dad's) / Other stores /
       Unsorted.
       - Archive `- [x]` items (bought since last run) by APPENDING them to
         ${spaceDir}/System/Grocery Log/<YYYY-MM>.md (record which store
         section each was under), then remove them from Grocery.
       - File new items (Inbox captures, Unsorted, bare lines) under the
         right store per ${spaceDir}/System/Store Preferences.md. Unknown
         item: best guess from similar items, and record the guess there
         marked "(guessed)".
       - LEARN: diff Grocery + Store Preferences against yesterday (space
         git); if a human MOVED an item between stores or edited a
         preference, update Store Preferences to match — a human move always
         wins and un-marks "(guessed)".
       - Order the Walmart section by the table in
         ${spaceDir}/System/Walmart Aisle Contents.md, TOP TO BOTTOM — that
         table IS the walking route, including the sidewall diverts. NEVER
         reorder that table yourself (humans correct it, you follow it), and
         never regroup its rows: fresh meat belongs at the MEAT WALL divert
         (partway through the aisles) and lunch meat/cheese at the A30
         sidewall (one past peanut butter), NOT lumped with each other or
         with the dairy back wall. Chris shops it that way on purpose.
         Items whose category isn't in the table go in a short "unsorted"
         group at the end of the Walmart section — never guess an aisle.
         Group Kroger loosely by type. "Other stores" items keep a store
         prefix ("Lowes: …").
       - Dedupe. NEVER drop an unchecked item.
    3. Sweep open tasks across the space (`- [ ]`, due dates, overdue items)
       and yesterday's Journal/Day page for carryover.
    4. Write/overwrite today's ${spaceDir}/Journal/Day/<YYYY-MM-DD>.md (page
       name from `date +%F`; header weekday from `date '+%F (%A)'` — NEVER
       guess the weekday) with:
       - "## Plan" — 3–7 concrete focus items, each [[linked]] to its page
       - today's calendar events (times), from `agenda`
       - carryover/overdue: reschedule or explicitly drop them AT THE SOURCE
         (you have authority; note what you moved)
       - anything homelab-urgent for today
    5. TLDR = the shape of today in one sentence.
  '';

  pmPrompt = pkgs.writeText "daybook-pm.md" ''
    Daybook — EVENING run (~20:00). Review the day, stage tomorrow.

    ${commonRules}

    Evening specifics:
    1. What changed today: in ${spaceDir} run `git log --since=07:00 --stat`
       and `git diff` against this morning where useful; compare against
       today's "## Plan". Which tasks got checked off; which didn't move.
    2. Append to today's Journal/Day/<YYYY-MM-DD>.md:
       - "## Review" — done (concise), not-done (reschedule each at its source
         page or explicitly punt it, and say so), anything worth remembering
       - "## Tomorrow" — tomorrow's calendar (`agenda list tomorrow 1d`) plus
         the 2–4 likely focus items
    3. TLDR = what got done + tomorrow's shape, one sentence.
  '';

  mkDaybookRun = { name, prompt, onCalendar, title, ntfyTag }: {
    services."claude-daybook-${name}" = {
      description = "Daybook ${name} run (claude -p -> space journal + ntfy)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = "claude";
        # Space files must be born group-writable or their ACL mask locks the
        # SilverBullet web UI out of them (see silverbullet.nix).
        UMask = "0002";
        WorkingDirectory = "/home/claude/nixos-homelab-improvements";
        TimeoutStartSec = "25min";
        Environment = [
          "HOME=/home/claude"
          # Overrides the systemd `path` option (digest.nix / #32 lesson):
          # anything not in these profiles is called by absolute store path.
          "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
          "CLAUDE_AUTONOMOUS=1"
        ];
      };
      script = ''
        set -uo pipefail
        out="$(timeout 20m claude -p "$(cat ${prompt})" 2>/dev/null)" \
          || out="TLDR: Daybook ${name} run FAILED — check journalctl -u claude-daybook-${name}"
        tldr="$(printf '%s' "$out" | grep -m1 -iE '^TLDR:' | sed -E 's/^[Tt][Ll][Dd][Rr]:[[:space:]]*//')"
        [ -n "$tldr" ] || tldr="Daybook ${name} run finished (no TLDR line — check the journal page)."
        gromit-notify "${title}" "$tldr
        ${notesUrl}/Journal/Day/$(date +%F)" default "${ntfyTag}"

        # Commit this run's space changes (the autosave repo is the undo log).
        cd ${spaceDir}
        if [ -d .git ]; then
          ${pkgs.git}/bin/git add -A
          ${pkgs.git}/bin/git diff --cached --quiet \
            || ${pkgs.git}/bin/git commit -q -m "daybook ${name} $(date '+%Y-%m-%d %H:%M')"
        fi
      '';
    };
    timers."claude-daybook-${name}" = {
      description = "Daybook ${name} run";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = onCalendar;
        Persistent = true;
        RandomizedDelaySec = "2m";
      };
    };
  };

  am = mkDaybookRun {
    name = "am";
    prompt = amPrompt;
    onCalendar = "09:00";
    title = "Daybook — morning plan";
    ntfyTag = "sunrise";
  };
  pm = mkDaybookRun {
    name = "pm";
    prompt = pmPrompt;
    onCalendar = "20:00";
    title = "Daybook — evening review";
    ntfyTag = "city_sunset";
  };
in
{
  systemd.services = am.services // pm.services;
  systemd.timers = am.timers // pm.timers;
}
