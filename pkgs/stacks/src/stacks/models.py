"""Database schema for stacks.

Design notes worth knowing before changing anything here:

* A **Work** is the abstract book ("Moby-Dick"). An **Edition** is a specific
  printing with its own ISBN. A **Copy** is a physical object sitting on a
  shelf. Owning three paperbacks of one printing is three Copy rows sharing one
  Edition. This split is what lets "do we already have this?" answer across
  editions, and what lets "do we want *another*?" be answerable at all.

* ``Work.ol_work_keys`` is an ARRAY on purpose. Open Library sometimes holds
  several work records for the same book; if we treated their key as our
  identity we would under-match and re-buy a duplicate — the exact failure this
  catalog exists to prevent. We merge their splits locally, once, permanently.

* ``CopyStatus.unverified`` is the import default, never ``present``. The 2025
  flood means the Libib export asserts "we owned this in 2024", not "this book
  exists". The physical sweep promotes rows to ``present``; whatever is still
  ``unverified`` once a shelf is swept is a candidate loss. This is why the
  scanner must surface confidence — see ``stacks.match``.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


class CopyStatus(enum.StrEnum):
    """Lifecycle of one physical object.

    ``unverified`` is the *import* state: something claims we own this, but no
    human has laid hands on it since the flood. It must never be presented as a
    confident "you have this" at a book sale.
    """

    unverified = "unverified"
    present = "present"
    lost_flood = "lost_flood"
    loaned = "loaned"
    missing = "missing"
    discarded = "discarded"


class Provenance(enum.StrEnum):
    """How a copy came to be recorded."""

    libib_import = "libib_import"
    flood_doc = "flood_doc"  # recorded in the hand-written loss list
    re_acquired = "re_acquired"  # bought again after the flood
    new_purchase = "new_purchase"
    gift = "gift"
    manual = "manual"


class LocationKind(enum.StrEnum):
    household = "household"
    room = "room"
    shelf = "shelf"
    section = "section"
    box = "box"


class WishlistReason(enum.StrEnum):
    series_gap = "series_gap"
    author_gap = "author_gap"
    flood_loss = "flood_loss"
    manual = "manual"


class RequestStatus(enum.StrEnum):
    open = "open"
    fulfilled = "fulfilled"
    cancelled = "cancelled"


class MatchTier(enum.StrEnum):
    """How a scanned barcode was resolved. Recorded so we can audit bad calls."""

    exact_isbn = "exact_isbn"
    work_expansion = "work_expansion"
    fuzzy_title = "fuzzy_title"
    manual = "manual"


# --------------------------------------------------------------------------
# bibliographic — sourced from Open Library, corrected by us
# --------------------------------------------------------------------------


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    sort_name: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    ol_author_key: Mapped[str | None] = mapped_column(String(32), unique=True)

    __table_args__ = (Index("ix_authors_sort_name_trgm", "sort_name",
                            postgresql_using="gin",
                            postgresql_ops={"sort_name": "gin_trgm_ops"}),)


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    hardcover_id: Mapped[str | None] = mapped_column(String(64))
    # Total entries, when a source knows it. Null means "we don't know yet",
    # which is different from zero and must not render as "0 of 0".
    total_count: Mapped[int | None] = mapped_column(Integer)


class Work(Base):
    """The abstract book. Our identity anchor."""

    __tablename__ = "works"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(600), nullable=False)
    # Normalised for matching: lowercased, articles stripped, punctuation folded.
    sort_title: Mapped[str] = mapped_column(String(600), nullable=False, index=True)
    subtitle: Mapped[str | None] = mapped_column(String(600))
    description: Mapped[str | None] = mapped_column(Text)

    primary_author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"))
    series_position: Mapped[float | None] = mapped_column()  # 1, 2, 2.5 ...

    # Several OL work keys may collapse into one of ours. See module docstring.
    ol_work_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, server_default="{}"
    )

    # How many copies we actually want. Default 1; bump for references,
    # cookbooks, and anything lent out often. Drives the "do we want another?"
    # half of the sale verdict.
    desired_copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # A deliberately chosen cover, overriding the automatic preference.
    #
    # Without this the art shown is whichever edition the query happened to
    # reach first, which can be a foreign printing with completely different
    # jacket art. The picture should match the book on the shelf.
    #
    # Deliberately NOT a foreign key. Edition.work_id already points at works,
    # so a real FK here closes a cycle and makes every works<->editions join
    # ambiguous ("tables have more than one foreign key constraint
    # relationship"). Nothing is lost: a stale id simply fails to resolve in
    # `coverchoice.choose`, which then falls through to the automatic
    # preference — exactly the behaviour wanted anyway.
    cover_edition_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    primary_author: Mapped[Author | None] = relationship(lazy="joined")
    series: Mapped[Series | None] = relationship(lazy="joined")
    editions: Mapped[list[Edition]] = relationship(back_populates="work")
    copies: Mapped[list[Copy]] = relationship(back_populates="work")

    __table_args__ = (
        CheckConstraint("desired_copies >= 0", name="ck_works_desired_copies_nonneg"),
        Index("ix_works_ol_work_keys", "ol_work_keys", postgresql_using="gin"),
        Index("ix_works_sort_title_trgm", "sort_title",
              postgresql_using="gin", postgresql_ops={"sort_title": "gin_trgm_ops"}),
    )


class Edition(Base):
    """A specific printing. The ISBN lives here, not on Work."""

    __tablename__ = "editions"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False, index=True)

    isbn13: Mapped[str | None] = mapped_column(String(13), index=True)
    isbn10: Mapped[str | None] = mapped_column(String(10), index=True)
    publisher: Mapped[str | None] = mapped_column(String(300))
    publish_date: Mapped[str | None] = mapped_column(String(64))  # OL dates are messy
    publish_year: Mapped[int | None] = mapped_column(Integer)
    binding: Mapped[str | None] = mapped_column(String(64))  # hardcover/paperback/...
    page_count: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(16))

    ol_edition_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    cover_url: Mapped[str | None] = mapped_column(String(600))
    #: Open Library's internal cover id.
    #:
    #: Worth storing rather than deriving: cover requests *by identifier*
    #: (ISBN) are capped at 100 per IP per five minutes, while requests by
    #: cover id are unlimited. Capturing this at ingest turns cover fetching
    #: from a rationed activity into an ordinary one.
    cover_id: Mapped[int | None] = mapped_column(Integer, index=True)

    work: Mapped[Work] = relationship(back_populates="editions")

    __table_args__ = (
        UniqueConstraint("isbn13", name="uq_editions_isbn13"),
        Index("ix_editions_work_isbn13", "work_id", "isbn13"),
    )


# --------------------------------------------------------------------------
# physical — where things actually are
# --------------------------------------------------------------------------


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    household_id: Mapped[int | None] = mapped_column(ForeignKey("households.id"))


class Location(Base):
    """A node in the household > room > shelf tree.

    ``sentinel_barcode`` is the printed card taped to the shelf. During the
    sweep you scan the card once, then every book on that shelf — which is what
    lets a 2,800-book re-inventory run at scan speed instead of typing speed.
    """

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), index=True)
    household_id: Mapped[int | None] = mapped_column(ForeignKey("households.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[LocationKind] = mapped_column(
        Enum(LocationKind, name="location_kind"), nullable=False
    )
    sentinel_barcode: Mapped[str | None] = mapped_column(String(64), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    parent: Mapped[Location | None] = relationship(remote_side=[id])


class Copy(Base):
    """One physical object. The unit of ownership."""

    __tablename__ = "copies"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False, index=True)
    # Null when we know *which book* but not *which printing* — common for
    # Libib rows with no usable ISBN, and for pre-ISBN books.
    edition_id: Mapped[int | None] = mapped_column(ForeignKey("editions.id"), index=True)

    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), index=True)
    owner_household_id: Mapped[int | None] = mapped_column(
        ForeignKey("households.id"), index=True
    )

    status: Mapped[CopyStatus] = mapped_column(
        Enum(CopyStatus, name="copy_status"), nullable=False, default=CopyStatus.unverified
    )
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), nullable=False, default=Provenance.manual
    )

    # Which Libib collection(s) this holding came from. PROVENANCE, NOT
    # LOCATION: the family moved out of the flooded house, boxed everything,
    # and moved back, so "Frankfort living room" says where a book was
    # catalogued in 2024, not where it is. Real locations come from the sweep.
    source_collections: Mapped[list[str]] = mapped_column(
        ARRAY(String(120)), nullable=False, server_default="{}"
    )

    condition: Mapped[str | None] = mapped_column(String(64))
    acquired_date: Mapped[date | None] = mapped_column(Date)
    # Our own printed label, for books with no scannable ISBN.
    label_barcode: Mapped[str | None] = mapped_column(String(64), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)

    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    work: Mapped[Work] = relationship(back_populates="copies")

    __table_args__ = (
        Index("ix_copies_work_status", "work_id", "status"),
    )


# --------------------------------------------------------------------------
# social — loans, requests, wants
# --------------------------------------------------------------------------


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    copy_id: Mapped[int] = mapped_column(ForeignKey("copies.id"), nullable=False, index=True)
    borrower_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    loaned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class Request(Base):
    """Someone wants to borrow a work, or wants us to look out for it."""

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False, index=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    status: Mapped[RequestStatus] = mapped_column(
        Enum(RequestStatus, name="request_status"), nullable=False, default=RequestStatus.open
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)


class WantKind(enum.StrEnum):
    """What a want rule targets.

    A wishlist of individual books cannot express what the sale list actually
    says. "Any books by Seymour Simon", "DK books" and "Hardy Boys, have
    1,2,3,4,6,10..." are all standing instructions, not titles, and they are
    what will actually fire when a barcode is scanned at a sale.
    """

    work = "work"  # one specific book
    author = "author"  # anything by this person
    publisher = "publisher"  # anything from this imprint (DK, Usborne, Landmark)
    series = "series"  # anything in this series, minus what we hold
    topic = "topic"  # free-text ("science encyclopedias")


class WantSource(enum.StrEnum):
    sale_doc = "sale_doc"
    flood_loss = "flood_loss"
    series_gap = "series_gap"
    manual = "manual"


class WantRule(Base):
    """A standing instruction for what to buy when we see it."""

    __tablename__ = "want_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[WantKind] = mapped_column(Enum(WantKind, name="want_kind"), nullable=False)
    source: Mapped[WantSource] = mapped_column(
        Enum(WantSource, name="want_source"), nullable=False, default=WantSource.manual
    )

    #: Human label — the series/author/publisher name as written.
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Normalised for matching.
    match_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)

    work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"))

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Set when the parser could not confidently classify the line.
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: The source line, verbatim. These rules come from a hand-written document
    #: and the original phrasing is often the only way to settle a question.
    raw_text: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    entries: Mapped[list[WantEntry]] = relationship(
        back_populates="rule", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_want_rules_match_key_trgm", "match_key",
              postgresql_using="gin", postgresql_ops={"match_key": "gin_trgm_ops"}),
    )


class WantEntry(Base):
    """One named item inside a rule's have- or missing-list.

    The sale document tracks series completion by naming what is already owned
    ("Cornerstones of freedom: have 50: The Alamo, Arlington Cemetery, ...") or
    what is not ("Rangers apprentice: missing 10"). Both are needed: the first
    says *don't buy this one*, the second says *buy exactly this one*.
    """

    __tablename__ = "want_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("want_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: True = we already have it; False = explicitly missing.
    have: Mapped[bool] = mapped_column(Boolean, nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    match_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    #: Series position when the list is numeric ("have 1,2,3,4,6,10").
    position: Mapped[int | None] = mapped_column(Integer)

    rule: Mapped[WantRule] = relationship(back_populates="entries")


class Tag(Base):
    """A label someone put on a book.

    Only for things a person decides — "sell", "Peter's", "check condition",
    "reading list". Derived state (HAVE, LOST, REPLACED, UNCONFIRMED) is
    deliberately NOT stored here: it is computed from copy status, and writing
    it down would create a second place for the truth to live and go stale.
    See :mod:`stacks.badges`.
    """

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: Optional hex colour for the badge; falls back to a neutral chip.
    color: Mapped[str | None] = mapped_column(String(7))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    works: Mapped[list[WorkTag]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )


class WorkTag(Base):
    __tablename__ = "work_tags"

    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tag: Mapped[Tag] = relationship(back_populates="works")


class WishlistItem(Base):
    """A book we want. Flood losses land here automatically."""

    __tablename__ = "wishlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False, index=True)
    reason: Mapped[WishlistReason] = mapped_column(
        Enum(WishlistReason, name="wishlist_reason"), nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    satisfied_by_copy_id: Mapped[int | None] = mapped_column(ForeignKey("copies.id"))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("work_id", "reason", name="uq_wishlist_work_reason"),
    )


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


class ScanEvent(Base):
    """Every barcode scanned, and what we decided.

    Kept because the sale verdict is the whole point of this system: when it
    tells us wrong, we need to be able to go back and see why.
    """

    __tablename__ = "scan_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    scanned_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    matched_work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"))
    match_tier: Mapped[MatchTier | None] = mapped_column(Enum(MatchTier, name="match_tier"))
    verdict: Mapped[str | None] = mapped_column(String(32))
    context: Mapped[dict | None] = mapped_column(JSONB)
