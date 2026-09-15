"""
Ephemeral-растворение переходных сообщений (Task 35, Bot API 10.3 era).

Политика чата v15 (решение владельца 2026-09-14; дополняет дедуп дублей
из utils/chat_dedup.py — дедуп не тронут, работает поверх):

  Группа D (FSM-промпты):   промпт + ввод юзера стираются ПАРОЙ батчем
                            deleteMessages после завершения ввода (успех /
                            отмена). Оставленный без ответа промпт уходит
                            сам через PROMPT_TTL. При НЕВАЛИДНОМ вводе
                            пара «ввод юзера + ошибка» растворяется через
                            ERROR_DELAY — юзер пробует ещё раз, мусор уходит.
  Группа E (ошибки):        message-ошибки растворяются через ERROR_DELAY.
  Группа C (переходные):    промежуточные «Секунду…»-сообщения — INFO_DELAY.
  Группа G (сообщения юзера): команды (с «/») и нажатия reply-кнопок меню
                            растворяются через COMMAND_DELAY во ВСЕХ
                            личных чатах, ВКЛЮЧАЯ админские (решение
                            владельца «во всех чатах»).

  НИКОГДА не растворяется: навигационные экраны, каталог, карточки
  заказов, экраны оплат и QR, подписки, тикеты, результаты действий,
  проактивные уведомления/рассылки, ответы поддержки, сообщения бота
  в админских чатах, обычный (не-FSM) текст юзера.

Нативный режим Bot API 10.3 (EPHEMERAL_NATIVE=true в .env): transient-
сообщения отправляются с ephemeral_message_parameters — жизнью такого
сообщения управляет сам Telegram. При любом отказе API — тихий откат
на классику («отправили + стёрли по нашему таймеру»). По умолчанию
выключен: классика гарантирует доставку переходного сообщения.
"""

import asyncio
import logging

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import EphemeralMessageParameters, Message

from config import EPHEMERAL_ENABLED, EPHEMERAL_NATIVE

logger = logging.getLogger(__name__)

# Задержки растворения (секунды) — наследие v4–v9, проверенные значения.
SUCCESS_DELAY = 5     # краткие подтверждения-переходы
INFO_DELAY = 6        # переходные «Секунду…»
ERROR_DELAY = 15      # ошибки FSM-ввода (успеть прочитать и повторить)
COMMAND_DELAY = 5     # команды юзера и нажатия кнопок меню
PAIR_DELAY = 2        # пара «промпт + ввод» после успешного завершения
PROMPT_TTL = 600      # оставленный без ответа FSM-промпт уходит сам (10 мин)

# Живые задачи растворения (защита от GC по PEP 668)
_tasks: set = set()


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def schedule_erase(bot, chat_id: int, message_id, delay: float) -> None:
    """Растворить сообщение через delay секунд (fire-and-forget, тихо).

    Любая ошибка (уже стёрто юзером, старше 48 ч, сетевой сбой) — норма
    для гигиены чата и гасится: растворение не должно ничего ломать.
    """
    if not EPHEMERAL_ENABLED or not message_id or delay <= 0:
        return
    try:
        _spawn(_erase_later(bot, int(chat_id), int(message_id), delay))
    except Exception as e:
        logger.debug(f"ephemeral schedule_erase: {e}")


async def _erase_later(bot, chat_id: int, message_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.debug(f"ephemeral erase {message_id}/{chat_id}: {e}")


def dissolve(message: Message, delay: float) -> None:
    """Растворить отправленное ботом сообщение (Message-объект)."""
    if message is None:
        return
    try:
        schedule_erase(message.bot, message.chat.id, message.message_id, delay)
    except Exception as e:
        logger.debug(f"ephemeral dissolve: {e}")


def dissolve_ids(bot, chat_id: int, message_ids, delay: float) -> None:
    """Растворить несколько сообщений (по id) — пачкой, одним запросом.

    Батч deleteMessages (Bot API 7.x+) уходит ОДНИМ запросом; если API
    батч отклонил — тихий добор по одному через delete_message.
    Используется для пар «промпт + ввод юзера» (группа D).
    """
    ids = []
    for mid in message_ids or []:
        if mid:
            try:
                mid = int(mid)
            except (TypeError, ValueError):
                continue
            if mid not in ids:
                ids.append(mid)
    if not EPHEMERAL_ENABLED or not ids:
        return
    try:
        _spawn(_erase_ids_later(bot, int(chat_id), ids, delay))
    except Exception as e:
        logger.debug(f"ephemeral dissolve_ids: {e}")


async def _erase_ids_later(bot, chat_id: int, ids: list, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        if len(ids) > 1:
            await bot.delete_messages(chat_id=chat_id, message_ids=ids)
            return
    except Exception as e:
        logger.debug(f"ephemeral batch erase {ids}/{chat_id}: {e}")
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception as e:
            logger.debug(f"ephemeral erase {mid}/{chat_id}: {e}")


# ─── Нативные Ephemeral Messages (Bot API 10.3) ───────────────────

def native_kwargs(
    receiver_user_id: int,
    callback_query_id: str | None = None,
    replace_callback_query_message: bool | None = None,
) -> dict:
    """kwargs для send_*: нативный ephemeral при EPHEMERAL_NATIVE=true.

    Пустой dict при выключенном режиме — вызов в хендлерах без ветвлений:
      await message.answer(text, **eph.native_kwargs(uid)).
    callback_query_id привязывает ephemeral к нажатию кнопки (Telegram
    показывает его как реакцию на нажатие); replace_callback_query_message
    =True показывает ephemeral НА МЕСТЕ исходного сообщения кнопки.
    """
    if not (EPHEMERAL_NATIVE and EPHEMERAL_ENABLED):
        return {}
    # EphemeralMessageParameters — замороженная pydantic-модель:
    # все поля передаются только в конструктор
    kw: dict = {
        "callback_query_id": str(callback_query_id) if callback_query_id else None,
        "replace_callback_query_message": (
            bool(replace_callback_query_message)
            if replace_callback_query_message is not None else None
        ),
    }
    return {
        "ephemeral_message_parameters": EphemeralMessageParameters(
            receiver_user_id=int(receiver_user_id),
            **kw,
        )
    }


# ─── Группа G: растворение команд и кнопок меню юзера ─────────────

def _menu_buttons() -> set:
    """Тексты reply-кнопок меню (единый источник — utils/chat_dedup)."""
    try:
        from utils.chat_dedup import _menu_buttons as _mb
        return _mb()
    except Exception:
        return set()


class UserCommandCleanupMiddleware(BaseMiddleware):
    """Команды и нажатия reply-кнопок меню растворяются через COMMAND_DELAY.

    Отличие от UserCommandDedupMiddleware (chat_dedup.py): тот стирает
    только ПОВТОРЫ, этот растворяет КАЖДУЮ команду/кнопку через 5 с —
    по всему личному чату, включая админские (решение владельца «во
    всех чатах»). Регистрация в main.py ПОСЛЕ дедуп-middleware: дедуп
    сначала запоминает команду в реестре, затем cleanup планирует её
    растворение. Растворённая команда остаётся в дедуп-реестре — поздний
    дедуп-стир по ней станет futile delete и тихо погасится (норма).

    Регистрация: dp.message.outer_middleware(UserCommandCleanupMiddleware()).
    """

    async def __call__(self, handler, event, data):
        try:
            text = (event.text or "").strip() if isinstance(event, Message) else ""
            is_command = text.startswith("/")
            is_menu_tap = bool(text) and text in _menu_buttons()
            if (
                EPHEMERAL_ENABLED
                and isinstance(event, Message)
                and event.bot is not None
                and getattr(event.chat, "type", None) == "private"
                and event.from_user is not None
                and (is_command or is_menu_tap)
            ):
                schedule_erase(event.bot, event.chat.id, event.message_id, COMMAND_DELAY)
        except Exception as e:  # гигиена чата не должна ломать обработку
            logger.debug(f"UserCommandCleanupMiddleware: {e}")
        return await handler(event, data)
