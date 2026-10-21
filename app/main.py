import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from arq import create_pool
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.bot.middlewares.db import DatabaseMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.bot.router import root_router
from app.clients.remnawave import RemnawaveClient
from app.config import settings
from app.db.session import async_session_factory, engine


def _setup_logging() -> None:
    class _Fmt(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            tg = getattr(record, "tg_user_id", None)
            ev = getattr(record, "event", None)
            extra = ""
            if tg is not None:
                extra += f',"tg_user_id":{tg}'
            if ev:
                extra += f',"event":"{ev}"'
            record.extra = extra
            return super().format(record)

    fmt = _Fmt('{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"%(extra)s}')
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))


async def main() -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)

    redis = Redis.from_url(settings.redis_url)
    arq_redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    storage = RedisStorage(redis=redis)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=storage)
    remnawave = RemnawaveClient(redis=redis)

    dp.update.middleware(DatabaseMiddleware(async_session_factory))
    dp.update.middleware(UserMiddleware())
    dp.message.middleware(ThrottlingMiddleware(redis, rate_limit=1.0))
    dp.callback_query.middleware(ThrottlingMiddleware(redis, rate_limit=1.0))

    dp["remnawave"] = remnawave
    dp["arq_redis"] = arq_redis

    dp.include_router(root_router)

    logger.info("bot starting", extra={"tg_user_id": None, "event": "startup"})
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("bot stopping", extra={"tg_user_id": None, "event": "shutdown"})
        await engine.dispose()
        await remnawave.aclose()
        await bot.session.close()
        await arq_redis.aclose()
        await redis.aclose()
        logger.info("bot stopped", extra={"tg_user_id": None, "event": "stopped"})


if __name__ == "__main__":
    asyncio.run(main())
