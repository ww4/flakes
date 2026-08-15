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
#
# Inbox triage is ALSO event-driven (Chris, 2026-07-24): a systemd .path unit
# watches Inbox.md via inotify and fires a light, SILENT triage run whenever
# Chris saves a capture — so items get filed within a couple minutes instead of
# waiting for the 09:00 daybook. The triage does ONLY inbox filing (no Journal
# rewrite, no grocery route work, no ntfy). Two guards keep it well-behaved:
#   - debounce: wait until Inbox.md's mtime is stable (~90s) so we never triage
#     a half-typed capture (SilverBullet autosaves as you type);
#   - self-trigger guard: emptying the inbox is itself a write, so a fast
#     "is there real content?" check (placeholder line counts as empty) makes
#     that echo a cheap no-op with no `claude` call.
#
# ESCAPE HATCH (Chris, 2026-07-24): typing "run the daybook" ALONE on a line in
# the Inbox escalates that same trigger to the FULL morning run (inbox triage +
# grocery route ordering + open-task sweep + refresh today's Plan) instead of
# the light filing, and ntfys the TLDR. It exists because the light triage
# deliberately won't retro-fit today's Plan or route-sort the grocery list, so
# there had to be a way to say "do the whole thing now". Loop safety: the phrase
# is stripped BEFORE the run (so a failed run can't leave a phrase that re-fires
# it), the match is anchored at both ends (prose about the daybook, and the
# placeholder that quotes the phrase mid-line, never fire it), and the strip
# truncates the existing inode rather than replacing the file, which would drop
# the ACL the SilverBullet web UI needs.
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
    - If a page opens with a YAML frontmatter block, PRESERVE it verbatim and
      rewrite only the body below it (the imported Keep/ pages carry their
      date, category and source there).
    - Calendar: run `pim-vdirsyncer sync 2>/dev/null || true` first, then
      `agenda` (next 7 days) / `agenda list <when>` (khal). If sync legs are
      not set up yet, say so in one line and move on — never treat it as a
      failure.
    - Homelab: skim the open-loops memory board and the last-24h sentinel
      incidents (the current month's ${spaceDir}/System/Sentinel/ page, or
      /var/lib/sentinel/incidents/); surface only what genuinely needs
      Chris — no noise. Weekly digests live in ${spaceDir}/System/Digest/.
    - **YOU CANNOT EXECUTE. This is a planning-only run** — nothing invokes an
      agent between your runs to work items off your list, and this run itself
      does not do repo/PR work. Anything that requires action is one of:
        (a) a Chris-keyboard ask — write it clearly and don't repeatedly
            re-plan the same one day after day; if he hasn't done it, that's
            his call, not a signal to escalate it back onto your own plan;
        (b) an agent-executable task — APPEND it to the shared `open-loops`
            memory board so the next interactive session (which auto-loads
            that board under Chris's direction) can evaluate and run it.
            Location: /home/claude/.claude/projects/-home-claude-nixos-homelab-improvements/memory/open-loops.md
            Spell it out: what to do, where, how to verify, which
            memory/PR/module to touch. Do NOT write it inline in the Journal
            as if you will run it; Chris directs the session, not you.
      In either case, do NOT put an action item on YOUR plan as though you
      will do it — you can't. When an item you previously wrote slips, do NOT
      roll it forward as "attempt N" / "SHIP Plan #N"; either surface it as
      the appropriate (a) or (b), or drop it. The `SHIP Plan #N / attempt N
      of N` pattern papered over a missing execution mechanism and produced
      multi-week slippage on real items; it is the anti-pattern.
    - Writing in the space and reading anywhere is in scope.
    - Your final message: a one-paragraph summary for a phone notification,
      ending with a line "TLDR: <one sentence>".
  '';

  # The morning run's actual work — shared verbatim with the on-demand run so
  # "run the daybook" gives exactly the scheduled 09:00 treatment.
  amBody = ''
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
       - ORPHANED SECTION CHECK: if a run of items at the END of the Walmart
         section is dominated by Kroger-preferred goods (produce especially),
         it is almost certainly a Kroger section whose header got deleted —
         Chris's old lists did this constantly. Move those items to Kroger and
         SAY SO in the notification; never sort them into the Walmart route
         (that would send him to the wrong store).
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
       - Keep the page PLAIN: no frontmatter, no headings beyond the store
         sections, no commentary. It gets read one-handed in a store. (The
         TOC and Linked-Mentions widgets are suppressed for this page in the
         space's CONFIG.md, keyed on the page name.)
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

  amPrompt = pkgs.writeText "daybook-am.md" ''
    Daybook — MORNING run (~09:00). Plan Chris's day.

    ${amBody}'';

  # Same work as the morning run, but fired by hand at an arbitrary hour.
  onDemandPrompt = pkgs.writeText "daybook-ondemand.md" ''
    Daybook — ON-DEMAND run, triggered by Chris typing "run the daybook" on its
    own line in the Inbox. Same work as the morning run, with three differences:
      - It can fire at ANY hour. Do NOT frame anything as "this morning"; get
        the real time from `date '+%F (%A) %H:%M'` and write for where the day
        actually is.
      - Today's Journal/Day page usually EXISTS already. REFRESH it rather than
        assuming it is unwritten: keep the "## Plan" items that still hold (and
        anything already checked off), fold in what is new, drop what the day
        has overtaken. If an evening "## Review" section is already there,
        leave it alone.
      - The trigger line has already been stripped from the Inbox for you, so
        treat whatever remains there as the captures to file. An empty Inbox is
        normal here — Chris may be asking for a re-plan, not a filing run.

    ${amBody}
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

  triagePrompt = pkgs.writeText "inbox-triage.md" ''
    Inbox triage — file Chris's new captures. This is a LIGHT, SILENT run
    triggered whenever ${spaceDir}/Inbox.md changes. Do ONLY inbox filing —
    this is NOT the daybook: do not rewrite the Journal, do not sweep tasks,
    do not reorder the Grocery route, do not send any notification.

    - The SilverBullet space at ${spaceDir} is the shared markdown source of
      truth (Chris edits via the web UI, you via files). Read
      ${spaceDir}/CONVENTIONS.md first and follow it (task syntax, page
      layout, where things live).
    - If a destination page opens with a YAML frontmatter block, PRESERVE it
      verbatim and edit only the body below it.

    Do exactly this:
    1. Read ${spaceDir}/Inbox.md. For each capture, move it onto the RIGHT
       page (create the page if needed), editing the task where it will live
       and keeping [[wiki-links]] coherent. Prefer editing a task where it
       lives over duplicating it.
       - A capture that is clearly a grocery item: APPEND it to
         ${spaceDir}/"Grocery List.md" under the best-guess store section
         (or "Unsorted" if unsure). Keep that page PLAIN (no frontmatter,
         no headings beyond the store sections, no commentary) and do NOT
         reorder the Walmart route or archive bought items — the 09:00
         daybook owns full grocery upkeep. NEVER drop an unchecked item.
    2. When done, leave Inbox.md empty except this single placeholder line
       (keep it on ONE line, and keep the quotes — the trigger only fires when
       the phrase is alone on a line, so the placeholder can never self-trigger):
       *(empty — brain-dump anything here; it's filed automatically when you save. Type "run the daybook" alone on a line to force a full daybook run.)*
    3. If, on reading, the Inbox has no real captures (only the placeholder
       or blank lines), make NO changes at all.

    Keep every page understandable to a human reading the raw file. Your
    final message: one short line naming what you filed and where (this is
    for the journal only — no notification is sent).
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

  inboxFile = "${spaceDir}/Inbox.md";

  triageService = {
    "claude-inbox-triage" = {
      description = "Inbox triage / on-demand daybook (Inbox.md change -> file captures, or full run on 'run the daybook')";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = "claude";
        # Match the daybook: space files must be born group-writable or their
        # ACL mask locks the SilverBullet web UI out of them (silverbullet.nix).
        UMask = "0002";
        WorkingDirectory = "/home/claude/nixos-homelab-improvements";
        # Debounce (up to ~5x90s) + a 10min claude run, with headroom.
        TimeoutStartSec = "25min";
        Environment = [
          "HOME=/home/claude"
          "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
          "CLAUDE_AUTONOMOUS=1"
        ];
      };
      script = ''
        set -uo pipefail
        inbox=${lib.escapeShellArg inboxFile}

        # "run the daybook" ALONE on a line (optionally as a list item or task,
        # any case) forces the full daybook now. Anchored at BOTH ends so prose
        # about the daybook can't fire it — including the Inbox placeholder,
        # which quotes the phrase mid-line.
        trigger_re='^[[:space:]]*(-[[:space:]]*(\[[ xX]\][[:space:]]*)?)?run the daybook[[:space:]]*$'

        has_trigger() {
          [ -f "$inbox" ] && grep -qiE "$trigger_re" "$inbox"
        }

        # Real content = any non-blank line that isn't the italic placeholder
        # (`*(... )*`). If there's nothing real, do nothing — this is also how
        # the self-triggered echo (our own emptying write) exits for free,
        # before any debounce or `claude` call.
        has_content() {
          [ -f "$inbox" ] || return 1
          grep -qvE '^[[:space:]]*$|^[[:space:]]*\*\(.*\)\*[[:space:]]*$' "$inbox"
        }

        has_content || exit 0

        # Debounce: SilverBullet autosaves as Chris types, so wait for the file
        # to go quiet before acting on a possibly half-written capture. The
        # trigger phrase is itself an explicit "go", so it settles briefly.
        if has_trigger; then settle=15; rounds=2; else settle=90; rounds=5; fi
        i=0
        while [ "$i" -lt "$rounds" ]; do
          i=$((i + 1))
          m1=$(stat -c %Y "$inbox")
          sleep "$settle"
          m2=$(stat -c %Y "$inbox")
          [ "$m1" = "$m2" ] && break
        done

        has_content || exit 0

        if has_trigger; then
          # Strip the trigger FIRST: if the run then fails or is killed, the
          # phrase is already gone and cannot re-fire the daybook in a loop.
          # Truncate the EXISTING inode (not sed -i, which replaces the file and
          # would drop the ACL that keeps the web UI able to write it).
          remaining="$(grep -viE "$trigger_re" "$inbox" || true)"
          printf '%s\n' "$remaining" > "$inbox"

          out="$(timeout 20m claude -p "$(cat ${onDemandPrompt})" 2>/dev/null)" \
            || out="TLDR: On-demand daybook FAILED — check journalctl -u claude-inbox-triage"
          tldr="$(printf '%s' "$out" | grep -m1 -iE '^TLDR:' | sed -E 's/^[Tt][Ll][Dd][Rr]:[[:space:]]*//')"
          [ -n "$tldr" ] || tldr="On-demand daybook finished (no TLDR line — check the journal page)."
          gromit-notify "Daybook — on-demand run" \
            "$tldr"$'\n'"${notesUrl}/Journal/Day/$(date +%F)" default "zap"
          commit_msg="daybook on-demand $(date '+%Y-%m-%d %H:%M')"
        else
          timeout 10m claude -p "$(cat ${triagePrompt})" 2>/dev/null || true
          commit_msg="inbox triage $(date '+%Y-%m-%d %H:%M')"
        fi

        # Commit this run's space changes (same undo-log pattern as the daybook).
        cd ${spaceDir}
        if [ -d .git ]; then
          ${pkgs.git}/bin/git add -A
          ${pkgs.git}/bin/git diff --cached --quiet \
            || ${pkgs.git}/bin/git commit -q -m "$commit_msg"
        fi
      '';
    };
  };

  triagePath = {
    "claude-inbox-triage" = {
      description = "Watch the SilverBullet Inbox for new captures";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathModified = inboxFile;
        Unit = "claude-inbox-triage.service";
      };
    };
  };
in
{
  systemd.services = am.services // pm.services // triageService;
  systemd.timers = am.timers // pm.timers;
  systemd.paths = triagePath;
}
