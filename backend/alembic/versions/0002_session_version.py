"""add revocable JWT session version

Revision ID: 0002_session_version
Revises: 0001_initial
"""


revision = "0002_session_version"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # session_version is already part of the initial User model.
    # 0001_initial creates it through Base.metadata.create_all().
    pass


def downgrade() -> None:
    # Intentionally left empty because session_version belongs
    # to the initial schema.
    pass