"""add due day to envelopes

Revision ID: 80b8ad6d3964
Revises: c98fd74b3d23
Create Date: 2026-06-09 15:02:26.738499

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '80b8ad6d3964'
down_revision: Union[str, Sequence[str], None] = 'c98fd74b3d23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('envelopes')]
    if 'due_day' not in columns:
        op.add_column('envelopes', sa.Column('due_day', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('envelopes')]
    if 'due_day' in columns:
        op.drop_column('envelopes', 'due_day')
