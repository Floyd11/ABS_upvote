"""Initial tables

Revision ID: 9fe80e1cc768
Revises: 
Create Date: 2026-03-16 10:26:25.688321

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9fe80e1cc768'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create schema if not exists
    op.execute("CREATE SCHEMA IF NOT EXISTS upvote")
    
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('wallet_address', sa.String(length=42), nullable=False),
        sa.Column('session_key_enc', sa.String(), nullable=False),
        sa.Column('session_expires_at', sa.DateTime(timezone=sa.timezone.utc), nullable=True),
        sa.Column('voting_contract', sa.String(length=42), nullable=False),
        sa.Column('base_vote_second', sa.Integer(), nullable=False),
        sa.Column('current_epoch', sa.Integer(), nullable=False),
        sa.Column('week_app_ids', sa.ARRAY(sa.Integer()), nullable=True),
        sa.Column('week_app_index', sa.Integer(), nullable=False),
        sa.Column('last_voted_at', sa.DateTime(timezone=sa.timezone.utc), nullable=True),
        sa.Column('streak_days', sa.Integer(), nullable=False),
        sa.Column('total_votes', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=sa.timezone.utc), nullable=False),
        sa.PrimaryKeyConstraint('id', name='users_pkey'),
        sa.UniqueConstraint('wallet_address', name='users_wallet_address_key'),
        schema='upvote'
    )
    op.create_index('ix_upvote_users_wallet_address', 'users', ['wallet_address'], unique=True, schema='upvote')

    op.create_table(
        'vote_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('app_id', sa.Integer(), nullable=False),
        sa.Column('epoch', sa.Integer(), nullable=False),
        sa.Column('tx_hash', sa.String(length=66), nullable=True),
        sa.Column('voted_at', sa.DateTime(timezone=sa.timezone.utc), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('error_msg', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['upvote.users.id'], name='vote_log_user_id_fkey'),
        sa.PrimaryKeyConstraint('id', name='vote_log_pkey'),
        schema='upvote'
    )
    op.create_index('ix_vote_log_user_epoch', 'vote_log', ['user_id', 'epoch'], unique=False, schema='upvote')


def downgrade() -> None:
    op.drop_table('vote_log', schema='upvote')
    op.drop_table('users', schema='upvote')
