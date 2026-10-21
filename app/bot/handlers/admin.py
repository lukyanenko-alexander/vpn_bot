import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.repositories.subscription import SubscriptionRepository
from app.repositories.user import UserRepository

logger = logging.getLogger(__name__)
router = Router(name="admin")


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


@router.message(Command("stats"))
async def cmd_stats(message: Message, session) -> None:
    if not is_admin(message.from_user.id):
        return

    user_repo = UserRepository(session)
    sub_repo = SubscriptionRepository(session)

    total_users = await user_repo.count()
    active_subs = await sub_repo.count_active()

    text = (
        f"📊 Статистика:\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"✅ Активных подписок: {active_subs}"
    )
    logger.info("admin stats", extra={"tg_user_id": message.from_user.id, "event": "admin_stats"})
    await message.answer(text)


@router.message(Command("user"))
async def cmd_user(message: Message, session) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: /user <tg_id>")
        return

    tg_id = int(parts[1].strip())
    user_repo = UserRepository(session)
    sub_repo = SubscriptionRepository(session)

    user = await user_repo.get(tg_id)
    if not user:
        await message.answer(f"Пользователь {tg_id} не найден.")
        return

    sub = await sub_repo.get_latest(tg_id)
    sub_info = "нет"
    if sub:
        expires_str = sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—"
        sub_info = f"{sub.status}, до {expires_str}"

    text = (
        f"👤 Пользователь {tg_id}:\n"
        f"  username: @{user.username or '—'}\n"
        f"  имя: {user.first_name} {user.last_name or ''}\n"
        f"  зарегистрирован: {user.registered_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"  реферал: {user.referral_code or '—'}\n"
        f"  заблокировал бота: {'да' if user.is_bot_blocked else 'нет'}\n"
        f"  подписка: {sub_info}"
    )
    logger.info("admin user lookup", extra={"tg_user_id": message.from_user.id, "event": "admin_user", "target_id": tg_id})
    await message.answer(text)
