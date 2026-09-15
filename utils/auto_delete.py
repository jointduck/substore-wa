"""
Auto-delete utility for temporary bot messages.

Messages that are purely informational (status updates, confirmations,
error toasts) automatically disappear after a configurable delay,
keeping the chat clean and professional.

Features:
  - Dissolve animation that mimics Telegram's native delete effect
  - Preserves HTML formatting and custom emoji
  - Clean QR/photo cleanup helpers
  - Works with aiogram 3.x Message objects

Usage:
    from utils.auto_delete import auto_delete

    msg = await message.answer("Сохранено!", parse_mode="HTML")
    await auto_delete(msg, delay=5)  # dissolves and disappears after 5s
"""

import asyncio
import logging
import re

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import Message

logger = logging.getLogger(__name__)

# Default delays (seconds) for different message types
DEFAULT_DELAY = 5
ERROR_DELAY = 8
SUCCESS_DELAY = 5
INFO_DELAY = 6
# Промежуточные подсказки, которые нужны юзеру лишь на время ввода
# (например, «Введите промокод:») — живут минуту и растворяются.
PROMPT_DELAY = 60

# Через сколько секунд стирать команды юзера (/start, /catalog, ...).
# За это время юзер успевает увидеть, ЧТО он отправил, а обработчик
# читает сообщение мгновенно — атрибуция и FSM не страдают.
USER_COMMAND_DELAY = 5

# Dissolve animation timing
DISSOLVE_STRIKETHROUGH_PAUSE = 0.5   # pause after strikethrough before final delete
DISSOLVE_FADE_PAUSE = 0.4            # pause after fade step


def _wrap_strikethrough(html: str) -> str:
    """Wrap visible text content in <s> tags for strikethrough effect.

    Preserves HTML tags like <tg-emoji>, <b>, <i>, <code> etc.
    by only wrapping the text nodes, not the tags themselves.
    """
    # Simple approach: wrap the entire content in <s>...</s>
    # This works because Telegram renders <s> inside other tags correctly
    return f"<s>{html}</s>"


def _fade_text(html: str) -> str:
    """Replace visible text with a minimal fading indicator.

    Keeps the same structure but replaces content with just '··'
    to simulate the last stage of dissolution.
    """
    return "··"


async def auto_delete(message, delay: float = DEFAULT_DELAY, animated: bool = True):
    """
    Schedule a message for automatic deletion after `delay` seconds.

    If animated=True (default), the message dissolves like Telegram's
    native delete animation:
      1. Text gets strikethrough (marked for removal)
      2. Text fades to minimal indicator
      3. Message is deleted

    This creates a smooth visual progression that looks like the
    message is dissolving away, similar to when you delete a message
    in Telegram and watch it fade out.

    Args:
        message: aiogram Message object to delete
        delay: seconds before deletion (default: 5)
        animated: show dissolve animation before deletion (default: True)

    The deletion runs as a background task and won't block the handler.
    If deletion fails (message already deleted, no permission, etc.),
    the error is silently logged.
    """
    async def _delete():
        # Wait the main delay period
        await asyncio.sleep(delay)

        if animated:
            is_photo = bool(message.photo)
            original_html = getattr(message, 'html_text', None)
            original_caption = None

            if is_photo:
                # For photo messages, caption lives in message.caption / html_caption
                original_caption = getattr(message, 'html_caption', None) or message.caption or ""

            # Step 1: Strikethrough — marks the message as "about to vanish"
            try:
                if is_photo:
                    faded = _wrap_strikethrough(original_caption or "")
                    await message.edit_caption(caption=faded, parse_mode="HTML")
                elif original_html:
                    faded = _wrap_strikethrough(original_html)
                    await message.edit_text(faded, parse_mode="HTML")
                else:
                    faded = f"<s>{message.text or ''}</s>"
                    await message.edit_text(faded, parse_mode="HTML")
            except Exception:
                # Message already deleted or can't be edited — stop
                return

            await asyncio.sleep(DISSOLVE_STRIKETHROUGH_PAUSE)

            # Step 2: Fade to minimal indicator — content dissolves away
            try:
                if is_photo:
                    await message.edit_caption(caption="··", parse_mode="HTML")
                else:
                    await message.edit_text("··", parse_mode="HTML")
            except Exception:
                return

            await asyncio.sleep(DISSOLVE_FADE_PAUSE)

        # Step 3: Final deletion — message vanishes
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"auto_delete: could not delete message: {e}")

    asyncio.create_task(_delete())


async def auto_delete_reply(message, reply_msg, delay: float = DEFAULT_DELAY, animated: bool = True):
    """
    Delete both the user's message and the bot's reply after `delay` seconds.
    Useful for sensitive data (account credentials, payment info).

    The bot's reply gets a dissolve animation; the user's original message
    is deleted silently at the same time as the final deletion.

    Args:
        message: the user's original message
        reply_msg: the bot's reply message
        delay: seconds before deletion (default: 5)
        animated: show dissolve animation on bot reply (default: True)
    """
    # Animate the bot reply
    await auto_delete(reply_msg, delay=delay, animated=animated)

    # Delete user message at the same time as the final bot message deletion
    # Total delay = delay + animation time
    total_delay = delay
    if animated:
        total_delay += DISSOLVE_STRIKETHROUGH_PAUSE + DISSOLVE_FADE_PAUSE

    async def _delete_user_msg():
        await asyncio.sleep(total_delay)
        try:
            await message.delete()
        except Exception:
            pass

    asyncio.create_task(_delete_user_msg())


async def delete_user_message(message, delay: float = USER_COMMAND_DELAY):
    """Тихо удалить сообщение ЮЗЕРА через `delay` секунд.

    Чужие сообщения бот редактировать не может (анимация растворения
    недоступна — edit_text на сообщении юзера запрещён Telegram),
    поэтому удаление происходит без анимации, одним вызовом delete().

    Используется для команд юзера (/start, /catalog, ...) и подобных
    «одноразовых» сообщений, которые не несут ценности в истории чата.

    Args:
        message: aiogram Message от юзера
        delay: секунд до удаления (default: USER_COMMAND_DELAY = 5)
    """
    async def _del():
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"delete_user_message: could not delete: {e}")

    asyncio.create_task(_del())


class UserCommandCleanupMiddleware(BaseMiddleware):
    """Гигиена чата: команды юзера растворяются через USER_COMMAND_DELAY.

    Telegram никогда не удаляет сообщения сам — /start, /catalog и
    прочие команды навсегда остаются в истории и захламляют чат.
    В личном чате бот имеет право удалить сообщение юзера, поэтому
    middleware планирует удаление каждой команды через 5 секунд.

    Что НЕ удаляется:
      - обычный текст юзера (ввод email, промокода, логина/пароля —
        их удаление привязано к успешной обработке, а не к факту приёма;
      - сообщения в группах (бот работает в личных чатах, но вдруг);
      - всё это можно отключить: .env → USER_COMMANDS_AUTODELETE=false.

    Регистрация: dp.message.outer_middleware(UserCommandCleanupMiddleware())
    — outer, чтобы сработало ДО любого хендлера и независимо от состояний FSM.
    """

    async def __call__(self, handler, event, data):
        try:
            from config import USER_COMMANDS_AUTODELETE  # лениво — патчабельно в тестах
            if (
                USER_COMMANDS_AUTODELETE
                and isinstance(event, Message)
                and getattr(event.chat, "type", None) == "private"
                and (event.text or "").strip().startswith("/")
            ):
                await delete_user_message(event, delay=USER_COMMAND_DELAY)
        except Exception as e:  # гигиена чата не должна ломать обработку
            logger.debug(f"UserCommandCleanupMiddleware: {e}")
        return await handler(event, data)


async def cleanup_qr_message(bot, chat_id: int, qr_message_id: int | None):
    """
    Delete a QR code photo message by its message_id.

    Used after payment cancellation or confirmation to remove the
    orphaned QR photo from the chat.

    Args:
        bot: aiogram Bot instance (callback.bot or message.bot)
        chat_id: Telegram chat ID
        qr_message_id: message_id of the QR photo to delete, or None
    """
    if not qr_message_id:
        return
    try:
        await bot.delete_message(chat_id, qr_message_id)
    except Exception as e:
        logger.debug(f"cleanup_qr: could not delete QR message {qr_message_id}: {e}")
