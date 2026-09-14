"""Middleware registration and implementations."""

from aiogram import Dispatcher

from telemon.bot.middlewares.access import AccessControlMiddleware
from telemon.bot.middlewares.database import DatabaseMiddleware
from telemon.bot.middlewares.registration import RegistrationMiddleware
from telemon.bot.middlewares.user import UserMiddleware


def register_all_middlewares(dp: Dispatcher) -> None:
    """Register all middlewares with the dispatcher."""
    # Database session middleware (must be first)
    dp.message.middleware(DatabaseMiddleware())
    dp.callback_query.middleware(DatabaseMiddleware())

    # User loading middleware (requires database)
    dp.message.middleware(UserMiddleware())
    dp.callback_query.middleware(UserMiddleware())

    # Access controls (requires database/user only for downstream handlers; uses bot from aiogram)
    dp.message.middleware(AccessControlMiddleware())
    dp.callback_query.middleware(AccessControlMiddleware())

    # Registration check middleware (requires user)
    dp.message.middleware(RegistrationMiddleware())
    dp.callback_query.middleware(RegistrationMiddleware())


__all__ = ["register_all_middlewares"]
