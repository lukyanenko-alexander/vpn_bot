from aiogram import Router

from app.bot.handlers import admin, start, subscription

root_router = Router(name="root")
root_router.include_router(start.router)
root_router.include_router(subscription.router)
root_router.include_router(admin.router)
