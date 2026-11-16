"""Share validated model inputs across independently scheduled containers."""
from alembic import op
import sqlalchemy as sa

revision = "20261116_04"
down_revision = "20261116_03"
branch_labels = None
depends_on = None


def upgrade():
    if "input_artifacts" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("input_artifacts",
            sa.Column("key", sa.String(128), primary_key=True),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("content", sa.LargeBinary(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("byte_count", sa.Integer(), nullable=False))


def downgrade():
    raise RuntimeError("Retain shared input provenance when rolling application code back.")
