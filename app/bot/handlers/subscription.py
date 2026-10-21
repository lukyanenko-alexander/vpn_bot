import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from arq import ArqRedis

from app.bot.keyboards.main import main_menu_keyboard
from app.clients.remnawave import RemnawaveClient
from app.repositories.subscription import SubscriptionRepository
from app.services.subscription import SubscriptionService

logger = logging.getLogger(__name__)
router = Router(name="subscription")


def _make_service(session, remnawave: RemnawaveClient, arq_redis: ArqRedis) -> SubscriptionService:
    return SubscriptionService(
        session=session,
        sub_repo=SubscriptionRepository(session),
        arq_redis=arq_redis,
        remnawave=remnawave,
    )


@router.callback_query(F.data == "sub:create")
async def create_key(callback: CallbackQuery, session, remnawave: RemnawaveClient, arq_redis: ArqRedis) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    service = _make_service(session, remnawave, arq_redis)
    result = await service.create(user_id)
    sub = result.subscription

    if sub.status == "active" and sub.subscription_url:
        expires_str = sub.expires_at.strftime("%d.%m.%Y %H:%M UTC") if sub.expires_at else "—"
        prefix = "✅ Подписка создана!" if result.is_new else "ℹ️ У вас уже есть активная подписка."
        text = f"{prefix}\n\n🔗 Ссылка: <code>{sub.subscription_url}</code>\n📅 Действует до: {expires_str}"
    else:
        text = "⏳ Подписка создаётся. Используйте «Получить ключ» через несколько секунд."

    logger.info("create key", extra={"tg_user_id": user_id, "event": "create_key"})
    await callback.message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "sub:get")
async def get_key(callback: CallbackQuery, session) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    # get_latest shows expired subs too (PDF: "отправляет ссылку, дату окончания и статус")
    sub = await SubscriptionRepository(session).get_latest(user_id)
    if sub is None:
        await callback.message.answer("У вас нет подписки. Нажмите «Создать ключ».", reply_markup=main_menu_keyboard())
        return

    expires_str = sub.expires_at.strftime("%d.%m.%Y %H:%M UTC") if sub.expires_at else "—"
    status_map = {"active": "✅ Активна", "pending": "⏳ Создаётся", "expired": "❌ Истекла"}
    text = (
        f"📋 Ваша подписка:\n\n"
        f"🔗 Ссылка: <code>{sub.subscription_url or '—'}</code>\n"
        f"📅 Действует до: {expires_str}\n"
        f"📊 Статус: {status_map.get(sub.status, sub.status)}"
    )
    logger.info("get key", extra={"tg_user_id": user_id, "event": "get_key"})
    await callback.message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "sub:extend")
async def extend_key(callback: CallbackQuery, session, remnawave: RemnawaveClient, arq_redis: ArqRedis) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    service = _make_service(session, remnawave, arq_redis)
    try:
        sub = await service.extend(user_id)
        expires_str = sub.expires_at.strftime("%d.%m.%Y %H:%M UTC") if sub.expires_at else "—"
        text = (
            f"✅ Подписка продлена!\n\n"
            f"🔗 Ссылка: <code>{sub.subscription_url or '—'}</code>\n"
            f"📅 Действует до: {expires_str}"
        )
    except ValueError as e:
        if str(e) == "no_subscription":
            text = "У вас нет подписки. Нажмите «Создать ключ»."
        elif str(e) == "subscription_pending":
            text = "⏳ Подписка ещё создаётся. Попробуйте через несколько секунд."
        elif str(e) == "limit_exceeded":
            max_date = (datetime.now(tz=timezone.utc) + timedelta(days=90)).strftime("%d.%m.%Y")
            text = f"⚠️ Максимальный срок подписки — 90 дней от сегодня ({max_date})."
        else:
            text = "Произошла ошибка. Попробуйте позже."

    logger.info("extend key", extra={"tg_user_id": user_id, "event": "extend_key"})
    await callback.message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
