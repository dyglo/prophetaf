"""add historical candles table"""

from alembic import op
import sqlalchemy as sa


revision = "20260401_0005"
down_revision = "20260311_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_candles",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pair", sa.String(length=20), nullable=False),
        sa.Column("granularity", sa.String(length=10), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "time",
            "pair",
            "granularity",
            name="uq_historical_candles_time_pair_granularity",
        ),
    )
    op.create_index(
        "ix_historical_candles_pair_granularity_time",
        "historical_candles",
        ["pair", "granularity", "time"],
    )


def downgrade() -> None:
    op.drop_index("ix_historical_candles_pair_granularity_time", table_name="historical_candles")
    op.drop_table("historical_candles")
