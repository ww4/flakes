"""The sale-day verdict engine.

This is the part that costs money when it is wrong, so it is built to be
explicit about *how sure it is* rather than to look confident.

The central rule: a copy imported from Libib is ``unverified``. It asserts we
owned the book in 2024, before the flood. Telling someone "you already have
this, skip it" on that basis is exactly the failure that makes them go home
without a book they no longer own. So an unverified holding never produces a
confident SKIP — it produces a CAUTION that says what we actually know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from stacks.models import (
    Copy,
    CopyStatus,
    Edition,
    MatchTier,
    Provenance,
    WantKind,
    WantRule,
    Work,
)
from stacks.normalize import (
    normalize_author,
    normalize_title,
    numeric_conflict,
    to_isbn13,
)


class Verdict(StrEnum):
    """What to do about the book in hand.

    Note what is *not* here: a "buy this" verdict for a book we simply do not
    own. Most books at a sale are books this family has never heard of, and
    recommending all of them is worse than useless — it drowns the three
    signals that matter. An unrecognised book is reported as
    ``NOT_IN_CATALOG``: information, not advice.

    A buy recommendation requires a positive reason — it is on a want list, the
    flood destroyed it, or we deliberately want more copies than we hold.
    """

    BUY_WANTED = "BUY_WANTED"          # matches a standing want rule
    BUY_REPLACE = "BUY_REPLACE"        # flood took it, not yet replaced
    BUY_MORE = "BUY_MORE"              # have some, want more than we have
    CAUTION_UNVERIFIED = "CAUTION_UNVERIFIED"  # Libib says yes, unconfirmed
    SKIP_HAVE = "SKIP_HAVE"            # verified in hand, enough of them
    NOT_IN_CATALOG = "NOT_IN_CATALOG"  # we have no record — neutral, not advice
    UNKNOWN = "UNKNOWN"                # could not read or identify the code


#: Whether each verdict means "put it in the basket".
BUYS: frozenset[Verdict] = frozenset(
    {Verdict.BUY_WANTED, Verdict.BUY_REPLACE, Verdict.BUY_MORE}
)

#: Short label for what we *hold*, as distinct from what to *do*.
#:
#: Owning a book is good news and should not read as an alarm. The status tag
#: states the fact ("HAVE"); the recommendation states the action ("skip"). The
#: earlier design collapsed the two and made a book you own announce itself as
#: CAUTION, which felt like a warning about a happy thing.
STATUS_LABEL: dict[Verdict, str] = {
    Verdict.SKIP_HAVE: "HAVE",
    Verdict.BUY_MORE: "HAVE",
    Verdict.CAUTION_UNVERIFIED: "UNCONFIRMED",
    Verdict.BUY_REPLACE: "LOST",
    Verdict.BUY_WANTED: "WANTED",
    Verdict.NOT_IN_CATALOG: "NOT OWNED",
    Verdict.UNKNOWN: "UNREADABLE",
}


def status_for(verdict: Verdict, holding: Holding | None = None) -> str:
    """Status tag, refined by how a holding came to be unverified.

    "REPLACED" is worth distinguishing from plain "UNCONFIRMED": one means a
    book was deliberately bought again after the flood, the other means it sat
    in a 2023 export and nobody has laid eyes on it since. Same confidence,
    very different stories.
    """
    if (
        holding is not None
        and verdict is Verdict.CAUTION_UNVERIFIED
        and holding.re_acquired
    ):
        return "REPLACED"
    return STATUS_LABEL.get(verdict, "NOT OWNED")


@dataclass(slots=True)
class Holding:
    """What we hold of one work, broken out by confidence."""

    present: int = 0
    unverified: int = 0
    lost_flood: int = 0
    loaned: int = 0
    missing: int = 0
    #: Unverified copies that were deliberately bought again after the flood.
    #:
    #: Tracked separately because it is a different kind of "unverified". The
    #: Libib export is all pre-flood (2023), so an ordinary unverified holding
    #: means "catalogued before the water came, unseen since". A re-acquired one
    #: means someone went out and replaced the book — the loss has been made
    #: good, and saying "not replaced yet" about it would be wrong.
    re_acquired: int = 0

    @property
    def confirmed(self) -> int:
        """Copies we have actually laid hands on. Loaned counts — it exists."""
        return self.present + self.loaned


@dataclass(slots=True)
class MatchResult:
    verdict: Verdict
    tier: MatchTier | None
    work: Work | None
    holding: Holding
    desired: int
    headline: str
    detail: list[str] = field(default_factory=list)
    confidence: float = 1.0
    #: Standing want rules this book satisfies, as display strings.
    wants: list[str] = field(default_factory=list)

    @property
    def should_buy(self) -> bool:
        return self.verdict in BUYS


def wants_for_work(session: Session, work: Work) -> list[str]:
    """Standing want rules this book satisfies.

    A want rule is not about *this* book — it is about its author, its series,
    or its publisher. "Any books by Seymour Simon" fires on a book we have never
    heard of, which is exactly the case a copy-based verdict cannot see.
    """
    hits: list[str] = []

    rules = session.scalars(
        select(WantRule).where(
            WantRule.active.is_(True),
            or_(
                WantRule.work_id == work.id,
                and_(WantRule.author_id.is_not(None),
                     WantRule.author_id == work.primary_author_id),
                and_(WantRule.series_id.is_not(None),
                     WantRule.series_id == work.series_id),
            ),
        )
    ).all()

    for r in rules:
        if r.kind is WantKind.author:
            hits.append(f"author on your want list: {r.label}")
        elif r.kind is WantKind.series:
            missing = [e for e in r.entries if not e.have]
            have = [e for e in r.entries if e.have]
            note = f"series you're collecting: {r.label}"
            if have:
                note += f" (you have {len(have)})"
            if missing:
                note += f" — missing {len(missing)}"
            hits.append(note)
        elif r.kind is WantKind.work:
            hits.append(f"on your want list: {r.label}")
        else:
            hits.append(f"wanted: {r.label}")

    return hits


def wants_for_metadata(session: Session, meta: dict) -> list[str]:
    """Check want rules against metadata for a book we have no record of.

    This is the payoff of author- and publisher-level rules. "Any books by
    Seymour Simon" is precisely an instruction about books not yet owned, so it
    can only fire once something outside the catalog says who wrote the thing
    in your hand.
    """
    hits: list[str] = []
    author = (meta.get("author") or "").strip()
    publisher = (meta.get("publisher") or "").strip()

    if author:
        key = normalize_author(author)
        for rule in session.scalars(
            select(WantRule).where(
                WantRule.active.is_(True), WantRule.kind == WantKind.author
            )
        ).all():
            if key and normalize_author(rule.label) == key:
                hits.append(f"author on your want list: {rule.label}")

    if publisher:
        low = publisher.lower()
        for rule in session.scalars(
            select(WantRule).where(
                WantRule.active.is_(True), WantRule.kind == WantKind.publisher
            )
        ).all():
            if rule.label.lower() in low:
                hits.append(f"publisher on your want list: {rule.label}")

    return hits


def _holding_for_work(session: Session, work_id: int) -> Holding:
    rows = session.execute(
        select(Copy.status, func.count(Copy.id))
        .where(Copy.work_id == work_id)
        .group_by(Copy.status)
    ).all()
    counts = {status: n for status, n in rows}
    re_acq = session.scalar(
        select(func.count(Copy.id)).where(
            Copy.work_id == work_id,
            Copy.status == CopyStatus.unverified,
            Copy.provenance == Provenance.re_acquired,
        )
    ) or 0
    return Holding(
        present=counts.get(CopyStatus.present, 0),
        unverified=counts.get(CopyStatus.unverified, 0),
        lost_flood=counts.get(CopyStatus.lost_flood, 0),
        loaned=counts.get(CopyStatus.loaned, 0),
        missing=counts.get(CopyStatus.missing, 0),
        re_acquired=re_acq,
    )


def _decide(work: Work, holding: Holding) -> tuple[Verdict, str, list[str]]:
    """Choose a verdict, a recommendation, and the facts behind it.

    The recommendation says what to do; the detail lines say what we hold. They
    must not repeat each other — an earlier version announced "unconfirmed
    since the flood" in both, which read as the system saying the same worried
    thing twice.
    """
    desired = work.desired_copies
    verdict, recommendation, spent = _choose(work, holding, desired)

    # Facts, minus whatever the recommendation already said.
    detail: list[str] = []
    if holding.lost_flood and "lost_flood" not in spent:
        detail.append(f"{holding.lost_flood} lost in the flood")
    if holding.re_acquired and "re_acquired" not in spent:
        detail.append(f"{holding.re_acquired} bought again after the flood")
    if holding.unverified - holding.re_acquired > 0 and "unverified" not in spent:
        detail.append(
            f"{holding.unverified - holding.re_acquired} unconfirmed since the flood"
        )
    if holding.loaned and "loaned" not in spent:
        detail.append(f"{holding.loaned} out on loan")
    if holding.missing:
        detail.append(f"{holding.missing} recorded missing")

    return verdict, recommendation, detail


def _choose(
    work: Work, holding: Holding, desired: int
) -> tuple[Verdict, str, set[str]]:
    """Returns (verdict, recommendation, which counts the wording used)."""
    # desired_copies == 0 is an explicit "we do not want this" marker. It must
    # short-circuit everything below, including the flood-replacement path:
    # a book we decided against is not one to re-buy because the water took it.
    if desired <= 0:
        if holding.confirmed:
            return (Verdict.SKIP_HAVE,
                    f"You have {holding.confirmed} — not collecting more", set())
        return Verdict.SKIP_HAVE, "Marked as not wanted", set()

    # Confirmed holdings decide first — they are the only thing we actually know.
    if holding.confirmed >= desired:
        if holding.loaned and not holding.present:
            return (Verdict.SKIP_HAVE,
                    f"You have {holding.loaned} — out on loan", {"loaned"})
        return Verdict.SKIP_HAVE, f"You have {holding.confirmed} — confirmed", set()

    if holding.confirmed > 0:
        return (Verdict.BUY_MORE,
                f"You have {holding.confirmed} of {desired} you want", set())

    # A replacement was deliberately bought, so the loss has been made good.
    # This must be checked BEFORE the loss branch: a re-bought book that still
    # announced "not replaced yet" would send someone to buy a second one.
    if holding.re_acquired:
        return (Verdict.CAUTION_UNVERIFIED,
                "Replaced after the flood — not scanned yet",
                {"unverified", "lost_flood", "re_acquired"})

    # Nothing confirmed, no replacement. Was it destroyed?
    if holding.lost_flood:
        return (Verdict.BUY_REPLACE,
                "The flood took this — not replaced yet", {"lost_flood"})

    # Nothing confirmed, nothing known-destroyed, but Libib claims we own it.
    # The export is entirely pre-flood (2023), so this means "catalogued before
    # the water came, unseen since". The dangerous middle: it must not read as
    # a confident skip.
    if holding.unverified:
        n = holding.unverified
        return (Verdict.CAUTION_UNVERIFIED,
                f"Probably yours — {n} in the catalog, but not seen since the flood",
                {"unverified"})

    return Verdict.NOT_IN_CATALOG, "No copies recorded", set()


def resolve_work_by_isbn(session: Session, isbn13: str) -> tuple[Work | None, MatchTier | None]:
    """Exact ISBN -> work, through the edition table.

    Because enrichment expands every owned work to all of its known editions,
    this single lookup already covers "same book, different printing" — which
    is why the sale-day path does not need fuzzy matching at all.
    """
    work = session.scalar(
        select(Work).join(Edition, Edition.work_id == Work.id).where(Edition.isbn13 == isbn13)
    )
    if work:
        return work, MatchTier.exact_isbn
    return None, None


def search_works(
    session: Session, query: str, limit: int = 25, threshold: float = 0.28
) -> list[tuple[Work, float]]:
    """Find every work matching a text query, best first.

    Distinct from :func:`resolve_work_by_title`, which answers "which single
    book is this barcode?". This one answers "what have I got like this?" and
    must return *all* the candidates: typing "magic school bus" matches 23
    works, and showing only the best one is useless for browsing.

    Substring matches are included alongside trigram similarity, because a short
    query ("magic") has low trigram similarity to a long title even though it is
    obviously what the person meant.
    """
    q = normalize_title(query)
    if not q or len(q) < 2:
        return []

    sim = func.similarity(Work.sort_title, q)
    rows = session.execute(
        select(Work, sim.label("sim"), Work.sort_title.contains(q).label("sub"))
        .where(or_(sim > threshold, Work.sort_title.contains(q)))
        .order_by(Work.sort_title.contains(q).desc(), sim.desc(), Work.sort_title)
        .limit(limit)
    ).all()
    return [(r[0], float(r[1])) for r in rows]


def resolve_work_by_title(
    session: Session, title: str, threshold: float = 0.45
) -> tuple[Work | None, float]:
    """Trigram fallback for books with no usable ISBN.

    Uses pg_trgm similarity, so there is no matching code of our own to get
    wrong. Returns the best candidate above ``threshold`` with its score.
    """
    norm = normalize_title(title)
    if not norm:
        return None, 0.0
    sim = func.similarity(Work.sort_title, norm)
    rows = session.execute(
        select(Work, sim.label("score"))
        .where(sim > threshold)
        .order_by(sim.desc())
        .limit(5)
    ).all()
    # Veto candidates whose volume number disagrees. "I can read it! Book 2"
    # scores 0.82 against "Book 1" — high enough to match, and wrong.
    for work, score in rows:
        if not numeric_conflict(norm, work.sort_title):
            return work, float(score)
    return None, 0.0


def evaluate_scan(
    session: Session,
    code: str,
    title_hint: str | None = None,
    external: dict | None = None,
) -> MatchResult:
    """Turn a scanned barcode into a decision.

    ``title_hint`` lets a caller that already knows the title (from an online
    lookup, or typed in for a pre-ISBN book) reach the fuzzy path.
    ``external`` carries metadata for a book we hold no record of, so that
    author- and publisher-level want rules can still fire on it.
    """
    # Scanned input: never repair a failed check digit into a different book.
    isbn13 = to_isbn13(code, repair=False)

    work: Work | None = None
    tier: MatchTier | None = None
    confidence = 1.0

    if isbn13:
        work, tier = resolve_work_by_isbn(session, isbn13)

    if work is None and title_hint:
        work, score = resolve_work_by_title(session, title_hint)
        if work is not None:
            tier = MatchTier.fuzzy_title
            confidence = score

    if work is None:
        # No record of this book. That is not a reason to buy it — see the
        # Verdict docstring. If external metadata identifies its author or
        # publisher we can still check the standing want rules, which is the
        # one case where an unrecognised book *should* be recommended.
        wants = wants_for_metadata(session, external) if external else []
        return MatchResult(
            verdict=(
                Verdict.BUY_WANTED if wants
                else Verdict.NOT_IN_CATALOG if isbn13
                else Verdict.UNKNOWN
            ),
            tier=None,
            work=None,
            holding=Holding(),
            desired=1,
            headline=(
                "On your want list" if wants
                else "Not in your catalog" if isbn13
                else "Could not read that code"
            ),
            detail=([] if isbn13 else ["No valid ISBN, and no title to fall back on"]),
            confidence=1.0 if isbn13 else 0.0,
            wants=wants,
        )

    holding = _holding_for_work(session, work.id)
    verdict, headline, detail = _decide(work, holding)

    # A fuzzy match that says SKIP is the riskiest output in the system: we are
    # telling someone not to buy a book on the strength of a title similarity.
    # Downgrade it to a caution and say so.
    if tier is MatchTier.fuzzy_title and verdict is Verdict.SKIP_HAVE:
        verdict = Verdict.CAUTION_UNVERIFIED
        detail.append(f"matched on title similarity only ({confidence:.0%})")

    wants = wants_for_work(session, work)
    # A book we hold no copies of but which matches a standing want rule is the
    # one case where "buy it" is real advice rather than noise.
    if wants and verdict is Verdict.NOT_IN_CATALOG:
        verdict = Verdict.BUY_WANTED
        headline = "On your want list"

    return MatchResult(
        verdict=verdict,
        tier=tier,
        work=work,
        holding=holding,
        desired=work.desired_copies,
        headline=headline,
        detail=detail,
        confidence=confidence,
        wants=wants,
    )
