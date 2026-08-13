"""Give the volume-numbered placeholders their real titles.

Splitting the loss document's list lines left records like "Magic Tree House
#17" — true, and useless at a sale, because a number does not scan and does
not match. This names them.

The mapping lives in ``data/series-volumes.toml`` rather than being derived at
run time, because deriving it is research rather than computation: Open
Library cannot answer "what is Magic Tree House #17" reliably, so the numbers
were resolved against Wikipedia's series tables and then confirmed one by one
against Open Library. That work is done once and its result is reviewable.

Renaming is not always the right move. Naming "Boxcar Children #14" as *Tree
House Mystery* can collide with a copy the Libib export already holds under
that title, and two records for one book split its holdings — the scanner then
says "not owned" for something on the shelf. So each volume resolves against
the catalog first, and the loss moves onto the existing book where there is
one.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from stacks.importers.list_split import _attach, _drop_work
from stacks.models import Copy, Work
from stacks.normalize import normalize_title


@dataclass
class VolumeStats:
    named: int = 0
    merged_into_existing: int = 0
    already_done: int = 0
    merges: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.named} placeholders named, "
            f"{self.merged_into_existing} merged onto a book already in the "
            f"catalog, {self.already_done} already done"
        )


def load(session: Session, path: Path) -> VolumeStats:
    stats = VolumeStats()
    spec = tomllib.loads(path.read_text())

    for vol in spec.get("volume", []):
        placeholder_title = f"{vol['series']} #{vol['number']}"
        placeholder = session.scalar(
            select(Work).where(Work.sort_title == normalize_title(placeholder_title))
        )
        if placeholder is None:
            stats.already_done += 1
            continue

        real = vol["title"]
        existing = session.scalar(
            select(Work).where(
                Work.sort_title == normalize_title(real), Work.id != placeholder.id
            )
        )

        if existing is None:
            placeholder.title = real
            placeholder.sort_title = normalize_title(real)
            stats.named += 1
            continue

        # The book is already known. Move the loss onto it rather than leaving
        # two records that split the holdings between them.
        for copy in session.scalars(
            select(Copy).where(Copy.work_id == placeholder.id)
        ).all():
            note = (
                f"{copy.notes or ''} || volume {vol['number']} of "
                f"{vol['series']} is this book"
            ).strip(" |")
            _attach(session, existing, copy.status, copy.provenance, note)
        _drop_work(session, placeholder)
        stats.merged_into_existing += 1
        stats.merges.append((placeholder_title, existing.title))

    session.flush()
    return stats
