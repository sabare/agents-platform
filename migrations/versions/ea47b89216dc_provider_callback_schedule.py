"""Persist provider-specific callback appointments.

Revision ID: ea47b89216dc
Revises: 194c3e8f82b4
"""

import sqlalchemy as sa
from alembic import op

revision = "ea47b89216dc"
down_revision = "194c3e8f82b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("records_requests", sa.Column("callback_at", sa.DateTime(timezone=True)))
    op.add_column("records_requests", sa.Column("callback_timezone", sa.String()))


def downgrade():
    op.drop_column("records_requests", "callback_timezone")
    op.drop_column("records_requests", "callback_at")
