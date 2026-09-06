"""0004_operations

Revision ID: 0004_operations
Revises: 0003_delivery
Create Date: 2026-09-06

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_operations"
down_revision: Union[str, Sequence[str], None] = "0003_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE audit_log (
 id uuid PRIMARY KEY, actor_id uuid REFERENCES users(id), action text NOT NULL,
 target_type text NOT NULL, target_id uuid, request_id uuid NOT NULL,
 details jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
)
""")

    op.execute("CREATE INDEX audit_created_idx ON audit_log(created_at DESC,id)")
    op.execute("CREATE INDEX audit_target_idx ON audit_log(target_type,target_id,created_at)")

    op.execute("""
CREATE TABLE feedback (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
 rating smallint NOT NULL CHECK(rating BETWEEN 1 AND 5),
 task_completed boolean NOT NULL,
 difficulty text NOT NULL CHECK(length(difficulty)<=2000),
 improvement text NOT NULL CHECK(length(improvement)<=2000),
 consent_version text NOT NULL CHECK(consent_version='2026-09-v1'),
 created_at timestamptz NOT NULL DEFAULT now()
)
""")

    op.execute("CREATE INDEX feedback_user_idx ON feedback(user_id)")

    op.execute("""
CREATE TABLE rate_limits (
 scope text NOT NULL, identity_hash char(64) NOT NULL, window_start timestamptz NOT NULL,
 count integer NOT NULL CHECK(count>0), PRIMARY KEY(scope,identity_hash,window_start)
)
""")

    op.execute("""
CREATE TABLE worker_heartbeat (
 name text PRIMARY KEY CHECK(name='primary'), seen_at timestamptz NOT NULL,
 expiry_scan_at timestamptz NOT NULL
)
""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_heartbeat")
    op.execute("DROP TABLE IF EXISTS rate_limits")
    op.execute("DROP INDEX IF EXISTS feedback_user_idx")
    op.execute("DROP TABLE IF EXISTS feedback")
    op.execute("DROP INDEX IF EXISTS audit_target_idx")
    op.execute("DROP INDEX IF EXISTS audit_created_idx")
    op.execute("DROP TABLE IF EXISTS audit_log")
