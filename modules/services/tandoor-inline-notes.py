#!/usr/bin/env python3
"""Render Tandoor ingredient notes inline, inside the food cell.

Tandoor hides an ingredient's note behind a click-to-open tooltip and only
writes it into the DOM in print mode. This rewrites the compiled
IngredientsTable component so the note is appended to the FOOD cell as
", <note>" — giving "1 cup onion, chopped" the way a printed cookbook sets
it — and suppresses both note cells (the print-mode one and the bubble).

Everything is located by SHAPE, never literal text: the bundle is minified and
its identifiers are renamed on every Tandoor release. Any failure exits
non-zero so the nix build stops rather than silently shipping the bubbles.
"""
import glob, os, re, sys

def match_paren(s, i):
    """Index of the ) closing the ( at s[i], skipping string literals."""
    assert s[i] == '('
    depth, j, n = 0, i, len(s)
    while j < n:
        ch = s[j]
        if ch in '`\'"':
            q, j = ch, j + 1
            while j < n and s[j] != q:
                j += 2 if s[j] == '\\' else 1
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return j
        j += 1
    sys.exit('inline-notes: unbalanced parentheses while scanning the food cell')

paths = glob.glob(os.path.join(sys.argv[1], 'assets', 'IngredientsTable-*.js'))
if len(paths) != 1:
    sys.exit(f'inline-notes: expected 1 IngredientsTable asset, found {paths}')
p = paths[0]
src = open(p, encoding='utf8').read()

# The print-mode branch renders  c(`span`,J,_(f.note),1)  — that one expression
# names every helper we need: element factory, toDisplayString, and the loop
# variable holding the ingredient.
m = re.search(r'([A-Za-z_$][\w$]*)\(`span`,[A-Za-z_$][\w$]*,'
              r'([A-Za-z_$][\w$]*)\(([A-Za-z_$][\w$]*)\.note\),1\)', src)
if not m:
    sys.exit('inline-notes: could not locate the note span; component changed')
mk, tostr, ing = m.groups()

# The comment-vnode helper, from any  :X(``,!0)  site.
nilm = re.search(r':([A-Za-z_$][\w$]*)\(``,!0\)', src)
if not nilm:
    sys.exit('inline-notes: could not locate the comment-vnode helper')
nil = nilm.group(1)

# The food cell: the only td whose children start with <ing>.food?
starts = list(re.finditer(r'[A-Za-z_$][\w$]*\(`td`,null,\[' + re.escape(ing) + r'\.food\?', src))
if len(starts) != 1:
    sys.exit(f'inline-notes: expected 1 food cell, found {len(starts)}; component changed')
open_paren = src.index('(', starts[0].start())
close = match_paren(src, open_paren)
if src[close - 1] != ']':
    sys.exit('inline-notes: food cell does not end in "])"; component changed')

# 1. Append the note inside the food cell. No class, so it inherits the
#    ingredient's own styling rather than Tandoor's grey italic note style.
inline = (f',{ing}.note?{mk}(`span`,null,`, `+{tostr}({ing}.note),1)'
          f':{nil}(``,!0)')
s = src[:close - 1] + inline + src[close - 1:]

# 2. Suppress both note cells. They are `X?noteCell:nothing` then
#    `X?nothing:bubbleCell`, so the first wants false and the second true.
ident = r'[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\)\(\)\.isPrintMode'
found = len(re.findall(ident, s))
if found != 2:
    sys.exit(f'inline-notes: expected 2 isPrintMode uses, found {found}; component changed')
s = re.sub(ident, '!1', s, count=1)
s = re.sub(ident, '!0', s, count=1)

if 'isPrintMode' in s or s == src:
    sys.exit('inline-notes: patch did not apply cleanly')
open(p, 'w', encoding='utf8').write(s)
print(f'inline-notes: patched {os.path.basename(p)} '
      f'(ingredient={ing}, mk={mk}, toStr={tostr}, nil={nil})')
