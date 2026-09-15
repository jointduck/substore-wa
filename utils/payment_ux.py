"""
Платёжный UX: гашение мёртвых платёжных экранов + кулдаун проверки оплаты.

Политика чата не нарушается: НИЧЕГО не удаляется. «Гашение» — это
РЕДАКТИРОВАНИЕ уже отправленного сообщения, история покупок остаётся
в чате целиком. Админские чаты не затрагиваются: реестр заполняют
только экраны, отправленные покупателю в его личный чат.

1. Реестр платёжных экранов.
   Когда бот показывает покупателю платёжный экран (текст с кнопками
   «Оплатить»/«Проверить оплату» или QR-фото), message_id запоминается
   здесь. Когда заказ ОПЛАЧЕН, эти экраны превращаются в мёртвые:
   кнопки уже не работают, QR больше не нужен. extinguish() редактирует:
     - текстовый экран  → короткое «✅ Оплата подтверждена» (без кнопок);
     - QR-фото          → подпись «оплачено» (фото-квитанция остаётся).
   Экран, который обработчик оплаты УЖЕ переоформил сам (rich-сообщение
   «Оплата подтверждена + введите данные»), пропускается через
   skip_message_ids — его не трогаем.

2. Кулдаун «Проверить оплату».
   Юзеры часто спамят кнопку каждые 1–2 секунды; каждая проверка —
   это запрос к блокчейну/Tribute/Digiseller. Между РЕАЛЬНЫМИ
   проверками — пауза CHECK_COOLDOWN_SECONDS. Попапы «Заказ не найден»
   / «Оплата уже подтверждена» дешёвые и под кулдаун не попадают:
   блокируется только дорогой вызов API.

Реестр хранится в памяти процесса (best-effort, как chat_dedup):
после рестарта просто нечего гасить — не критично.
"""

import logging
import time
from typing import Iterable, Tuple

from emojis import ce

logger = logging.getLogger(__name__)

# ── Реестр платёжных экранов ────────────────────────────────────────

# Запись живёт сутки: оплаты подтверждаются за минуты, заказы истекают
# через 30 минут — сутки с запасом покрывают любой живой сценарий.
SCREEN_TTL = 24 * 3600

# Защита памяти: суммарное число запомненных экранов (FIFO-выселение).
MAX_ENTRIES = 2048

# (chat_id, order_id) → [{message_id, kind, ts}]
# kind: "text" — текстовый экран с кнопками, "photo" — QR-фото с подписью.
_registry: dict[Tuple[int, int], list[dict]] = {}


def remember_screen(chat_id: int, order_id: int, message_id: int, kind: str) -> None:
    """Запомнить платёжный экран заказа (вызывается сразу после отправки).

    Повторное запоминание того же message_id (экран переоткрыт — бот
    отредактировал то же сообщение) просто обновляет временную метку.
    """
    if not isinstance(chat_id, int) or chat_id <= 0:
        return
    try:
        key = (chat_id, int(order_id))
        entry = {"message_id": int(message_id), "kind": kind, "ts": time.monotonic()}
        screens = _registry.setdefault(key, [])
        for i, old in enumerate(screens):
            if old["message_id"] == entry["message_id"]:
                screens[i] = entry
                return
        screens.append(entry)
        # Кап памяти: выселяем самую старую запись (FIFO по вставке).
        total = sum(len(v) for v in _registry.values())
        if total > MAX_ENTRIES:
            oldest_key = next(iter(_registry))
            bucket = _registry[oldest_key]
            bucket.pop(0)
            if not bucket:
                _registry.pop(oldest_key, None)
    except Exception as e:  # гигиена чата не должна ломать платёжный флоу
        logger.debug(f"payment_ux.remember_screen: {e}")


def _paid_caption(order_id: int) -> str:
    return f"{ce('check')}  QR-код заказа #{order_id} — оплата подтверждена."


def _paid_text(order_id: int) -> str:
    return f"{ce('check')}  Оплата заказа #{order_id} подтверждена."


def _expired_caption(order_id: int) -> str:
    return f"{ce('clock')}  QR-код заказа #{order_id} — время оплаты истекло."


def _expired_text(order_id: int) -> str:
    return (
        f"{ce('clock')}  <b>Время оплаты заказа #{order_id} истекло</b>\n\n"
        f"Если вы уже отправили деньги — напишите в «Поддержку», "
        f"платёж проверят вручную и оформят заказ.\n\n"
        f"Иначе оформите новый заказ через каталог."
    )


async def extinguish(
    bot,
    chat_id: int,
    order_id: int,
    *,
    skip_message_ids: Iterable[int] = (),
) -> None:
    """Погасить мёртвые платёжные экраны заказа (после успешной оплаты).

    РЕДАКТИРУЕТ сообщения, ничего не удаляет. Экраны из skip_message_ids
    не трогаются — их обработчик уже переоформил в rich-сообщение об
    успехе. Ошибки (сообщение удалено юзером, старое и т.п.) глушатся:
    это гигиена чата, а не критичный путь.
    """
    try:
        key = (int(chat_id), int(order_id))
        screens = _registry.pop(key, [])
        skip = set(skip_message_ids)
        now = time.monotonic()
        for screen in screens:
            if screen["message_id"] in skip:
                continue  # обработчик уже сделал из него сообщение об успехе
            if now - screen["ts"] > SCREEN_TTL:
                continue
            try:
                if screen["kind"] == "photo":
                    await bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=screen["message_id"],
                        caption=_paid_caption(order_id),
                        reply_markup=None,
                    )
                else:
                    await bot.edit_message_text(
                        _paid_text(order_id),
                        chat_id=chat_id,
                        message_id=screen["message_id"],
                        reply_markup=None,
                    )
            except Exception as e:
                logger.debug(
                    f"payment_ux.extinguish: screen {screen['message_id']} "
                    f"of order #{order_id}: {e}"
                )
    except Exception as e:
        logger.debug(f"payment_ux.extinguish: {e}")


async def extinguish_expired(bot, chat_id: int, order_id: int) -> None:
    """Погасить платёжные экраны заказа при ИСТЕЧЕНИИ окна оплаты (UX №2).

    Вызывается фоновым cleanup после отмены просроченного заказа: экраны
    TON/USDT/Digiseller с кнопками «Оплатить»/«Проверить оплату» и QR-фото
    редактируются в честное состояние «⌛ Время оплаты истекло», кнопки
    деактивируются. РЕДАКТИРОВАНИЕ, не удаление — политика чата не
    нарушается (админские чаты и тут не задействованы: реестр содержит
    только юзерские экраны).

    Идемпотентно: реестр очищается после первого прогона, повторный вызов
    ничего не делает. Если платёж всё же подтвердится позже, поллер
    «воскресит» заказ и пришлёт новое сообщение — юзер не потеряется.
    """
    try:
        key = (int(chat_id), int(order_id))
        screens = _registry.pop(key, [])
        now = time.monotonic()
        catalog_kb = None
        for screen in screens:
            if now - screen["ts"] > SCREEN_TTL:
                continue
            try:
                if screen["kind"] == "photo":
                    await bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=screen["message_id"],
                        caption=_expired_caption(order_id),
                        reply_markup=None,
                    )
                else:
                    if catalog_kb is None:
                        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                        catalog_kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="В каталог", callback_data="back_catalog")],
                        ])
                    await bot.edit_message_text(
                        _expired_text(order_id),
                        chat_id=chat_id,
                        message_id=screen["message_id"],
                        reply_markup=catalog_kb,
                        parse_mode="HTML",
                    )
            except Exception as e:
                logger.debug(
                    f"payment_ux.extinguish_expired: screen {screen['message_id']} "
                    f"of order #{order_id}: {e}"
                )
    except Exception as e:
        logger.debug(f"payment_ux.extinguish_expired: {e}")


async def extinguish_for_callback(bot, event, order_id: int) -> None:
    """Гашение экранов заказа из обработчика успешной оплаты.

    Принимает CallbackQuery (кнопка «Проверить оплату») или Message
    (successful_payment от Telegram Stars).

    Для CallbackQuery: event.message — это платёжный экран, который
    обработчик ТОЛЬКО ЧТО переоформил в rich-сообщение об успехе
    («Оплата подтверждена + введите данные»). Он исключается из
    гашения, остальные экраны заказа (QR-фото и т.п.) погашаются.

    Для Message: обработчик ничего не переоформлял — гасим всё,
    что записано в реестре для этого заказа.
    """
    try:
        msg = getattr(event, "message", None)
        if msg is None and getattr(event, "chat", None) is not None:
            # Message: гасим все записанные экраны заказа
            await extinguish(bot, event.chat.id, order_id)
            return
        chat_id = msg.chat.id if msg is not None else event.from_user.id
        skip = {msg.message_id} if msg is not None else set()
        await extinguish(bot, chat_id, order_id, skip_message_ids=skip)
    except Exception as e:
        logger.debug(f"payment_ux.extinguish_for_callback: {e}")


# ── Кулдаун «Проверить оплату» ──────────────────────────────────────

# Проверка оплаты бьёт во внешний API (блокчейн/Tribute/Digiseller) —
# чаще раза в 10 секунд проверять бессмысленно: подтверждение приходит
# с задержкой десятки секунд.
CHECK_COOLDOWN_SECONDS = 10

# Защита памяти: максимум отслеживаемых юзеров (FIFO-выселение).
CHECK_COOLDOWN_MAX_TRACKED = 10000

_last_check_press: dict[int, float] = {}


def check_cooldown_left(user_id: int) -> int:
    """Сколько секунд до следующей реальной проверки (0 — можно)."""
    last = _last_check_press.get(user_id)
    if not last:
        return 0
    left = CHECK_COOLDOWN_SECONDS - (time.monotonic() - last)
    if left <= 0:
        return 0
    return int(left) + (1 if left % 1 else 0)  # округление вверх


def note_check_press(user_id: int) -> None:
    """Отметить РЕАЛЬНУЮ проверку (вызывается перед запросом к API)."""
    try:
        if len(_last_check_press) >= CHECK_COOLDOWN_MAX_TRACKED and user_id not in _last_check_press:
            oldest = next(iter(_last_check_press))
            _last_check_press.pop(oldest, None)
        _last_check_press[user_id] = time.monotonic()
    except Exception as e:
        logger.debug(f"payment_ux.note_check_press: {e}")
