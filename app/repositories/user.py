import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import User

logger = logging.getLogger(__name__)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        tg_id: int,
        username: str | None,
        first_name: str,
        last_name: str | None,
        referral_code: str | None = None,
    ) -> User:
        stmt = (
            insert(User)
            .values(
                id=tg_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                referral_code=referral_code,
                registered_at=datetime.now(tz=timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
                # No WHERE — referral_code and registered_at are not in set_{},
                # so they are never overwritten regardless.
            )
        )
        await self._session.execute(stmt)

        result = await self._session.execute(select(User).where(User.id == tg_id))
        user = result.scalar_one()
        logger.debug("upserted user", extra={"tg_user_id": tg_id})
        return user

    async def get(self, tg_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.id == tg_id))
        return result.scalar_one_or_none()

    async def set_referral_code(self, tg_id: int, referral_code: str) -> None:
        await self._session.execute(
            update(User)
            .where(User.id == tg_id, User.referral_code.is_(None))
            .values(referral_code=referral_code)
        )

    async def mark_blocked(self, tg_id: int) -> None:
        await self._session.execute(
            update(User).where(User.id == tg_id).values(is_bot_blocked=True)
        )

    async def count(self) -> int:
        from sqlalchemy import func
        result = await self._session.execute(select(func.count()).select_from(User))
        return result.scalar_one()
