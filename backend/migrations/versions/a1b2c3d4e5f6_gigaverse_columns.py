"""Add Gigaverse columns to users table.

Revision ID: a1b2c3d4e5f6
Revises: 9fe80e1cc768
Create Date: 2026-03-27 13:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9fe80e1cc768"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Schema used by this project
SCHEMA = "upvote_bot"


def upgrade() -> None:
    # Idempotent — safe to re-run
    op.execute(f"""
        ALTER TABLE {SCHEMA}.users
            ADD COLUMN IF NOT EXISTS gigaverse_jwt_enc  TEXT,
            ADD COLUMN IF NOT EXISTS gigaverse_last_run TIMESTAMPTZ
    """)


def downgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.users
            DROP COLUMN IF EXISTS gigaverse_jwt_enc,
            DROP COLUMN IF EXISTS gigaverse_last_run
    """)
