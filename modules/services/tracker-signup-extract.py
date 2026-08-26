"""Extract tracker stats from an announcement with `claude -p`, then validate.

Reads the announcement text on stdin, writes one validated JSON object to
stdout. Never raises: on any failure it emits `{}` and explains itself on
stderr, because a stats lookup that fails must degrade the notification, not
suppress it.

WHY AN LLM. These posts are free-form prose written by whoever runs the
tracker, and the numbers live in emoji-laden stat blocks with no consistent
labelling. A proximity regex over them is not merely imprecise, it is wrong in
ways that read as plausible -- measured against the live feed, a five-minute
regex reported Seedpool as "4.19 torrents / 230,050 users" (it had grabbed the
PiB figure and a per-category TV-show count) and read a tracker's minimum-ratio
requirement as its total size. Confident wrong numbers are worse than none.

WHAT THE MODEL IS AND IS NOT TRUSTED FOR. It is trusted to read stated values
out of messy prose -- that is the one thing it is better at than a parser. It
is NOT trusted to be right: everything it returns goes through validate()
below, which drops any field that is malformed, out of physical range, or
internally inconsistent. A dropped field becomes "not stated", which is always
a safe answer here.

The model is also the only component that can answer the question that matters
most for fit, and which no tag lookup gets right: a tracker run by a Romanian
team whose content and site language are English is an ENGLISH tracker.
Prowlarr's `language: ro-RO` describes the operators, and taking it at face
value is what caused SeedCore to be wrongly skipped twice.
"""

import json
import os
import re
import subprocess
import sys

MODEL = os.environ.get("EXTRACT_MODEL", "claude-sonnet-5")
TIMEOUT = int(os.environ.get("EXTRACT_TIMEOUT", "180"))

# Tools are all disabled: this is pure text-in/JSON-out, so a tool call would
# only be a way for the run to wander, hang, or touch the filesystem.
NO_TOOLS = "Bash,Read,Write,Edit,WebFetch,WebSearch,Glob,Grep,Task,NotebookEdit"

PROMPT = """\
Extract facts from this private-tracker signup announcement. Output ONLY a \
JSON object, no prose, no markdown fence.

Schema (use null for anything NOT EXPLICITLY STATED in the text):
{"torrents":int|null,"users":int|null,"size_tib":number|null,"seeders":int|null,
 "leechers":int|null,"hit_and_run":string|null,"min_seed_days":number|null,
 "min_ratio":number|null,"content":string|null,"language":string|null,
 "signup_type":string|null,"freeleech":string|null}

RULES:
- Copy numbers EXACTLY as stated. Never estimate, infer, round, or compute.
- If a number is absent, use null. Do not guess from context. A post with no
  statistics must return null for every numeric field -- that is a correct
  answer, not a failure.
- Do not confuse a per-category count (e.g. "230,050 TV Shows") with a total,
  and do not confuse a REQUIREMENT (e.g. "you must keep a 1.0 ratio",
  "50 GB minimum upload") with a tracker-wide statistic.
- NEVER add numbers together. If the post gives "Active torrents: 5,881" and
  "Inactive torrents: 120,327", report torrents = 5881 (the ACTIVE figure --
  dead torrents are not a usable library). Do not report their sum. Only use a
  total when the post states a total itself.
- size_tib: total size of the tracker's content. Convert only when the unit is
  explicitly given (1 PiB = 1024 TiB). Otherwise null.
- hit_and_run: quote the seeding requirement verbatim, condensed to one line.
- language: the tracker's PRIMARY CONTENT/SITE language. Two directions, and
  both matter:
  * The announcement is almost always written in English. That tells you
    NOTHING about the tracker's content. Never answer "English" merely because
    the post is in English.
  * If the post describes the tracker as belonging to a country or region
    ("Argentinian general private tracker", "Turkish", "Israeli"), report that
    country's language -- unless the post ALSO says the content or site is in
    English, which overrides the nationality.
  * A tracker explicitly described as English-language, or one whose stated
    content is plainly English-market (e.g. "HD releases", "0DAY/SCENE") with
    no national framing, is "English".
  * If genuinely unclear, null.
"""

# Physical plausibility bounds. Anything outside these is dropped rather than
# reported: a number that cannot be true is worse than a missing one, because
# it will be believed. Bounds are deliberately loose -- the job is catching
# nonsense (a ratio read as a torrent count), not second-guessing real figures.
BOUNDS = {
    "torrents": (0, 50_000_000),
    "users": (0, 20_000_000),
    "size_tib": (0, 500_000),
    "seeders": (0, 200_000_000),
    "leechers": (0, 50_000_000),
    "min_seed_days": (0, 365),
    "min_ratio": (0, 50),
}

# Fields that count discrete things and therefore cannot be fractional. A
# fractional "torrent count" is the exact shape of a size figure being read as
# a count -- the regex this replaces reported Seedpool as "4.19 torrents",
# having picked up its 4.19 PiB. In range, wrong type, still nonsense.
COUNT_FIELDS = ("torrents", "users", "seeders", "leechers")

TEXT_FIELDS = ("hit_and_run", "content", "language", "signup_type", "freeleech")


def strip_fence(s):
    """Remove a ```json ... ``` wrapper.

    Observed in 3 of 4 live runs despite the prompt forbidding it -- so this
    is the normal case, not an edge case, and must not be treated as an error.
    """
    s = s.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        return m.group(1).strip()
    # Fall back to the outermost braces, in case of a stray preamble.
    i, j = s.find("{"), s.rfind("}")
    return s[i:j + 1] if i != -1 and j > i else s


def validate(raw):
    """Return only the fields that survive scrutiny. Never raises.

    Every field is independently droppable: one bad number must not discard
    the good ones alongside it.
    """
    if not isinstance(raw, dict):
        return {}, ["extractor did not return a JSON object"]

    out, dropped = {}, []

    for key, (lo, hi) in BOUNDS.items():
        v = raw.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            dropped.append("%s (not a number: %r)" % (key, v))
            continue
        if not (lo <= v <= hi):
            dropped.append("%s (%s outside %s-%s)" % (key, v, lo, hi))
            continue
        if key in COUNT_FIELDS:
            if float(v) != int(v):
                dropped.append("%s (%s is fractional; counts are whole)" % (key, v))
                continue
            v = int(v)
        out[key] = v

    for key in TEXT_FIELDS:
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = re.sub(r"\s+", " ", v.strip())[:200]

    # Cross-check: mean torrent size must be physically sensible. This is the
    # check that catches a confidently-paired hallucination, where each number
    # looks fine alone but they cannot describe the same tracker. Verified
    # against real posts: SeedCore 3.9 GiB, Seedpool 4.9 GiB per torrent.
    t, s = out.get("torrents"), out.get("size_tib")
    if t and s:
        mean_gib = (s * 1024.0) / t
        if not (0.001 <= mean_gib <= 500):
            dropped.append(
                "size_tib (%.2f TiB over %d torrents = %.3f GiB each, implausible)"
                % (s, t, mean_gib))
            out.pop("size_tib")

    # Seeders below torrents is possible (dead torrents); seeders far above
    # leechers is normal and healthy. Only a seeders/torrents ratio that is
    # absurd on its face indicates a mixed-up figure.
    t, sd = out.get("torrents"), out.get("seeders")
    if t and sd and t > 0 and sd / t > 10_000:
        dropped.append("seeders (%d over %d torrents, implausible)" % (sd, t))
        out.pop("seeders")

    return out, dropped


def main():
    text = sys.stdin.read().strip()
    if not text:
        print("{}")
        sys.stderr.write("extract: empty input\n")
        return

    # Bound the input: these posts are short, and an enormous one is either a
    # feed bug or an attempt to spend tokens.
    text = text[:12000]

    try:
        proc = subprocess.run(
            ["claude", "-p", PROMPT + "\n--- ANNOUNCEMENT ---\n" + text,
             "--model", MODEL,
             "--disallowedTools", NO_TOOLS],
            capture_output=True, text=True, timeout=TIMEOUT,
            # `claude -p` waits ~3s for stdin it will never get, and that delay
            # is per-call. Closing stdin explicitly removes it.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        print("{}")
        sys.stderr.write("extract: timed out after %ds\n" % TIMEOUT)
        return
    except (OSError, ValueError) as e:
        print("{}")
        sys.stderr.write("extract: could not run claude: %s\n" % e)
        return

    if proc.returncode != 0:
        print("{}")
        sys.stderr.write("extract: claude exited %d: %s\n"
                         % (proc.returncode, (proc.stderr or "").strip()[:300]))
        return

    try:
        raw = json.loads(strip_fence(proc.stdout))
    except ValueError:
        print("{}")
        sys.stderr.write("extract: unparseable output: %s\n"
                         % proc.stdout.strip()[:300])
        return

    stats, dropped = validate(raw)
    for d in dropped:
        sys.stderr.write("extract: DROPPED %s\n" % d)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
