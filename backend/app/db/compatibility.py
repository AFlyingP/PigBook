import json
from pathlib import Path
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class SchemaIncompatibleError(Exception):
    """Raised when the database schema does not match the supported allowlist."""


def load_supported_revisions(
    config_path: Path | str | None = None,
) -> list[str]:
    """Load supported Alembic revisions from schema_compatibility.json."""
    if config_path is None:
        path = Path(__file__).parent / "schema_compatibility.json"
    else:
        path = Path(config_path)

    if not path.is_file():
        raise SchemaIncompatibleError(f"Compatibility file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SchemaIncompatibleError(
            f"Failed to parse schema compatibility config: {exc}"
        ) from exc

    revisions = data.get("supported_revisions")
    if not isinstance(revisions, list) or not revisions:
        raise SchemaIncompatibleError(
            "schema_compatibility.json must contain a nonempty supported_revisions list"
        )

    return [str(r) for r in revisions]


async def validate_schema_compatibility(
    session: AsyncSession,
    supported_revisions: Sequence[str] | None = None,
) -> str:
    """Validate that the live database has exactly one revision head present in the allowlist.

    Enforces:
    1. Exactly one row in alembic_version (missing head or multiple heads must fail).
    2. The version_num is in the supported allowlist.
    3. No lexical comparison of revision identifiers is performed.
    """
    if supported_revisions is None:
        supported = load_supported_revisions()
    else:
        supported = list(supported_revisions)

    stmt = text("SELECT version_num FROM alembic_version")
    result = await session.execute(stmt)
    rows = [row[0] for row in result.fetchall()]

    if len(rows) == 0:
        raise SchemaIncompatibleError("Missing Alembic revision head in database")

    if len(rows) > 1:
        raise SchemaIncompatibleError(f"Multiple Alembic revision heads detected: {rows}")

    head = str(rows[0])
    if head not in supported:
        raise SchemaIncompatibleError(
            f"Unsupported Alembic revision head: {head!r}. Supported revisions: {supported}"
        )

    return head
