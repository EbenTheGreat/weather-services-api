"""Initial baseline

Revision ID: 31691a820f68
Revises: 81a2c06d0183
Create Date: 2026-04-07 11:42:44.538518

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlmodel import SQLModel


# revision identifiers, used by Alembic.
revision: str = '31691a820f68'
down_revision: Union[str, Sequence[str], None] = '81a2c06d0183'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
