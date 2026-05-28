"""add family fields to user

Revision ID: a0d7a2af58c8
Revises: 26c663ce6cb6
Create Date: 2026-05-28 18:43:12.148440

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a0d7a2af58c8'
down_revision: Union[str, Sequence[str], None] = '26c663ce6cb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspect = sa.inspect(bind)
    columns = [c['name'] for c in inspect.get_columns('users')]
    
    with op.batch_alter_table('users') as batch_op:
        if 'family_host_id' not in columns:
            batch_op.add_column(sa.Column('family_host_id', sa.BigInteger(), nullable=True))
        if 'invite_code' not in columns:
            batch_op.add_column(sa.Column('invite_code', sa.String(), nullable=True))

    # Add unique constraint in a separate batch block if it doesn't exist
    constraints = inspect.get_unique_constraints('users')
    has_uq = any(c['name'] == 'uq_users_invite_code' for c in constraints)
    if not has_uq:
        with op.batch_alter_table('users') as batch_op:
            batch_op.create_unique_constraint('uq_users_invite_code', ['invite_code'])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspect = sa.inspect(bind)
    columns = [c['name'] for c in inspect.get_columns('users')]
    
    with op.batch_alter_table('users') as batch_op:
        if 'invite_code' in columns:
            batch_op.drop_constraint('uq_users_invite_code', type_='unique')
            batch_op.drop_column('invite_code')
        if 'family_host_id' in columns:
            batch_op.drop_column('family_host_id')
