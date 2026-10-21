"""
ARQ worker: all background tasks in one place.

Panel tasks  — create/extend subscription in Remnawave (recovery path)
Notification tasks — send Telegram messages
Cron tasks   — scan_and_notify, recover_pending
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from arq import cron
from arq import Retry
from arq.connections import RedisSettings

from app.bot.keyboards.main import extend_key_keyboard
from redis.asyncio import Redis
from app.clients.remnawave import RemnawaveClient
from app.config import settings
from app.db.session import async_session_factory, engine
from app.repositories.notification import NotificationRepository
from app.repositories.subscription import SubscriptionRepository
from app.repositories.user import UserRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Panel tasks (recovery path when synchronous call in bot failed)
# ---------------------------------------------------------------------------

async def create_subscription_in_panel(ctx: dict, subscription_id: str) -> None:
    extra = {"tg_user_id": None, "event": "arq_create_subscription"}
    remnawave: RemnawaveClient = ctx["remnawave"]

    async with async_session_factory() as session:
        sub_repo = SubscriptionRepository(session)

        async with session.begin():
            sub = await sub_repo.get_by_id(subscription_id)

        if sub is None or sub.status != "pending":
            return

        panel_username = f"tg_{sub.user_id}"
        expires_at = datetime.now(tz=timezone.utc) + timedelta(days=7)

        panel_user = await remnawave.get_user_by_username(panel_username)
        if panel_user is None:
            panel_user = await remnawave.create_user(username=panel_username, expires_at=expires_at)

        async with session.begin():
            await sub_repo.activate(
                sub_id=sub.id,
                panel_user_uuid=str(panel_user.uuid),
                subscription_url=panel_user.subscription_url,
                expires_at=panel_user.expires_at,
                traffic_limit=panel_user.traffic_limit,
            )

    logger.info("subscription created via recovery", extra=extra)


async def extend_subscription_in_panel(ctx: dict, subscription_id: str, new_expires_at: str) -> None:
    extra = {"tg_user_id": None, "event": "arq_extend_subscription"}
    remnawave: RemnawaveClient = ctx["remnawave"]
    new_expires = datetime.fromisoformat(new_expires_at)

    async with async_session_factory() as session:
        sub_repo = SubscriptionRepository(session)

        async with session.begin():
            sub = await sub_repo.get_by_id(subscription_id)

        if sub is None or sub.panel_user_uuid is None:
            return

        await remnawave.update_user(
            user_uuid=sub.panel_user_uuid,
            expires_at=new_expires,
            enabled=True,
        )
        async with session.begin():
            await sub_repo.update_expiry(sub.id, new_expires)
            await sub_repo.clear_pending_panel_extend(sub.id)

    logger.info("subscription extended via recovery", extra=extra)


# ---------------------------------------------------------------------------
# Notification task
# ---------------------------------------------------------------------------

async def send_notification(ctx: dict, user_id: int, subscription_id: str, event_type: str) -> None:
    bot: Bot = ctx["bot"]
    extra = {"tg_user_id": user_id, "event": f"arq_notify_{event_type.lower()}"}

    async with async_session_factory() as session:
        notif_repo = NotificationRepository(session)
        user_repo = UserRepository(session)
        sub_repo = SubscriptionRepository(session)

        # Idempotency: skip if already sent (handles job re-runs after a crash)
        async with session.begin():
            already_sent = await notif_repo.exists(subscription_id, event_type)
        if already_sent:
            logger.info("notification already sent, skipping", extra=extra)
            return

        try:
            if event_type == "NOTIFY_EXPIRING_SOON":
                await bot.send_message(
                    user_id,
                    "⏰ Ваша подписка истекает через 24 часа. Продлите, чтобы не прерывать доступ к VPN.",
                    reply_markup=extend_key_keyboard(),
                )
            else:
                async with session.begin():
                    await sub_repo.mark_expired(subscription_id)
                await bot.send_message(user_id, "❌ Ваша подписка истекла. Нажмите «Создать ключ», чтобы получить новую.")

            # Record after successful send — prevents re-sending on job retry
            async with session.begin():
                await notif_repo.try_create(subscription_id, event_type)

            logger.info("notification sent", extra=extra)

        except TelegramRetryAfter as e:
            # Don't record — let the job retry and send after the rate limit window
            logger.warning("rate limited, retry after %ss", e.retry_after, extra=extra)
            raise Retry(defer=e.retry_after)

        except TelegramForbiddenError:
            logger.info("user blocked bot", extra=extra)
            async with session.begin():
                await user_repo.mark_blocked(user_id)
            async with session.begin():
                await notif_repo.try_create(subscription_id, event_type)

        except TelegramAPIError:
            # Permanent Telegram error (e.g. bad request, deleted account) — record to prevent infinite retries
            logger.warning("telegram error, skipping notification", extra=extra, exc_info=True)
            async with session.begin():
                await notif_repo.try_create(subscription_id, event_type)


# ---------------------------------------------------------------------------
# Cron: scan subscriptions and enqueue notification tasks
# ---------------------------------------------------------------------------

async def scan_and_notify(ctx: dict) -> None:
    arq_redis = ctx["redis"]
    extra = {"tg_user_id": None, "event": "arq_scan_notify"}

    async with async_session_factory() as session:
        sub_repo = SubscriptionRepository(session)

        for event_type, scan_fn, mark_fn in [
            ("NOTIFY_EXPIRING_SOON", sub_repo.scan_expiring_soon, sub_repo.mark_expiring_notified),
            ("NOTIFY_EXPIRED",       sub_repo.scan_expired,       sub_repo.mark_expired_notified),
        ]:
            cursor: str | None = None
            total = 0
            while True:
                async with session.begin():
                    batch = await scan_fn(cursor_id=cursor, limit=1000)

                if not batch:
                    break

                for sub in batch:
                    await arq_redis.enqueue_job(
                        "send_notification",
                        sub.user_id, sub.id, event_type,
                        _job_id=f"notify_{sub.id}_{event_type}",
                    )

                async with session.begin():
                    await mark_fn([s.id for s in batch])

                total += len(batch)
                cursor = batch[-1].id
                if len(batch) < 1000:
                    break

            if total:
                logger.info("enqueued notifications", extra={**extra, "type": event_type, "count": total})


# ---------------------------------------------------------------------------
# Cron: recover pending extends (panel call failed after DB was already updated)
# ---------------------------------------------------------------------------

async def recover_pending_extends(ctx: dict) -> None:
    arq_redis = ctx["redis"]
    extra = {"tg_user_id": None, "event": "arq_recover_pending_extends"}

    async with async_session_factory() as session:
        sub_repo = SubscriptionRepository(session)
        stuck = await sub_repo.get_stuck_pending_extends(older_than_seconds=30)

        for sub in stuck:
            await arq_redis.enqueue_job(
                "extend_subscription_in_panel",
                sub.id,
                sub.pending_panel_expires_at.isoformat(),
                _job_id=f"extend_{sub.id}",
            )

        if stuck:
            logger.info("queued extend recovery jobs", extra={**extra, "count": len(stuck)})


# ---------------------------------------------------------------------------
# Cron: recover pending subscriptions (bot crashed after DB write, before panel call)
# ---------------------------------------------------------------------------

async def recover_pending(ctx: dict) -> None:
    arq_redis = ctx["redis"]
    extra = {"tg_user_id": None, "event": "arq_recover_pending"}

    async with async_session_factory() as session:
        sub_repo = SubscriptionRepository(session)
        stuck = await sub_repo.get_stuck_pending(older_than_seconds=30)

        for sub in stuck:
            await arq_redis.enqueue_job(
                "create_subscription_in_panel",
                sub.id,
                _job_id=f"create_{sub.id}",
            )

        if stuck:
            logger.info("queued recovery jobs", extra={**extra, "count": len(stuck)})


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

async def startup(ctx: dict) -> None:
    ctx["bot"] = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    ctx["redis_client"] = Redis.from_url(settings.redis_url)
    ctx["remnawave"] = RemnawaveClient(redis=ctx["redis_client"])
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.info("arq worker started", extra={"tg_user_id": None, "event": "worker_start"})


async def shutdown(ctx: dict) -> None:
    await ctx["bot"].session.close()
    await ctx["remnawave"].aclose()
    await ctx["redis_client"].aclose()
    await engine.dispose()
    logger.info("arq worker stopped", extra={"tg_user_id": None, "event": "worker_stop"})


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [
        create_subscription_in_panel,
        extend_subscription_in_panel,
        send_notification,
        scan_and_notify,
        recover_pending,
        recover_pending_extends,
    ]
    cron_jobs = [
        cron(scan_and_notify, minute=set(range(60))),
        cron(recover_pending, minute=set(range(60))),
        cron(recover_pending_extends, minute=set(range(60))),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 100
    job_timeout = 60
    keep_result = 60
