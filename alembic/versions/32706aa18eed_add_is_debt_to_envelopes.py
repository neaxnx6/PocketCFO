"""add_is_debt_to_envelopes

Revision ID: 32706aa18eed
Revises: 1071c9c000c4
Create Date: 2026-04-13 19:50:56.007436

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '32706aa18eed'
down_revision: Union[str, Sequence[str], None] = '1071c9c000c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # SQLite cannot add NOT NULL column without a default to an existing table.
    # Fix: use server_default so existing rows get a proper value.
    op.add_column('envelopes', sa.Column(
        'is_debt', sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    op.add_column('envelopes', sa.Column(
        'is_goal', sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    # monthly_income on User — nullable, no default needed
    op.add_column('users', sa.Column(
        'monthly_income', sa.Float(), nullable=True
    ))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('envelopes', 'is_debt')
    op.drop_column('envelopes', 'is_goal')
    op.drop_column('users', 'monthly_income')
