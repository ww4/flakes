# gromit-sentinel — Phase 1: a lightweight watchdog that detects trouble and
# notifies (NO autonomous action yet — that's Phase 2/3).
#
# Tier 1 (this module): a cheap check runs every 2 min (systemd timer). It reads
# its checks from a config file, debounces + dedups, and on a confirmed NEW
# incident gathers a bit of evidence and posts to ntfy. The hand-off point where
# Phase 2 will call `claude -p` is marked in the script.
#
# Edit `sentinelConfig` below to change what it watches — it renders to
# /etc/sentinel/config.json, which the watcher reads each run (no code change
# needed to add/remove checks, just edit the attrset + deploy).
#
# Quick test without waiting for a real failure: trigger the built-in `selftest`
# marker check —  `touch /run/sentinel/fire-test`  (as the claude user, or
# `sudo touch ...`). Within ~2 min you get a "🔍 Sentinel: selftest" ntfy, the
# marker auto-clears, and ~2 min later a "✅ resolved" — exercising the whole
# detect → debounce → notify → resolve pipeline. Nothing actually wrong.
{ config, lib, pkgs, ... }:

let
  # ─────────────────────────── EDIT ME ───────────────────────────
  # The watcher config. `checks` is a list; each has an id, a type, and
  # type-specific fields. Common optional fields: enabled (default true),
  # severity (test|info|warning|critical), minConsecutive (debounce override),
  # cooldownSec (re-alert suppression override).
  sentinelConfig = {
    enabled = true;
    pollSec = 120;          # informational; the systemd timer drives the cadence
    debounce = 2;           # a condition must persist this many checks before it escalates
    cooldownSec = 7200;     # don't re-escalate the same incident within 2 h
    maxPerHour = 6;         # global rate limits (cost + anti-storm guard)
    maxPerDay = 30;
    ntfyServer = "http://127.0.0.1:8090";
    ntfyTopic = "gromit-alerts";

    # Phase 2: checks flagged `agent = true` get a read-only `claude -p`
    # diagnosis (per /etc/sentinel/playbook.md) after the detection notice.
    # agentEnabled is the master kill-switch for that layer (false => Phase-1
    # notify-only behaviour for every check).
    agentEnabled = true;
    agentTimeout = 300;     # seconds; claude -p is killed past this

    # Phase 3: checks flagged `act = true` (and only when agent = true) MAY take
    # one bounded corrective action (whitelisted restart or a Chris-gated fix
    # PR). actEnabled is the master kill-switch for ACTING (false => diagnose +
    # notify only, even for act-flagged checks). Acting is further bounded by a
    # daily cap and a per-incident action cooldown (don't re-act on a recurrence).
    actEnabled = true;
    maxActionsPerDay = 5;
    actionCooldownSec = 86400;   # after acting on a check, won't act again on it for 24 h (recurrence => escalate)

    checks = [
      # Any systemd unit in the failed state (excluding known-noisy ones).
      { id = "failed-units"; type = "failed-units"; severity = "warning"; exclude = [ ]; agent = true; act = true; }

      # comin couldn't build/eval/deploy/fetch — gromit silently stuck on the old gen.
      { id = "comin-deploy"; type = "comin"; severity = "warning"; agent = true; act = true; }

      # Example metric check (commented — enable + tune when wanted):
      # { id = "rootfs-full"; type = "metric"; severity = "warning"; agent = true;
      #   expr = "100 - (node_filesystem_avail_bytes{mountpoint=\"/\"} * 100 / node_filesystem_size_bytes{mountpoint=\"/\"})";
      #   op = ">"; threshold = 90; }

      # Built-in self-test (notify path only — no agent): fires when the marker
      # exists, then clears it. The "trigger on demand" hook for the pipeline.
      { id = "selftest"; type = "marker"; path = "/run/sentinel/fire-test";
        minConsecutive = 1; clearAfter = true; severity = "test"; agent = false;
        message = "Synthetic sentinel self-test — pipeline is working, nothing is wrong."; }

      # Agent diagnosis-path self-test (agent on, act OFF): verifies claude -p
      # diagnoses AND that it does NOT act when acting isn't permitted.
      # `touch /run/sentinel/fire-agenttest`.
      { id = "agenttest"; type = "marker"; path = "/run/sentinel/fire-agenttest";
        minConsecutive = 1; clearAfter = true; severity = "test"; agent = true; act = false;
        message = "Synthetic AGENT-path test — exercise claude -p DIAGNOSIS only; do not act; nothing is wrong."; }

      # Action-path drill (agent on, act ON): exercises the Phase-3 act path with
      # a safe, reversible, Chris-gated action. `touch /run/sentinel/fire-acttest`.
      { id = "acttest"; type = "marker"; path = "/run/sentinel/fire-acttest";
        minConsecutive = 1; clearAfter = true; severity = "test"; agent = true; act = true;
        message = "DRILL — Phase-3 action-path test. Perform EXACTLY ONE safe action: in the ww4/flakes repo (~/flakes locally) open a Chris-gated PR — branch, append one timestamped line to SENTINEL-DRILLS.md at the repo root (create it if missing), push, open the PR via the ww4-bot Forgejo API, request `chris` as reviewer, and do NOT merge it — titled '[sentinel drill] action-path test'. Report the PR number. Nothing is actually wrong; take no other action."; }
    ];
  };
  # ─────────────────────────────────────────────────────────────────

  watcher = pkgs.writers.writePython3Bin "gromit-sentinel" {
    flakeIgnore = [ "E501" "W503" "W504" ];
  } ''
    import json
    import os
    import subprocess
    import sys
    import time
    import urllib.parse
    import urllib.request

    CONFIG = os.environ.get("SENTINEL_CONFIG", "/etc/sentinel/config.json")
    STATE = os.environ.get("SENTINEL_STATE", "/var/lib/sentinel/state.json")
    INCIDENT_DIR = os.environ.get("SENTINEL_INCIDENTS", "/var/lib/sentinel/incidents")


    def load_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return default


    def save_json(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


    def http_get(url, timeout=5):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode()


    def sh(cmd, timeout=15):
        try:
            return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None


    def check_failed_units(c):
        exclude = set(c.get("exclude", []))
        r = sh(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])
        lines = r.stdout.splitlines() if (r and r.stdout) else []
        units = [ln.split()[0] for ln in lines if ln.split()]
        units = [u for u in units if u not in exclude]
        return (len(units) > 0, "failed: " + ", ".join(units))


    def check_comin(c):
        try:
            text = http_get("http://127.0.0.1:4243/metrics")
        except Exception:
            return (False, "")
        bad = []
        for line in text.splitlines():
            if line.startswith("comin_last_") and "_failed" in line:
                try:
                    val = float(line.rsplit(None, 1)[1])
                except (ValueError, IndexError):
                    continue
                if val >= 1:
                    bad.append(line.split("{")[0].split()[0])
        return (len(bad) > 0, "comin: " + ", ".join(sorted(set(bad))))


    def check_metric(c):
        op = c.get("op", ">")
        thr = float(c.get("threshold", 0))
        url = "http://localhost:9090/api/v1/query?query=" + urllib.parse.quote(c["expr"])
        try:
            data = json.loads(http_get(url))
        except Exception:
            return (False, "")
        hits = []
        for s in data.get("data", {}).get("result", []):
            try:
                v = float(s["value"][1])
            except (KeyError, IndexError, ValueError):
                continue
            ok = {">": v > thr, "<": v < thr, ">=": v >= thr, "<=": v <= thr, "==": v == thr}.get(op, False)
            if ok:
                m = s.get("metric", {})
                tag = m.get("device") or m.get("instance") or m.get("__name__") or ""
                hits.append(("%s=%g" % (tag, v)).strip("="))
        return (len(hits) > 0, "%s: %s" % (c["id"], ", ".join(hits)))


    def check_command(c):
        r = sh(c["cmd"], timeout=int(c.get("timeout", 15)))
        if r is None:
            return (False, "")
        fired = (r.returncode == int(c.get("fireOnExit", 0)))
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return (fired, (out[0][:200] if out else c.get("message", c["id"])) if fired else "")


    def check_marker(c):
        if os.path.exists(c["path"]):
            if c.get("clearAfter", True):
                try:
                    os.remove(c["path"])
                except OSError:
                    pass
            return (True, c.get("message", "synthetic test trigger"))
        return (False, "")


    DISPATCH = {
        "failed-units": check_failed_units,
        "comin": check_comin,
        "metric": check_metric,
        "command": check_command,
        "marker": check_marker,
    }


    def gather_evidence(c, detail):
        t = c["type"]
        lines = ["[%s] %s" % (c["id"], detail), ""]
        if t == "failed-units":
            for u in detail.replace("failed: ", "").split(", "):
                if not u:
                    continue
                s = sh(["systemctl", "status", u, "--no-pager", "-l", "-n", "20"])
                lines += ["### systemctl status %s" % u, (s.stdout if s else "")[:2000], ""]
        elif t == "comin":
            s = sh(["journalctl", "-u", "comin", "-n", "40", "--no-pager"])
            lines += ["### comin log tail", (s.stdout if s else "")[-2000:]]
        elif t == "command":
            r = sh(c["cmd"])
            lines += ["### command output", ((r.stdout or "") if r else "")[:2000]]
        elif t == "metric":
            lines += ["Prometheus: %s  %s %s" % (c.get("expr"), c.get("op", ">"), c.get("threshold"))]
        elif t == "marker":
            lines += ["Synthetic test incident. The detect -> debounce -> handler -> ntfy pipeline is working; nothing is actually wrong."]
        return "\n".join(lines)


    def ntfy(cfg, title, body, priority, tags):
        server = cfg.get("ntfyServer", "http://127.0.0.1:8090")
        topic = cfg.get("ntfyTopic", "gromit-alerts")
        # HTTP headers are latin-1, so the Title must be ASCII — emoji belong in
        # the Tags field (ntfy renders tag names as icons). Strip any stray
        # non-ASCII defensively so a notification can never fail to send.
        safe_title = title.encode("ascii", "ignore").decode().strip() or "Sentinel"
        req = urllib.request.Request(
            "%s/%s" % (server, topic), data=body.encode(),
            headers={"Title": safe_title, "Priority": str(priority), "Tags": tags})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print("ntfy post failed: %r" % e, file=sys.stderr)


    PRI = {"test": 2, "info": 2, "warning": 3, "critical": 4}
    TAG = {"test": "test_tube", "info": "information_source", "warning": "warning", "critical": "rotating_light"}


    def run_agent(cid, c, detail, path, cfg, act_permitted, recently_acted):
        # Phase 3: hand the incident to a headless `claude -p`. It diagnoses and,
        # only when act_permitted, may take ONE bounded action (whitelisted
        # restart or a Chris-gated PR) per the playbook. Returns the reply text,
        # or "" on timeout/failure (caller falls back to a plain notice).
        timeout = int(c.get("agentTimeout", cfg.get("agentTimeout", 300)))
        if act_permitted:
            permit = "ACTING IS PERMITTED for this incident (you may take ONE bounded action per the playbook, or escalate)."
        elif recently_acted:
            permit = "ACTING IS NOT PERMITTED: you acted on this within the last day and it has recurred — do NOT act again; diagnose and escalate to Chris."
        else:
            permit = "ACTING IS NOT PERMITTED for this incident — diagnose only and recommend any fix for Chris."
        prompt = (
            "You are gromit-sentinel's incident handler. Read /etc/sentinel/playbook.md "
            "and follow it EXACTLY.\n%s\n"
            "Incident:\n  check: %s (type %s, severity %s)\n  detail: %s\n  evidence file: %s\n"
            "Read the evidence file first, investigate read-only, then act-or-escalate per "
            "the playbook and reply in the required format (first line must be the ACTION: line)."
            % (permit, cid, c.get("type"), c.get("severity", "warning"), detail, path)
        )
        r = sh(["claude", "-p", prompt], timeout=timeout)
        if r is None or r.returncode != 0:
            return ""
        return (r.stdout or "").strip()


    def main():
        cfg = load_json(CONFIG, {})
        if not cfg.get("enabled", True):
            return
        now = time.time()
        debounce = int(cfg.get("debounce", 2))
        cooldown = int(cfg.get("cooldownSec", 7200))
        max_hour = int(cfg.get("maxPerHour", 6))
        max_day = int(cfg.get("maxPerDay", 30))
        max_actions = int(cfg.get("maxActionsPerDay", 5))
        act_cooldown = int(cfg.get("actionCooldownSec", 86400))

        state = load_json(STATE, {"checks": {}, "escalations": [], "actions": []})
        cstate = state.setdefault("checks", {})
        state["escalations"] = [t for t in state.get("escalations", []) if now - t < 86400]
        state["actions"] = [t for t in state.get("actions", []) if now - t < 86400]
        esc_hour = sum(1 for t in state["escalations"] if now - t < 3600)
        esc_day = len(state["escalations"])
        acts_today = len(state["actions"])

        os.makedirs(INCIDENT_DIR, exist_ok=True)

        for c in cfg.get("checks", []):
            if not c.get("enabled", True):
                continue
            fn = DISPATCH.get(c.get("type"))
            if fn is None:
                continue
            cid = c["id"]
            st = cstate.setdefault(cid, {"consecutive": 0, "active": False, "last_escalated": 0})
            try:
                fired, detail = fn(c)
            except Exception as e:
                print("check %s errored: %r" % (cid, e), file=sys.stderr)
                continue

            if not fired:
                if st.get("active"):
                    st["active"] = False
                    ntfy(cfg, "Sentinel resolved: %s" % cid, "%s cleared." % cid, 2, "white_check_mark")
                st["consecutive"] = 0
                continue

            st["consecutive"] = st.get("consecutive", 0) + 1
            if st.get("active"):
                continue
            if st["consecutive"] < int(c.get("minConsecutive", debounce)):
                continue
            if now - st.get("last_escalated", 0) < int(c.get("cooldownSec", cooldown)):
                continue
            if esc_hour >= max_hour or esc_day >= max_day:
                print("rate-limited, skipping %s" % cid, file=sys.stderr)
                continue

            # ── ESCALATE ──
            evidence = gather_evidence(c, detail)
            ts = int(now)
            path = os.path.join(INCIDENT_DIR, "%s-%d.txt" % (cid, ts))
            try:
                with open(path, "w") as f:
                    f.write(evidence)
            except OSError:
                path = "(could not write evidence file)"
            sev = c.get("severity", "warning")
            # 1) Immediate detection notice — don't make Chris wait on the agent.
            ntfy(cfg, "Sentinel: %s" % cid,
                 "%s\n\nEvidence: %s" % (detail, path),
                 PRI.get(sev, 3), TAG.get(sev, "warning"))
            # 2) Hand off to `claude -p`. Phase 3: an act-flagged check MAY take
            #    one bounded action when permitted; otherwise it diagnoses only.
            if cfg.get("agentEnabled", True) and c.get("agent", False):
                recently_acted = (now - st.get("last_action", 0)) < act_cooldown
                act_permitted = (cfg.get("actEnabled", True) and c.get("act", False)
                                 and acts_today < max_actions and not recently_acted)
                diag = run_agent(cid, c, detail, path, cfg, act_permitted, recently_acted)
                if diag:
                    try:
                        with open(path, "a") as f:
                            f.write("\n\n=== agent report ===\n" + diag)
                    except OSError:
                        pass
                    first = (diag.splitlines() or [""])[0].strip().upper()
                    acted = first.startswith("ACTION:") and "NONE" not in first
                    if acted:
                        st["last_action"] = now
                        state["actions"].append(now)
                        acts_today += 1
                    ntfy(cfg, "Sentinel: %s (%s)" % (cid, "acted" if acted else "diagnosed"),
                         diag[:1200], PRI.get(sev, 3), "wrench" if acted else "robot")
                else:
                    ntfy(cfg, "Sentinel: %s (agent unavailable)" % cid,
                         "claude -p produced no output or timed out; evidence at %s" % path, 3, "warning")
            st["active"] = True
            st["last_escalated"] = now
            state["escalations"].append(now)
            esc_hour += 1
            esc_day += 1

        save_json(STATE, state)


    if __name__ == "__main__":
        main()
  '';
in
{
  environment.etc."sentinel/config.json".text = builtins.toJSON sentinelConfig;

  # Prebaked instructions handed to `claude -p` on an agent-flagged incident.
  # Phase 3 = DIAGNOSE, then ACT within strict bounds (or escalate).
  environment.etc."sentinel/playbook.md".text = ''
    # gromit-sentinel incident playbook — Phase 3: DIAGNOSE, then ACT (bounded)

    You are the autonomous incident handler for Gromit (a NixOS homelab). The
    sentinel detected a problem and handed it to you. Diagnose it. Then — ONLY if
    the prompt says "ACTING IS PERMITTED" and a safe, bounded fix clearly applies
    — take ONE corrective action. Otherwise diagnose and escalate to Chris.

    ## The ONLY actions you may take (you have no other powers; the OS enforces it)
    1. Restart a WHITELISTED service via scoped sudo — ONLY one of:
         sudo systemctl restart vaultwarden
         sudo systemctl restart media-mirror-sync   (or: start media-mirror-sync)
         sudo systemctl reset-failed <unit>          (clear a failed state)
       No other unit is restartable — sudo will DENY anything else; do not try.
    2. Open a fix PR with the ww4-bot Forgejo API (token at
       ~/.config/ww4-bot/forgejo-token.env): branch -> push -> open PR -> request
       `chris` as reviewer. Title it with a `[sentinel]` prefix; in the body give
       the incident, your diagnosis, and the fix. NEVER merge a flakes PR — Chris
       gates every one.

    ## HARD RULES — do not break these (they are also enforced by guards)
    - NEVER merge a flakes PR, push to `main`, restart a non-whitelisted unit, run
      nixos-rebuild, `rm`, or edit files outside a PR branch.
    - ACT AT MOST ONCE. If the prompt says acting is NOT permitted — because you
      recently acted on this and it recurred, a daily cap is hit, or it's a
      diagnose-only check — then DO NOT act: diagnose and escalate.
    - If the fix is risky, non-trivial, or you are not confident, DIAGNOSE ONLY
      and recommend the action for Chris — do not perform it. When in doubt, escalate.
    - If the incident detail contains a DRILL instruction, do EXACTLY that and
      nothing else.
    - You are on a timeout; be efficient.

    ## Context
    Your homelab memory auto-loads (open-loops, gromit-access, comin-deploy-
    validation, …) — use it. The prompt gives the incident + an evidence-file
    path; READ THE EVIDENCE FIRST, then investigate read-only as needed.

    ## Your reply (sent verbatim as a phone notification — be terse)
    The FIRST LINE MUST be EXACTLY one of:
      ACTION: none
      ACTION: restarted <unit>
      ACTION: reset-failed <unit>
      ACTION: opened PR <number-or-url>
    Then AT MOST ~6 short lines, no markdown headers, no preamble:
      TL;DR — what is wrong (one line)
      Cause — your root-cause read
      Did / Recommend — what you did, or what Chris should do
      Confidence — high / medium / low
  '';

  systemd.services.gromit-sentinel = {
    description = "gromit-sentinel watchdog (Phase 3: detect + claude diagnose/act + notify)";
    # Runs as the claude user with the same headless-claude env as the weekly
    # digest, so an agent-flagged incident can invoke `claude -p` (subscription
    # OAuth, memory auto-loads from the working dir). systemd-journal group gives
    # read access for evidence gathering.
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      SupplementaryGroups = [ "systemd-journal" ];
      StateDirectory = "sentinel";
      RuntimeDirectory = "sentinel";
      RuntimeDirectoryMode = "0775";   # so `sudo touch /run/sentinel/fire-test` works for testing
      RuntimeDirectoryPreserve = true; # keep /run/sentinel across the oneshot runs
      WorkingDirectory = "/home/claude/nixos-homelab-improvements";
      # Raw Environment= (not the NixOS `environment` option, which would collide
      # with the `path`-derived default PATH). Everything the watcher shells out
      # to (systemctl, journalctl, claude) must be on this PATH. Mirrors digest.nix.
      Environment = [
        "HOME=/home/claude"
        "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
        "CLAUDE_AUTONOMOUS=1"   # the reflection Stop-hook no-ops in headless runs
      ];
      ExecStart = "${watcher}/bin/gromit-sentinel";
    };
  };

  systemd.timers.gromit-sentinel = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "${toString sentinelConfig.pollSec}s";
      Persistent = true;
    };
  };
}
