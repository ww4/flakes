"""Root-level tag names get a real uniqueness guarantee.

7e151d52129b replaced the global tag-name unique with
UNIQUE(parent_id, name) — but Postgres treats NULLs as distinct in unique
constraints (NULLS NOT DISTINCT is PG15+ and production is 14), so at the
root level, where most tags live, the constraint is inert: two root tags
named "Science" insert without complaint. Verified live in the 2026-08
audit. The fix that works on 14 is a partial unique index, on lower(name)
to match the app's case-insensitive identity rule (labels.find_or_create).

Because the old API add_tag endpoint uppercased names while the labels page
preserved case, real databases can already hold case-twins ("SELL"/"sell").
The upgrade folds those first — links repointed to the survivor, children
re-parented — or the index build would fail on exactly the data that proves
it necessary.

Revision ID: 556ec74ecae5
Revises: 7e151d52129b
Create Date: 2026-08-17
"""

from alembic import op
import sqlalchemy as sa

revision = '556ec74ecae5'
down_revision = '7e151d52129b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Fold root-level case-twins into the lowest-id survivor.
    dupes = conn.execute(sa.text(
        """
        SELECT lower(name) AS key, array_agg(id ORDER BY id) AS ids
        FROM tags WHERE parent_id IS NULL
        GROUP BY lower(name) HAVING count(*) > 1
        """
    )).all()
    for _key, ids in dupes:
        keeper, extras = ids[0], ids[1:]
        for extra in extras:
            # Repoint work links, skipping ones the keeper already has
            # (composite primary key).
            conn.execute(sa.text(
                """
                UPDATE work_tags SET tag_id = :keeper
                WHERE tag_id = :extra
                  AND work_id NOT IN
                      (SELECT work_id FROM work_tags WHERE tag_id = :keeper)
                """
            ), {"keeper": keeper, "extra": extra})
            conn.execute(sa.text(
                "DELETE FROM work_tags WHERE tag_id = :extra"
            ), {"extra": extra})
            # Children of the duplicate move under the survivor.
            conn.execute(sa.text(
                "UPDATE tags SET parent_id = :keeper WHERE parent_id = :extra"
            ), {"keeper": keeper, "extra": extra})
            conn.execute(sa.text(
                "DELETE FROM tags WHERE id = :extra"
            ), {"extra": extra})

    op.create_index(
        "uq_tags_root_lower_name",
        "tags",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )


def downgrade() -> None:
    # The fold of case-twins is not reversed — it removed genuine duplicates.
    op.drop_index("uq_tags_root_lower_name", table_name="tags")
