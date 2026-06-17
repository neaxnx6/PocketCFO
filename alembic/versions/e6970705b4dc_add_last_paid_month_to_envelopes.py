"""add last_paid_month to envelopes

Revision ID: e6970705b4dc
Revises: 80b8ad6d3964
Create Date: 2026-06-17 19:21:37.774654

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6970705b4dc'
down_revision: Union[str, Sequence[str], None] = '80b8ad6d3964'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('envelopes', sa.Column('last_paid_month', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('envelopes', 'last_paid_month')
