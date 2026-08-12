"""Badges — the one vocabulary for slicing the library.

A note on the architecture, because it is easy to get wrong in a way that only
hurts later.

Badges come from two genuinely different places, and the difference matters:

**Derived state** — HAVE, UNCONFIRMED, LOST, REPLACED. These are projections of
copy status. They are not opinions and must never be *stored*: the moment a
derived fact is written down it acquires a second place to be wrong, and it
will go stale exactly when someone confirms a book and forgets to update the
label. Every stale-data bug in this project so far has had that shape. So they
stay computed, always, from the copies.

**Assigned labels** — anything a person decides: "sell", "Peter's", "reading
list", "check condition". These have no computed source, so a table is the only
place they can live.

WANTED sits across the line and is allowed to: it can be *derived* from a
standing want rule (an author or series being collected) or *assigned* by hand.
Both produce the same badge, and the union is what the reader cares about.

Series is deliberately NOT a tag. A tag is a set; a series is a *sequence*, and
the position is what powers "you have 17 of 21" and "missing #10". Flattening it
into a tag would throw that away for a cosmetic gain in uniformity. It is
exposed through the same browsing vocabulary without being modelled as one.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Strongest first. A destroyed book that is also wanted shows LOST, because
#: what happened to it outranks what you would like to happen next.
PRECEDENCE: tuple[str, ...] = (
    "LOST",
    "REPLACED",
    "WANTED",
    "HAVE",
    "UNCONFIRMED",
    "NOT OWNED",
)

#: Badges the system computes and a person cannot set directly. Assigning these
#: by hand would put a second, staleable copy of the truth in the database.
DERIVED: frozenset[str] = frozenset({"HAVE", "REPLACED", "LOST", "UNCONFIRMED", "NOT OWNED"})


@dataclass(slots=True)
class Badges:
    all: list[str]

    @property
    def primary(self) -> str:
        """The one badge a cover corner has room for."""
        for name in PRECEDENCE:
            if name in self.all:
                return name
        return self.all[0] if self.all else "NOT OWNED"


def derive_status(present: int, unverified: int, lost: int, reacq: int) -> str:
    """The single badge implied by what we hold."""
    if present:
        return "HAVE"
    if reacq:
        return "REPLACED"
    if lost:
        return "LOST"
    if unverified:
        return "UNCONFIRMED"
    return "NOT OWNED"


def compute(
    *,
    present: int = 0,
    unverified: int = 0,
    lost: int = 0,
    reacq: int = 0,
    wanted: bool = False,
    assigned: list[str] | None = None,
) -> Badges:
    """Every badge for one work, ordered strongest first."""
    found = [derive_status(present, unverified, lost, reacq)]
    if wanted:
        found.append("WANTED")
    for label in assigned or []:
        if label not in found:
            found.append(label)

    ordered = [b for b in PRECEDENCE if b in found]
    ordered += [b for b in found if b not in PRECEDENCE]  # user labels, as given
    return Badges(all=ordered)
