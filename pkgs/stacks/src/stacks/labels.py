"""Places and tags — two trees, one set of rules.

A **place** is where a copy physically is. A **tag** is anything else someone
wants to say about a book: which Sonlight core it belongs to, what grade it
reads at, whether it is a reader, what subject it covers.

They differ in exactly one rule:

    A copy is in ONE place. A book has MANY tags.

Everything else — the tree, the path notation, the rollup counts, renaming,
bulk application — is shared, which is why it lives here rather than being
written twice.

Both are stored as real trees (``parent_id``) and *typed and displayed* as
paths (``Frankfort / science shelf``). An earlier design made the name itself
the path and rolled up with a prefix match, which is wrong in two ways worth
remembering: ``Frankfort`` prefix-matches ``Frankfort Annex``, so every rollup
needs ``= OR LIKE`` and one forgotten call site miscounts; and renaming a
parent rewrites every descendant. A tree makes rollup exact and a rename one
row, while the path notation keeps entry to a single text field.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks.models import Copy, Location, Tag, Work, WorkTag

#: What separates the levels when a tree is written on one line. Spaces around
#: it are optional on input and always present on output.
SEP = "/"


def split_path(path: str) -> list[str]:
    """``"Frankfort / science shelf"`` -> ``["Frankfort", "science shelf"]``."""
    return [part.strip() for part in path.split(SEP) if part.strip()]


def join_path(parts: list[str]) -> str:
    return f" {SEP} ".join(parts)


@dataclass
class LabelNode:
    id: int
    name: str
    path: str
    parent_id: int | None
    depth: int
    #: Books labelled with this node exactly.
    own_count: int = 0
    #: Books labelled with this node or anything under it — the number Chris
    #: asked for: "how many books are in a location across all sublocations".
    total_count: int = 0
    children: list[LabelNode] = field(default_factory=list)


def _model(kind: str):
    if kind == "place":
        return Location
    if kind == "tag":
        return Tag
    raise ValueError(f"unknown label kind: {kind!r}")


def find_or_create(session: Session, kind: str, path: str) -> Location | Tag:
    """Resolve a path to a node, creating any missing levels along the way.

    Typing ``Frankfort / science shelf`` when neither exists creates both, so
    the common case — inventing a place while standing in front of it — is one
    text field and no ceremony.
    """
    model = _model(kind)
    parts = split_path(path)
    if not parts:
        raise ValueError("a label needs a name")

    parent_id = None
    node = None
    for part in parts:
        node = session.scalar(
            select(model).where(model.parent_id.is_(parent_id) if parent_id is None
                                else model.parent_id == parent_id,
                                func.lower(model.name) == part.lower())
        )
        if node is None:
            node = model(name=part, parent_id=parent_id)
            session.add(node)
            session.flush()
        parent_id = node.id
    return node


def path_of(session: Session, kind: str, node_id: int) -> str:
    """Walk parents to build the display path."""
    model = _model(kind)
    parts: list[str] = []
    seen: set[int] = set()
    current = session.get(model, node_id)
    while current is not None and current.id not in seen:
        seen.add(current.id)
        parts.append(current.name)
        current = session.get(model, current.parent_id) if current.parent_id else None
    return join_path(list(reversed(parts)))


def _descendant_ids(session: Session, kind: str, root_id: int) -> list[int]:
    """Every id at or below a node, via a recursive CTE.

    Exact by construction — no string prefixes, so "Frankfort" cannot pick up
    "Frankfort Annex".
    """
    model = _model(kind)
    top = select(model.id).where(model.id == root_id).cte("sub", recursive=True)
    tree = top.union_all(
        select(model.id).where(model.parent_id == top.c.id)
    )
    return list(session.scalars(select(tree.c.id)).all())


def _own_works(session: Session, kind: str) -> dict[int, set[int]]:
    """Which works hang off each node, as sets.

    Sets rather than counts, because the rollup has to be a **distinct** count
    and not a sum. A book can be in Sonlight Core B *and* Core D — Sonlight
    reuses titles across cores — so adding the children together would report
    six books where three exist. Places happen to be immune, since a copy is in
    one place, but getting it right once beats being right by accident in half
    the cases.

    One query, and the sets are bounded by the number of assignments, which for
    this library is a few thousand integers.
    """
    if kind == "place":
        rows = session.execute(
            select(Copy.location_id, Copy.work_id).where(Copy.location_id.is_not(None))
        ).all()
    else:
        rows = session.execute(select(WorkTag.tag_id, WorkTag.work_id)).all()

    out: dict[int, set[int]] = {}
    for node_id, work_id in rows:
        out.setdefault(int(node_id), set()).add(int(work_id))
    return out


def tree(session: Session, kind: str) -> list[LabelNode]:
    """The whole tree with rollup counts, ready to render.

    Built in Python from two flat queries rather than one recursive query per
    node: these trees are tens of nodes, not thousands, and a query per node
    would be a hundred round trips to answer one screen.
    """
    model = _model(kind)
    rows = session.scalars(
        select(model).order_by(model.sort_order, func.lower(model.name))
    ).all()
    own = _own_works(session, kind)

    nodes: dict[int, LabelNode] = {
        r.id: LabelNode(id=r.id, name=r.name, path=r.name, parent_id=r.parent_id,
                        depth=0, own_count=len(own.get(r.id, ())))
        for r in rows
    }

    roots: list[LabelNode] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_id) if node.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    def finish(node: LabelNode, prefix: str, depth: int) -> set[int]:
        node.depth = depth
        node.path = f"{prefix} {SEP} {node.name}" if prefix else node.name
        reach = set(own.get(node.id, ()))
        for child in node.children:
            reach |= finish(child, node.path, depth + 1)
        node.total_count = len(reach)
        return reach

    for root in roots:
        finish(root, "", 0)
    return roots


def flatten(roots: list[LabelNode]) -> list[LabelNode]:
    """Depth-first, for a list UI."""
    out: list[LabelNode] = []

    def walk(nodes: list[LabelNode]) -> None:
        for n in nodes:
            out.append(n)
            walk(n.children)

    walk(roots)
    return out


def works_in(session: Session, kind: str, node_id: int) -> list[int]:
    """Work ids at or below a node."""
    ids = _descendant_ids(session, kind, node_id)
    if not ids:
        return []
    if kind == "place":
        q = select(func.distinct(Copy.work_id)).where(Copy.location_id.in_(ids))
    else:
        q = select(func.distinct(WorkTag.work_id)).where(WorkTag.tag_id.in_(ids))
    return list(session.scalars(q).all())


def unplaced_count(session: Session) -> int:
    """Works with no copy in any place — the number that only goes down."""
    placed = select(func.distinct(Copy.work_id)).where(Copy.location_id.is_not(None))
    return int(
        session.scalar(
            select(func.count()).select_from(
                select(Work.id).where(Work.id.notin_(placed)).subquery()
            )
        )
        or 0
    )


def place_works(session: Session, work_ids: list[int], location_id: int | None) -> int:
    """Put every copy of these works in a place. Returns copies touched.

    Exclusive by construction: a copy has one ``location_id``, so assigning a
    new one replaces the old with no chance of a book accumulating
    contradictory places over years of reshuffling. Passing ``None`` unplaces.
    """
    if not work_ids:
        return 0
    now = func.now()
    result = session.execute(
        Copy.__table__.update()
        .where(Copy.work_id.in_(work_ids))
        .values(location_id=location_id, placed_at=None if location_id is None else now)
    )
    return int(result.rowcount or 0)


def tag_works(session: Session, work_ids: list[int], tag_id: int, add: bool) -> int:
    """Add or remove one tag across many works. Returns rows changed.

    Additive on purpose — Sonlight reuses titles across cores, so a book
    belonging to Core B must not stop it belonging to Core D.
    """
    if not work_ids:
        return 0
    if not add:
        result = session.execute(
            WorkTag.__table__.delete().where(
                WorkTag.work_id.in_(work_ids), WorkTag.tag_id == tag_id
            )
        )
        return int(result.rowcount or 0)

    existing = set(
        session.scalars(
            select(WorkTag.work_id).where(
                WorkTag.work_id.in_(work_ids), WorkTag.tag_id == tag_id
            )
        ).all()
    )
    fresh = [w for w in work_ids if w not in existing]
    if fresh:
        session.execute(
            WorkTag.__table__.insert(),
            [{"work_id": w, "tag_id": tag_id} for w in fresh],
        )
    return len(fresh)


__all__ = [
    "LabelNode",
    "SEP",
    "find_or_create",
    "flatten",
    "join_path",
    "path_of",
    "place_works",
    "split_path",
    "tag_works",
    "tree",
    "unplaced_count",
    "works_in",
]
