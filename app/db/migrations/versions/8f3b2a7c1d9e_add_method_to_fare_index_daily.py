"""add method to fare_index_daily

Revision ID: 8f3b2a7c1d9e
Revises: 6c88c6e41e45
Create Date: 2026-09-06 15:30:00.000000

Lets a second index-construction formula (see app/index/psd_index.py's
module docstring) be computed and persisted alongside the original one
instead of overwriting it -- every row that already exists was produced by
the original arithmetic-mean-of-relatives (Carli-style) formula, so this
backfills them as 'carli_arithmetic_v1' rather than leaving method blank,
and widens the upsert key from (index_date, lead_window) to
(index_date, lead_window, method) so a newer method's rows land as
additional rows, never as an overwrite of the old ones. Reverting to the
old formula is then just flipping app.index.psd_index.ACTIVE_INDEX_METHOD
back -- no migration, no data loss, no downtime.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f3b2a7c1d9e'
down_revision: Union[str, Sequence[str], None] = '6c88c6e41e45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'fare_index_daily',
        sa.Column('method', sa.String(), nullable=False, server_default='carli_arithmetic_v1'),
    )
    op.drop_constraint('uq_fare_index_daily_date_window', 'fare_index_daily', type_='unique')
    op.create_unique_constraint(
        'uq_fare_index_daily_date_window_method',
        'fare_index_daily',
        ['index_date', 'lead_window', 'method'],
    )
    # Existing rows are backfilled by the server_default above; new rows
    # always pass method explicitly (see psd_index.py), so the column
    # shouldn't keep silently defaulting for anything written from here on.
    op.alter_column('fare_index_daily', 'method', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    # A row under any method other than the original can't satisfy the old
    # (index_date, lead_window)-only uniqueness, so restoring that
    # constraint means removing those rows first. This only ever touches
    # rows written under the newer method(s) -- every 'carli_arithmetic_v1'
    # row (the full original series) is untouched and stays intact.
    op.execute("DELETE FROM fare_index_daily WHERE method <> 'carli_arithmetic_v1'")
    op.drop_constraint('uq_fare_index_daily_date_window_method', 'fare_index_daily', type_='unique')
    op.create_unique_constraint(
        'uq_fare_index_daily_date_window',
        'fare_index_daily',
        ['index_date', 'lead_window'],
    )
    op.drop_column('fare_index_daily', 'method')
