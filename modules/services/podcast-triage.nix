# PODCAST TRIAGE — surface the occasional worthwhile episode from shows Chris
# does not listen to.
#
# The archives split into two ROLES (see services.contentArchives):
#   personal  — Bitcoin Audible, What Bitcoin Did, LINUX Unplugged, TWiB.
#               Chris listens to these; they are archived so questions about
#               what was discussed can be answered from the text.
#   discovery — TFTC, Rabbit Hole Recap, Citadel Dispatch, Bitcoin Explained.
#               Chris does NOT listen to these. In his words they carry "way
#               too much opinion and honestly slightly arrogant junk, but
#               genuinely good content comes through now and again". The
#               archive exists purely to mine that signal.
#
# This module is the discovery half. Two stages, deliberately split by what each
# is actually good at:
#
#   1. podcast-triage (python) — cheap deterministic ranking over every new
#      episode. Keyword density against a TUNABLE interest profile, normalised
#      per-1000-words so a rambling three-hour episode cannot out-score a tight
#      technical one on sheer volume.
#   2. `claude -p` — reads the shortlisted transcripts and makes the call that
#      keywords cannot: is this substance or is it punditry? Writes a short,
#      honest verdict per episode into the SilverBullet listening queue.
#
# Stage 2 exists because Chris's filter is not topical, it is qualitative — the
# shows talk about the right subjects constantly and are still mostly not worth
# his time. Only a reader can tell those apart.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.podcastTriage;

  interests = ./podcast-interests.json;

  triage = pkgs.writers.writePython3Bin "podcast-triage" {
    flakeIgnore = [ "E501" "E203" "W503" "W504" ];
  } (builtins.readFile ./podcast-triage.py);

  # The judgement prompt. Written to the store so the unit stays a one-liner
  # and the prompt is reviewable in the diff like any other config.
  prompt = pkgs.writeText "podcast-triage-prompt" ''
    You are triaging podcast episodes for Chris.

    CONTEXT. Four Bitcoin shows are archived that Chris does NOT listen to:
    TFTC, Rabbit Hole Recap, Citadel Dispatch and Bitcoin Explained. His own
    assessment is that they contain "way too much opinion and honestly slightly
    arrogant junk, but genuinely good content comes through now and again".
    Your job is to find that good content so he can cherry-pick, and to spare
    him everything else.

    INPUT. Read /var/lib/podcast-triage/candidates.json — a keyword-ranked
    shortlist of new episodes, each with a `path` to its transcript markdown.
    The ranking is crude; it only got these onto your desk. You decide.

    FOR EACH shortlisted episode: read enough of the transcript to judge it,
    then decide whether Chris should spend the time.

    RECOMMEND an episode when it contains something he could ACT on or LEARN
    from: a specific tool, project, protocol change, node/self-hosting
    technique, a builder explaining how something actually works, a genuine
    postmortem. He runs a NixOS homelab, a full Bitcoin node stack (Core/Knots,
    Fulcrum, mempool, Lightning), self-hosts nearly everything, and is a WISP
    operator by trade.

    REJECT — and expect to reject MOST of them — episodes that are price talk,
    macro punditry, political commentary, personality drama, or two people
    agreeing with each other at length. Topic alone is not enough: an episode
    can be nominally about node running and still be an hour of opinion. Being
    ruthless here IS the product. A queue full of maybes is one he stops
    reading, and that would waste the whole archive.

    OUTPUT. Append to the SilverBullet page `Areas/Podcast Queue` (create it if
    absent) under a new `## <today's date>` heading. One line per recommended
    episode, using the space's task syntax so his existing tooling sees it:

      - [ ] **<show> <episode number/title>** — <2-3 sentences, YOUR OWN words:
            what is actually in it, what specifically he would get out of it,
            and roughly where in the episode the substance sits if it is buried.
            #podcast

    Write your own characterisation rather than reproducing chunks of the
    transcript — he wants a verdict he can act on, not the text. A short quoted
    phrase to illustrate a point is fine.

    If NOTHING is worth recommending, say so in one line under the date heading.
    That is a perfectly good result and much better than padding.

    Finally, print a one-line summary to stdout: how many you reviewed, how many
    you recommended.
  '';
in
{
  options.services.podcastTriage = {
    enable = lib.mkEnableOption "weekly triage of the discovery podcast archives";

    archives = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "/home/claude/tftc-archive" ];
      description = "Discovery-role archive checkouts to mine.";
    };

    limit = lib.mkOption {
      type = lib.types.int;
      default = 12;
      description = ''
        How many keyword-ranked candidates to hand the reader each week. Keep
        it small: the reader is the expensive stage, and a shortlist Chris can
        skim in a minute beats an exhaustive one he ignores.
      '';
    };

    schedule = lib.mkOption {
      type = lib.types.str;
      # After content-archives-refresh (Thu 04:30) has pulled the new episodes.
      default = "Thu 06:00";
      description = "systemd OnCalendar for the weekly triage run.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ triage ];

    systemd.services.podcast-triage = {
      description = "Weekly podcast triage (rank -> claude -p -> listening queue)";
      after = [ "network-online.target" "content-archives-refresh.service" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = "claude";
        StateDirectory = "podcast-triage";
        WorkingDirectory = "/home/claude";
        Environment = [
          "HOME=/home/claude"
          # Mirrors digest.nix: the interactive claude env, so `claude -p` finds
          # its subscription OAuth creds rather than looking for an API key.
          "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
          "TRIAGE_STATE=/var/lib/podcast-triage"
          "TRIAGE_DEFAULT_PROFILE=${interests}"
          "TRIAGE_ARCHIVES=${lib.concatStringsSep " " cfg.archives}"
        ];
        TimeoutStartSec = "40min";
      };
      script = ''
        set -u
        out=/var/lib/podcast-triage/candidates.json
        if ! ${lib.getExe triage} --limit ${toString cfg.limit} > "$out"; then
          echo "podcast-triage: ranking failed — not invoking the reader"
          exit 0
        fi
        n=$(${pkgs.jq}/bin/jq -r '.shortlisted // 0' "$out" 2>/dev/null || echo 0)
        if [ "$n" -eq 0 ]; then
          echo "podcast-triage: no new episodes to review this week"
          exit 0
        fi
        echo "podcast-triage: $n candidate(s) shortlisted; handing to the reader"
        # Exit 0 regardless: a bad week for the reader must not trip the
        # failed-unit alert. Freshness is visible in the queue page itself.
        timeout 35m claude -p "$(cat ${prompt})" 2>/dev/null || \
          echo "podcast-triage: reader did not complete (see journalctl)"
      '';
    };

    systemd.timers.podcast-triage = {
      description = "Weekly podcast triage";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.schedule;
        Persistent = true;
        RandomizedDelaySec = "10m";
      };
    };
  };
}
