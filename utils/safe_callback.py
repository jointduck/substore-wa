"""Safe callback-query answering.

Telegram allows answering a callback query (the toast / alert on the
button) only within a few seconds after the click. When the bot's
connection to Telegram is flaky (proxy drops, long-poll reconnects),
clicks are delivered late — the query has already expired and ANY
`callback.answer()` raises:

    TelegramBadRequest: Bad Request: query is too old and response
    timeout expired or query ID is invalid

which crashes the whole update. `safe_answer()` wraps `callback.answer()`
and turns that error into a harmless fallback (optionally delivering the
text as a regular message instead).
"""

import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


async def safe_answer(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
    cache_time: int = 0,
    fallback_to_message: bool = True,
) -> None:
    """Answer a callback query without crashing on expired/invalid queries.

    - Query still valid  -> behaves exactly like callback.answer().
    - Query expired/invalid -> error is swallowed; if `text` was passed and
      `fallback_to_message` is True, the text is sent as a regular message
      so the user still gets feedback.
    - User blocked the bot (TelegramForbiddenError) -> silently ignored.
    """
    try:
        await callback.answer(text=text, show_alert=show_alert, cache_time=cache_time)
        return
    except TelegramBadRequest as e:
        msg = str(e)
        if "query is too old" in msg or "query ID is invalid" in msg:
            logger.debug("Callback query expired (user %s): %s", callback.from_user.id, msg)
        else:
            logger.warning("callback.answer failed: %s", msg)
    except TelegramForbiddenError:
        logger.debug("User %s blocked the bot; callback answer skipped", callback.from_user.id)
    except Exception as e:  # never let a toast kill the whole update
        logger.warning("Unexpected error in callback.answer: %s", e)

    # Fallback: deliver the text as a plain message (only for alerts and
    # meaningful texts — empty toasts are just dropped)
    if text and fallback_to_message:
        try:
            await callback.bot.send_message(
                callback.from_user.id,
                text,
            )
        except Exception:
            pass
