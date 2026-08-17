"""drop the superseded wishlist table

The wishlist table was designed before want-rules existed. A rule says
"complete this series" or "anything by this author" and derives its entries,
which is what the sale-day list actually needs; a flat list of wanted works
cannot express either. WantRule/WantEntry replaced it and nothing has written
to `wishlist` since — it holds no rows in any database.

Revision ID: 76d131433779
Revises: 5c61dbdfda47
Create Date: 2026-08-13 00:44:19.580254
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '76d131433779'
down_revision = '5c61dbdfda47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The docstring's premise ("it holds no rows in any database"), enforced
    # rather than asserted. On any database where it is wrong — a restore
    # from an old dump, someone else's instance — destroying data silently
    # is the one thing a migration must never do.
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM wishlist")).scalar()
    if count:
        raise RuntimeError(
            f"wishlist holds {count} row(s); this migration expects it empty. "
            "Export or migrate those rows to want_rules first."
        )
    op.drop_table("wishlist")
    sa.Enum(name="wishlist_reason").drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # postgresql.ENUM, not sa.Enum: create_type is a dialect option and plain
    # sa.Enum silently ignores it, so create_table emits CREATE TYPE a second
    # time and the downgrade dies on "type wishlist_reason already exists".
    # An untested downgrade is not a downgrade.
    reason = postgresql.ENUM(
        "series_gap", "author_gap", "flood_loss", "manual",
        name="wishlist_reason", create_type=False,
    )
    reason.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "wishlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("reason", reason, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("satisfied_by_copy_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"]),
        sa.ForeignKeyConstraint(["satisfied_by_copy_id"], ["copies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_id", "reason", name="uq_wishlist_work_reason"),
    )
    op.create_index(op.f("ix_wishlist_work_id"), "wishlist", ["work_id"])
