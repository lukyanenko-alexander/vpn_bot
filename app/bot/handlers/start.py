import logging

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.bot.keyboards.main import main_menu_keyboard
from app.repositories.user import UserRepository

logger = logging.getLogger(__name__)
router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, session) -> None:
    tg_user = message.from_user
    referral_code = command.args or None

    repo = UserRepository(session)
    async with session.begin():
        user = await repo.upsert(
            tg_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name or "",
            last_name=tg_user.last_name,
        )
        if referral_code and not user.referral_code:
            await repo.set_referral_code(user.id, referral_code)
            logger.info("referral code saved", extra={"tg_user_id": tg_user.id, "event": "referral_saved"})

    await message.answer(
        f"Привет, {user.first_name}! 👋\n\nВыберите действие:",
        reply_markup=main_menu_keyboard(),
    )
    logger.info("start command", extra={"tg_user_id": tg_user.id, "event": "start"})
