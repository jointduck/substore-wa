"""
Дедупликация повторяющихся сообщений (стираются ТОЛЬКО дубли).

Переходные сообщения (подсказки/ошибки/команды) растворяет соседний
движок utils/ephemeral.py (Task 35) — модули не пересекаются.

Политика чата (решение владельца, 2026-09; обновлено в Task 35):
  1. По таймеру НЕ удаляется ничего из содержательного: карточки заказов,
     оплаты, результаты действий, уведомления (переходные сообщения
     растворяет отдельный движок utils/ephemeral.py, группы D/E/G).
  2. Админские чаты: с Task 35 дедуп применяется И В НИХ
     (DEDUP_ADMINS=true — решение владельца «во всех чатах»).
     false возвращает прежнюю политику «админский лог неприкосновенен».
  3. Стираются ТОЛЬКО дубли: если бот отправляет в один и тот же чат
     сообщение с таким же содержимым, как у уже отправленного ранее
     (юзер в N-й раз открыл каталог, повторил команду /catalog,
     заново открыл оплату того же заказа) — предыдущая копия стирается,
     в чате остаётся одна свежая.

Как работает:
  - DuplicateCleanupMiddleware — session-middleware на уровне aiogram:
    перехватывает исходящие send_message (по тексту) и send_photo
    (по подписи), стирает прошлую копию с такой же сигнатурой и
    запоминает message_id нового сообщения.
  - UserCommandDedupMiddleware — входящий middleware для команд юзера:
    повторная отправка той же команды стирает её предыдущую копию.
    Однократная команда остаётся в чате навсегда.

Реестр хранится в памяти процесса (chat_id + сигнатура → message_id) и
ПЕРИОДИЧЕСКИ СБРАСЫВАЕТСЯ В SQLITE (UX №10): при старте реестр
восстанавливается из БД, поэтому после рестарта бота столбик «Каталог»
не начинает расти заново. Скидка на надёжность: при аварийном падении
теряется до одного интервала сброса (30 с) свежих записей — это
best-effort гигиена чата, а не гарантия. Отключается целиком:
.env → CHAT_DEDUP_ENABLED=false (тогда ни память, ни БД не используются).
"""

import asyncio
import hashlib
import logging
import time
from typing import Optional, Tuple

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.methods import SendMessage, SendPhoto, TelegramMethod
from aiogram.types import Message

from config import ADMIN_IDS, CHAT_DEDUP_ENABLED, DEDUP_ADMINS

logger = logging.getLogger(__name__)

# Хранить последнюю копию каждой сигнатуры N секунд (24 ч).
# За это время «долгоживущие» экраны (меню, каталог) успевают
# продублироваться много раз, а реестр не растёт бесконечно.
ENTRY_TTL = 24 * 3600

# Максимум записей в реестре (защита памяти): при переполнении
# выселяется самая старая запись (FIFO).
MAX_ENTRIES = 4096

# Интервал сброса реестра в SQLite (секунды). Компромисс: чем чаще,
# тем меньше теряется при падении, тем больше фоновых записей.
FLUSH_INTERVAL = 30

# Раз в сколько интервалов чистить просроченные строки в самой БД
# ( sent_at старше ENTRY_TTL ) — чтобы таблица не росла вечно.
PRUNE_EVERY_FLUSHES = 20  # 20 × 30 с = раз в 10 минут

# Реестр: (chat_id, signature) → (message_id, sent_at)
# sent_at — WALL CLOCK (time.time()), а не monotonic: только wall-clock
# переживает сохранение в БД и восстановление после рестарта.
_registry: dict[Tuple[int, str], Tuple[int, float]] = {}

# Изменённые с последнего сброса ключи: добавленные/обновлённые (_dirty)
# и удалённые из памяти (_deleted — их строки надо стереть из БД).
_dirty: set[Tuple[int, str]] = set()
_deleted: set[Tuple[int, str]] = set()


def _make_signature(kind: str, content: str) -> str:
    """Короткий отпечаток содержимого сообщения.

    kind отделяет текстовые сообщения («t») от фото-подписей («p»)
    и команд юзера («c»), чтобы разные типы не совпали случайно.
    """
    digest = hashlib.sha1(f"{kind}|{content}".encode("utf-8")).hexdigest()[:20]
    return f"{kind}:{digest}"


def _outgoing_signature(method: TelegramMethod) -> Optional[Tuple[int, str]]:
    """Сигнатура исходящего сообщения или None, если дедуп не применим.

    Применимо только к личным чатам юзеров (не админам, не группам):
      - SendMessage  → по тексту + parse_mode;
      - SendPhoto    → по подписи (caption), только если она есть.
    """
    chat_id = getattr(method, "chat_id", None)
    if not isinstance(chat_id, int) or chat_id <= 0:
        return None  # группы/каналы/строковые chat_id не трогаем
    if chat_id in ADMIN_IDS and not DEDUP_ADMINS:
        return None  # DEDUP_ADMINS=false: админские чаты не трогаем

    if isinstance(method, SendMessage):
        text = (method.text or "").strip()
        if not text:
            return None
        return chat_id, _make_signature("t", f"{method.parse_mode}|{text}")

    if isinstance(method, SendPhoto):
        caption = (method.caption or "").strip()
        if not caption:
            return None  # фото без подписи не дедуплицируем
        return chat_id, _make_signature("p", f"{method.parse_mode}|{caption}")

    return None


async def _erase_previous_copies(bot, chat_id: int, signature: str) -> None:
    """Стереть прошлую копию сообщения с такой же сигнатурой (если жива)."""
    entry = _registry.get((chat_id, signature))
    if not entry:
        return
    _registry.pop((chat_id, signature), None)
    # Строка в БД больше не актуальна — пометить к удалению при сбросе
    # (иначе после рестарта «воскреснет» ссылка на уже стёртое сообщение:
    # не опасно, но лишний futile delete_message).
    _deleted.add((chat_id, signature))
    _dirty.discard((chat_id, signature))
    message_id, sent_at = entry
    if time.time() - sent_at > ENTRY_TTL:
        return  # запись просрочена — просто забыли её
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        # Сообщение уже удалено (юзером/устарело) — это норма.
        logger.debug(f"chat_dedup: could not erase copy {message_id} in {chat_id}: {e}")


def _remember_message(chat_id: int, signature: str, message_id: int) -> None:
    """Запомнить свежую копию (в реестре живёт только последняя)."""
    key = (chat_id, signature)
    if key not in _registry and len(_registry) >= MAX_ENTRIES:
        oldest = next(iter(_registry))
        _registry.pop(oldest, None)
        _deleted.add(oldest)  # выселенная запись подлежит удалению из БД
    _registry[key] = (message_id, time.time())
    _dirty.add(key)
    _deleted.discard(key)


# ─── Persistence: SQLite (UX №10) ─────────────────────────────────

async def load_registry(db) -> None:
    """Восстановить реестр из БД при старте бота.

    Берёт только живые записи (моложе ENTRY_TTL), самые свежие
    MAX_ENTRIES штук. Просроченные строки стираются из БД сразу.
    Вызывается в main() ПОСЛЕ db.init() и только при включённом дедупе.
    """
    cutoff = time.time() - ENTRY_TTL
    async with db._get_connection() as conn:
        await conn.execute("DELETE FROM chat_dedup_registry WHERE sent_at < ?", (cutoff,))
        cursor = await conn.execute(
            """SELECT chat_id, signature, message_id, sent_at
               FROM chat_dedup_registry
               ORDER BY sent_at DESC LIMIT ?""",
            (MAX_ENTRIES,),
        )
        rows = await cursor.fetchall()
        await conn.commit()
    _registry.clear()
    _dirty.clear()
    _deleted.clear()
    for chat_id, signature, message_id, sent_at in rows:
        _registry[(chat_id, signature)] = (message_id, float(sent_at))


async def flush_registry(db) -> None:
    """Сбросить накопленные изменения реестра в БД (write-behind).

    Применяет только дельту (_dirty/_deleted), поэтому обычный сброс —
    несколько мелких запросов. Вызывается из flusher_loop; при пустой
    дельте сразу выходит (ноль запросов в простое).
    """
    global _dirty, _deleted
    if not _dirty and not _deleted:
        return
    dirty, deleted = set(_dirty), set(_deleted)
    _dirty.clear()
    _deleted.clear()
    try:
        async with db._get_connection() as conn:
            for (chat_id, signature), (message_id, sent_at) in [
                (k, _registry[k]) for k in dirty if k in _registry
            ]:
                await conn.execute(
                    """INSERT INTO chat_dedup_registry (chat_id, signature, message_id, sent_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(chat_id, signature)
                       DO UPDATE SET message_id = excluded.message_id,
                                     sent_at = excluded.sent_at""",
                    (chat_id, signature, message_id, sent_at),
                )
            for chat_id, signature in deleted:
                await conn.execute(
                    "DELETE FROM chat_dedup_registry WHERE chat_id = ? AND signature = ?",
                    (chat_id, signature),
                )
            await conn.commit()
    except Exception as e:
        # Вернуть дельту в очереди — попробуем на следующем сбросе
        _dirty |= dirty
        _deleted |= deleted
        logger.warning(f"chat_dedup flush failed (retry in {FLUSH_INTERVAL}s): {e}")


async def flusher_loop(db) -> None:
    """Фоновый цикл: раз в FLUSH_INTERVAL сбрасывает реестр в БД.

    Периодически (каждые PRUNE_EVERY_FLUSHES циклов) подчищает из БД
    просроченные строки. Запускается в main() только при включённом
    дедупе: _start_task(chat_dedup.flusher_loop(db), "chat_dedup_flusher").
    """
    cycles = 0
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        cycles += 1
        try:
            await flush_registry(db)
            if cycles % PRUNE_EVERY_FLUSHES == 0:
                cutoff = time.time() - ENTRY_TTL
                async with db._get_connection() as conn:
                    await conn.execute("DELETE FROM chat_dedup_registry WHERE sent_at < ?", (cutoff,))
                    await conn.commit()
        except Exception as e:
            logger.warning(f"chat_dedup flusher cycle failed: {e}")


class DuplicateCleanupMiddleware:
    """Session-middleware: стирает дубль ДО отправки нового сообщения.

    Регистрация: bot.session.middleware.register(DuplicateCleanupMiddleware()).
    Работает для ЛЮБОГО способа отправки (message.answer, callback.answer
    не трогает — это попапы; broadcast, drip, ответы поддержки) — всё,
    что уходит в личный чат юзера через send_message/send_photo.
    """

    async def __call__(self, make_request, bot, method: TelegramMethod):
        signature = None
        chat_id = None
        try:
            if CHAT_DEDUP_ENABLED:
                found = _outgoing_signature(method)
                if found:
                    chat_id, signature = found
                    await _erase_previous_copies(bot, chat_id, signature)
        except Exception as e:  # гигиена чата не должна ломать отправку
            logger.debug(f"DuplicateCleanupMiddleware pre-send: {e}")

        result = await make_request(bot, method)

        try:
            if signature and chat_id is not None and isinstance(result, Message):
                _remember_message(chat_id, signature, result.message_id)
        except Exception as e:
            logger.debug(f"DuplicateCleanupMiddleware post-send: {e}")

        return result


def _command_signature(text: str) -> str:
    """Сигнатура команды: первый токен без @упоминания бота, в нижнем регистре.

    Дубликат — ПОВТОР ТОЙ ЖЕ КОМАНДЫ: «/catalog» и «/catalog@MyShopBot» —
    одна команда, повторная отправка стирает предыдущую копию. «/start»
    и «/start ref_abc» — тоже одна и та же команда /start (повторный
    запуск по другой ссылке), старая копия уходит. Разные команды
    (/start, /catalog, /help) друг другу не мешают.
    """
    first = text.strip().split(maxsplit=1)[0]
    first = first.split("@", 1)[0].lower()
    return _make_signature("c", first)


def _menu_buttons() -> set:
    """Тексты reply-кнопок меню (импорт ленивый — без циклических зависимостей).

    Повторное нажатие кнопки меню — тот же случай, что и повтор команды:
    «Каталог», «Каталог», «Каталог» столбиком в чате — это дубли одного
    и того же действия (каноничный пример владельца). Стирается копия
    ТОЧНОГО текста кнопки: ручной ввод «каталог» с маленькой буквы или
    «зайди в каталог» — обычные сообщения и не стираются никогда.
    """
    try:
        from handlers.user_handlers import MENU_BUTTONS
        return MENU_BUTTONS
    except Exception:
        return set()


class UserCommandDedupMiddleware(BaseMiddleware):
    """Команды и нажатия кнопок меню стираются ТОЛЬКО как дубли.

    Юзер отправил /catalog второй раз → первая копия /catalog стирается.
    Юзер нажал кнопку «Каталог» второй раз → его предыдущее нажатие
    «Каталог» стирается (сам экран каталога дедуплицируется отдельно —
    DuplicateCleanupMiddleware). Однократная команда / разовое нажатие
    остаётся в чате — а растворяет их уже UserCommandCleanupMiddleware
    (utils/ephemeral.py, Task 35). Админам: DEDUP_ADMINS=true — дедуп
    применяется и к ним, false — не применяется.

    Регистрация: dp.message.outer_middleware(...) — срабатывает до
    любых хендлеров и независимо от состояний FSM.
    """

    async def __call__(self, handler, event, data):
        try:
            text = (event.text or "").strip() if isinstance(event, Message) else ""
            is_command = text.startswith("/")
            is_menu_tap = bool(text) and text in _menu_buttons()
            if (
                CHAT_DEDUP_ENABLED
                and isinstance(event, Message)
                and event.bot is not None
                and getattr(event.chat, "type", None) == "private"
                and (is_command or is_menu_tap)
                and event.from_user
                and (event.from_user.id not in ADMIN_IDS or DEDUP_ADMINS)
            ):
                if is_command:
                    signature = _command_signature(text)
                else:
                    # kind "m" — кнопки меню не пересекаются с командами
                    # и текстами бота даже при совпадении хэша содержимого.
                    signature = _make_signature("m", text)
                await _erase_previous_copies(event.bot, event.chat.id, signature)
                _remember_message(event.chat.id, signature, event.message_id)
        except Exception as e:  # гигиена чата не должна ломать обработку
            logger.debug(f"UserCommandDedupMiddleware: {e}")
        return await handler(event, data)
