import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.clients.remnawave import RemnawaveUser
from app.models.db import Subscription
from app.services.subscription import MAX_SUBSCRIPTION_DAYS, SUBSCRIPTION_DURATION_DAYS, SubscriptionService


def _make_subscription(status: str = "active", expires_at: datetime | None = None) -> Subscription:
    sub = Subscription()
    sub.id = str(uuid.uuid4())
    sub.user_id = 123
    sub.panel_user_uuid = str(uuid.uuid4())
    sub.subscription_url = "https://example.com/sub"
    sub.status = status
    sub.expires_at = expires_at or datetime.now(tz=timezone.utc) + timedelta(days=7)
    return sub


def _make_panel_user(expires_at: datetime | None = None) -> RemnawaveUser:
    return RemnawaveUser(
        uuid=uuid.uuid4(),
        username="tg_123",
        subscription_url="https://example.com/sub",
        expires_at=expires_at or datetime.now(tz=timezone.utc) + timedelta(days=7),
        enabled=True,
        traffic_limit=None,
    )


def _make_session() -> MagicMock:
    s = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=False)
    s.begin = MagicMock(return_value=ctx)
    s.execute = AsyncMock()
    s.flush = AsyncMock()
    s.commit = AsyncMock()
    s.refresh = AsyncMock()
    s.rollback = AsyncMock()
    s.add = MagicMock()
    return s


@pytest.fixture
def session():
    return _make_session()


@pytest.fixture
def sub_repo():
    return AsyncMock()


@pytest.fixture
def arq_redis():
    return AsyncMock()


@pytest.fixture
def remnawave():
    return AsyncMock()


@pytest.fixture
def service(session, sub_repo, arq_redis, remnawave):
    return SubscriptionService(
        session=session,
        sub_repo=sub_repo,
        arq_redis=arq_redis,
        remnawave=remnawave,
    )


class TestCreate:
    async def test_returns_existing_if_active(self, service, sub_repo, arq_redis):
        existing = _make_subscription("active")
        sub_repo.get_active_for_update.return_value = existing

        result = await service.create(123)

        assert result.subscription is existing
        assert not result.is_new
        sub_repo.create_pending.assert_not_called()
        arq_redis.enqueue_job.assert_not_called()

    async def test_creates_new_subscription(self, service, sub_repo, remnawave, arq_redis):
        sub_repo.get_active_for_update.return_value = None
        pending = _make_subscription("pending")
        sub_repo.create_pending.return_value = pending
        remnawave.create_user.return_value = _make_panel_user()

        result = await service.create(123)

        assert result.is_new
        sub_repo.activate.assert_called_once()
        arq_redis.enqueue_job.assert_not_called()

    async def test_enqueues_recovery_job_on_api_failure(self, service, sub_repo, remnawave, arq_redis):
        sub_repo.get_active_for_update.return_value = None
        pending = _make_subscription("pending")
        sub_repo.create_pending.return_value = pending
        remnawave.create_user.side_effect = Exception("timeout")

        result = await service.create(123)

        assert result.is_new
        sub_repo.activate.assert_not_called()
        arq_redis.enqueue_job.assert_called_once()
        assert arq_redis.enqueue_job.call_args[0][0] == "create_subscription_in_panel"


class TestExtend:
    async def test_extends_active_subscription(self, service, sub_repo, remnawave, arq_redis):
        now = datetime.now(tz=timezone.utc)
        sub = _make_subscription("active", expires_at=now + timedelta(days=3))
        sub_repo.get_extendable_for_update.return_value = sub
        remnawave.update_user.return_value = _make_panel_user()

        await service.extend(123)

        sub_repo.update_expiry.assert_called_once()
        call_args = sub_repo.update_expiry.call_args[0]
        expected = max(now, sub.expires_at) + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
        assert abs((call_args[1] - expected).total_seconds()) < 2
        arq_redis.enqueue_job.assert_not_called()

    async def test_raises_when_no_subscription(self, service, sub_repo):
        sub_repo.get_extendable_for_update.return_value = None

        with pytest.raises(ValueError, match="no_subscription"):
            await service.extend(123)

    async def test_raises_when_subscription_pending(self, service, sub_repo):
        sub = _make_subscription("active")
        sub.panel_user_uuid = None  # pending — panel UUID not assigned yet
        sub_repo.get_extendable_for_update.return_value = sub

        with pytest.raises(ValueError, match="subscription_pending"):
            await service.extend(123)

    async def test_raises_when_limit_exceeded(self, service, sub_repo):
        now = datetime.now(tz=timezone.utc)
        sub = _make_subscription("active", expires_at=now + timedelta(days=MAX_SUBSCRIPTION_DAYS - 3))
        sub_repo.get_extendable_for_update.return_value = sub

        with pytest.raises(ValueError, match="limit_exceeded"):
            await service.extend(123)

    async def test_extends_from_now_if_expired(self, service, sub_repo, remnawave, arq_redis):
        now = datetime.now(tz=timezone.utc)
        sub = _make_subscription("expired", expires_at=now - timedelta(days=2))
        sub_repo.get_extendable_for_update.return_value = sub
        remnawave.update_user.return_value = _make_panel_user()

        await service.extend(123)

        call_args = sub_repo.update_expiry.call_args[0]
        expected = now + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
        assert abs((call_args[1] - expected).total_seconds()) < 2

    async def test_enqueues_recovery_job_on_extend_failure(self, service, sub_repo, remnawave, arq_redis):
        now = datetime.now(tz=timezone.utc)
        sub = _make_subscription("active", expires_at=now + timedelta(days=3))
        sub_repo.get_extendable_for_update.return_value = sub
        remnawave.update_user.side_effect = Exception("timeout")

        await service.extend(123)

        arq_redis.enqueue_job.assert_called_once()
        assert arq_redis.enqueue_job.call_args[0][0] == "extend_subscription_in_panel"
