from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Создать ключ", callback_data="sub:create")],
            [InlineKeyboardButton(text="📋 Получить ключ", callback_data="sub:get")],
            [InlineKeyboardButton(text="🔄 Продлить ключ", callback_data="sub:extend")],
        ]
    )


def extend_key_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продлить ключ", callback_data="sub:extend")]
        ]
    )
