from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from redis.asyncio import Redis


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis, rate_limit: float = 1.0) -> None:
        self._redis = redis
        self._rate_limit = rate_limit

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        key = f"throttle:{user.id}"
        result = await self._redis.set(key, "1", nx=True, ex=int(self._rate_limit))
        if not result:
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком много запросов. Подождите немного.", show_alert=True)
            else:
                await event.answer("Слишком много запросов. Подождите немного.")
            return

        return await handler(event, data)
