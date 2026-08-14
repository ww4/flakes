"""label trees: tag hierarchy, optional location kind, placed_at

Locations and tags both become trees, because the question Chris actually
wants answered is "how many books are in this location across all
sublocations" — and the same question applies to "how many Sonlight books".

Three changes:

* ``tags`` gains ``parent_id`` and ``sort_order``. It was a flat unique name,
  which cannot express Sonlight / Core B, and alphabetical ordering puts
  "Grade / 10" before "Grade / 2".
* ``tags.name`` loses its global unique constraint in favour of unique per
  parent, so "Science" can sit under more than one group.
* ``locations.kind`` becomes nullable. The tree carries the meaning and
  demanding a taxonomy at creation is friction on the one action that has to
  stay cheap.
* ``copies.placed_at`` records when a copy was last said to be somewhere.

Revision ID: 7e151d52129b
Revises: 76d131433779
Create Date: 2026-08-13 21:50:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '7e151d52129b'
down_revision = '76d131433779'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tags", sa.Column("parent_id", sa.Integer(), nullable=True))
    op.add_column(
        "tags",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_tags_parent_id", "tags", ["parent_id"])
    op.create_foreign_key("fk_tags_parent", "tags", "tags", ["parent_id"], ["id"])

    # Was unique across every tag; now unique within a parent.
    op.drop_constraint("tags_name_key", "tags", type_="unique")
    op.create_unique_constraint("uq_tag_parent_name", "tags", ["parent_id", "name"])

    op.alter_column("locations", "kind", existing_type=sa.Enum(name="location_kind"),
                    nullable=True)
    op.add_column(
        "copies", sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("copies", "placed_at")
    op.alter_column("locations", "kind", existing_type=sa.Enum(name="location_kind"),
                    nullable=False)

    op.drop_constraint("uq_tag_parent_name", "tags", type_="unique")
    op.create_unique_constraint("tags_name_key", "tags", ["name"])
    op.drop_constraint("fk_tags_parent", "tags", type_="foreignkey")
    op.drop_index("ix_tags_parent_id", table_name="tags")
    op.drop_column("tags", "sort_order")
    op.drop_column("tags", "parent_id")
