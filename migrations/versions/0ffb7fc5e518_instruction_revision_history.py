"""instruction revision history

Revision ID: 0ffb7fc5e518
Revises: 236b2317dd38
"""

import sqlalchemy as sa
from alembic import op

revision = "0ffb7fc5e518"
down_revision = "236b2317dd38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_instructions_tenant_id_id", "instructions", ["tenant_id", "id"])
    op.create_table(
        "instruction_revisions",
        sa.Column("instruction_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("provider_contact_id", sa.Uuid(), nullable=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision > 0", name=op.f("ck_instruction_revisions_positive_instruction_revision")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "instruction_id"],
            ["instructions.tenant_id", "instructions.id"],
            name=op.f("fk_instruction_revisions_tenant_id_instruction_id_instructions"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["firms.id"], name=op.f("fk_instruction_revisions_tenant_id_firms")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruction_revisions")),
        sa.UniqueConstraint(
            "tenant_id",
            "instruction_id",
            "revision",
            name=op.f("uq_instruction_revisions_tenant_id_instruction_id_revision"),
        ),
    )
    op.execute("""
        INSERT INTO instruction_revisions
            (id, tenant_id, instruction_id, revision, scope, provider_contact_id,
             text, approved_by, reason)
        SELECT gen_random_uuid(), tenant_id, id, revision, scope, provider_contact_id,
               text, 'legacy-import', 'Current instruction preserved during migration'
        FROM instructions
    """)
    op.execute("""
        CREATE FUNCTION protect_instruction_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Published instruction revisions are immutable'; END $$
    """)
    op.execute("""
        CREATE TRIGGER instruction_history_immutable BEFORE UPDATE OR DELETE
        ON instruction_revisions FOR EACH ROW EXECUTE FUNCTION protect_instruction_history()
    """)


def downgrade() -> None:
    op.drop_table("instruction_revisions")
    op.execute("DROP FUNCTION protect_instruction_history()")
    op.drop_constraint("uq_instructions_tenant_id_id", "instructions", type_="unique")
