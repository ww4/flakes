#!/usr/bin/env python3
"""Report AI-writing tells in a markdown or text file.

Usage: python3 lint.py FILE [> lint.txt]

This is a REPORT, not a gate: exit status is always 0 and nothing is rewritten.
The editor pass reads it and decides. Every finding is a line of the form

    LINE: <flag> :: <sentence>

Checks that cannot be decided mechanically (tricolons, false ranges, passives,
metaphors, specificity) are flagged for a human to adjudicate rather than
asserted as errors. Python 3 stdlib only.
"""
import re
import sys
from collections import namedtuple

Sent = namedtuple("Sent", "line text para")
Para = namedtuple("Para", "line lines sents is_list")

# ---------------------------------------------------------------- parsing ---

ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "al",
    "fig", "no", "vol", "cf", "approx", "dept", "inc", "ltd", "co", "ca",
    "e.g", "i.e", "u.s", "u.k", "a.m", "p.m",
}

SENT_END = re.compile(r'(?<=[.!?])["\'”’)\]]*\s+')


def split_sentences(text):
    """Split on sentence enders, then glue back the false splits."""
    parts = SENT_END.split(text)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out:
            prev = out[-1]
            tail = prev.rsplit(None, 1)[-1] if prev.split() else ""
            bare = tail.rstrip('."\'”)').lower()
            # "Dr." / "e.g." / a lone initial keep the sentence open, and so
            # does a list marker -- but ONLY when the whole preceding segment is
            # that marker. Merging on any trailing digits swallowed every
            # sentence ending in a year ("...returning us to 1700."), which is
            # common in exactly the prose this lints.
            if (bare in ABBREV or re.fullmatch(r"[a-z]", bare)
                    or (re.fullmatch(r"\d{1,2}", bare)
                        and prev.strip().rstrip('.') == bare)):
                out[-1] = prev + " " + p
                continue
        out.append(p)
    return out


def parse(path):
    """Return (paragraphs, sentences, raw_lines), skipping code and headings."""
    raw = open(path, encoding="utf-8", errors="replace").read().split("\n")
    in_fence = False
    blocks, cur, cur_line = [], [], 0
    for i, ln in enumerate(raw, 1):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = ln.strip()
        # headings and horizontal rules break paragraphs but are not prose
        if not stripped or stripped.startswith("#") or re.fullmatch(r"[-*_]{3,}", stripped):
            if cur:
                blocks.append((cur_line, cur))
                cur, cur_line = [], 0
            continue
        if not cur:
            cur_line = i
        cur.append((i, ln))
    if cur:
        blocks.append((cur_line, cur))

    paras, sents = [], []
    for pidx, (start, lines) in enumerate(blocks):
        is_list = all(re.match(r"\s*([-*+]|\d+[.)])\s", l) for _, l in lines)
        body = " ".join(l.strip() for _, l in lines)
        body = re.sub(r"\s+", " ", body).strip()
        psents = []
        for s in split_sentences(body):
            # attribute the sentence to the line where its first words appear
            key = re.escape(s.split()[0]) if s.split() else ""
            lineno = start
            if key:
                for n, l in lines:
                    if re.search(r"\b" + key, l):
                        lineno = n
                        break
            sn = Sent(lineno, s, pidx)
            psents.append(sn)
            sents.append(sn)
        paras.append(Para(start, lines, psents, is_list))
    return paras, sents, raw


# ------------------------------------------------------------- utilities ---

def words(s):
    return re.findall(r"[A-Za-z'’-]+", s)


def emit(title, rule, lines, note=None):
    print("=" * 72)
    print("%s  (rule %s)" % (title, rule))
    print("=" * 72)
    if note:
        print(note)
    if not lines:
        print("  (none)")
    else:
        for ln in lines:
            print(ln)
    print()


def flag(s, label):
    return "%5d: %-22s :: %s" % (s.line, label, s.text)


def phrase_hits(sents, phrases, label=None):
    out = []
    for s in sents:
        low = s.text.lower()
        for p in phrases:
            if p in low:
                out.append(flag(s, label or p))
                break
    return out


def word_hits(sents, vocab, label):
    out = []
    for s in sents:
        found = [w for w in words(s.text) if w.lower() in vocab]
        if found:
            out.append(flag(s, "%s: %s" % (label, ",".join(sorted(set(found))))))
    return out


# ---------------------------------------------------------------- checks ---

INTENSIFIERS = {
    # rule 2
    "genuinely", "truly", "actually", "quietly", "seamlessly", "fundamentally",
    "crucially", "importantly", "notably", "crucial", "pivotal", "vital",
    "vibrant", "robust", "nuanced", "significant", "strategic", "key",
    "intricate", "multifaceted",
    # rule 1 abstract nouns
    "ecosystem", "infrastructure", "landscape", "framework", "realm",
    "paradigm", "trajectory", "tapestry", "testament", "interplay",
    # rule 3 verbs + the task's additional AI vocabulary
    "utilize", "facilitate", "demonstrate", "delve", "underpin", "leverage",
    "foster", "navigate", "elevate", "synthesize", "streamline", "enhance",
    "showcase", "garner", "underscore", "highlight", "highlights",
    "underscores", "showcases", "streamlines", "enhances", "garners",
    "fosters", "leverages",
}

PARTICIPLES = {
    "underscoring", "highlighting", "reflecting", "signaling", "signalling",
    "ensuring", "contributing", "showcasing", "emphasizing", "demonstrating",
    "illustrating", "marking", "cementing", "solidifying",
}

CONTRAST = [
    "isn't x, it's", "not x but", "it's not", "it isn't", "it is not",
    "this has nothing to do with", "rather than", "the real reason",
    "not just", "not only", "nothing to do with",
]

ANNOUNCERS = [
    "here's the thing", "it's important to", "it is important to",
    "it's worth noting", "it is worth noting", "what most people miss",
    "taken together", "this means that", "in this way", "in summary",
    "in conclusion", "the bottom line", "put simply", "at its core",
]

RESIDUE = [
    "i hope this helps", "let me know", "certainly", "great question",
    "you're absolutely right", "as an ai", "knowledge cutoff", "feel free to",
]

HEDGES = [
    "on balance", "both sides", "the truth lies", "it depends",
    "often but not always", "in many ways", "to some extent",
]

IRREGULAR_PP = {
    "known", "seen", "done", "made", "given", "taken", "shown", "written",
    "held", "built", "found", "told", "kept", "left", "sent", "brought",
    "thought", "caught", "bought", "taught", "sold", "felt", "meant", "put",
    "set", "lost", "won", "drawn", "grown", "chosen", "driven",
}

SUPERLATIVE = {
    "most", "least", "first", "only", "never", "every", "all", "best",
    "worst", "biggest", "largest", "smallest", "greatest", "highest", "lowest",
}
MULTIPLIER = {"twofold", "threefold", "tenfold", "double", "triple", "half", "quadruple"}


def check_edges(paras):
    counts = [len(p.sents) for p in paras]
    print("=" * 72)
    print("EDGES  (rules 12, 13b, 14, 19)")
    print("=" * 72)
    print("Sentences per paragraph: " + " ".join(str(c) for c in counts))
    runs = []
    i = 0
    while i < len(counts):
        j = i
        while j + 1 < len(counts) and max(counts[i:j + 2]) - min(counts[i:j + 2]) <= 1:
            j += 1
        if j - i + 1 >= 3:
            runs.append((i + 1, j + 1, counts[i:j + 1]))
        i = j + 1 if j > i else i + 1
    if runs:
        for a, b, c in runs:
            print("  FLAG rule 12/19: paragraphs %d-%d are within +/-1 sentence (%s)"
                  % (a, b, " ".join(map(str, c))))
    else:
        print("  no run of 3+ same-length paragraphs")
    print()
    print("FIRST sentence of each paragraph (rule 14 openers, rule 6):")
    for n, p in enumerate(paras, 1):
        if p.sents:
            print("  P%-3d %5d: %s" % (n, p.sents[0].line, p.sents[0].text))
    print()
    print("LAST sentence of each paragraph (rule 13b closers, rule 14 recaps):")
    for n, p in enumerate(paras, 1):
        if p.sents:
            print("  P%-3d %5d: %s" % (n, p.sents[-1].line, p.sents[-1].text))
    print()


def check_oneline(paras):
    out = []
    for n, p in enumerate(paras, 1):
        if len(p.sents) == 1 and not p.is_list:
            label = "P%d one-sentence" % n + (" (OPENER)" if n == 1 else "")
            out.append(flag(p.sents[0], label))
    return out


def check_contrast(sents):
    out = phrase_hits(sents, CONTRAST, None)
    seen = set(o[:5] for o in out)
    # the two-sentence countdown: short "It's ..." follow-up under 12 words
    for a, b in zip(sents, sents[1:]):
        if a.para != b.para:
            continue
        if re.match(r"(It's|It is|They're|They are)\b", b.text) and len(words(b.text)) < 12:
            key = "%5d" % b.line
            if key not in seen:
                out.append(flag(b, "countdown after P-sent"))
    return out


def check_qa(sents):
    out = []
    for a, b in zip(sents, sents[1:]):
        if a.para == b.para and a.text.endswith("?") and len(words(a.text)) <= 6 \
                and len(words(b.text)) <= 6:
            out.append(flag(a, "Q fragment"))
            out.append(flag(b, "A fragment"))
    return out


def check_tricolon(sents):
    pat = re.compile(r"\b((?:\w+\s?){1,2}),\s+((?:\w+\s?){1,2}),\s+and\s+((?:\w+\s?){1,2})\b")
    out = []
    for s in sents:
        for m in pat.finditer(s.text):
            items = [g.strip() for g in m.groups()]
            if all(1 <= len(i.split()) <= 2 for i in items):
                out.append(flag(s, "tricolon? " + "/".join(items)))
                break
    return out


def check_false_range(sents):
    numish = re.compile(r"^(\d|\$|£|€)|^(january|february|march|april|may|june|july|"
                        r"august|september|october|november|december|\d{4})", re.I)
    out = []
    for s in sents:
        for m in re.finditer(r"\bfrom\s+([^,;.]{1,40}?)\s+to\s+([^,;.]{1,40}?)(?=[,;.]|$)",
                             s.text, re.I):
            a, b = m.group(1).strip(), m.group(2).strip()
            if numish.match(a) and numish.match(b):
                continue
            out.append(flag(s, "range? %s -> %s" % (a, b)))
            break
    return out


def check_participles(sents):
    out = []
    for s in sents:
        for m in re.finditer(r",\s*(\w+ing)\b", s.text):
            if m.group(1).lower() in PARTICIPLES:
                # only the last clause counts as a tail
                if len(s.text) - m.start() < 120:
                    out.append(flag(s, "tail: " + m.group(1)))
                    break
    return out


def check_nominalization(sents, total_words):
    nom = re.compile(r"\w+(tion|ment|ity|ness|ance|ence)s?$", re.I)
    n = sum(1 for s in sents for w in words(s.text) if nom.match(w))
    density = (n * 100.0 / total_words) if total_words else 0.0
    lines = ["  %d nominalizations / %d words = %.1f per 100" % (n, total_words, density)]
    if density > 4:
        lines.append("  FLAG rule 10: above 4 per 100 words")
    hits = []
    for s in sents:
        f = [w for w in words(s.text) if nom.match(w)]
        if len(f) >= 3:
            hits.append(flag(s, "dense: " + ",".join(f[:4])))
    return lines + ([""] + hits if hits else [])


def check_passive(sents):
    be = r"\b(was|were|is|are|been|being|be)\s+(?:\w+ly\s+)?(\w+ed|%s)\b" % "|".join(IRREGULAR_PP)
    out = []
    for s in sents:
        for m in re.finditer(be, s.text, re.I):
            after = s.text[m.end():]
            if re.match(r"(\s+\S+){0,6}", after) and " by " in " " + " ".join(after.split()[:6]) + " ":
                continue
            out.append(flag(s, "passive? " + m.group(0)))
            break
    return out


def check_emdash(raw):
    total = sum(l.count("—") for l in raw)
    lines = ["  %d em dashes in the file" % total]
    for i, l in enumerate(raw, 1):
        if l.count("—") > 1:
            lines.append("%5d: %-22s :: %s" % (i, "%d on this line" % l.count("—"), l.strip()))
    return lines


def check_numbers(sents):
    out = []
    for s in sents:
        ws = [w.lower() for w in words(s.text)]
        why = []
        if re.search(r"\d", s.text):
            why.append("digit")
        sup = [w for w in ws if w in SUPERLATIVE or (w.endswith("est") and len(w) > 4)]
        if sup:
            why.append("sup:" + ",".join(sorted(set(sup))[:3]))
        mul = [w for w in ws if w in MULTIPLIER]
        if mul:
            why.append("mult:" + ",".join(sorted(set(mul))))
        if why:
            out.append(flag(s, " ".join(why)))
    return out


# ------------------------------------------------------------------ main ---

def main():
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 0
    path = sys.argv[1]
    paras, sents, raw = parse(path)
    total_words = sum(len(words(s.text)) for s in sents)

    print("plain-prose lint: %s" % path)
    print("%d paragraphs, %d sentences, %d words" % (len(paras), len(sents), total_words))
    print()

    check_edges(paras)
    emit("ONE-LINE PARAGRAPHS", "13", check_oneline(paras))
    emit("CONTRAST OPENERS", "6", check_contrast(sents))
    emit("Q-AND-A FRAGMENTS", "8", check_qa(sents))
    emit("TRICOLONS", "7", check_tricolon(sents),
         "Flag only: cut the third item if it adds nothing the second didn't.")
    emit("FALSE RANGES", "9", check_false_range(sents),
         "Flag only: if an endpoint can be swapped for an unrelated noun, there is no scale.")
    emit("INTENSIFIERS AND AI VOCABULARY", "1, 2, 3", word_hits(sents, INTENSIFIERS, "words"))
    emit("PARTICIPLE TAILS", "10", check_participles(sents))
    emit("NOMINALIZATION DENSITY", "10", check_nominalization(sents, total_words))
    emit("AGENTLESS PASSIVE", "10", check_passive(sents),
         "Flag only: name the agent, or leave it if the agent is genuinely unknown.")
    emit("EM DASHES", "11", check_emdash(raw))
    emit("ANNOUNCERS AND RECAPPERS", "14", phrase_hits(sents, ANNOUNCERS))
    emit("CHATBOT RESIDUE", "21", phrase_hits(sents, RESIDUE))
    emit("HEDGES", "16", phrase_hits(sents, HEDGES))
    emit("METAPHORS", "4", [
        "  Editor: count figurative phrases; target <= 1 per 200 words "
        "(this piece: %d words -> <= %d)." % (total_words, max(1, total_words // 200))])
    emit("NUMBERS AND SUPERLATIVES", "22", check_numbers(sents),
         "Editor: for each, did you measure it or reach for it? Soften what you reached for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
