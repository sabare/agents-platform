"""Make surgical notes an explicit, separately tracked requirement."""

from alembic import op

revision = "ab37d1e2c904"
down_revision = "f13a17c8e901"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_record_requirements_record_type", "record_requirements")
    op.create_check_constraint(
        "record_type",
        "record_requirements",
        "record_type IN ('RECORDS','BILLS','IMAGING','SURGICAL_NOTES')",
    )


def downgrade():
    # Do not remove the new label from existing evidence during rollback.
    pass
