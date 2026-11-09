#!/usr/bin/env python3
"""Append the auto-refresh snippet to Tandoor's compiled Vue entry chunk.

    tandoor-auto-refresh.py <frontend-out-dir> <snippet.js>

The entry chunk is found through Vite's own manifest.json (the one record with
"isEntry": true) rather than a filename glob, because the chunk name carries a
content hash that changes every release. Exits non-zero on anything it does not
recognise, so a Tandoor upgrade that reshapes the build fails loudly instead of
silently dropping the feature.
"""
import json
import pathlib
import sys

MARKER = "hl-recipe-scroll"   # appears in the snippet; makes this idempotent

if len(sys.argv) != 3:
    sys.exit(__doc__)
out, snippet_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

manifest_path = out / "manifest.json"
if not manifest_path.is_file():
    sys.exit(f"tandoor-auto-refresh: no {manifest_path} — build layout changed")

manifest = json.loads(manifest_path.read_text())
entries = [v["file"] for v in manifest.values() if v.get("isEntry")]
if len(entries) != 1:
    sys.exit(f"tandoor-auto-refresh: expected 1 entry chunk, found {entries}")

target = out / entries[0]
if not target.is_file():
    sys.exit(f"tandoor-auto-refresh: entry chunk {target} missing")

src = target.read_text()
if MARKER in src:
    sys.exit(f"tandoor-auto-refresh: {target.name} already patched")

snippet = snippet_path.read_text()
if MARKER not in snippet:
    sys.exit("tandoor-auto-refresh: snippet lost its marker — refusing to append")

# A trailing newline matters: the chunk may end in a line comment or a
# sourcemap pragma, which would otherwise swallow the first appended line.
target.write_text(src + "\n;" + snippet + "\n")
print(f"tandoor-auto-refresh: appended {len(snippet)} bytes to {entries[0]}")
