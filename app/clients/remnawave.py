import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

_RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)
_RETRYABLE_STATUS = {500, 502, 503, 504}
_AUTH_ERROR_STATUS = {401, 403}
_RETRY_DELAYS = [1, 2, 4]  # seconds between attempts

_CB_KEY_FAILURES = "circuit:remnawave:failures"
_CB_KEY_OPEN     = "circuit:remnawave:open"


@dataclass
class RemnawaveUser:
    uuid: uuid.UUID
    username: str
    subscription_url: str
    expires_at: datetime
    enabled: bool
    traffic_limit: int | None


class _RedisCircuitBreaker:
    """
    Shared circuit breaker state in Redis — works across multiple processes.

    Opens after `threshold` failures within `failures_ttl` seconds.
    Stays open for `recovery_timeout` seconds, then allows one probe (half-open).
    """
    def __init__(
        self,
        redis: Redis,
        threshold: int = 5,
        failures_ttl: int = 60,
        recovery_timeout: int = 60,
    ) -> None:
        self._redis = redis
        self._threshold = threshold
        self._failures_ttl = failures_ttl
        self._recovery_timeout = recovery_timeout

    async def is_open(self) -> bool:
        return bool(await self._redis.exists(_CB_KEY_OPEN))

    async def record_success(self) -> None:
        await self._redis.delete(_CB_KEY_OPEN, _CB_KEY_FAILURES)

    async def record_failure(self) -> None:
        failures = await self._redis.incr(_CB_KEY_FAILURES)
        if failures == 1:
            # Set TTL only on the first failure in this window — subsequent failures
            # must not reset it, otherwise the window slides and never closes
            await self._redis.expire(_CB_KEY_FAILURES, self._failures_ttl)
        if failures >= self._threshold:
            # SET NX so the first process to trip the breaker sets the TTL
            opened = await self._redis.set(_CB_KEY_OPEN, "1", ex=self._recovery_timeout, nx=True)
            if opened:
                logger.warning(
                    "remnawave circuit breaker OPEN; will probe after %ss",
                    self._recovery_timeout,
                    extra={"tg_user_id": None, "event": "circuit_open"},
                )


class RemnawaveClient:
    def __init__(self, redis: Redis) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.remnawave_url,
            headers={"Authorization": f"Bearer {settings.remnawave_token}"},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0),
        )
        self._breaker = _RedisCircuitBreaker(redis, threshold=5, failures_ttl=300, recovery_timeout=60)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _guarded(self, coro):
        """Run coro through circuit breaker; only transient/server errors count."""
        if await self._breaker.is_open():
            raise httpx.ConnectError("panel unavailable (circuit open)")
        try:
            result = await coro
            await self._breaker.record_success()
            return result
        except _RETRYABLE_ERRORS:
            await self._breaker.record_failure()
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in _RETRYABLE_STATUS:
                await self._breaker.record_failure()
            raise

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Retry idempotent requests on transient errors with exponential backoff."""
        last_exc: Exception | None = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                logger.warning(
                    "remnawave retry attempt %d after %ss",
                    attempt, delay,
                    extra={"tg_user_id": None, "event": "remnawave_retry"},
                )
                await asyncio.sleep(delay)
            try:
                response = await self._client.request(method, url, **kwargs)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    logger.warning(
                        "remnawave rate limited, retry after %ss",
                        retry_after,
                        extra={"tg_user_id": None, "event": "remnawave_rate_limited"},
                    )
                    await asyncio.sleep(retry_after)
                    last_exc = httpx.HTTPStatusError(
                        "Rate limited 429",
                        request=response.request,
                        response=response,
                    )
                    continue

                if response.status_code in _AUTH_ERROR_STATUS:
                    logger.error(
                        "remnawave auth error %d — check REMNAWAVE_TOKEN",
                        response.status_code,
                        extra={"tg_user_id": None, "event": "remnawave_auth_error"},
                    )
                    response.raise_for_status()

                if response.status_code in _RETRYABLE_STATUS:
                    last_exc = httpx.HTTPStatusError(
                        f"Server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    continue

                return response
            except _RETRYABLE_ERRORS as e:
                last_exc = e
        raise last_exc

    async def create_user(
        self,
        username: str,
        expires_at: datetime,
        traffic_limit: int | None = None,
    ) -> RemnawaveUser:
        # POST is not idempotent — no retry; caller handles failure via ARQ recovery
        async def _do():
            payload: dict = {"username": username, "expireAt": int(expires_at.timestamp() * 1000)}
            if traffic_limit is not None:
                payload["trafficLimitBytes"] = traffic_limit
            response = await self._client.post("/api/users", json=payload)
            response.raise_for_status()
            return self._parse(response.json())

        return await self._guarded(_do())

    async def get_user(self, user_uuid: uuid.UUID) -> RemnawaveUser:
        async def _do():
            response = await self._request_with_retry("GET", f"/api/users/{user_uuid}")
            response.raise_for_status()
            return self._parse(response.json())

        return await self._guarded(_do())

    async def get_user_by_username(self, username: str) -> RemnawaveUser | None:
        async def _do():
            response = await self._request_with_retry("GET", f"/api/users/by-username/{username}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return self._parse(response.json())

        return await self._guarded(_do())

    async def update_user(
        self,
        user_uuid: uuid.UUID,
        expires_at: datetime,
        enabled: bool,
    ) -> RemnawaveUser:
        async def _do():
            payload = {
                "expireAt": int(expires_at.timestamp() * 1000),
                "status": "ACTIVE" if enabled else "DISABLED",
            }
            response = await self._request_with_retry("PATCH", f"/api/users/{user_uuid}", json=payload)
            response.raise_for_status()
            return self._parse(response.json())

        return await self._guarded(_do())

    async def delete_user(self, user_uuid: uuid.UUID) -> None:
        async def _do():
            response = await self._request_with_retry("DELETE", f"/api/users/{user_uuid}")
            response.raise_for_status()

        await self._guarded(_do())

    def _parse(self, data: dict) -> RemnawaveUser:
        response_data = data.get("response", data)
        expires_ms = response_data.get("expireAt") or response_data.get("expire_at") or 0
        return RemnawaveUser(
            uuid=uuid.UUID(response_data["uuid"]),
            username=response_data["username"],
            subscription_url=response_data.get("subscriptionUrl") or response_data.get("subscription_url", ""),
            expires_at=datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc),
            enabled=response_data.get("status", "ACTIVE") == "ACTIVE",
            traffic_limit=response_data.get("trafficLimitBytes") or response_data.get("traffic_limit_bytes"),
        )
