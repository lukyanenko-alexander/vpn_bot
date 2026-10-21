import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user import UserRepository

logger = logging.getLogger(__name__)


def _is_start_command(event: Update) -> bool:
    if not event.message:
        return False
    text = event.message.text or ""
    return text == "/start" or text.startswith("/start ")


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        tg_user = None
        if event.message:
            tg_user = event.message.from_user
        elif event.callback_query:
            tg_user = event.callback_query.from_user

        if tg_user:
            session: AsyncSession = data["session"]
            async with session.begin():
                user = await UserRepository(session).get(tg_user.id)

            if user is None:
                if _is_start_command(event):
                    return await handler(event, data)
                if event.callback_query:
                    await event.callback_query.answer("Нажмите /start для начала работы.")
                logger.debug("unknown user, dropping update", extra={"tg_user_id": tg_user.id, "event": "middleware_unknown_user"})
                return

            data["db_user"] = user
            logger.debug("user resolved", extra={"tg_user_id": tg_user.id, "event": "middleware_user"})

        return await handler(event, data)
