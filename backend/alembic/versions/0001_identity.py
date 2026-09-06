"""0001_identity

Revision ID: 0001_identity
Revises: None
Create Date: 2026-09-06

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_identity"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE users (
 id uuid PRIMARY KEY, email text NOT NULL UNIQUE,
 password_hash text NOT NULL, display_name text NOT NULL,
 role text NOT NULL DEFAULT 'member' CHECK (role IN ('member','admin')),
 enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(email=lower(btrim(email)) AND length(email)<=254),
 CHECK(length(display_name) BETWEEN 1 AND 80)
)
""")

    op.execute("""
CREATE TABLE invitations (
 id uuid PRIMARY KEY, email text NOT NULL, token_hash char(64) NOT NULL UNIQUE,
 role text NOT NULL CHECK(role IN ('member','admin')),
 created_by uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL, consumed_at timestamptz,
 CHECK(email=lower(btrim(email)) AND length(email)<=254), CHECK(expires_at>created_at)
)
""")

    op.execute("CREATE INDEX invitations_email_idx ON invitations(email)")

    op.execute("""
CREATE TABLE refresh_tokens (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
 token_hash char(64) NOT NULL UNIQUE,
 family_id uuid NOT NULL, parent_id uuid REFERENCES refresh_tokens(id),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 family_expires_at timestamptz NOT NULL, used_at timestamptz, revoked_at timestamptz,
 CHECK(expires_at>created_at AND expires_at<=family_expires_at)
)
""")

    op.execute(
        "CREATE UNIQUE INDEX refresh_one_child_idx "
        "ON refresh_tokens(parent_id) WHERE parent_id IS NOT NULL"
    )
    op.execute("CREATE INDEX refresh_family_idx ON refresh_tokens(family_id)")
    op.execute("CREATE INDEX refresh_user_idx ON refresh_tokens(user_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS refresh_user_idx")
    op.execute("DROP INDEX IF EXISTS refresh_family_idx")
    op.execute("DROP INDEX IF EXISTS refresh_one_child_idx")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
    op.execute("DROP INDEX IF EXISTS invitations_email_idx")
    op.execute("DROP TABLE IF EXISTS invitations")
    op.execute("DROP TABLE IF EXISTS users")
