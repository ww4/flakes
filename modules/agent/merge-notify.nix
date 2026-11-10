# merge-notify — the forge tells us a PR merged, instead of us going to look.
#
# THE PROBLEM, in Chris's words on 2026-11-09: "You have a terrible time
# detecting when I merge a PR. I have instructed you to check, but that seems to
# fail 90% of the time. So you wind up building on a branch then rebasing once
# you do figure it out." Measured the same afternoon: flakes #489, #490 and #491
# were all merged while the session's own notes still said #489 was open.
#
# Checking is a poll a model has to remember to run, and a poll it forgets is
# indistinguishable from a quiet forge. So: Forgejo pushes.
#
#   ONE org-level webhook on ww4 (every repo, including ones not created yet)
#     -> merge-notify, loopback-only on 127.0.0.1:8794, HMAC-verified
#     -> one sanitised line appended to /var/lib/merge-notify/merges.log
#     -> a pane runs `merge-watch` under the Monitor tool; each line arrives
#        in the conversation as an event.
#
# The log doubles as the history a cold session reads (`merge-watch --recent`),
# which is the half a live stream cannot cover: no pane is armed at 03:00.
#
# ⚠️ THE HOOK REGISTERS ITSELF. The webhook is not declarative in Forgejo, so
# the provision unit creates it through the API (idempotent, matched by URL) and
# re-points it if the secret has been rotated. Nobody has to remember a manual
# step after this merges — which is the same class of failure the module exists
# to fix.
#
# ⚠️ A LINE IS A WAKE-UP, NOT A FACT. PR titles are free text that lands in a
# model's context. Fields are validated or charset-filtered (see
# merge-notify.py), but the right reaction to a line is still to go read git or
# the API, never to act on what the line claims.
{ config, lib, pkgs, ... }:

let
  port = 8794;                      # 8790/8791/8793/8799 are taken on gromit
  stateDir = "/var/lib/merge-notify";
  logFile = "${stateDir}/merges.log";
  secretFile = "${stateDir}/hook-secret.txt";
  markerFile = "${stateDir}/registered-hook.txt";
  forge = "https://git.rosemaryacres.com";
  org = "ww4";
  hookUrl = "http://127.0.0.1:${toString port}/forgejo";

  merge-notify = pkgs.writers.writePython3Bin "merge-notify" {
    flakeIgnore = [ "E501" "E203" "W503" "W504" ];
  } (builtins.readFile ./merge-notify.py);

  # The consumer side. `merge-watch` is what a pane hands to the Monitor tool,
  # so its behaviour is the contract: one line per event, nothing else on
  # stdout, and it must survive the log being rotated or absent.
  # The consumer side. `merge-watch` is what a pane hands to the Monitor tool,
  # so its behaviour is the contract: one line per event, nothing else on
  # stdout, and it must survive the log being rotated or absent.
  #
  # ⚠️ IT FILTERS BY STAKE, AND THAT IS THE WHOLE POINT. Chris, 2026-11-10:
  # *"I have much sessions that are all armed and all watching for all PRs. Now
  # every time I merge anything multiple sessions respond to it even though all
  # but one of them have nothing to do with it. This may be a structural
  # issue."* It was structural: the subscription is ORG-WIDE (one webhook, one
  # log) while relevance is PER-SESSION. Every armed pane was a consumer of a
  # broadcast, so one merge produced N reactions and N-1 of them were noise.
  #
  # The fix is ownership, not a quieter firehose. `fj` records each PR it opens
  # in a session-scoped ledger under $XDG_RUNTIME_DIR/merge-watch/, and this
  # reports a merge ONLY if that PR is in this session's ledger — then drops the
  # entry, so a pane stops being woken once its own work has landed. Exactly one
  # session reacts to any given merge: the one that opened it. `--all` restores
  # the firehose for a pane that really wants it.
  #
  # ⚠️ A "your base moved" rule was written and then CUT, because it recreated
  # the same problem: a session holding one open flakes PR collected eight
  # wake-ups from an unrelated session merging notes work. A base-moved push is
  # not actionable anyway — you cannot rebase usefully mid-edit, and by the time
  # you push, more will have landed. That check belongs at push/build time,
  # where the answer is needed, not in a notification stream.
  merge-watch = pkgs.writeShellApplication {
    name = "merge-watch";
    runtimeInputs = with pkgs; [ coreutils gnugrep ];
    text = ''
      recent=""
      all=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --recent) recent="''${2:-20}"; shift 2 ;;
          --all)    all=1; shift ;;
          -h|--help)
            echo "merge-watch [--all] [--recent N]"
            echo "  report merges in ${org}/* that THIS session has a stake in"
            echo "  --recent N   print the last N instead of streaming"
            echo "  --all        every merge, including ones nothing here opened"
            exit 0 ;;
          *) echo "merge-watch: unknown argument $1" >&2; exit 2 ;;
        esac
      done

      ledger="''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/merge-watch/''${CLAUDE_CODE_SESSION_ID:-nosession}.prs"
      # Overridable so the filter can be exercised against a fixture log. The
      # first version hardcoded the path, so a test that set MERGE_LOG silently
      # read the REAL log and "passed" while proving nothing.
      log="''${MERGE_LOG:-${logFile}}"

      # ⚠️ An absent log is ambiguous — "no merges yet" or "the receiver is
      # dead". Say which, once, on stdout, so a silent watch is never mistaken
      # for a quiet forge.
      if [ ! -e "$log" ]; then
        echo "merge-watch: $log absent — is merge-notify.service running?"
      fi

      # No stake recorded means nothing to report, and saying so is the honest
      # answer — streaming every merge "just in case" is the behaviour that
      # made one merge wake five panes.
      if [ -z "$all" ] && [ ! -s "$ledger" ]; then
        echo "merge-watch: this session has opened no PRs, so it has no stake in any merge."
        echo "merge-watch: it will start reporting once you open one (fj records it). --all streams every merge."
        exit 0
      fi

      report() {   # report <log line>
        local line=$1 ref
        # ⚠️ `if`, not `[ -n "$all" ] && { ...; }`. writeShellApplication injects
        # `set -o errexit`, and an `A && B` STATEMENT whose A is false exits 1 —
        # which kills the shell. The first version of this filter did exactly
        # that and silently reported nothing at all for every session that had
        # not passed --all: a filter that fails closed, which is the original
        # bug wearing a disguise. Caught only because the test asserted a
        # POSITIVE expectation instead of looking for absence of errors.
        if [ -n "$all" ]; then printf '%s\n' "$line"; return; fi
        # field 3 of a line is the ref: owner/repo#N
        ref=$(printf '%s\n' "$line" | cut -d" " -f3)
        case "$ref" in */*#*) : ;; *) return 0 ;; esac
        # ⚠️ `return 0`, never a bare `return`. A bare one hands back the status
        # of the last command — here a non-matching `grep`, i.e. 1 — and under
        # errexit a function that returns non-zero AS A LOOP BODY STATEMENT
        # kills the shell. That is what the third rewrite of this filter did:
        # it processed the first log line, found it irrelevant, and exited. The
        # stream then looked exactly like a quiet forge.
        grep -qxF "$ref" "$ledger" 2>/dev/null || return 0
        printf '%s\n' "$line"
        # The stake is discharged: this session asked about that PR and now
        # knows. Leaving it would keep the entry matching nothing forever.
        # Same errexit trap, and worse here: discharging the LAST entry makes
        # `grep -v` output nothing and exit 1, so the `&&` form died exactly
        # when the ledger emptied — the one moment it has something to say.
        if grep -vxF "$ref" "$ledger" > "$ledger.tmp" 2>/dev/null || true; then
          mv "$ledger.tmp" "$ledger"
        fi
        [ -s "$ledger" ] || echo "merge-watch: that was the last tracked PR for this session; now idle."
        return 0
      }

      if [ -n "$recent" ]; then
        tail -n "$recent" "$log" 2>/dev/null | while IFS= read -r l; do report "$l"; done
      else
        # -F, not -f: follow by name, so a rotated or recreated log is picked
        # up instead of silently tailing an unlinked inode forever.
        tail -n 0 -F "$log" | while IFS= read -r l; do report "$l"; done
      fi
    '';
  };
in
{
  environment.systemPackages = [ merge-notify merge-watch ];

  # ⚠️ WITHOUT THIS, NOTHING IS DELIVERED — and the failure is only in Forgejo's
  # journal, never at the receiver. Measured 2026-11-09 against the real hook:
  #
  #   Unable to deliver webhook task[1] ... webhook can only call allowed HTTP
  #   servers (check your webhook.ALLOWED_HOST_LIST setting), deny
  #   '127.0.0.1(127.0.0.1:8794)'
  #
  # Forgejo's default is `external`, which blocks loopback AND private ranges —
  # so binding to the tailnet IP instead would be refused the same way. The hook
  # itself fired correctly; only the destination was vetoed.
  #
  # The value REPLACES the list, it does not extend it (same shape as nginx's
  # `types {}` clobbering the MIME map), so `external` is restated here rather
  # than lost. No external webhooks exist on this forge today; dropping it
  # silently would be a trap for whoever adds the first one.
  services.forgejo.settings.webhook.ALLOWED_HOST_LIST = "external,loopback";

  systemd.services.merge-notify = {
    description = "Forgejo pull_request webhook receiver (merge notifications)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    serviceConfig = {
      ExecStart = "${merge-notify}/bin/merge-notify";
      Restart = "always";
      RestartSec = 5;

      # Runs as claude because claude is the consumer: the log and the state
      # directory are then readable without a group dance, and nothing here
      # needs privilege. StateDirectory (not DynamicUser) on purpose — a
      # DynamicUser state dir lives under /var/lib/private, which claude
      # cannot traverse whatever the file mode says (learned in queue-replies).
      User = "claude";
      Group = "users";
      StateDirectory = "merge-notify";
      StateDirectoryMode = "0750";

      # A secret must exist before the first delivery, and it must not depend
      # on the provision unit having run — otherwise a forge that is down at
      # boot leaves the receiver accepting nothing.
      ExecStartPre = pkgs.writeShellScript "merge-notify-secret" ''
        set -euo pipefail
        export PATH=${lib.makeBinPath (with pkgs; [ coreutils ])}
        if [ ! -s ${secretFile} ]; then
          head -c 32 /dev/urandom | base64 \
            | tr -dc 'A-Za-z0-9' | head -c 40 > ${secretFile}
          chmod 600 ${secretFile}
        fi
      '';

      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ProtectHome = true;            # the receiver needs no credential at all
      ReadWritePaths = [ stateDir ];
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      RestrictRealtime = true;
      LockPersonality = true;
      MemoryMax = "128M";
    };
    environment = {
      MERGE_LOG = logFile;
      MERGE_SECRET_FILE = secretFile;
      MERGE_PORT = toString port;
    };
  };

  systemd.services.merge-notify-provision = {
    description = "Register the ${org} org webhook that feeds merge-notify";
    wantedBy = [ "multi-user.target" ];
    after = [ "merge-notify.service" "forgejo.service" "network-online.target" ];
    wants = [ "network-online.target" ];
    requires = [ "merge-notify.service" ];
    path = with pkgs; [ curl jq coreutils ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # Forgejo may not be answering yet at boot, and a missed registration is
      # silent for days. Retry rather than fail once and stay failed.
      Restart = "on-failure";
      RestartSec = 120;
      # As claude, with HOME set: the ww4-bot Forgejo token lives in claude's
      # config and a unit inherits no HOME. Same arrangement as lock3-deploy.
      User = "claude";
      Group = "users";
      ProtectHome = false;
      NoNewPrivileges = true;
      PrivateTmp = true;
    };
    environment.HOME = "/home/claude";
    script = ''
      set -euo pipefail
      . "$HOME/.config/ww4-bot/forgejo-token.env"
      api=${forge}/api/v1
      auth="Authorization: token $FORGEJO_BOT_TOKEN"

      secret=$(cat ${secretFile})
      want=$(printf '%s' "$secret" | sha256sum | cut -d' ' -f1)

      body=$(jq -n --arg url '${hookUrl}' --arg secret "$secret" '{
        type: "gitea", active: true, events: ["pull_request"],
        config: { url: $url, content_type: "json",
                  http_method: "post", secret: $secret }
      }')

      # ⚠️ No `| head -1` on a jq pipeline under `set -o pipefail`: head closing
      # the pipe early kills jq with SIGPIPE and fails the unit on the SUCCESS
      # path. `first` inside jq has no such edge.
      existing=$(curl -fsS -H "$auth" "$api/orgs/${org}/hooks" \
        | jq -r --arg u '${hookUrl}' \
            '[.[] | select(.config.url == $u) | .id] | first // empty')

      if [ -z "$existing" ]; then
        id=$(printf '%s' "$body" | curl -fsS -X POST -H "$auth" \
               -H 'Content-Type: application/json' --data-binary @- \
               "$api/orgs/${org}/hooks" | jq -r '.id')
        echo "merge-notify: created ${org} org hook $id -> ${hookUrl}"
        printf '%s %s\n' "$id" "$want" > ${markerFile}
      else
        have=$(cut -d' ' -f2 ${markerFile} 2>/dev/null || true)
        if [ "$have" != "$want" ]; then
          # The API never gives the secret back, so a marker file is the only
          # way to know the hook still holds the one the receiver verifies
          # against. Mismatch (or a fresh state dir) => re-point it.
          printf '%s' "$body" | curl -fsS -X PATCH -H "$auth" \
            -H 'Content-Type: application/json' --data-binary @- \
            "$api/orgs/${org}/hooks/$existing" > /dev/null
          echo "merge-notify: re-pointed ${org} org hook $existing (secret rotated)"
          printf '%s %s\n' "$existing" "$want" > ${markerFile}
        else
          echo "merge-notify: ${org} org hook $existing already current"
        fi
      fi
    '';
  };
}
