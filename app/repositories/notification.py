from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import NotificationEvent


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, subscription_id: str, event_type: str) -> bool:
        result = await self._session.execute(
            select(NotificationEvent.id).where(
                NotificationEvent.subscription_id == subscription_id,
                NotificationEvent.event_type == event_type,
            )
        )
        return result.scalar_one_or_none() is not None

    async def try_create(self, subscription_id: str, event_type: str) -> bool:
        """Insert and return True if newly created, False if already existed."""
        stmt = (
            insert(NotificationEvent)
            .values(subscription_id=subscription_id, event_type=event_type)
            .on_conflict_do_nothing(constraint="uq_notification_events_sub_type")
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1
