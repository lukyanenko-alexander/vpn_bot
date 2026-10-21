import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from arq import ArqRedis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.remnawave import RemnawaveClient
from app.models.db import Subscription
from app.repositories.subscription import SubscriptionRepository

logger = logging.getLogger(__name__)

MAX_SUBSCRIPTION_DAYS = 90
SUBSCRIPTION_DURATION_DAYS = 7


@dataclass
class SubscriptionResult:
    subscription: Subscription
    is_new: bool = False


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        sub_repo: SubscriptionRepository,
        arq_redis: ArqRedis,
        remnawave: RemnawaveClient,
    ) -> None:
        self._session = session
        self._sub_repo = sub_repo
        self._arq_redis = arq_redis
        self._remnawave = remnawave

    async def create(self, user_id: int) -> SubscriptionResult:
        try:
            async with self._session.begin():
                existing = await self._sub_repo.get_active_for_update(user_id)
                if existing:
                    return SubscriptionResult(subscription=existing)
                sub = await self._sub_repo.create_pending(user_id)
        except IntegrityError:
            async with self._session.begin():
                existing = await self._sub_repo.get_active(user_id)
            return SubscriptionResult(subscription=existing)

        extra = {"tg_user_id": user_id, "event": "create_subscription"}
        expires_at = datetime.now(tz=timezone.utc) + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
        panel_username = f"tg_{user_id}"

        try:
            panel_user = await self._remnawave.create_user(username=panel_username, expires_at=expires_at)
            async with self._session.begin():
                await self._sub_repo.activate(
                    sub_id=sub.id,
                    panel_user_uuid=str(panel_user.uuid),
                    subscription_url=panel_user.subscription_url,
                    expires_at=panel_user.expires_at,
                    traffic_limit=panel_user.traffic_limit,
                )
            await self._session.refresh(sub)
            logger.info("subscription created", extra=extra)
        except Exception:
            logger.exception("panel call failed, enqueuing recovery job", extra=extra)
            await self._arq_redis.enqueue_job(
                "create_subscription_in_panel",
                sub.id,
                _job_id=f"create_{sub.id}",
            )
            await self._session.refresh(sub)

        return SubscriptionResult(subscription=sub, is_new=True)

    async def get(self, user_id: int) -> Subscription | None:
        return await self._sub_repo.get_active(user_id)

    async def extend(self, user_id: int) -> Subscription:
        async with self._session.begin():
            sub = await self._sub_repo.get_extendable_for_update(user_id)
            if sub is None:
                raise ValueError("no_subscription")
            if sub.panel_user_uuid is None:
                raise ValueError("subscription_pending")

            now = datetime.now(tz=timezone.utc)
            current_expiry = sub.expires_at
            if current_expiry and current_expiry.tzinfo is None:
                current_expiry = current_expiry.replace(tzinfo=timezone.utc)
            base = max(now, current_expiry) if current_expiry else now
            new_expires = base + timedelta(days=SUBSCRIPTION_DURATION_DAYS)

            if new_expires > now + timedelta(days=MAX_SUBSCRIPTION_DAYS):
                raise ValueError("limit_exceeded")

            # DB write happens inside the lock — concurrent extends see the updated
            # expires_at and cannot bypass the 90-day limit check
            await self._sub_repo.update_expiry(sub.id, new_expires)
            await self._sub_repo.set_pending_panel_extend(sub.id, new_expires)
        # lock released, transaction committed

        extra = {"tg_user_id": user_id, "event": "extend_subscription"}
        try:
            await self._remnawave.update_user(
                user_uuid=sub.panel_user_uuid,
                expires_at=new_expires,
                enabled=True,
            )
            async with self._session.begin():
                await self._sub_repo.clear_pending_panel_extend(sub.id)
            logger.info("subscription extended", extra=extra)
        except Exception:
            logger.exception("panel extend failed, enqueuing recovery job", extra=extra)
            await self._arq_redis.enqueue_job(
                "extend_subscription_in_panel",
                sub.id,
                new_expires.isoformat(),
                _job_id=f"extend_{sub.id}",
            )

        await self._session.refresh(sub)
        return sub
