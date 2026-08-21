from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0002_add_user_role_mfa"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():

    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(length=50),
            nullable=False,
            server_default="viewer"
        )
    )

    op.add_column(
        "users",
        sa.Column(
            "mfa_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false"
        )
    )

    op.add_column(
        "users",
        sa.Column(
            "mfa_secret",
            sa.String(length=255),
            nullable=True
        )
    )

    op.add_column(
        "users",
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=True
        )
    )

    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True
        )
    )


def downgrade():

    op.drop_column(
        "users",
        "updated_at"
    )

    op.drop_column(
        "users",
        "created_at"
    )

    op.drop_column(
        "users",
        "mfa_secret"
    )

    op.drop_column(
        "users",
        "mfa_enabled"
    )

    op.drop_column(
        "users",
        "role"
    )
