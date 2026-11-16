"""Email ownership and append-only result corrections."""
from alembic import op
import sqlalchemy as sa
revision = "20261116_02"
down_revision = "20261116_01"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "email_verifications" not in inspector.get_table_names():
        op.create_table("email_verifications",
            sa.Column("token_id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(64), sa.ForeignKey("account_users.user_id"), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_email_verifications_user_id", "email_verifications", ["user_id"])
    columns = {c["name"] for c in inspector.get_columns("account_users")}
    for name in ("email_verified_at", "deleted_at"):
        if name not in columns:
            op.add_column("account_users", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade():
    raise RuntimeError("Rollback application code without discarding audit records or customer verification data.")
