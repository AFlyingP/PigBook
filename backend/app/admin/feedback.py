import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import Feedback as FeedbackModel
from app.admin.schemas import Feedback as FeedbackSchema
from app.admin.schemas import FeedbackCreate, FeedbackCreateResult
from app.auth.dependencies import AuthorizedScope
from app.resources.schemas import Page


class ConsentRequiredError(Exception):
    def __init__(
        self,
        message: str = "Affirmative consent and consent_version '2026-09-v1' are required",
    ) -> None:
        self.message = message
        super().__init__(message)


async def create_feedback(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: FeedbackCreate,
    now: datetime,
) -> FeedbackCreateResult:
    if scope.principal_id is None:
        raise ValueError("Principal ID required for feedback submission")

    if data.consent is not True or data.consent_version != "2026-09-v1":
        raise ConsentRequiredError("Affirmative consent and current consent_version required")

    feedback_id = uuid.uuid4()
    entry = FeedbackModel(
        id=feedback_id,
        user_id=scope.principal_id,
        rating=data.rating,
        task_completed=data.task_completed,
        difficulty=data.difficulty,
        improvement=data.improvement,
        consent_version=data.consent_version,
        created_at=now,
    )
    session.add(entry)
    await session.flush()

    return FeedbackCreateResult(id=feedback_id, created_at=now)


async def list_feedback(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
) -> Page[FeedbackSchema]:
    count_stmt = select(func.count(FeedbackModel.id))
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(FeedbackModel)
        .order_by(FeedbackModel.created_at.desc(), FeedbackModel.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    items = [FeedbackSchema.model_validate(row) for row in rows]
    return Page[FeedbackSchema](items=items, total=total, limit=limit, offset=offset)
