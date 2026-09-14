"""Access control middleware for group-only and force-sub rules."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from telemon.config import settings
from telemon.logging import get_logger

logger = get_logger(__name__)

_GROUP_CHAT_TYPES = {"group", "supergroup"}
_ALLOWED_MEMBER_STATUSES = {"creator", "administrator", "member"}


class AccessControlMiddleware(BaseMiddleware):
    """Keep the game in groups and require users to join the force-sub chat."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Block private chats and users who are not subscribed to the configured chat."""
        if not settings.group_only_enabled and not settings.force_sub_enabled:
            return await handler(event, data)

        message = self._event_message(event)
        if not message:
            return await handler(event, data)

        if settings.group_only_enabled and message.chat.type not in _GROUP_CHAT_TYPES:
            await self._answer_group_only(event)
            return None

        if settings.force_sub_enabled:
            bot: Bot | None = data.get("bot")
            user_id = self._event_user_id(event)
            if bot and user_id and not await self._is_subscribed(bot, user_id):
                await self._answer_force_sub(event)
                return None

        return await handler(event, data)

    def _event_message(self, event: TelegramObject) -> Message | None:
        if isinstance(event, Message):
            return event
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            return event.message
        return None

    def _event_user_id(self, event: TelegramObject) -> int | None:
        if isinstance(event, Message) and event.from_user:
            return event.from_user.id
        if isinstance(event, CallbackQuery) and event.from_user:
            return event.from_user.id
        return None

    async def _is_subscribed(self, bot: Bot, user_id: int) -> bool:
        """Return whether the user belongs to the configured force-sub chat."""
        if settings.force_sub_chat_id is None:
            logger.warning("Force-sub is enabled but FORCE_SUB_CHAT_ID is not set")
            return True

        try:
            member = await bot.get_chat_member(settings.force_sub_chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "Force-sub membership check failed",
                force_sub_chat_id=settings.force_sub_chat_id,
                user_id=user_id,
                error=str(exc),
            )
            # Avoid locking everyone out if the bot cannot inspect the channel/group.
            return True

        return member.status in _ALLOWED_MEMBER_STATUSES

    async def _answer_group_only(self, event: TelegramObject) -> None:
        text = (
            "<b>This bot only works in Telegram groups.</b>\n\n"
            "Add me to a group, then use /start there to begin your journey."
        )
        markup = self._force_sub_keyboard()
        if isinstance(event, Message):
            await event.answer(text, reply_markup=markup)
        elif isinstance(event, CallbackQuery):
            await event.answer("This bot only works in Telegram groups.", show_alert=True)

    async def _answer_force_sub(self, event: TelegramObject) -> None:
        text = (
            "<b>Subscription required</b>\n\n"
            "Join the required Telegram chat/channel, then press your command again."
        )
        markup = self._force_sub_keyboard()
        if isinstance(event, Message):
            await event.answer(text, reply_markup=markup)
        elif isinstance(event, CallbackQuery):
            await event.answer("Join the required chat/channel first.", show_alert=True)
            if event.message:
                await event.message.answer(text, reply_markup=markup)

    def _force_sub_keyboard(self) -> InlineKeyboardMarkup | None:
        if not settings.force_sub_url:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Join required chat", url=str(settings.force_sub_url))]
            ]
        )
