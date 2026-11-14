"""Normalize task memory and track lifecycle without deleting source history.

Revision ID: d6e8f1a209bc
Revises: 2f5c65142b88
"""

import sqlalchemy as sa
from alembic import op

revision = "d6e8f1a209bc"
down_revision = "2f5c65142b88"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("instructions", "case_restrictions"):
        for field in ("effective_from", "review_at", "next_transition_at", "closed_at"):
            op.add_column(table, sa.Column(field, sa.DateTime(timezone=True)))
        op.create_index(f"ix_{table}_next_transition_at", table, ["next_transition_at"])
    for table in ("instructions", "instruction_revisions"):
        op.add_column(
            table, sa.Column("status", sa.String(), server_default="ACTIVE", nullable=False)
        )
    for field in ("effective_from", "review_at", "closed_at"):
        op.add_column("instruction_revisions", sa.Column(field, sa.DateTime(timezone=True)))
    for field in ("supersedes_id", "superseded_by_id"):
        op.add_column("case_restrictions", sa.Column(field, sa.Uuid()))
    op.drop_constraint("ck_case_restrictions_case_restriction_state", "case_restrictions")
    op.create_check_constraint(
        "case_restriction_state",
        "case_restrictions",
        "status IN ('OPEN','RESOLVED','SUPERSEDED') AND revision > 0",
    )
    op.create_table(
        "task_memory_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("firms.id"), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        *[
            sa.Column(f, sa.DateTime(timezone=True))
            for f in (
                "due_at",
                "expires_at",
                "effective_from",
                "review_at",
                "next_transition_at",
                "closed_at",
            )
        ],
        *[
            sa.Column(f, sa.Uuid())
            for f in ("supersedes_id", "superseded_by_id", "resolution_source_id")
        ],
        sa.Column("resolution_reason", sa.String()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "resolution_source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
        sa.CheckConstraint("kind IN ('STOP_CONTACT','OBLIGATION','NOTE')", name="task_memory_kind"),
        sa.CheckConstraint(
            "status IN ('OPEN','RESOLVED','SUPERSEDED','EXPIRED') AND revision > 0",
            name="task_memory_state",
        ),
        sa.CheckConstraint("expires_at IS NULL OR kind = 'NOTE'", name="only_notes_expire"),
    )
    op.create_index(
        "ix_task_memory_scope", "task_memory_items", ["tenant_id", "case_id", "task_id", "status"]
    )
    op.create_index(
        "ix_task_memory_items_next_transition_at", "task_memory_items", ["next_transition_at"]
    )
    # Retain every legacy item and its existing source links; remove the duplicate JSON projection.
    op.execute(
        sa.text("""
        INSERT INTO task_memory_items
        (id,tenant_id,case_id,task_id,created_at,kind,text,status,revision,source_id,due_at,
         resolution_source_id,resolution_reason)
        SELECT (m->>'id')::uuid,t.tenant_id,t.case_id,t.id,
               (SELECT s.created_at FROM sources s WHERE s.id=(m->>'source_id')::uuid
                AND s.tenant_id=t.tenant_id),
               m->>'kind',m->>'text',m->>'status',(m->>'revision')::integer,
               (m->>'source_id')::uuid,(m->>'due_at')::timestamptz,
               (m->>'resolution_source_id')::uuid,m->>'resolution_reason'
        FROM tasks t CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(t.progress_json->'memory_items','[]'::jsonb)) m
    """)
    )
    op.execute("""
        UPDATE task_memory_items m SET closed_at=s.created_at FROM sources s
        WHERE m.resolution_source_id=s.id AND m.tenant_id=s.tenant_id
    """)
    op.execute("""
        UPDATE case_restrictions m SET closed_at=s.created_at FROM sources s
        WHERE m.resolution_source_id=s.id AND m.tenant_id=s.tenant_id
    """)
    op.execute(
        "UPDATE tasks SET progress_json = progress_json - 'memory_items' "
        "WHERE progress_json ? 'memory_items'"
    )


def downgrade():
    # Legacy binaries cannot represent retirement or timed lifecycles safely.
    incompatible = op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS (SELECT 1 FROM instructions
                       WHERE status <> 'ACTIVE' OR effective_from IS NOT NULL
                          OR review_at IS NOT NULL)
        OR EXISTS (SELECT 1 FROM case_restrictions
                   WHERE effective_from IS NOT NULL OR review_at IS NOT NULL)
        OR EXISTS (SELECT 1 FROM task_memory_items
                   WHERE kind = 'NOTE' OR effective_from IS NOT NULL OR review_at IS NOT NULL)
    """)
    )
    if incompatible:
        raise RuntimeError(
            "Resolve lifecycle settings before downgrade; older code cannot enforce them"
        )
    # Keep old clients' memory intact when returning to the JSON representation.
    op.execute("""
        UPDATE tasks t SET progress_json = t.progress_json || jsonb_build_object('memory_items', (
            SELECT jsonb_agg(to_jsonb(m) - 'tenant_id' - 'case_id' - 'task_id' - 'created_at')
            FROM task_memory_items m WHERE m.task_id=t.id AND m.tenant_id=t.tenant_id
        )) WHERE EXISTS (SELECT 1 FROM task_memory_items m WHERE m.task_id=t.id)
    """)
    op.drop_table("task_memory_items")
    # Older code cannot represent supersession; preserve it as a resolved historical hold.
    op.execute("UPDATE case_restrictions SET status='RESOLVED' WHERE status='SUPERSEDED'")
    op.drop_constraint("ck_case_restrictions_case_restriction_state", "case_restrictions")
    op.create_check_constraint(
        "case_restriction_state",
        "case_restrictions",
        "status IN ('OPEN','RESOLVED') AND revision > 0",
    )
    for field in ("supersedes_id", "superseded_by_id"):
        op.drop_column("case_restrictions", field)
    for table in ("instructions", "instruction_revisions"):
        op.drop_column(table, "status")
    for field in ("effective_from", "review_at", "closed_at"):
        op.drop_column("instruction_revisions", field)
    for table in ("instructions", "case_restrictions"):
        op.drop_index(f"ix_{table}_next_transition_at", table_name=table)
        for field in ("effective_from", "review_at", "next_transition_at", "closed_at"):
            op.drop_column(table, field)
