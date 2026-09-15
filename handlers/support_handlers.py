"""Support tickets: full-cycle request handling between users and admins.

Flow (user side):
  «Поддержка» → screen → «Создать обращение» → pick order → describe problem
  (text or screenshot) → ticket created → admins notified with buttons.
  «Мои обращения» → list with statuses → thread view → follow-up / close.

Flow (admin side):
  «Обращения» in the admin menu — filterable browser (open / answered /
  closed / all) with pagination; every entry opens the full thread with
  [Ответить] [Закрыть] buttons right in it.
  Notification card with [Ответить] [Закрыть] [Тред] buttons.
  /tickets — quick list of open tickets, /ticket <id> — open a thread.

Anti-spam: max 2 open tickets per user + 5 min cooldown between tickets.

Registered in main.py BEFORE user_router so FSM state handlers here
take priority over the generic fallbacks in user_handlers.
"""

import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS
from models.database import db
from keyboards.keyboards import cancel_kb, pagination_row
from emojis import ce
from utils.html_utils import safe_html
from utils.safe_callback import safe_answer
from utils import ephemeral as eph  # Task 35: растворение переходных сообщений
from handlers.user_handlers import get_menu, MENU_BUTTONS, is_admin, STATUS_ICON

logger = logging.getLogger(__name__)
router = Router()

# ─── Settings ───────────────────────────────────────────────────────

MAX_OPEN_TICKETS = 2      # max simultaneously open tickets per user
COOLDOWN_MINUTES = 5      # pause between new tickets
HISTORY_LIMIT = 12        # messages shown in thread view
TEXT_LIMIT = 3500         # max stored length of one ticket message
ADMIN_LIST_PER_PAGE = 10  # tickets per page in the admin browser

# filter key → tab label (sup:alf: / sup:alp: callbacks)
ADMIN_FILTERS = (
    ("open", "🟢 Открытые"),
    ("answered", "🔵 С ответом"),
    ("closed", "⚪️ Закрытые"),
    ("all", "📚 Все"),
)


def _filter_label(flt: str) -> str:
    for key, label in ADMIN_FILTERS:
        if key == flt:
            return label
    return flt

# Menu buttons that must ESCAPE ticket FSM flows (fall through to the
# menu handlers in user_router). «Отмена» is handled locally.
_ESCAPES = MENU_BUTTONS - {"Отмена"}

STATUS_LABEL = {
    "open": ("🟢", "Открыт"),
    "answered": ("🔵", "Есть ответ"),
    "closed": ("⚪️", "Закрыт"),
}


# ─── FSM states ─────────────────────────────────────────────────────

class TicketFlow(StatesGroup):
    waiting_for_text = State()      # user describes the problem
    waiting_for_followup = State()  # user adds details to existing ticket


class AdminTicketReply(StatesGroup):
    waiting_for_reply = State()     # admin writes an answer


# ─── Helpers ────────────────────────────────────────────────────────

def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _ago(ts: str | None) -> str:
    """Human-readable 'time ago' for SQLite UTC timestamps."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(str(ts)[:19])
        except ValueError:
            return ""
    secs = max(0, int((datetime.utcnow() - dt).total_seconds()))
    if secs < 60:
        return "только что"
    mins = secs // 60
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    if days < 30:
        return f"{days} дн. назад"
    return dt.strftime("%d.%m.%Y")


def _short_time(ts: str | None) -> str:
    return str(ts)[11:16] if ts else ""


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_str(status: str) -> str:
    icon, label = STATUS_LABEL.get(status, ("⚪️", status))
    return f"{icon} {label}"


def _order_line(order: dict | None) -> str:
    """One-line order summary for ticket cards."""
    if not order:
        return ""
    price = order.get("price_usdt", 0)
    price_s = f"${price:g}" if isinstance(price, (int, float)) else str(price)
    created = str(order.get("created_at") or "")[:16]
    return (
        f"{ce('package')} Заказ #{order['order_id']}: "
        f"{safe_html(order.get('service_name', ''))} — {safe_html(order.get('plan_name', ''))}\n"
        f"   Статус: {STATUS_ICON.get(order.get('status', ''), '')} "
        f"{safe_html(str(order.get('status', '')))} · {price_s}"
        + (f"\n   Создан: {created}" if created else "")
    )


async def _notify_admins(bot, text: str, ticket_id: int) -> None:
    """Send a ticket card to every admin (best effort)."""
    kb = _kb(
        [_btn(f"✍️ Ответить", f"sup:areply:{ticket_id}"),
         _btn(f"✅ Закрыть", f"sup:aclose:{ticket_id}")],
        [_btn(f"📜 Открыть тред", f"sup:detail:{ticket_id}")],
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Ticket notify to admin {admin_id} failed: {e}")


# ─── Screen texts ───────────────────────────────────────────────────

SUPPORT_SCREEN = (
    f"{ce('speech')}   <b>Поддержка</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    f"{ce('ticket')} <b>Создать обращение</b> — проблема с заказом, оплатой или доступом\n"
    f"{ce('copy')} <b>Мои обращения</b> — статусы и вся переписка\n\n"
    "<i>Опишите проблему максимально подробно — так мы поможем быстрее.</i>"
)

SUPPORT_KB = _kb(
    [_btn(f"➕ Создать обращение", "sup:new")],
    [_btn(f"📋 Мои обращения", "sup:my")],
)


# ─── Entry point: «Поддержка» menu button ───────────────────────────

@router.message(StateFilter("*"), F.text == "Поддержка")
async def support_menu_open(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(SUPPORT_SCREEN, reply_markup=SUPPORT_KB, parse_mode="HTML")


@router.callback_query(F.data == "sup:menu")
async def support_menu_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(
            SUPPORT_SCREEN, reply_markup=SUPPORT_KB, parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            SUPPORT_SCREEN, reply_markup=SUPPORT_KB, parse_mode="HTML"
        )
    await safe_answer(callback)


# ─── Create ticket: step 1 — pick an order ──────────────────────────

@router.callback_query(F.data == "sup:new")
async def ticket_new(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    open_cnt = await db.count_open_tickets(user_id)
    if open_cnt >= MAX_OPEN_TICKETS:
        await callback.message.answer(
            f"{ce('warning')} У вас уже <b>{open_cnt}</b> открытых обращений.\n\n"
            "Дождитесь ответа или закройте одно из них в разделе "
            "«Мои обращения».",
            parse_mode="HTML",
        )
        await safe_answer(callback)
        return

    last_ts = await db.get_last_ticket_created(user_id)
    if last_ts:
        try:
            last_dt = datetime.strptime(str(last_ts)[:19], "%Y-%m-%d %H:%M:%S")
            passed = (datetime.utcnow() - last_dt).total_seconds()
            if passed < COOLDOWN_MINUTES * 60:
                wait = int(COOLDOWN_MINUTES * 60 - passed)
                mins = max(1, (wait + 59) // 60)
                await callback.message.answer(
                    f"{ce('clock')} Следующее обращение можно создать через "
                    f"<b>~{mins} мин</b>.",
                    parse_mode="HTML",
                )
                await safe_answer(callback)
                return
        except ValueError:
            pass

    orders = await db.get_user_orders(user_id, limit=10)
    rows = [
        [_btn(
            _clip(f"{o.get('service_name', '')} · {o.get('plan_name', '')}", 34),
            f"sup:pick:{o['order_id']}",
        )]
        for o in orders
    ]
    rows.append([_btn("Без заказа / другой вопрос", "sup:pick:0")])
    rows.append([_btn("⬅️ Назад", "sup:menu")])

    try:
        await callback.message.edit_text(
            f"{ce('ticket')} <b>Новое обращение</b>\n\n"
            "Выберите заказ, с которым возникла проблема:",
            reply_markup=_kb(*rows),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            f"{ce('ticket')} <b>Новое обращение</b>\n\n"
            "Выберите заказ, с которым возникла проблема:",
            reply_markup=_kb(*rows),
            parse_mode="HTML",
        )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("sup:pick:"))
async def ticket_pick_order(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id

    if raw == "0":
        order_id, topic = None, "Другое"
    else:
        try:
            order_id = int(raw)
        except ValueError:
            await safe_answer(callback, "Некорректный заказ")
            return
        order = await db.get_order(order_id)
        if not order or order.get("user_id") != user_id:
            await safe_answer(callback, "Заказ не найден")
            return
        topic = _clip(
            f"{order.get('service_name', '')} — {order.get('plan_name', '')}", 60
        )

    await state.set_state(TicketFlow.waiting_for_text)
    await state.update_data(order_id=order_id, topic=topic)

    order_name = topic if order_id else "без привязки к заказу"
    await callback.message.answer(
        f"{ce('pencil')} <b>Опишите проблему</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Обращение: <b>{safe_html(order_name)}</b>\n\n"
        "Напишите, что случилось — можно приложить скриншот "
        "(фото с подписью или отдельным сообщением).\n\n"
        "<i>«Отмена» — отменить создание.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


# ─── Create ticket: step 2 — message text / screenshot ─────────────

@router.message(TicketFlow.waiting_for_text, F.text == "Отмена")
async def ticket_create_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_menu(message.from_user.id))


@router.message(TicketFlow.waiting_for_text, F.photo)
async def ticket_create_photo(message: Message, state: FSMContext):
    await _finish_ticket_creation(message, state, msg_type="photo",
                                  text=message.caption, file_id=message.photo[-1].file_id)


@router.message(TicketFlow.waiting_for_text, F.text, ~F.text.in_(_ESCAPES), ~F.text.startswith("/"))
async def ticket_create_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 5:
        short_msg = await message.answer(
            f"{ce('warning')} Опишите проблему чуть подробнее "
            "(минимум 5 символов) — или нажмите «Отмена»."
        )
        # Группа E: слишком короткий ввод + подсказка уйдут сами
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, short_msg.message_id], eph.ERROR_DELAY)
        return
    await _finish_ticket_creation(message, state, msg_type="text",
                                  text=text, file_id=None)


async def _finish_ticket_creation(
    message: Message, state: FSMContext, msg_type: str, text: str | None, file_id: str | None
):
    # v17: повторная проверка лимита ПЕРЕД созданием. Проверка была только
    # на «Новое обращение» (sup:new): два параллельных флоу (или флоу,
    # оставленный открытым) позволяли превысить лимит открытых тикетов.
    open_cnt = await db.count_open_tickets(message.from_user.id)
    if open_cnt >= MAX_OPEN_TICKETS:
        await state.clear()
        await message.answer(
            f"{ce('warning')} У вас уже <b>{open_cnt}</b> открытых обращений — "
            "создание отменено. Дождитесь ответа или закройте одно из них.",
            reply_markup=get_menu(message.from_user.id),
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    topic = data.get("topic", "Обращение")
    user = message.from_user
    body = _clip(text, TEXT_LIMIT)

    ticket_id = await db.create_ticket(
        user_id=user.id, topic=topic, order_id=order_id,
        first_text=body, msg_type=msg_type, file_id=file_id,
    )

    # Admin card
    order = await db.get_order(order_id) if order_id else None
    uname = f"@{safe_html(user.username)}" if user.username else "без username"
    card = (
        f"{ce('ticket')} <b>Новое обращение #{ticket_id}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('profile')} {safe_html(user.first_name or '')} ({uname})\n"
        f"   ID: <code>{user.id}</code>\n"
        f"{ce('clock')} {_ago(_now_iso())}\n\n"
    )
    if order:
        card += f"{_order_line(order)}\n\n"
    else:
        card += f"{ce('package')} Без привязки к заказу\n\n"
    if msg_type == "photo":
        card += f"{ce('camera')} <i>Скриншот приложен</i>\n"
    if body:
        card += f"{ce('speech')} {safe_html(_clip(body, 1200))}"

    await _notify_admins(message.bot, card, ticket_id)

    # Send the screenshot itself so admins see it immediately
    if msg_type == "photo" and file_id:
        for admin_id in ADMIN_IDS:
            try:
                await message.bot.send_photo(admin_id, photo=file_id)
            except Exception as e:
                logger.warning(f"Ticket photo to admin {admin_id} failed: {e}")

    await state.clear()
    done = await message.answer(
        f"{ce('check')} <b>Обращение #{ticket_id} создано!</b>\n\n"
        "Ответ придёт сообщением от этого бота. История — в разделе "
        "«Поддержка → Мои обращения».",
        reply_markup=get_menu(user.id),
        parse_mode="HTML",
    )
    # Task 35, группа D: текст описания уже сохранён в обращении —
    # сырая копия в чате растворяется (фото-скриншоты остаются как доказательства)
    if msg_type == "text":
        eph.dissolve_ids(message.bot, message.chat.id, [message.message_id], eph.PAIR_DELAY)


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ─── «Мои обращения»: list ──────────────────────────────────────────

@router.callback_query(F.data == "sup:my")
async def ticket_my_list(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    tickets = await db.get_user_tickets(callback.from_user.id, limit=10)

    if not tickets:
        text = (
            f"{ce('copy')}   <b>Мои обращения</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Обращений пока нет.\n"
            "Если что-то пошло не так — создайте обращение, поможем."
        )
        kb = _kb([_btn("➕ Создать обращение", "sup:new")], [_btn("⬅️ Назад", "sup:menu")])
    else:
        text = (
            f"{ce('copy')}   <b>Мои обращения</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Нажмите на обращение, чтобы открыть переписку."
        )
        rows = []
        for t in tickets:
            icon, _ = STATUS_LABEL.get(t["status"], ("⚪️", t["status"]))
            rows.append([_btn(
                f"#{t['ticket_id']} {icon} {_clip(t['topic'], 24)} · {_ago(t['updated_at'])}",
                f"sup:detail:{t['ticket_id']}",
            )])
        rows.append([_btn("➕ Создать обращение", "sup:new")])
        rows.append([_btn("⬅️ Назад", "sup:menu")])
        kb = _kb(*rows)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await safe_answer(callback)


# ─── Thread view (owner or admin) ───────────────────────────────────

def _render_thread(ticket: dict, msgs: list[dict], viewer_is_admin: bool) -> str:
    """Build thread view text, trimming oldest messages to fit 4096 chars."""
    order_block = ""
    if ticket.get("order_id"):
        order_block = f"\n{ce('package')} Заказ #{ticket['order_id']}"
    head = (
        f"{ce('ticket')} <b>Обращение #{ticket['ticket_id']}</b> · {_status_str(ticket['status'])}\n"
        f"{safe_html(_clip(ticket['topic'], 60))}\n"
        f"{ce('clock')} Создано {_ago(ticket['created_at'])}"
        f"{order_block}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )

    lines = []
    for m in reversed(msgs):  # newest first for trimming
        if m["sender"] == "admin":
            who = f"🛠 <b>Поддержка</b>"
        else:
            who = f"👤 <b>Вы</b>"
        body = ""
        if m.get("msg_type") == "photo":
            body += f"{ce('camera')} <i>Скриншот</i>\n"
        if m.get("text"):
            body += safe_html(_clip(m["text"], 400))
        line = f"\n{who} · {_short_time(m.get('created_at'))}\n{body}"
        lines.append(line)

    # Trim to fit one message
    budget = 3900 - len(head)
    shown = []
    used = 0
    for line in lines:
        if used + len(line) > budget:
            break
        shown.append(line)
        used += len(line)
    shown.reverse()

    text = head + "".join(shown)
    if len(shown) < len(lines):
        text = head + f"\n<i>… показаны последние {len(shown)} сообщ.</i>" + "".join(shown)
    return text


def _thread_kb(ticket: dict, viewer_id: int) -> InlineKeyboardMarkup:
    rows = []
    if is_admin(viewer_id):
        # Admin view: actions inline + back to the browser (was: user screen
        # with a single useless «В меню поддержки» button).
        if ticket["status"] != "closed":
            rows.append([
                _btn("✍️ Ответить", f"sup:areply:{ticket['ticket_id']}"),
                _btn("✅ Закрыть", f"sup:aclose:{ticket['ticket_id']}"),
            ])
        else:
            # Закрытый тред: ответ всё ещё возможен (переведёт в answered),
            # а «Закрыть» уже не имеет смысла.
            rows.append([_btn("✍️ Ответить", f"sup:areply:{ticket['ticket_id']}")])
        rows.append([_btn("⬅️ Ко всем обращениям", "sup:alf:all")])
        return _kb(*rows)
    if ticket["user_id"] == viewer_id:
        if ticket["status"] != "closed":
            rows.append([_btn("✍️ Уточнить", f"sup:fu:{ticket['ticket_id']}")])
            rows.append([_btn("🔒 Закрыть обращение", f"sup:close:{ticket['ticket_id']}")])
        else:
            rows.append([_btn("➕ Новое обращение", "sup:new")])
        rows.append([_btn("⬅️ К списку", "sup:my")])
    rows.append([_btn("⬅️ В меню поддержки", "sup:menu")])
    return _kb(*rows)


async def _send_thread(target_message, ticket_id: int, viewer_id: int, edit: bool):
    ticket = await db.get_ticket(ticket_id)
    if not ticket:
        await target_message.answer("Обращение не найдено.")
        return
    # Access control: owner or admin only
    if ticket["user_id"] != viewer_id and not is_admin(viewer_id):
        await target_message.answer("Нет доступа к этому обращению.")
        return

    msgs = await db.get_ticket_messages(ticket_id, limit=HISTORY_LIMIT)
    text = _render_thread(ticket, msgs, is_admin(viewer_id))
    kb = _thread_kb(ticket, viewer_id)

    if edit:
        try:
            await target_message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await target_message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("sup:detail:"))
async def ticket_detail(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        ticket_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await safe_answer(callback, "Некорректное обращение")
        return
    await _send_thread(callback.message, ticket_id, callback.from_user.id, edit=True)
    await safe_answer(callback)


# ─── Follow-up (user adds a message) ────────────────────────────────

@router.callback_query(F.data.startswith("sup:fu:"))
async def ticket_followup_start(callback: CallbackQuery, state: FSMContext):
    try:
        ticket_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await safe_answer(callback, "Некорректное обращение")
        return
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Нет доступа")
        return

    await state.set_state(TicketFlow.waiting_for_followup)
    await state.update_data(ticket_id=ticket_id)
    await callback.message.answer(
        f"{ce('pencil')} <b>Уточнение к обращению #{ticket_id}</b>\n\n"
        "Напишите дополнение или пришлите скриншот. "
        "Обращение снова станет «открытым».\n\n"
        "<i>«Отмена» — вернуться.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(TicketFlow.waiting_for_followup, F.text == "Отмена")
async def ticket_followup_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_menu(message.from_user.id))


@router.message(TicketFlow.waiting_for_followup, F.photo)
async def ticket_followup_photo(message: Message, state: FSMContext):
    await _finish_followup(message, state, msg_type="photo",
                           text=message.caption, file_id=message.photo[-1].file_id)


@router.message(TicketFlow.waiting_for_followup, F.text, ~F.text.in_(_ESCAPES), ~F.text.startswith("/"))
async def ticket_followup_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 2:
        short_msg = await message.answer(f"{ce('warning')} Напишите уточнение текстом.")
        # Группа E: короткий ввод + подсказка уйдут сами
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, short_msg.message_id], eph.ERROR_DELAY)
        return
    await _finish_followup(message, state, msg_type="text", text=text, file_id=None)


async def _finish_followup(
    message: Message, state: FSMContext, msg_type: str, text: str | None, file_id: str | None
):
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    await state.clear()
    if not ticket_id:
        await message.answer("Обращение не найдено.", reply_markup=get_menu(message.from_user.id))
        return

    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != message.from_user.id:
        await message.answer("Нет доступа к обращению.", reply_markup=get_menu(message.from_user.id))
        return

    body = _clip(text, TEXT_LIMIT)
    await db.add_ticket_message(
        ticket_id, sender="user", sender_id=message.from_user.id,
        text=body, msg_type=msg_type, file_id=file_id,
    )
    # Task 35, группа D: текст уточнения сохранён в обращении —
    # сырая копия в чате растворяется (фото остаются)
    if msg_type == "text":
        eph.dissolve_ids(message.bot, message.chat.id, [message.message_id], eph.PAIR_DELAY)
    # Reopen if it was answered or closed
    if ticket["status"] != "open":
        await db.set_ticket_status(ticket_id, "open")

    uname = f"@{safe_html(message.from_user.username)}" if message.from_user.username else "без username"
    card = (
        f"{ce('bell')} <b>Уточнение по обращению #{ticket_id}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('profile')} {safe_html(message.from_user.first_name or '')} ({uname})\n"
        f"   ID: <code>{message.from_user.id}</code>\n\n"
    )
    if msg_type == "photo":
        card += f"{ce('camera')} <i>Скриншот приложен</i>\n"
    if body:
        card += f"{ce('speech')} {safe_html(_clip(body, 1200))}"
    await _notify_admins(message.bot, card, ticket_id)

    if msg_type == "photo" and file_id:
        for admin_id in ADMIN_IDS:
            try:
                await message.bot.send_photo(admin_id, photo=file_id)
            except Exception as e:
                logger.warning(f"Followup photo to admin {admin_id} failed: {e}")

    done = await message.answer(
        f"{ce('check')} Уточнение добавлено к обращению #{ticket_id}.",
        reply_markup=get_menu(message.from_user.id),
    )


# ─── User closes the ticket ─────────────────────────────────────────

@router.callback_query(F.data.startswith("sup:close:"))
async def ticket_close_user(callback: CallbackQuery, state: FSMContext):
    try:
        ticket_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await safe_answer(callback, "Некорректное обращение")
        return
    ticket = await db.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Нет доступа")
        return
    if ticket["status"] == "closed":
        await safe_answer(callback, "Уже закрыто")
        return

    await db.set_ticket_status(ticket_id, "closed")
    await safe_answer(callback, "Обращение закрыто")
    await _send_thread(callback.message, ticket_id, callback.from_user.id, edit=True)


# ─── Admin: «Обращения» browser ─────────────────────────────────────

def _admin_list_kb(
    flt: str,
    page: int,
    total_pages: int,
    ticket_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Filter tabs (2×2, active marked ✓) + ticket rows + pagination row.

    ticket_rows — по одной кнопке-«пилюле» на обращение списка (открывает
    тред). Без них список был бы мёртвым текстом: владелец тапал по строке
    и ничего не происходило (баг-репорт перед релизом v16).
    """
    rows = [
        [_btn(f"{lbl} ✓" if key == flt else lbl, f"sup:alf:{key}")
         for key, lbl in ADMIN_FILTERS[:2]],
        [_btn(f"{lbl} ✓" if key == flt else lbl, f"sup:alf:{key}")
         for key, lbl in ADMIN_FILTERS[2:]],
    ]
    rows.extend(ticket_rows or [])
    pag = pagination_row(f"sup:alp:{flt}:", page, total_pages)
    if pag:  # пустой ряд в инлайн-клавиатуре недопустим (Telegram Bad Request)
        rows.append(pag)
    return _kb(*rows)


async def _send_admin_list(message: Message, flt: str, page: int) -> None:
    """Render the admin tickets browser for (filter, page); edit-in-place
    when possible (callbacks), otherwise send a new message («Обращения»)."""
    status = None if flt == "all" else flt
    total = await db.count_admin_tickets(status)
    total_pages = max(1, (total + ADMIN_LIST_PER_PAGE - 1) // ADMIN_LIST_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    tickets = await db.get_admin_tickets(
        status, offset=page * ADMIN_LIST_PER_PAGE, limit=ADMIN_LIST_PER_PAGE
    )

    text = (
        f"{ce('ticket')}   <b>Обращения</b> · {_filter_label(flt)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )
    ticket_rows: list[list[InlineKeyboardButton]] = []
    if not tickets:
        text += "\nПо этому фильтру пусто."
    else:
        text += f"\nВсего: <b>{total}</b> — нажмите кнопку обращения, чтобы открыть тред:\n"
        for t in tickets:
            icon, _ = STATUS_LABEL.get(t["status"], ("⚪️", t["status"]))
            uname = (
                f"@{safe_html(t['username'])}" if t.get("username")
                else f"id {t['user_id']}"
            )
            text += (
                f"\n#{t['ticket_id']} {icon} {_clip(t['topic'], 26)} · "
                f"{_ago(t['updated_at'])} · {uname}"
            )
            # Кнопка-строка: тап открывает тред (sup:detail: — админу пускает
            # _send_thread, юзеру — только своё). Лимит текста кнопки 64:
            # «#» + id (≤7) + icon + topic≤40 — безопасно.
            label = f"#{t['ticket_id']} {icon} {_clip(t['topic'], 40)}"
            ticket_rows.append([_btn(label, f"sup:detail:{t['ticket_id']}")])

    kb = _admin_list_kb(flt, page, total_pages, ticket_rows=ticket_rows)
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(StateFilter("*"), F.text == "Обращения",
                F.from_user.id.in_(ADMIN_IDS))
async def admin_tickets_open(message: Message, state: FSMContext):
    """«Обращения» in the admin keyboard → browser on the open filter."""
    await state.clear()
    await _send_admin_list(message, "open", 0)


@router.callback_query(F.data.startswith("sup:alf:"))
async def admin_tickets_filter(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Только для админов")
        return
    flt = callback.data.split(":")[2]
    if flt not in {key for key, _ in ADMIN_FILTERS}:
        await safe_answer(callback, "Неизвестный фильтр")
        return
    await state.clear()
    await _send_admin_list(callback.message, flt, 0)
    await safe_answer(callback)


@router.callback_query(F.data.startswith("sup:alp:"))
async def admin_tickets_page(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Только для админов")
        return
    try:
        _, _, flt, page_s = callback.data.split(":")
        page = int(page_s)
    except (ValueError, IndexError):
        await safe_answer(callback, "Некорректная страница")
        return
    if flt not in {key for key, _ in ADMIN_FILTERS}:
        await safe_answer(callback, "Неизвестный фильтр")
        return
    await state.clear()
    await _send_admin_list(callback.message, flt, page)
    await safe_answer(callback)


# ─── Admin: reply / close ───────────────────────────────────────────

@router.callback_query(F.data.startswith("sup:areply:"))
async def ticket_admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Только для админов")
        return
    try:
        ticket_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await safe_answer(callback, "Некорректное обращение")
        return
    ticket = await db.get_ticket(ticket_id)
    if not ticket:
        await safe_answer(callback, "Обращение не найдено")
        return

    await state.set_state(AdminTicketReply.waiting_for_reply)
    await state.update_data(ticket_id=ticket_id)
    await callback.message.answer(
        f"✍️ <b>Ответ на обращение #{ticket_id}</b>\n\n"
        "Напишите ответ — он уйдёт пользователю с пометкой "
        "«Ответ поддержки». Можно приложить скриншот.\n\n"
        "<i>«Отмена» — не отвечать.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(AdminTicketReply.waiting_for_reply, F.text == "Отмена")
async def ticket_admin_reply_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_menu(message.from_user.id))


@router.message(AdminTicketReply.waiting_for_reply, F.photo)
async def ticket_admin_reply_photo(message: Message, state: FSMContext):
    await _finish_admin_reply(message, state, msg_type="photo",
                              text=message.caption, file_id=message.photo[-1].file_id)


@router.message(AdminTicketReply.waiting_for_reply, F.text, ~F.text.in_(_ESCAPES), ~F.text.startswith("/"))
async def ticket_admin_reply_text(message: Message, state: FSMContext):
    await _finish_admin_reply(message, state, msg_type="text",
                              text=(message.text or "").strip(), file_id=None)


async def _finish_admin_reply(
    message: Message, state: FSMContext, msg_type: str, text: str | None, file_id: str | None
):
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    await state.clear()
    if not ticket_id:
        await message.answer("Тикет не найден.", reply_markup=get_menu(message.from_user.id))
        return

    ticket = await db.get_ticket(ticket_id)
    if not ticket:
        await message.answer("Тикет не найден.", reply_markup=get_menu(message.from_user.id))
        return

    body = _clip(text, TEXT_LIMIT)
    await db.add_ticket_message(
        ticket_id, sender="admin", sender_id=message.from_user.id,
        text=body, msg_type=msg_type, file_id=file_id,
    )
    await db.set_ticket_status(ticket_id, "answered")

    # Deliver to user
    header = (
        f"🛠 <b>Ответ поддержки</b> · обращение #{ticket_id}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    kb = _kb([_btn("📋 Открыть обращение", f"sup:detail:{ticket_id}")])
    delivered = True
    try:
        if msg_type == "photo" and file_id:
            if body and len(safe_html(body)) <= 900:
                await message.bot.send_photo(
                    ticket["user_id"], photo=file_id,
                    caption=_clip(header + safe_html(body), 1024), parse_mode="HTML",
                )
            else:
                await message.bot.send_photo(ticket["user_id"], photo=file_id)
                if body:
                    await message.bot.send_message(
                        ticket["user_id"], header + safe_html(body),
                        reply_markup=kb, parse_mode="HTML",
                    )
        elif body:
            await message.bot.send_message(
                ticket["user_id"], header + safe_html(body),
                reply_markup=kb, parse_mode="HTML",
            )
        else:
            delivered = False
    except Exception as e:
        delivered = False
        logger.warning(f"Ticket reply delivery to user {ticket['user_id']} failed: {e}")

    if delivered:
        done = await message.answer(
            f"{ce('check')} Ответ отправлен пользователю "
            f"(обращение #{ticket_id}).",
        )
    else:
        done = await message.answer(
            f"{ce('warning')} Не удалось доставить ответ пользователю "
            f"{ticket['user_id']} — возможно, бот заблокирован. "
            f"Ответ сохранён в треде.",
        )


@router.callback_query(F.data.startswith("sup:aclose:"))
async def ticket_admin_close(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Только для админов")
        return
    try:
        ticket_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await safe_answer(callback, "Некорректное обращение")
        return
    ticket = await db.get_ticket(ticket_id)
    if not ticket:
        await safe_answer(callback, "Обращение не найдено")
        return
    if ticket["status"] == "closed":
        await safe_answer(callback, "Уже закрыто")
        return

    await db.set_ticket_status(ticket_id, "closed")
    await safe_answer(callback, f"Обращение #{ticket_id} закрыто")
    try:
        await callback.message.bot.send_message(
            ticket["user_id"],
            f"{ce('lock')} Ваше обращение #{ticket_id} закрыто.\n"
            "Если проблема осталась — создайте новое в разделе «Поддержка».",
        )
    except Exception as e:
        logger.warning(f"Close notice to user {ticket['user_id']} failed: {e}")


# ─── Admin commands: /tickets, /ticket <id> ─────────────────────────

@router.message(Command("tickets"))
async def cmd_tickets(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    tickets = await db.get_open_tickets(limit=15)
    if not tickets:
        await message.answer(
            f"{ce('check')} Открытых обращений нет — всё спокойно."
        )
        return

    lines = [f"{ce('ticket')} <b>Открытые обращения</b> ({len(tickets)}):\n"]
    for t in tickets:
        icon, _ = STATUS_LABEL.get(t["status"], ("⚪️", t["status"]))
        uname = f"@{t['username']}" if t.get("username") else f"id {t['user_id']}"
        lines.append(
            f"#{t['ticket_id']} {icon} · {_ago(t['updated_at'])} · "
            f"{safe_html(uname)} · {_clip(t['topic'], 30)}"
        )
    lines.append(
        "\nОтветить — кнопки под уведомлением, <code>/ticket &lt;номер&gt;</code> "
        "или раздел «Обращения» в меню."
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("ticket"))
async def cmd_ticket_open(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/ticket 12</code>", parse_mode="HTML")
        return
    await _send_thread(message, int(parts[1]), message.from_user.id, edit=False)

