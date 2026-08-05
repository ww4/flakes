#!/usr/bin/env python3
"""arr-settings — converge Sonarr/Radarr/Prowlarr app settings to a declared state.

WHY: these apps keep their configuration in SQLite, not in files, so it cannot be
managed the way the rest of this host is. Every setting tuned through a UI or API
is invisible to the flake and lost if a config volume is ever recreated. Over one
afternoon we accumulated four such settings (propers/repacks, per-indexer minimum
seeders, download-client removal, qBittorrent seeding) with nothing recording
them but a chat log.

This walks the declared state, compares it with what each app currently reports,
and PUTs back only what differs. Converged runs make no writes at all, so it is
safe to run on a timer purely to catch drift.

DELIBERATELY NOT MANAGED: quality profiles, custom formats, quality definitions.
Recyclarr owns those — two tools writing the same resource would fight forever.
"""
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 30
DRY_RUN = False          # set from argv; when true nothing is ever PUT


def api(base, ver, key, path, method="GET", body=None):
    if DRY_RUN and method != "GET":
        return {}
    req = urllib.request.Request(
        f"{base}/api/{ver}/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
        raw = f.read()
        return json.loads(raw) if raw else {}


def field_of(resource, name):
    """Read a Prowlarr-style fields[] entry."""
    for f in resource.get("fields", []):
        if f.get("name") == name:
            return f.get("value")
    return None


def set_field(resource, name, value):
    for f in resource.get("fields", []):
        if f.get("name") == name:
            f["value"] = value
            return True
    resource.setdefault("fields", []).append({"name": name, "value": value})
    return True


def converge(app, spec, changes, problems):
    base, ver = spec["url"], spec["apiVersion"]
    key = os.environ.get(spec["keyEnv"], "")
    if not key:
        problems.append(f"{app}: {spec['keyEnv']} not set — skipped")
        return
    try:
        # --- config/* singletons (e.g. mediamanagement) ---------------------
        for section, wanted in spec.get("config", {}).items():
            cur = api(base, ver, key, f"config/{section}")
            delta = {k: v for k, v in wanted.items() if cur.get(k) != v}
            if delta:
                cur.update(delta)
                api(base, ver, key, f"config/{section}", "PUT", cur)
                for k, v in delta.items():
                    changes.append(f"{app} config/{section}.{k} -> {v}")

        # --- download clients (list resource) --------------------------------
        wanted = spec.get("downloadClients", {})
        if wanted:
            for dc in api(base, ver, key, "downloadclient"):
                delta = {k: v for k, v in wanted.items() if dc.get(k) != v}
                if delta:
                    dc.update(delta)
                    api(base, ver, key, f"downloadclient/{dc['id']}", "PUT", dc)
                    for k, v in delta.items():
                        changes.append(f"{app} downloadclient[{dc['name']}].{k} -> {v}")

        # --- indexer fields, with a default + per-name overrides -------------
        for fname, rule in spec.get("indexerFields", {}).items():
            for ix in api(base, ver, key, "indexer"):
                want = rule.get("byName", {}).get(ix["name"], rule.get("default"))
                if want is None or field_of(ix, fname) == want:
                    continue
                set_field(ix, fname, want)
                api(base, ver, key, f"indexer/{ix['id']}", "PUT", ix)
                changes.append(f"{app} indexer[{ix['name']}].{fname.split('.')[-1]} -> {want}")
    except urllib.error.URLError as e:
        # A single app being down must never fail the whole run.
        problems.append(f"{app}: unreachable ({e.reason}) — skipped, will retry next run")
    except Exception as e:                                    # noqa: BLE001
        problems.append(f"{app}: {type(e).__name__}: {e}")


def main():
    global DRY_RUN
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    DRY_RUN = "--dry-run" in sys.argv
    spec = json.load(open(args[0]))
    changes, problems = [], []
    for app, s in spec.items():
        converge(app, s, changes, problems)
    tag = "WOULD CHANGE" if DRY_RUN else "CHANGED "
    for line in changes:
        print(f"{tag} {line}")
    for line in problems:
        print(f"PROBLEM  {line}", file=sys.stderr)
    if not changes and not problems:
        print("converged — no changes needed")
    # Drift is worth surfacing: something outside the flake moved a setting back.
    print(f"\nsummary: {len(changes)} change(s), {len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
