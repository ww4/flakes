"""Which cover to show for a work.

The picture should match the book on the shelf. Anything else is actively
misleading — a foreign reprint's jacket looks nothing like the copy in the
house, and at a sale that is exactly the comparison being made by eye.

Preference, strongest first:

1. **A deliberate choice.** ``Work.cover_edition_id``, set by hand.
2. **The printing being scanned.** If a barcode produced this view, show that
   book's own art.
3. **The printing we own.** The copy on the shelf, which is what someone
   recognises.
4. **Any other edition**, only when none of the above has art at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from stacks.models import Edition


@dataclass(slots=True)
class CoverPick:
    edition: Edition | None
    #: Why this one — surfaced in the UI so an odd cover is explicable.
    reason: str

    @property
    def url(self) -> str | None:
        e = self.edition
        if e is None:
            return None
        if e.cover_id:
            return f"covers/id/{e.cover_id}?size=M"
        if e.isbn13:
            return f"covers/{e.isbn13}?size=M"
        return None


def _has_art(e: Edition | None) -> bool:
    return bool(e is not None and (e.cover_id or e.isbn13))


def choose(
    editions: list[Edition],
    *,
    chosen_edition_id: int | None = None,
    scanned_isbn: str | None = None,
    owned_isbns: set[str] | None = None,
) -> CoverPick:
    owned_isbns = owned_isbns or set()
    by_id = {e.id: e for e in editions}

    chosen = by_id.get(chosen_edition_id) if chosen_edition_id else None
    if _has_art(chosen):
        return CoverPick(chosen, "chosen by hand")

    if scanned_isbn:
        scanned = next((e for e in editions if e.isbn13 == scanned_isbn), None)
        if _has_art(scanned):
            return CoverPick(scanned, "the printing you scanned")

    # The copy on the shelf. Prefer one with a real cover id — an ISBN-only
    # fallback may well turn out to have no art behind it.
    owned = [e for e in editions if e.isbn13 in owned_isbns]
    for e in sorted(owned, key=lambda e: (e.cover_id is None, -(e.publish_year or 0))):
        if _has_art(e):
            return CoverPick(e, "your copy")

    for e in sorted(
        editions, key=lambda e: (e.cover_id is None, -(e.publish_year or 0))
    ):
        if _has_art(e):
            return CoverPick(e, "another printing")

    return CoverPick(None, "no cover known")
