"""client report human review

Revision ID: 4263a5a26181
Revises: a8121096bc69
"""

import sqlalchemy as sa
from alembic import op

revision = "4263a5a26181"
down_revision = "a8121096bc69"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "client_reviews",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("firms.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("report_id", sa.UUID(), nullable=False),
        sa.Column("report_fingerprint", sa.String(), nullable=False),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String()),
        sa.Column("reason", sa.String()),
        sa.Column("decided_by", sa.String()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_source_id", sa.UUID()),
        sa.UniqueConstraint("tenant_id", "report_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "report_id"],
            ["client_reports.tenant_id", "client_reports.case_id", "client_reports.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "decision_source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
        sa.CheckConstraint("version > 0", name="client_review_version"),
        sa.CheckConstraint("status IN ('OPEN','RESOLVED')", name="client_review_status"),
        sa.CheckConstraint(
            "(status = 'RESOLVED' AND COALESCE(decision, '') = 'ALLOW_COMPLETION') OR "
            "(status = 'OPEN' AND (decision IS NULL OR decision = 'REQUEST_CHANGES'))",
            name="client_review_decision",
        ),
    )
    # Existing active sensitive reports also need an actionable review after upgrade.
    import hashlib
    import json
    from uuid import uuid4

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT r.id, r.tenant_id, r.case_id, r.report, r.changes, s.content, c.task_id "
            "FROM client_reports r JOIN client_check_ins c ON c.id=r.check_in_id "
            "AND c.tenant_id=r.tenant_id AND c.case_id=r.case_id "
            "JOIN tasks t ON t.id=c.task_id AND t.tenant_id=c.tenant_id AND t.case_id=c.case_id "
            "JOIN sources s ON s.id=r.source_id AND s.tenant_id=r.tenant_id "
            "AND s.case_id=r.case_id "
            "WHERE r.needs_review AND t.status NOT IN ('COMPLETED','FAILED','CANCELLED')"
        )
    ).mappings()
    for row in rows:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"report": row["report"], "changes": row["changes"], "source": row["content"]},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        connection.execute(
            table.insert().values(
                id=uuid4(),
                tenant_id=row["tenant_id"],
                case_id=row["case_id"],
                task_id=row["task_id"],
                report_id=row["id"],
                report_fingerprint=fingerprint,
                question="Client self-report needs teammate review: "
                + "; ".join(row["changes"]["reasons"]),
                status="OPEN",
                version=1,
            )
        )


def downgrade():
    op.drop_table("client_reviews")
