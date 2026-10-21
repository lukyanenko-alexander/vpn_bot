import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Subscription


class SubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_for_update(self, user_id: int) -> Subscription | None:
        result = await self._session.execute(
            select(Subscription)
            .where(and_(
                Subscription.user_id == user_id,
                Subscription.status.in_(["pending", "active"]),
            ))
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_extendable_for_update(self, user_id: int) -> Subscription | None:
        """FOR UPDATE on active OR expired — used by extend flow to re-activate expired subs."""
        result = await self._session.execute(
            select(Subscription)
            .where(and_(
                Subscription.user_id == user_id,
                Subscription.status.in_(["active", "expired"]),
            ))
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_active(self, user_id: int) -> Subscription | None:
        result = await self._session.execute(
            select(Subscription)
            .where(and_(
                Subscription.user_id == user_id,
                Subscription.status.in_(["pending", "active"]),
            ))
        )
        return result.scalar_one_or_none()

    async def get_latest(self, user_id: int) -> Subscription | None:
        """Most recent subscription regardless of status — used by get_key to show expired subs too."""
        result = await self._session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, sub_id: str) -> Subscription | None:
        result = await self._session.execute(
            select(Subscription).where(Subscription.id == sub_id)
        )
        return result.scalar_one_or_none()

    async def create_pending(self, user_id: int) -> Subscription:
        sub = Subscription(user_id=user_id, status="pending")
        self._session.add(sub)
        await self._session.flush()
        await self._session.refresh(sub)
        return sub

    async def activate(
        self,
        sub_id: str,
        panel_user_uuid: str,
        subscription_url: str,
        expires_at: datetime,
        traffic_limit: int | None,
    ) -> None:
        await self._session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(
                status="active",
                panel_user_uuid=panel_user_uuid,
                subscription_url=subscription_url,
                expires_at=expires_at,
                traffic_limit=traffic_limit,
                updated_at=func.now(),
            )
        )

    async def update_expiry(self, sub_id: str, expires_at: datetime) -> None:
        """Sets new expiry and re-activates the subscription (extend always yields active status)."""
        await self._session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(status="active", expires_at=expires_at, updated_at=func.now())
        )

    async def mark_expired(self, sub_id: str) -> None:
        await self._session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(status="expired", updated_at=func.now())
        )

    async def scan_expiring_soon(self, cursor_id: str | None, limit: int = 1000) -> list[Subscription]:
        now = datetime.now(tz=timezone.utc)
        q = (
            select(Subscription)
            .where(and_(
                Subscription.status == "active",
                Subscription.expiring_notified.is_(False),
                Subscription.expires_at >= now,
                Subscription.expires_at <= now + timedelta(hours=24),
            ))
            .order_by(Subscription.id)
            .limit(limit)
        )
        if cursor_id is not None:
            q = q.where(Subscription.id > cursor_id)
        return list((await self._session.execute(q)).scalars().all())

    async def scan_expired(self, cursor_id: str | None, limit: int = 1000) -> list[Subscription]:
        now = datetime.now(tz=timezone.utc)
        q = (
            select(Subscription)
            .where(and_(
                Subscription.status == "active",
                Subscription.expired_notified.is_(False),
                Subscription.expires_at < now,
            ))
            .order_by(Subscription.id)
            .limit(limit)
        )
        if cursor_id is not None:
            q = q.where(Subscription.id > cursor_id)
        return list((await self._session.execute(q)).scalars().all())

    async def mark_expiring_notified(self, sub_ids: list[str]) -> None:
        await self._session.execute(
            update(Subscription)
            .where(Subscription.id.in_(sub_ids))
            .values(expiring_notified=True, updated_at=func.now())
        )

    async def mark_expired_notified(self, sub_ids: list[str]) -> None:
        await self._session.execute(
            update(Subscription)
            .where(Subscription.id.in_(sub_ids))
            .values(expired_notified=True, status="expired", updated_at=func.now())
        )

    async def set_pending_panel_extend(self, sub_id: str, expires_at: datetime) -> None:
        await self._session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(pending_panel_expires_at=expires_at, updated_at=func.now())
        )

    async def clear_pending_panel_extend(self, sub_id: str) -> None:
        await self._session.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(pending_panel_expires_at=None, updated_at=func.now())
        )

    async def get_stuck_pending_extends(self, older_than_seconds: int = 30) -> list[Subscription]:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=older_than_seconds)
        result = await self._session.execute(
            select(Subscription)
            .where(and_(
                Subscription.pending_panel_expires_at.is_not(None),
                Subscription.updated_at < cutoff,
            ))
            .limit(100)
        )
        return list(result.scalars().all())

    async def get_stuck_pending(self, older_than_seconds: int = 30) -> list[Subscription]:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=older_than_seconds)
        result = await self._session.execute(
            select(Subscription)
            .where(and_(Subscription.status == "pending", Subscription.created_at < cutoff))
            .limit(100)
        )
        return list(result.scalars().all())

    async def count_active(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(Subscription).where(Subscription.status == "active")
        )
        return result.scalar_one()
