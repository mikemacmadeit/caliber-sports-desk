"""Account-scoped saved research, independent of the immutable published record."""
from alembic import op
import sqlalchemy as sa

revision = "20261116_03"
down_revision = "20261116_02"
branch_labels = None
depends_on = None


def upgrade():
    if "watchlist_entries" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("watchlist_entries",
            sa.Column("user_id", sa.String(64), sa.ForeignKey("account_users.user_id"), primary_key=True),
            sa.Column("selection_id", sa.String(24), primary_key=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    raise RuntimeError("Retain customer watchlists when rolling application code back.")
