"""bounded simulated records reminders

Revision ID: 2f5c65142b88
Revises: b44252f91811
"""

import sqlalchemy as sa
from alembic import op

revision = "2f5c65142b88"
down_revision = "b44252f91811"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "records_requests",
        sa.Column("reminder_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "records_requests",
        sa.Column("max_reminders", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column(
        "records_requests",
        sa.Column("reminder_interval_seconds", sa.Integer(), server_default="30", nullable=False),
    )
    op.add_column("records_requests", sa.Column("next_reminder_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "records_reminder_bounds",
        "records_requests",
        "reminder_count BETWEEN 0 AND max_reminders AND max_reminders BETWEEN 1 AND 3 "
        "AND reminder_interval_seconds >= 10",
    )


def downgrade():
    op.drop_constraint(
        op.f("ck_records_requests_records_reminder_bounds"), "records_requests", type_="check"
    )
    for name in (
        "next_reminder_at",
        "reminder_interval_seconds",
        "max_reminders",
        "reminder_count",
    ):
        op.drop_column("records_requests", name)
