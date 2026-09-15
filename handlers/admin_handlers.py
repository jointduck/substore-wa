import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.dispatcher.event.bases import SkipHandler

from models.database import (
    db, load_catalog, save_catalog, async_save_catalog,
    get_service_by_id, get_plan_from_service,
)
from keyboards.keyboards import (
    admin_menu_kb,
    admin_pending_orders_kb,
    admin_order_actions_kb,
    admin_promo_detail_kb,
    admin_promo_delask_kb,
    edit_catalog_kb,
    edit_service_kb,
    edit_plans_kb,
    edit_plan_detail_kb,
    back_kb,
    cancel_kb,
    pbtn,
)
from config import ADMIN_IDS, CURRENCY_SYMBOL, BROADCAST_DELAY
from handlers.user_handlers import MENU_BUTTONS
from emojis import ce
from utils.html_utils import safe_html, safe_code
from utils.safe_callback import safe_answer
from utils.order_status import effective_status, subscription_note
from services.ton_payments import get_ton_usd_rate, get_usd_rub_rate, usdt_to_rub
from services.marketing import (
    generate_referral_code, get_loyalty_balance, get_bot_username,
    validate_promo_code,
)

logger = logging.getLogger(__name__)


# ─── v17: защита FSM-вводов от не-текста ─────────────────────────

async def _require_text(message: Message) -> str | None:
    """Вернуть message.text либо вежливо отказать на фото/файл/стикер.

    Раньше не-текстовое сообщение в ЛЮБОМ админском FSM-вводе роняло
    хендлер с AttributeError (message.text = None → .strip()): state
    оставался, админ не получал НИКАКОГО фидбека — «бот завис». Теперь
    подсказка; состояние сохранено — можно прислать текст или «Отмена».
    """
    if message.text:
        return message.text
    await message.answer(
        f"{ce('warning')} Пришлите обычным <b>текстовым</b> сообщением — "
        f"фото, файлы и стикеры здесь не подходят.\n"
        f"<i>«Отмена» — выйти без сохранения.</i>",
        parse_mode="HTML",
    )
    return None

# ─── Router-level admin guard ─────────────────────────────────────
# All handlers in this router are automatically restricted to admins.
# No need for manual if not is_admin() checks in each handler.
# ADMIN_IDS is loaded from config at import time.

def _admin_filter_factory():
    """Build admin filter — returns a combined Message + CallbackQuery filter.
    
    Since ADMIN_IDS may be empty at import time (not yet loaded from .env),
    we use a custom filter class that checks at runtime.
    """
    from aiogram.filters import Filter
    
    class AdminFilter(Filter):
        async def __call__(self, event):
            user = getattr(event, "from_user", None)
            if user is None:
                return False
            return user.id in ADMIN_IDS
    
    return AdminFilter()

router = Router()
router.message.filter(_admin_filter_factory())
router.callback_query.filter(_admin_filter_factory())


# ─── Debug Command ──────────────────────────────────────────────────

@router.message(Command("debug"))
async def cmd_debug(message: Message, state: FSMContext):
    """Debug: show current FSM state and data for diagnosing issues."""
    current_state = await state.get_state()
    data = await state.get_data()
    text = (
        f"{ce('tool')} <b>Debug</b>\n\n"
        f"State: <code>{current_state}</code>\n"
        f"Data: <code>{json.dumps(data, ensure_ascii=False, default=str)[:500]}</code>"
    )
    await message.answer(text, parse_mode="HTML")


# ─── FSM Menu Escape (v16) ───────────────────────────────────────

@router.message(StateFilter("*"), F.text.in_(MENU_BUTTONS - {"Отмена"}))
async def admin_fsm_menu_escape(message: Message, state: FSMContext):
    """Меню-кнопка во время админ-FSM: гасим ввод и отдаём управление.

    Раньше «Каталог»/«Заказы»/«Помощь» в состоянии FSM перехватывал
    catch-all ввода (например имя тарифа сохранялось как «Каталог»),
    а FSM «залипал». Вне FSM — молча пропускаем дальше (SkipHandler),
    ничего не меняя.
    """
    if await state.get_state() is None:
        raise SkipHandler
    await state.clear()
    await message.answer(
        f"{ce('cross')} Ввод прерван.",
        reply_markup=admin_menu_kb(),
    )
    raise SkipHandler  # текст обработают штатные меню-хендлеры


# ─── Admin Guard ───────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─── FSM States ────────────────────────────────────────────────────

class AdminMessage(StatesGroup):
    entering_message = State()
    selecting_order = State()


class EditService(StatesGroup):
    entering_svc_name = State()
    entering_svc_desc = State()
    # NB: состояние ввода цены сервиса удалено (аудит №9, Task 32):
    # никогда не устанавливалось — цена правится через EditPlan.


class AddService(StatesGroup):
    entering_new_svc_name = State()
    entering_new_svc_emoji = State()
    entering_new_svc_desc = State()


class AddPlan(StatesGroup):
    entering_plan_name = State()
    entering_plan_duration = State()
    entering_plan_price = State()


class EditPlan(StatesGroup):
    entering_edit_plan_name = State()
    entering_edit_plan_duration = State()
    entering_edit_plan_price = State()


class Broadcast(StatesGroup):
    entering_message = State()
    confirming = State()  # v17: предпросмотр → явное «Начать рассылку»


class CreatePromo(StatesGroup):
    entering_code = State()
    entering_discount = State()
    entering_max_uses = State()
    entering_description = State()


class EditPromo(StatesGroup):
    """Редактирование параметров промокода (UX №5): скидка/лимит/срок."""
    entering_discount = State()
    entering_max_uses = State()
    entering_expires = State()


STATUS_NAMES = {
    "pending_payment": f"{ce('credit_card')} Ожидает оплату",
    "pending_account": f"{ce('clock')} Ожидает данные аккаунта",
    "pending_activation": f"{ce('clock')} Ожидает активации",
    "active": f"{ce('check')} Подписка активна",
    "cancelled": f"{ce('cross')} Отменён",
    "failed": f"{ce('warning')} Ошибка активации",
    # Виртуальные статусы — вычисляются на момент показа (utils.order_status),
    # статус в БД не меняется (на него завязана drip-рассылка)
    "expired": f"{ce('clock')} Подписка истекла",
    "payment_over": f"{ce('clock')} Время оплаты истекло",
}


# ─── Pending Orders ────────────────────────────────────────────────

# UX №6: заказов на странице «Новых заказов» (10 кнопок — лимит Telegram
# по строкам инлайн-клавиатуры 100, но длинные названия в кнопках громоздки)
PENDING_PER_PAGE = 10


async def _collect_pending_orders() -> list[dict]:
    """Все заказы, ожидающие обработки: ждущие данные аккаунта, затем
    ждущие активации. Раньше выборка была limit=20+20, а показывались
    вообще только первые 10 — заказы №11+ админ не видел никогда."""
    orders = await db.get_orders_by_status("pending_activation", limit=100)
    orders_account = await db.get_orders_by_status("pending_account", limit=100)
    return orders_account + orders


def _pending_page_text(all_pending: list[dict], page: int, per_page: int) -> str:
    """Текст одной страницы «Новых заказов» (0-based страница)."""
    text = f"{ce('bell')} <b>Заказы, ожидающие обработки:</b>\n\n"
    for o in all_pending[page * per_page:(page + 1) * per_page]:
        eff = effective_status(o)
        status = STATUS_NAMES.get(eff, o["status"])
        method = o.get("payment_method", "—")
        method_icon = {"ton": ce('diamond'), "usdt": ce('dollar'), "digiseller": ce('credit_card'), "tribute": ce('credit_card'), "stars": ce('star')}.get(method, ce('package'))
        price_usdt = o.get("price_usdt", 0)
        text += f"{ce('package')} #{o['order_id']} | {ce('shopping')} {o['service_name']} {o['plan_name']} | {price_usdt:.2f} USDT {method_icon} | {status}\n"
    return text


@router.message(StateFilter("*"), F.text.contains("Новые заказы"))
@router.message(Command("orders_admin"))
async def cmd_admin_orders(message: Message, state: FSMContext):
    await state.clear()

    all_pending = await _collect_pending_orders()

    if not all_pending:
        no_orders_msg = await message.answer(
            f"{ce('bell')} Нет заказов, ожидающих обработки.",
            reply_markup=admin_menu_kb(),
        )
        return

    # UX №6: страницы по PENDING_PER_PAGE, листание кнопками «◀/▶»
    total_pages = max(1, math.ceil(len(all_pending) / PENDING_PER_PAGE))
    text = _pending_page_text(all_pending, 0, PENDING_PER_PAGE)
    await message.answer(
        text,
        reply_markup=admin_pending_orders_kb(
            all_pending[:PENDING_PER_PAGE], page=0, total_pages=total_pages
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("apend_"))
async def admin_pending_switch_page(callback: CallbackQuery, state: FSMContext):
    """UX №6: листание «Новых заказов» — экран РЕДАКТИРУЕТСЯ на месте,
    новые сообщения не плодятся (политика чата)."""
    await state.clear()
    try:
        page = max(0, int(callback.data[len("apend_"):]))
    except ValueError:
        await safe_answer(callback)
        return

    all_pending = await _collect_pending_orders()
    if not all_pending:
        try:
            await callback.message.edit_text(f"{ce('bell')} Нет заказов, ожидающих обработки.")
        except Exception:
            pass
        await safe_answer(callback)
        return

    total_pages = max(1, math.ceil(len(all_pending) / PENDING_PER_PAGE))
    page = min(page, total_pages - 1)
    text = _pending_page_text(all_pending, page, PENDING_PER_PAGE)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=admin_pending_orders_kb(
                all_pending[page * PENDING_PER_PAGE:(page + 1) * PENDING_PER_PAGE],
                page=page,
                total_pages=total_pages,
            ),
            parse_mode="HTML",
        )
    except Exception:
        # «message is not modified» и т.п. — уже на нужной странице
        pass
    await safe_answer(callback)


@router.callback_query(F.data.startswith("aorder_"))
async def admin_view_order(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # v16: навигация гасит FSM-ввод

    order_id = int(callback.data.replace("aorder_", ""))
    order = await db.get_order(order_id)
    if not order:
        await safe_answer(callback, "❌ Заказ не найден")
        return

    eff = effective_status(order)
    status_name = STATUS_NAMES.get(eff, order["status"])
    sub_note = subscription_note(order)
    sub_line = f"{ce('calendar')} Срок: {sub_note}\n" if sub_note else ""
    account_data = {}
    if order["account_data"]:
        try:
            account_data = json.loads(order["account_data"])
        except (json.JSONDecodeError, TypeError):
            account_data = {"raw": order["account_data"]}

    method = order.get("payment_method", "—")
    method_name = {"ton": "Gram", "usdt": "USDT", "digiseller": "Карта (Digiseller)", "tribute": "Карта (Tribute)", "stars": f"Stars {ce('star')}"}.get(method, method)
    price_usdt = order.get("price_usdt", 0)

    text = (
        f"{ce('package')} <b>Заказ #{order_id}</b>\n\n"
        f"{ce('profile')} Клиент ID: <code>{order['user_id']}</code>\n"
        f"{ce('shopping')} Сервис: <b>{order['service_name']}</b>\n"
        f"{ce('plan_badge')} Тариф: {order['plan_name']} ({order['duration_days']} дн.)\n"
        f"{ce('wallet')} Сумма: <b>{price_usdt:.2f} USDT</b>\n"
        f"{ce('credit_card')} Оплата: {method_name}\n"
        f"{ce('chart')} Статус: {status_name}\n"
        f"{sub_line}"
        f"{ce('calendar')} Дата: {order['created_at'][:16]}\n"
    )

    # Show Gram transaction info if applicable
    if method in ("ton", "usdt") and order.get("ton_tx_hash"):
        text += f"\n{ce('link')} TX: <code>{order['ton_tx_hash']}</code>\n"
    # Show Tribute order UUID for support/diagnostics
    if method == "tribute" and order.get("tribute_order_uuid"):
        text += f"\n{ce('link')} Tribute UUID: <code>{order['tribute_order_uuid']}</code>\n"
    if method == "ton" and order.get("ton_amount"):
        text += f"{ce('diamond')} Gram: {order['ton_amount']:.3f}\n"

    if account_data:
        service = get_service_by_id(order["service_id"])
        if service:
            for field in service.get("account_fields", []):
                val = account_data.get(field["id"], "—")
                text += f"{ce('key')} {safe_html(field['label'])}: <code>{safe_code(str(val))}</code>\n"
        else:
            for k, v in account_data.items():
                if not k.startswith("_"):
                    text += f"{ce('key')} {safe_html(k)}: <code>{safe_code(str(v))}</code>\n"

    order_msg = await callback.message.answer(text, reply_markup=admin_order_actions_kb(order_id), parse_mode="HTML")
    await safe_answer(callback)


# ─── Admin: Mark Order Done / Failed ──────────────────────────────

@router.callback_query(F.data.startswith("a_done_"))
async def admin_mark_done(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # v16: навигация гасит FSM-ввод

    order_id = int(callback.data.replace("a_done_", ""))
    order = await db.get_order(order_id)
    if not order:
        await safe_answer(callback, "❌ Заказ не найден")
        return

    # Validate order is in a valid state for activation
    if order["status"] not in ("pending_activation", "pending_account"):
        await safe_answer(callback, f"❌ Нельзя активировать заказ в статусе {order['status']}")
        return

    # v17: заказ ПЛАТНЫЙ, но данные аккаунта ещё не введены. Раньше
    # активация проходила: статус active, промпт ввода больше не приходил,
    # юзерский fallback искал только pending_account — оплаченный заказ
    # навсегда оставался без реквизитов. Блокируем: в карточке видно «—»
    # по полям, админ ждёт ввода или связывается с клиентом.
    if order["status"] == "pending_account" and not order.get("account_data"):
        _svc = get_service_by_id(order.get("service_id", ""))
        if _svc and _svc.get("account_fields"):
            await safe_answer(
                callback,
                "❌ У заказа ещё НЕТ данных аккаунта (юзер не успел ввести).\n"
                "Активация без них заблокирована — дождитесь ввода данных "
                "или напишите клиенту через «Сообщение».",
                show_alert=True,
            )
            return

    # Use CAS to prevent race condition with other admin actions
    transitioned = await db.try_transition_order_status(
        order_id,
        from_status=order["status"],
        to_status="active",
        admin_id=callback.from_user.id,
        activated_at=datetime.utcnow().isoformat(),
    )
    if not transitioned:
        await safe_answer(callback, "❌ Статус заказа уже изменён другим действием")
        return

    # Notify user
    try:
        await callback.bot.send_message(
            order["user_id"],
            f"{ce('check')} <b>Подписка активирована!</b>\n\n"
            f"{ce('package')} Заказ #{order_id}\n"
            f"{ce('shopping')} {order['service_name']} — {order['plan_name']}\n\n"
            f"Ваша подписка успешно активирована на аккаунте. "
            f"Рекомендуем сменить пароль для безопасности.\n\n"
            f"Спасибо за покупку! {ce('heart')}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {order['user_id']}: {e}")

    done_msg = await callback.message.edit_text(
        f"{ce('check')} Заказ #{order_id} отмечен как <b>активированный</b>. Клиент уведомлён.",
        parse_mode="HTML",
    )
    await safe_answer(callback)
    logger.info(f"Admin {callback.from_user.id} activated order #{order_id}")


@router.callback_query(F.data.startswith("a_fail_"))
async def admin_mark_failed(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    order_id = int(callback.data.replace("a_fail_", ""))
    order = await db.get_order(order_id)
    if not order:
        await safe_answer(callback, "❌ Заказ не найден")
        return

    await state.set_state(AdminMessage.entering_message)
    await state.update_data(order_id=order_id, action="fail")

    await callback.message.answer(
        f"{ce('warning')} Введите причину ошибки (будет отправлена клиенту):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(AdminMessage.entering_message, F.text.contains("Отмена"))
async def admin_msg_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(AdminMessage.entering_message)
async def admin_msg_process(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    action = data.get("action")

    # v17: не-текст (фото/стикер) в вводе причины/сообщения — раньше фото
    # становилось «пустым» сообщением клиенту, а admin_comment=None
    if (txt := await _require_text(message)) is None:
        return
    if txt.strip().lower() == "отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_menu_kb())
        return

    await state.clear()

    if not order_id:
        err_msg = await message.answer("Ошибка.", reply_markup=admin_menu_kb())
        return

    if action == "fail":
        # v17: статус-guard + CAS. Раньше «Не активировано» писался БЕЗ
        # проверки статуса: админ мог запороть active-заказ, а его
        # подтверждение поллером могло быть перезаписано на failed.
        order = await db.get_order(order_id)
        if not order or order["status"] not in ("pending_activation", "pending_account"):
            await message.answer(
                f"{ce('cross')} Нельзя отметить ошибочным заказ в статусе "
                f"{order['status'] if order else '—'}.",
                reply_markup=admin_menu_kb(),
                parse_mode="HTML",
            )
            return
        transitioned = await db.try_transition_order_status(
            order_id,
            from_status=order["status"],
            to_status="failed",
            admin_id=message.from_user.id,
            admin_comment=txt,
        )
        if not transitioned:
            await message.answer(
                f"{ce('cross')} Статус заказа уже изменён другим действием.",
                reply_markup=admin_menu_kb(),
            )
            return
        order = await db.get_order(order_id)
        if order:
            try:
                await message.bot.send_message(
                    order["user_id"],
                    f"{ce('warning')} <b>Ошибка активации</b>\n\n"
                    f"{ce('package')} Заказ #{order_id}\n"
                    f"{ce('shopping')} {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
                    f"Причина: {safe_html(txt)}\n\n"
                    f"Свяжитесь с поддержкой для решения вопроса.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Failed to notify user: {e}")

        done_msg = await message.answer(
            f"{ce('warning')} Заказ #{order_id} отмечен как ошибочный. Клиент уведомлён.",
            reply_markup=admin_menu_kb(),
            parse_mode="HTML",
        )

    elif action == "msg":
        order = await db.get_order(order_id)
        if order:
            try:
                await message.bot.send_message(
                    order["user_id"],
                    f"{ce('speech')} <b>Сообщение по заказу #{order_id}</b>\n\n{safe_html(txt)}",
                    parse_mode="HTML",
                )
                sent_msg = await message.answer(
                    f"{ce('check')} Сообщение отправлено клиенту.",
                    reply_markup=admin_menu_kb(),
                    parse_mode="HTML",
                )
            except Exception as e:
                err_msg = await message.answer(
                    f"{ce('cross')} Не удалось отправить: {safe_html(str(e)[:300])}",
                    reply_markup=admin_menu_kb(),
                    parse_mode="HTML",
                )


@router.callback_query(F.data.startswith("a_msg_"))
async def admin_write_client(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    order_id = int(callback.data.replace("a_msg_", ""))
    await state.set_state(AdminMessage.entering_message)
    await state.update_data(order_id=order_id, action="msg")

    await callback.message.answer(
        f"{ce('speech')} Введите сообщение для клиента:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


# ─── Statistics ────────────────────────────────────────────────────

@router.message(StateFilter("*"), F.text.contains("Статистика"))
@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext):
    await state.clear()

    try:
        stats = await db.get_orders_stats()
    except Exception as e:
        logger.error(f"Stats DB error: {e}")
        stats = {}

    try:
        total_users = await db.get_total_users()
    except Exception:
        total_users = 0

    try:
        ton_rate = await get_ton_usd_rate()
    except Exception:
        ton_rate = 0

    try:
        rub_rate = await get_usd_rub_rate()
    except Exception:
        rub_rate = 0

    revenue_usdt = stats.get("revenue_usdt", 0) or 0
    revenue_rub = revenue_usdt * rub_rate if rub_rate else 0
    paid_queue = stats.get("paid_queue_usdt", 0) or 0

    gram_rate = ton_rate  # Gram = TON (same coin, renamed)

    # v20: экран статистики БЕЗ кастомных эмодзи. 14 <tg-emoji> в одном
    # сообщении отклонялись Telegram («can't parse entities») — админ видел
    # фолбэк «[Ошибка рендеринга: ...]». Чистый HTML: только <b> и «•».
    text = (
        "<b>Статистика</b>\n\n"
        f"Пользователей: <b>{total_users}</b>\n\n"
        "<b>Заказы:</b>\n"
        f"  • Ожидает оплату: {stats.get('pending_payment', 0) or 0}\n"
        f"  • Ожидает данные: {stats.get('pending_account', 0) or 0}\n"
        f"  • Ожидает активации: {stats.get('pending_activation', 0) or 0}\n"
        f"  • Активные: {stats.get('active', 0) or 0}\n"
        f"  • Истёкшие: {stats.get('expired', 0) or 0}\n"
        f"  • Отменено: {stats.get('cancelled', 0) or 0}\n"
        f"  • Ошибки: {stats.get('failed', 0) or 0}\n\n"
        "<b>Выручка (оплаченные):</b>\n"
        f"  • {revenue_rub:.0f} ₽\n"
        f"  • {revenue_usdt:.2f} USDT\n"
        f"  • В очереди (оплачено, не активировано): {paid_queue:.2f} USDT\n\n"
        "<b>По методам оплаты:</b>\n"
        f"  • Gram: {stats.get('ton_count', 0) or 0} заказов ({stats.get('ton_revenue', 0) or 0:.2f} USDT)\n"
        f"  • USDT: {stats.get('usdt_count', 0) or 0} заказов ({stats.get('usdt_revenue', 0) or 0:.2f} USDT)\n"
        f"  • Карта (Digiseller): {stats.get('digiseller_count', 0) or 0} заказов ({stats.get('digiseller_revenue', 0) or 0:.2f} USDT)\n"
        f"  • Карта (Tribute): {stats.get('tribute_count', 0) or 0} заказов ({stats.get('tribute_revenue', 0) or 0:.2f} USDT)\n"
        f"  • Stars: {stats.get('stars_count', 0) or 0} заказов ({stats.get('stars_revenue', 0) or 0:.2f} USDT)\n\n"
        "<b>Курсы (реальное время):</b>\n"
    )
    if gram_rate:
        text += f"  • Gram/USD: ${gram_rate:.2f}\n"
    else:
        text += "  • Gram/USD: недоступен\n"
    if rub_rate:
        text += f"  • USD/RUB: {rub_rate:.1f} ₽\n"
        text += f"  • Gram/RUB: {gram_rate * rub_rate:.1f} ₽"
    else:
        text += "  • USD/RUB: недоступен"

    try:
        stats_msg = await message.answer(text, parse_mode="HTML", reply_markup=admin_menu_kb())
    except Exception as e:
        # Сюда попадём только при сбое сети/Telegram: в основном тексте уже
        # нет ни кастомных эмодзи, ни проблемных сущностей — причина в логе
        logger.error(f"Stats send error: {e}")
        await message.answer(
            f"Статистика\n\n"
            f"Пользователей: {total_users}\n\n"
            f"Активные: {stats.get('active', 0) or 0}\n"
            f"Выручка: {revenue_usdt:.2f} USDT",
            reply_markup=admin_menu_kb(),
        )


# ─── Edit Catalog ──────────────────────────────────────────────────

@router.message(StateFilter("*"), F.text.contains("Ред. каталог"))
@router.message(Command("catalog_edit"))
async def cmd_edit_catalog(message: Message, state: FSMContext):
    await state.clear()

    catalog = load_catalog()
    text = f"{ce('pencil')} <b>Редактирование каталога</b>\n\nВыберите сервис для редактирования:"
    await message.answer(text, reply_markup=edit_catalog_kb(), parse_mode="HTML")


@router.callback_query(F.data == "back_edit_catalog")
async def back_to_edit_catalog(callback: CallbackQuery):
    text = f"{ce('pencil')} <b>Редактирование каталога</b>\n\nВыберите сервис:"
    await callback.message.edit_text(text, reply_markup=edit_catalog_kb(), parse_mode="HTML")
    await safe_answer(callback)


@router.callback_query(F.data.startswith("edit_"))
async def edit_service_menu(callback: CallbackQuery, state: FSMContext):

    # v16: инлайн-«Отмена» и любая навигация гасят активный FSM-ввод,
    # иначе состояние «залипает» и кнопка Отмена не работает
    await state.clear()
    service_id = callback.data[5:]  # len("edit_") = 5
    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return

    active = f"{ce('check')} Включён" if service.get("active", True) else f"{ce('cross')} Выключен"
    custom_emoji_info = ""
    if service.get("custom_emoji_id"):
        custom_emoji_info = f"\n{ce('sparkle')} Кастомный эмодзи ID: <code>{service['custom_emoji_id']}</code>"

    plans_info = ""
    for p in service.get("plans", []):
        plans_info += f"  {ce('wallet')} {p['name']}: {p.get('price_usdt', 0):.2f} USDT ({p['duration_days']} дн.)\n"

    text = (
        f"{ce('shopping')} <b>{service['name']}</b>\n\n"
        f"{ce('gear')} {service['description']}\n\n"
        f"Статус: {active}{custom_emoji_info}\n\n"
        f"{ce('wallet')} <b>Тарифы:</b>\n{plans_info}\n"
        f"Что хотите изменить?"
    )
    await callback.message.edit_text(text, reply_markup=edit_service_kb(service_id), parse_mode="HTML")
    await safe_answer(callback)


# ─── Toggle service on/off ─────────────────────────────────────────

@router.callback_query(F.data.startswith("edtoggle_"))
async def toggle_service(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # v16: навигация гасит FSM-ввод

    service_id = callback.data[9:]  # len("edtoggle_") = 9
    catalog = load_catalog()
    s = None
    for svc in catalog["services"]:
        if svc["id"] == service_id:
            svc["active"] = not svc.get("active", True)
            s = svc
            break

    if s is None:
        # Сервис удалён из каталога после отправки экрана — не падаем с
        # AttributeError на s.get(...), а спокойно отвечаем админу.
        await safe_answer(callback, "❌ Сервис не найден — обновите экран редактирования")
        return

    await async_save_catalog(catalog)

    new_status = "Включён" if s.get("active", True) else "Выключен"
    new_status_emoji = "✅" if s.get("active", True) else "❌"  # callback.answer — plain emoji only
    await safe_answer(callback, f"{new_status_emoji} {s['name']}: {new_status}")
    # Refresh the edit menu
    active = f"{ce('check')} Включён" if s.get("active", True) else f"{ce('cross')} Выключен"
    text = (
        f"{ce('shopping')} <b>{s['name']}</b>\n\n"
        f"{ce('gear')} {s['description']}\n\n"
        f"Статус: {active}\n\n"
        f"Что хотите изменить?"
    )
    await callback.message.edit_text(text, reply_markup=edit_service_kb(service_id), parse_mode="HTML")


# ─── Delete Service ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("delsvc_"))
async def delete_service_ask_confirm(callback: CallbackQuery, state: FSMContext):
    """Ask for confirmation before deleting a service."""
    await state.clear()  # v16: навигация гасит FSM-ввод

    service_id = callback.data[7:]  # len("delsvc_") = 7
    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return

    await callback.message.edit_text(
        f"{ce('warning')} <b>Удалить сервис «{safe_html(service['name'])}»?</b>\n\n"
        f"Все тарифы этого сервиса будут удалены. Это действие нельзя отменить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                pbtn("Да, удалить", f"delsvcok_{service_id}", "cross", "danger"),
                pbtn("Отмена", f"edit_{service_id}", "refresh"),
            ],
        ]),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("delsvcok_"))
async def delete_service_confirm(callback: CallbackQuery, state: FSMContext):
    """Actually delete a service from catalog after confirmation."""
    await state.clear()  # v16: навигация гасит FSM-ввод

    service_id = callback.data[9:]  # len("delsvcok_") = 9
    catalog = load_catalog()
    service_name = None
    original_count = len(catalog["services"])
    catalog["services"] = [s for s in catalog["services"] if s["id"] != service_id]

    if len(catalog["services"]) == original_count:
        await safe_answer(callback, "❌ Сервис не найден в каталоге")
        return

    # Find the name for logging
    for s in catalog.get("services", []):
        pass  # Already removed, name was from the old list
    await async_save_catalog(catalog)

    logger.info(f"Admin {callback.from_user.id} deleted service {service_id}")
    
    # Go back to catalog edit menu
    del_msg = await callback.message.edit_text(
        f"{ce('check')} Сервис удалён!",
        reply_markup=edit_catalog_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback, "Сервис удалён")


# ─── Edit Service Name ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("edname_"))
async def edit_name_start(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    service_id = callback.data[7:]  # len("edname_") = 7
    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return
    await state.set_state(EditService.entering_svc_name)
    await state.update_data(service_id=service_id)
    await callback.message.answer(
        f"{ce('pencil')} Текущее название: <b>{service['name']}</b>\n\nВведите новое название:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditService.entering_svc_name, F.text.contains("Отмена"))
async def edit_name_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditService.entering_svc_name)
async def edit_name_save(message: Message, state: FSMContext):
    if (new_text := await _require_text(message)) is None:
        return
    new_name = new_text.strip()
    if not new_name:
        await message.answer(f"{ce('cross')} Введите непустое название:", parse_mode="HTML")
        return
    data = await state.get_data()
    service_id = data["service_id"]
    catalog = load_catalog()
    found = False
    for s in catalog["services"]:
        if s["id"] == service_id:
            s["name"] = new_name
            found = True
            break
    if not found:
        logger.error(f"edit_name_save: Service {service_id} not found in catalog!")
        await state.clear()
        err_msg = await message.answer(f"{ce('cross')} Ошибка: сервис не найден в каталоге.", parse_mode="HTML", reply_markup=admin_menu_kb())
        return
    await async_save_catalog(catalog)
    await state.clear()
    done_msg = await message.answer(
        f"{ce('check')} Название изменено на: <b>{safe_html(new_name)}</b>",
        parse_mode="HTML",
        reply_markup=admin_menu_kb(),
    )
    logger.info(f"Admin {message.from_user.id} changed service {service_id} name to '{new_name}'")


# ─── Edit Service Description ──────────────────────────────────────

@router.callback_query(F.data.startswith("eddesc_"))
async def edit_desc_start(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    service_id = callback.data[7:]  # len("eddesc_") = 7
    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return
    await state.set_state(EditService.entering_svc_desc)
    await state.update_data(service_id=service_id)
    await callback.message.answer(
        f"{ce('pencil')} Текущее описание: {service['description']}\n\nВведите новое описание:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditService.entering_svc_desc, F.text.contains("Отмена"))
async def edit_desc_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditService.entering_svc_desc)
async def edit_desc_save(message: Message, state: FSMContext):
    if (new_text := await _require_text(message)) is None:
        return
    data = await state.get_data()
    service_id = data["service_id"]
    catalog = load_catalog()
    found = False
    for s in catalog["services"]:
        if s["id"] == service_id:
            s["description"] = new_text.strip()
            found = True
            break
    if not found:
        logger.error(f"edit_desc_save: Service {service_id} not found in catalog!")
        await state.clear()
        err_msg = await message.answer(f"{ce('cross')} Ошибка: сервис не найден в каталоге.", parse_mode="HTML", reply_markup=admin_menu_kb())
        return
    await async_save_catalog(catalog)
    await state.clear()
    done_msg = await message.answer(
        f"{ce('check')} Описание обновлено!",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


# ─── Edit Prices / Plans ───────────────────────────────────────────

@router.callback_query(F.data.startswith("edprice_"))
async def edit_prices_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # v16: навигация гасит FSM-ввод

    service_id = callback.data[8:]  # len("edprice_") = 8
    await callback.message.edit_text(
        f"{ce('wallet')} Выберите план для редактирования:",
        reply_markup=edit_plans_kb(service_id),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("edplan_"))
async def edit_plan_menu(callback: CallbackQuery, state: FSMContext):
    """Show plan editing menu — name, price, duration, delete."""

    # v16: инлайн-«Отмена» гасит активный FSM-ввод (баг-репорт владельца)
    await state.clear()
    # Format: edplan_{service_id}:{plan_id}
    payload = callback.data[7:]  # len("edplan_") = 7
    parts = payload.split(":", 1)
    service_id = parts[0]
    plan_id = parts[1] if len(parts) > 1 else ""

    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return
    plan = get_plan_from_service(service, plan_id)
    if not plan:
        logger.warning(f"Admin: Plan not found: service_id={service_id}, plan_id={plan_id}, callback_data={callback.data}, available_plans={[p['id'] for p in service.get('plans', [])]}")
        await safe_answer(callback, "❌ План не найден")
        return

    current_price = plan.get("price_usdt", 0)
    duration = plan.get("duration_days", 0)
    text = (
        f"{ce('wallet')} <b>{plan['name']}</b>\n\n"
        f"{ce('wallet')} Цена: <b>{current_price:.2f} USDT</b>\n"
        f"{ce('calendar')} Длительность: <b>{duration} дн.</b>\n\n"
        f"Что хотите изменить?"
    )
    await callback.message.edit_text(
        text,
        reply_markup=edit_plan_detail_kb(service_id, plan_id),
        parse_mode="HTML",
    )
    await safe_answer(callback)


# ─── Edit Plan: Name ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("epname_"))
async def edit_plan_name_start(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    payload = callback.data[7:]  # len("epname_") = 7
    parts = payload.split(":", 1)
    service_id = parts[0]
    plan_id = parts[1] if len(parts) > 1 else ""

    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return
    plan = get_plan_from_service(service, plan_id)
    if not plan:
        await safe_answer(callback, "❌ План не найден")
        return

    await state.set_state(EditPlan.entering_edit_plan_name)
    await state.update_data(service_id=service_id, plan_id=plan_id)

    await callback.message.answer(
        f"{ce('pencil')} <b>{plan['name']}</b> — текущее название.\n\n"
        f"Введите новое название:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditPlan.entering_edit_plan_name, F.text.contains("Отмена"))
async def edit_plan_name_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    service_id = data.get("service_id", "")
    await state.clear()
    if service_id:
        cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())
        from keyboards.keyboards import edit_plans_kb
        await message.answer(
            f"{ce('wallet')} Планы сервиса:",
            reply_markup=edit_plans_kb(service_id),
            parse_mode="HTML",
        )
    else:
        cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditPlan.entering_edit_plan_name)
async def edit_plan_name_save(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    new_name = raw.strip()
    if not new_name:
        await message.answer(f"{ce('cross')} Введите непустое название:", parse_mode="HTML")
        return

    data = await state.get_data()
    service_id = data.get("service_id", "")
    plan_id = data.get("plan_id", "")

    catalog = load_catalog()
    found = False
    for s in catalog["services"]:
        if s["id"] == service_id:
            for p in s["plans"]:
                if p["id"] == plan_id:
                    p["name"] = new_name
                    found = True
                    break
            break
    if not found:
        await state.clear()
        err_msg = await message.answer(f"{ce('cross')} Ошибка: план не найден.", parse_mode="HTML", reply_markup=admin_menu_kb())
        return
    await async_save_catalog(catalog)
    await state.clear()

    # Show updated plan detail
    plan = get_plan_from_service(get_service_by_id(service_id), plan_id)
    text = (
        f"{ce('check')} Название обновлено: <b>{new_name}</b>\n\n"
        f"{ce('wallet')} Цена: <b>{plan.get('price_usdt', 0):.2f} USDT</b>\n"
        f"{ce('calendar')} Длительность: <b>{plan.get('duration_days', 0)} дн.</b>"
    )
    await message.answer(text, reply_markup=edit_plan_detail_kb(service_id, plan_id), parse_mode="HTML")
    logger.info(f"Admin {message.from_user.id} renamed plan {service_id}/{plan_id} to '{new_name}'")


# ─── Edit Plan: Price ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("epprice_"))
async def edit_plan_price_start(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    payload = callback.data[8:]  # len("epprice_") = 8
    parts = payload.split(":", 1)
    service_id = parts[0]
    plan_id = parts[1] if len(parts) > 1 else ""

    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return
    plan = get_plan_from_service(service, plan_id)
    if not plan:
        await safe_answer(callback, "❌ План не найден")
        return

    await state.set_state(EditPlan.entering_edit_plan_price)
    await state.update_data(service_id=service_id, plan_id=plan_id)

    current_price = plan.get("price_usdt", 0)
    await callback.message.answer(
        f"{ce('wallet')} <b>{plan['name']}</b> — текущая цена: <b>{current_price:.2f} USDT</b>\n\n"
        f"Введите новую цену (в USDT):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditPlan.entering_edit_plan_price, F.text.contains("Отмена"))
async def edit_plan_price_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditPlan.entering_edit_plan_price)
async def edit_plan_price_save(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    try:
        new_price = float(raw.strip().replace(",", "."))
        # v17: было только < 0 — NaN проходил проверкой (NaN < 0 = False)
        # и писал невалидный JSON каталога; 0 создавал неоплачиваемый тариф
        if not math.isfinite(new_price) or new_price <= 0:
            raise ValueError
    except ValueError:
        await message.answer(f"{ce('cross')} Введите корректную цену (число больше 0 в USDT):", parse_mode="HTML")
        return

    data = await state.get_data()
    service_id = data.get("service_id", "")
    plan_id = data.get("plan_id", "")

    catalog = load_catalog()
    found = False
    for s in catalog["services"]:
        if s["id"] == service_id:
            for p in s["plans"]:
                if p["id"] == plan_id:
                    p["price_usdt"] = new_price
                    p["price"] = round(new_price * 100)  # v17: round вместо int — центр больше не теряется (19.99→1999, не 1998)
                    found = True
                    break
            break
    if not found:
        logger.error(f"edit_plan_price_save: Plan {service_id}/{plan_id} not found in catalog!")
        await state.clear()
        err_msg = await message.answer(f"{ce('cross')} Ошибка: план не найден в каталоге.", parse_mode="HTML", reply_markup=admin_menu_kb())
        return
    await async_save_catalog(catalog)
    await state.clear()

    # Show updated plan detail
    plan = get_plan_from_service(get_service_by_id(service_id), plan_id)
    text = (
        f"{ce('check')} Цена обновлена: <b>{new_price:.2f} USDT</b>\n\n"
        f"{ce('pencil')} Название: <b>{plan['name']}</b>\n"
        f"{ce('calendar')} Длительность: <b>{plan.get('duration_days', 0)} дн.</b>"
    )
    await message.answer(text, reply_markup=edit_plan_detail_kb(service_id, plan_id), parse_mode="HTML")
    logger.info(f"Admin {message.from_user.id} changed {service_id}/{plan_id} price to {new_price} USDT")


# ─── Edit Plan: Duration ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("epdur_"))
async def edit_plan_duration_start(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    payload = callback.data[6:]  # len("epdur_") = 6
    parts = payload.split(":", 1)
    service_id = parts[0]
    plan_id = parts[1] if len(parts) > 1 else ""

    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return
    plan = get_plan_from_service(service, plan_id)
    if not plan:
        await safe_answer(callback, "❌ План не найден")
        return

    await state.set_state(EditPlan.entering_edit_plan_duration)
    await state.update_data(service_id=service_id, plan_id=plan_id)

    current_dur = plan.get("duration_days", 0)
    await callback.message.answer(
        f"{ce('calendar')} <b>{plan['name']}</b> — текущая длительность: <b>{current_dur} дн.</b>\n\n"
        f"Введите новую длительность (в днях):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditPlan.entering_edit_plan_duration, F.text.contains("Отмена"))
async def edit_plan_duration_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditPlan.entering_edit_plan_duration)
async def edit_plan_duration_save(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    try:
        new_duration = int(raw.strip())
        if new_duration <= 0:
            raise ValueError
    except ValueError:
        await message.answer(f"{ce('cross')} Введите корректную длительность (целое число дней):", parse_mode="HTML")
        return

    data = await state.get_data()
    service_id = data.get("service_id", "")
    plan_id = data.get("plan_id", "")

    catalog = load_catalog()
    found = False
    for s in catalog["services"]:
        if s["id"] == service_id:
            for p in s["plans"]:
                if p["id"] == plan_id:
                    p["duration_days"] = new_duration
                    found = True
                    break
            break
    if not found:
        await state.clear()
        err_msg = await message.answer(f"{ce('cross')} Ошибка: план не найден.", parse_mode="HTML", reply_markup=admin_menu_kb())
        return
    await async_save_catalog(catalog)
    await state.clear()

    # Show updated plan detail
    plan = get_plan_from_service(get_service_by_id(service_id), plan_id)
    text = (
        f"{ce('check')} Длительность обновлена: <b>{new_duration} дн.</b>\n\n"
        f"{ce('pencil')} Название: <b>{plan['name']}</b>\n"
        f"{ce('wallet')} Цена: <b>{plan.get('price_usdt', 0):.2f} USDT</b>"
    )
    await message.answer(text, reply_markup=edit_plan_detail_kb(service_id, plan_id), parse_mode="HTML")
    logger.info(f"Admin {message.from_user.id} changed {service_id}/{plan_id} duration to {new_duration} days")


# ─── Add New Plan ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("addplan_"))
async def add_plan_start(callback: CallbackQuery, state: FSMContext):

    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    service_id = callback.data[8:]  # len("addplan_") = 8
    logger.info(f"ADD_PLAN_START: admin={callback.from_user.id}, service_id={service_id}, callback_data={callback.data}")
    await state.set_state(AddPlan.entering_plan_name)
    await state.update_data(service_id=service_id)
    logger.info(f"ADD_PLAN_START: state set to AddPlan.entering_plan_name, service_id saved")
    try:
        await callback.message.answer(
            f"{ce('plus')} <b>Добавление нового плана</b>\n\nВведите название (например, «1 месяц»):",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"ADD_PLAN_START: failed to send message: {e}")
        # Try without custom emoji keyboard (fallback)
        try:
            await callback.message.answer(
                f"{ce('plus')} <b>Добавление нового плана</b>\n\nВведите название (например, «1 месяц»):",
                reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True),
                parse_mode="HTML",
            )
        except Exception as e2:
            logger.error(f"ADD_PLAN_START: fallback also failed: {e2}")
    await safe_answer(callback)


@router.message(AddPlan.entering_plan_name, F.text.contains("Отмена"))
async def add_plan_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(AddPlan.entering_plan_name)
async def add_plan_duration(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    logger.info(f"ADD_PLAN_NAME: got name='{raw}', from={message.from_user.id}")
    await state.update_data(plan_name=raw.strip())
    await state.set_state(AddPlan.entering_plan_duration)
    try:
        await message.answer(
            f"{ce('calendar')} Введите длительность в днях (например, 30):",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"ADD_PLAN_NAME: failed to send prompt: {e}")
        await message.answer(
            f"{ce('calendar')} Введите длительность в днях (например, 30):",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True),
            parse_mode="HTML",
        )


@router.message(AddPlan.entering_plan_duration, F.text.contains("Отмена"))
async def add_plan_dur_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(AddPlan.entering_plan_duration)
async def add_plan_duration_entered(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    logger.info(f"ADD_PLAN_DURATION: got days='{raw}', from={message.from_user.id}")
    try:
        days = int(raw.strip())
        if days < 1:
            raise ValueError
    except ValueError:
        await message.answer(f"{ce('cross')} Введите корректное число дней:", parse_mode="HTML")
        return

    await state.update_data(duration_days=days)
    await state.set_state(AddPlan.entering_plan_price)
    try:
        await message.answer(
            f"{ce('wallet')} Введите цену в USDT (например, 1.50):",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"ADD_PLAN_DURATION: failed to send prompt: {e}")
        await message.answer(
            f"{ce('wallet')} Введите цену в USDT (например, 1.50):",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True),
            parse_mode="HTML",
        )


@router.message(AddPlan.entering_plan_price, F.text.contains("Отмена"))
async def add_plan_price_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(AddPlan.entering_plan_price)
async def add_plan_price_save(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    logger.info(f"ADD_PLAN_PRICE: got price='{raw}', from={message.from_user.id}")
    try:
        price_usdt = float(raw.strip().replace(",", "."))
        # v17: NaN/0 больше не проходят — неоплачиваемый тариф создать нельзя
        if not math.isfinite(price_usdt) or price_usdt <= 0:
            raise ValueError
    except ValueError:
        await message.answer(f"{ce('cross')} Введите корректную цену (число больше 0, USDT):", parse_mode="HTML")
        return

    data = await state.get_data()
    logger.info(f"ADD_PLAN_PRICE: state data = {data}")
    service_id = data.get("service_id")
    plan_name = data.get("plan_name")
    duration_days = data.get("duration_days")

    if not service_id or not plan_name or not duration_days:
        logger.error(f"ADD_PLAN_PRICE: MISSING state data! service_id={service_id}, plan_name={plan_name}, duration_days={duration_days}")
        await state.clear()
        err_msg = await message.answer(
            f"{ce('cross')} Ошибка: данные плана потеряны. Попробуйте снова.",
            parse_mode="HTML",
            reply_markup=admin_menu_kb(),
        )
        return

    # Generate a plan ID from name
    plan_id = f"plan_{duration_days}d_{int(time.time()) % 10000}"

    catalog = load_catalog()
    logger.info(f"ADD_PLAN_PRICE: catalog loaded, {len(catalog['services'])} services, looking for service_id={service_id}")
    service_found = False
    for s in catalog["services"]:
        if s["id"] == service_id:
            existing_ids = [p["id"] for p in s["plans"]]
            logger.info(f"ADD_PLAN_PRICE: found service '{s['name']}', existing plan ids: {existing_ids}")
            if plan_id in existing_ids:
                plan_id = f"{plan_id}_{len(s['plans']) + 1}"
            s["plans"].append({
                "id": plan_id,
                "name": plan_name,
                "duration_days": duration_days,
                "price_usdt": price_usdt,
                "price": round(price_usdt * 100),  # v17: round вместо int
            })
            service_found = True
            break
    if not service_found:
        logger.error(f"add_plan_price_save: Service {service_id} not found in catalog! Plan NOT saved.")
        await state.clear()
        err_msg = await message.answer(
            f"{ce('cross')} Ошибка: сервис не найден в каталоге. План не добавлен.",
            parse_mode="HTML",
            reply_markup=admin_menu_kb(),
        )
        return
    
    try:
        await async_save_catalog(catalog)
        logger.info(f"ADD_PLAN_PRICE: catalog saved successfully with plan {plan_id}")
    except Exception as e:
        logger.error(f"ADD_PLAN_PRICE: save_catalog FAILED: {e}")
        await state.clear()
        err_msg = await message.answer(
            f"{ce('cross')} Ошибка сохранения каталога: {safe_html(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=admin_menu_kb(),
        )
        return

    # Verify the plan was actually saved
    verify_catalog = load_catalog()
    verify_service = get_service_by_id(service_id)
    verify_plan = get_plan_from_service(verify_service, plan_id) if verify_service else None
    if not verify_plan:
        logger.error(f"add_plan_price_save: VERIFICATION FAILED! Plan {plan_id} not found after save for service {service_id}")
        await state.clear()
        warn_msg = await message.answer(
            f"{ce('warning')} План сохранён, но проверка не прошла. Проверьте каталог.",
            parse_mode="HTML",
            reply_markup=admin_menu_kb(),
        )
        return

    await state.clear()

    # Offer to add another plan or go back
    after_plan_kb = InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Добавить ещё план", f"addplan_{service_id}", "plus", "primary")],
        [pbtn("К планам сервиса", f"edprice_{service_id}", "wallet", "success")],
        [pbtn("В каталог", "back_edit_catalog", "refresh")],
    ])
    await message.answer(
        f"{ce('check')} План <b>{plan_name}</b> добавлен!\n"
        f"{ce('calendar')} {duration_days} дн. — {price_usdt:.2f} USDT\n\n"
        f"Добавьте ещё план или вернитесь к редактированию:",
        parse_mode="HTML",
        reply_markup=after_plan_kb,
    )
    # Restore admin menu keyboard
    menu_msg = await message.answer("Меню", reply_markup=admin_menu_kb())
    logger.info(f"Admin {message.from_user.id} added plan {plan_name} (id={plan_id}) to {service_id}")


# ─── Delete Plan ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("delplan_"))
async def delete_plan_menu(callback: CallbackQuery, state: FSMContext):
    """Show list of plans to delete."""
    await state.clear()  # v16: навигация гасит FSM-ввод

    service_id = callback.data[8:]  # len("delplan_") = 8
    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return

    if not service.get("plans"):
        await safe_answer(callback, "Нет планов для удаления")
        return

    from keyboards.keyboards import delete_plans_kb
    await callback.message.edit_text(
        f"{ce('cross')} <b>Удаление плана — {service['name']}</b>\n\n"
        f"Выберите план для удаления:",
        reply_markup=delete_plans_kb(service_id),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("delplanask_"))
async def delete_plan_ask_confirm(callback: CallbackQuery, state: FSMContext):
    """Ask for confirmation before deleting a plan.

    Раньше кнопки «Удалить план» вели сразу на delplanok_ — план удалялся
    одним нажатием без подтверждения (промах пальцем = потеря тарифа).
    """
    await state.clear()  # v16: навигация гасит FSM-ввод

    # Format: delplanask_{service_id}:{plan_id}
    payload = callback.data[11:]  # len("delplanask_") = 11
    parts = payload.split(":", 1)
    service_id = parts[0]
    plan_id = parts[1] if len(parts) > 1 else ""

    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "❌ Сервис не найден")
        return

    plan = next((p for p in service.get("plans", []) if p.get("id") == plan_id), None)
    if not plan:
        await safe_answer(callback, "❌ План не найден")
        return

    await callback.message.edit_text(
        f"{ce('warning')} <b>Удалить план «{safe_html(str(plan['name']))}»?</b>\n\n"
        f"Сервис: {safe_html(str(service['name']))}\n"
        f"Цена: {plan.get('price_usdt', 0):.2f} USDT ({plan.get('duration_days', 0)} дн.)\n\n"
        f"План исчезнет из каталога. Это действие нельзя отменить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                pbtn("Да, удалить", f"delplanok_{service_id}:{plan_id}", "cross", "danger"),
                pbtn("Отмена", f"edplan_{service_id}:{plan_id}", "refresh"),
            ]
        ]),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("delplanok_"))
async def delete_plan_confirm(callback: CallbackQuery, state: FSMContext):
    """Actually delete a plan."""
    await state.clear()  # v16: навигация гасит FSM-ввод

    # Format: delplanok_{service_id}:{plan_id}
    payload = callback.data[10:]  # len("delplanok_") = 10
    parts = payload.split(":", 1)
    service_id = parts[0]
    plan_id = parts[1] if len(parts) > 1 else ""

    catalog = load_catalog()
    found = False
    for s in catalog["services"]:
        if s["id"] == service_id:
            before_count = len(s["plans"])
            s["plans"] = [p for p in s["plans"] if p["id"] != plan_id]
            after_count = len(s["plans"])
            if after_count < before_count:
                found = True
            break

    if not found:
        await safe_answer(callback, "❌ План не найден")
        return

    await async_save_catalog(catalog)
    logger.info(f"Admin {callback.from_user.id} deleted plan {plan_id} from {service_id}")

    # Refresh the plans menu
    from keyboards.keyboards import edit_plans_kb
    await callback.message.edit_text(
        f"{ce('check')} План удалён!\n\n"
        f"{ce('wallet')} Выберите план для редактирования:",
        reply_markup=edit_plans_kb(service_id),
        parse_mode="HTML",
    )
    await safe_answer(callback, "План удалён")


# ─── Add New Service ───────────────────────────────────────────────

@router.callback_query(F.data == "add_service")
async def add_service_start(callback: CallbackQuery, state: FSMContext):

    await state.set_state(AddService.entering_new_svc_name)
    await callback.message.answer(
        f"{ce('plus')} <b>Добавление нового сервиса</b>\n\nВведите название (например, «YouTube Premium»):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(AddService.entering_new_svc_name, F.text.contains("Отмена"))
async def add_service_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(AddService.entering_new_svc_name)
async def add_service_emoji(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    await state.update_data(service_name=raw.strip())
    await state.set_state(AddService.entering_new_svc_emoji)
    await message.answer(
        f"{ce('sparkle')} Введите эмодзи для сервиса (например, {ce('spotify')}).\n\n"
        f"<i>Если хотите использовать кастомный эмодзи — введите его ID после эмодзи через пробел: {ce('spotify')} 5368324170671202286</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddService.entering_new_svc_emoji, F.text.contains("Отмена"))
async def add_service_emoji_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(AddService.entering_new_svc_emoji)
async def add_service_desc(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    parts = raw.strip().split()
    emoji = parts[0][:2] if parts else "📦"  # ReplyKeyboard — plain emoji only
    custom_emoji_id = parts[1] if len(parts) > 1 else ""

    await state.update_data(emoji=emoji, custom_emoji_id=custom_emoji_id)
    await state.set_state(AddService.entering_new_svc_desc)
    await message.answer(
        f"{ce('gear')} Введите описание сервиса:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddService.entering_new_svc_desc, F.text.contains("Отмена"))
async def add_service_desc_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(AddService.entering_new_svc_desc)
async def add_service_save(message: Message, state: FSMContext):
    if (raw := await _require_text(message)) is None:
        return
    data = await state.get_data()
    service_name = data["service_name"]
    emoji = data["emoji"]
    custom_emoji_id = data.get("custom_emoji_id", "")
    desc = raw.strip()

    # Generate service ID with transliteration for Cyrillic support
    # Transliterate common Cyrillic -> Latin
    _translit_map = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh',
        'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
        'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts',
        'ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
        'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'Yo','Ж':'Zh',
        'З':'Z','И':'I','Й':'Y','К':'K','Л':'L','М':'M','Н':'N','О':'O',
        'П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F','Х':'Kh','Ц':'Ts',
        'Ч':'Ch','Ш':'Sh','Щ':'Shch','Ъ':'','Ы':'Y','Ь':'','Э':'E','Ю':'Yu','Я':'Ya',
    }
    _transliterated = ''.join(_translit_map.get(c, c) for c in service_name)
    service_id = re.sub(r'[^a-z0-9]', '_', _transliterated.lower())[:30].strip('_')
    # If still empty (e.g., Chinese/Japanese), use timestamp-based ID
    if not service_id or service_id.strip('_') == '':
        service_id = f"svc_{int(time.time())}"

    catalog = load_catalog()
    existing_ids = [s["id"] for s in catalog["services"]]
    if service_id in existing_ids:
        service_id = f"{service_id}_{len(catalog['services']) + 1}"

    new_service = {
        "id": service_id,
        "name": service_name,
        "emoji": emoji,
        "custom_emoji_id": custom_emoji_id,
        "description": desc,
        "plans": [],
        "account_fields": [
            {"id": "email", "label": "Email/Логин", "placeholder": "example@email.com", "type": "email"},
            {"id": "password", "label": "Пароль", "placeholder": "Ваш пароль", "type": "password"},
        ],
        "active": True,
    }
    catalog["services"].append(new_service)
    await async_save_catalog(catalog)
    await state.clear()

    # Offer to add plans directly after creating service
    after_svc_kb = InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Добавить план", f"addplan_{service_id}", "plus", "primary")],
        [pbtn("Готово, в каталог", "back_edit_catalog", "check", "success")],
    ])
    await message.answer(
        f"{ce('check')} Сервис <b>{emoji} {service_name}</b> добавлен!\n\n"
        f"Теперь добавьте тарифные планы:",
        parse_mode="HTML",
        reply_markup=after_svc_kb,
    )
    # Restore admin menu keyboard
    menu_msg = await message.answer("Меню", reply_markup=admin_menu_kb())
    logger.info(f"Admin {message.from_user.id} added service {service_name}")


# ─── Broadcast ─────────────────────────────────────────────────────

# UX №7: флаги остановки рассылки по admin_id (кнопка «⏹ Стоп» на
# статус-сообщении). Один активный запуск на админа — достаточно.
_broadcast_stop: dict[int, bool] = {}


@router.message(StateFilter("*"), F.text.contains("Рассылка"))
@router.message(Command("broadcast"))
async def broadcast_start(message: Message, state: FSMContext):
    await state.clear()

    await state.set_state(Broadcast.entering_message)
    await message.answer(
        f"{ce('megaphone')} <b>Рассылка</b>\n\n"
        f"Отправьте <b>текст</b> или <b>фото с подписью</b> для рассылки всем пользователям\n"
        f"(HTML поддерживается; у подписи фото лимит 1024 символа).\n\n"
        f"Сначала придёт предпросмотр вам, затем рассылка начнётся —\n"
        f"на статус-сообщении появится кнопка «⏹ Стоп».",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Broadcast.entering_message, F.text.contains("Отмена"))
async def broadcast_cancel(message: Message, state: FSMContext):
    await state.clear()
    cancel_msg = await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(Broadcast.entering_message)
async def broadcast_send(message: Message, state: FSMContext):
    """UX №7: рассылка ТЕКСТА или ФОТО с подписью + кнопка «⏹ Стоп».

    Раньше принимался только message.text (фото молча игнорировалось),
    а запущенный цикл нельзя было остановить до конца базы — только
    перезапуском бота. Теперь фото уходит как send_photo с подписью,
    предпросмотр валидирует HTML и лимиты, «Стоп» корректно завершает
    цикл с отчётом «доставлено X из Y».
    """
    # Содержимое рассылки: фото с подписью ИЛИ текст
    if message.photo:
        broadcast_photo = message.photo[-1].file_id  # наибольший размер
        broadcast_caption = message.caption or ""
        broadcast_text = None
    elif message.text:
        broadcast_photo = None
        broadcast_caption = None
        broadcast_text = message.text
    else:
        # Видео/документ/стикер и т.п. — не поддерживается, просим ещё раз
        await state.set_state(Broadcast.entering_message)
        await message.answer(
            f"{ce('warning')} Поддерживается только <b>текст</b> или <b>фото с подписью</b>.\n"
            f"Отправьте содержимое ещё раз или нажмите «Отмена».",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return

    await state.clear()

    # Валидация HTML + лимитов пробной отправкой себе (фото — с подписью:
    # заодно проверяется лимит подписи 1024 и битый file_id)
    preview_text = (
        f"{ce('megaphone')} <b>Предпросмотр рассылки:</b>\n\n"
        f"{broadcast_text if broadcast_photo is None else broadcast_caption}"
    )
    try:
        if broadcast_photo:
            await message.bot.send_photo(
                message.from_user.id,
                photo=broadcast_photo,
                caption=preview_text,
                parse_mode="HTML",
            )
        else:
            await message.bot.send_message(
                message.from_user.id, preview_text, parse_mode="HTML"
            )
    except Exception as e:
        err_msg = await message.answer(
            f"{ce('cross')} <b>Telegram не смог отправить предпросмотр!</b>\n\n"
            f"Проверьте HTML-теги и лимиты (текст ≤ 4096, подпись фото ≤ 1024 символа).\n"
            f"Ошибка: {safe_html(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=admin_menu_kb(),
        )
        return

    # v17: шаг ПОДТВЕРЖДЕНИЯ. Раньше рассылка стартовала мгновенно — любой
    # случайный текст/опечатка в состоянии ввода УЛЕТАЛИ ВСЕМ пользователям
    # безвозвратно. Теперь: предпросмотр отправлен, контент ждёт явного
    # нажатия «🚀 Начать рассылку».
    await state.set_state(Broadcast.confirming)
    await state.update_data(
        broadcast_photo=broadcast_photo,
        broadcast_caption=broadcast_caption,
        broadcast_text=broadcast_text,
    )
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Начать рассылку", callback_data="bgo"),
        InlineKeyboardButton(text="✖️ Отменить", callback_data="bcancel"),
    ]])
    await message.answer(
        f"{ce('warning')} <b>Письмо будет отправлено ВСЕМ пользователям бота.</b>\n\n"
        f"Предпросмотр выше — проверьте его в своём чате. Начать?",
        reply_markup=confirm_kb,
        parse_mode="HTML",
    )


# v17: глобальный флаг «рассылка уже идёт». Раньше два админа (или один
# админ из двух чатов) могли запустить циклы параллельно — база получала
# каждое письмо дважды.
_broadcast_running = False


@router.callback_query(F.data == "bgo", Broadcast.confirming)
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    """v17: явный старт рассылки после предпросмотра."""
    global _broadcast_running
    data = await state.get_data()
    await state.clear()

    if _broadcast_running:
        await safe_answer(callback, "⚠️ Рассылка уже выполняется — дождитесь завершения.", show_alert=True)
        return

    broadcast_photo = data.get("broadcast_photo")
    broadcast_caption = data.get("broadcast_caption") or ""
    broadcast_text = data.get("broadcast_text")
    if broadcast_photo is None and not broadcast_text:
        await safe_answer(callback, "⚠️ Содержимое потерялось — начните заново (кнопка «Рассылка»).", show_alert=True)
        return

    try:
        await callback.message.edit_text(
            f"{ce('megaphone')} Контент принят — запускаю рассылку...",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await _run_broadcast(
        callback, callback.bot, callback.from_user.id,
        broadcast_photo, broadcast_caption, broadcast_text,
    )


@router.callback_query(F.data == "bcancel", Broadcast.confirming)
async def broadcast_cancel_cb(callback: CallbackQuery, state: FSMContext):
    """v17: отмена рассылки на шаге подтверждения."""
    await state.clear()
    try:
        await callback.message.edit_text(f"{ce('check')} Рассылка отменена — ничего не отправлено.")
    except Exception:
        pass
    await callback.message.answer("Меню", reply_markup=admin_menu_kb())
    await safe_answer(callback)


@router.message(Broadcast.confirming, F.text.contains("Отмена"))
async def broadcast_confirm_cancel_text(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено — ничего не отправлено.", reply_markup=admin_menu_kb())


@router.message(Broadcast.confirming, F.text)
async def broadcast_confirm_other_text(message: Message, state: FSMContext):
    """v17: в состоянии подтверждения новый текст = новое содержимое.
    Раньше confirm-состояние глотало текст молча."""
    if message.text and message.text.strip() in MENU_BUTTONS:
        await state.clear()
        await message.answer("Меню", reply_markup=admin_menu_kb())
        return
    await state.set_state(Broadcast.entering_message)
    await message.answer(
        f"{ce('info')} Это новое содержимое рассылки — оно сохранено.\n"
        f"Нажмите «Рассылка», чтобы показать предпросмотр и запустить.",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


async def _run_broadcast(
    callback: CallbackQuery, bot, admin_id: int,
    broadcast_photo: str | None, broadcast_caption: str | None,
    broadcast_text: str | None,
):
    """v17: цикл рассылки вынесен из broadcast_send — запускается только
    после явного подтверждения (bgo). Логика доставки прежняя."""
    global _broadcast_running
    _broadcast_running = True

    user_ids = await db.get_all_user_ids()
    success = 0
    failed = 0
    stopped = False
    _broadcast_stop[admin_id] = False

    stop_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏹ Остановить рассылку", callback_data="bstop"),
    ]])
    status_msg = await callback.message.answer(
        f"{ce('megaphone')} Рассылка начата ({len(user_ids)} пользователей)...",
        reply_markup=stop_kb,
        parse_mode="HTML",
    )

    async def _send_one(uid: int) -> None:
        if broadcast_photo:
            await bot.send_photo(
                uid, photo=broadcast_photo,
                caption=broadcast_caption, parse_mode="HTML",
            )
        else:
            await bot.send_message(uid, broadcast_text, parse_mode="HTML")

    for uid in user_ids:
        if _broadcast_stop.get(admin_id):
            stopped = True
            break
        if uid == admin_id:
            success += 1  # Уже получил предпросмотр
            continue
        try:
            await _send_one(uid)
            success += 1
        except Exception as e:
            # Handle Telegram rate limiting
            if hasattr(e, 'retry_after'):
                retry_after = getattr(e, 'retry_after', 5)
                logger.warning(f"Broadcast rate-limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                # Retry once after waiting
                try:
                    await _send_one(uid)
                    success += 1
                    continue
                except Exception:
                    pass
            # Handle blocked bot / deactivated user
            error_str = str(e)
            if "Forbidden" in error_str or "blocked" in error_str.lower() or "deactivated" in error_str.lower():
                logger.info(f"Broadcast: user {uid} blocked the bot, skipping")
            else:
                logger.warning(f"Broadcast failed for {uid}: {e}")
            failed += 1
        # Throttle: pause between messages to avoid rate limits
        await asyncio.sleep(BROADCAST_DELAY)

    _broadcast_stop.pop(admin_id, None)
    _broadcast_running = False

    headline = "Рассылка ОСТАНОВЛЕНА" if stopped else "Рассылка завершена"
    result_icon = ce('ban') if stopped else ce('check')
    try:
        await status_msg.edit_text(
            f"{ce('megaphone')} <b>{headline}</b>\n\n"
            f"{result_icon} Доставлено: {success}\n{ce('cross')} Ошибок: {failed}\n\n"
            f"<i>Кнопка «Стоп» снята — рассылка больше не идёт.</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Broadcast status edit failed: {e}")


@router.callback_query(F.data == "bstop")
async def broadcast_stop_pressed(callback: CallbackQuery):
    """UX №7: кнопка «⏹ Стоп» на статус-сообщении рассылки —
    цикл завершается на следующей итерации с итоговым отчётом."""
    _broadcast_stop[callback.from_user.id] = True
    await safe_answer(callback, "⏹ Останавливаю рассылку...")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ─── Promo Codes Management ──────────────────────────────────────────

# UX №6: промокодов на странице списка (раньше SQL LIMIT 10 — коды
# дальше десятого были невидимы и недоступны для управления)
PROMOS_PER_PAGE = 10


async def _fetch_promos() -> list[dict]:
    """Все промокоды (новые сверху), без LIMIT 10."""
    async with db._get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT code, discount_pct, discount_fixed_usdt, max_uses, used_count, active, expires_at FROM promo_codes ORDER BY created_at DESC LIMIT 200"
        )
        promos = await cursor.fetchall()
    return [dict(p) for p in promos]


def _promo_list_row(p: dict) -> str:
    """Строка одного промокода в списке."""
    status = f"{ce('check')}" if p["active"] else f"{ce('cross')}"
    pct = p.get("discount_pct") or 0
    fixed = p.get("discount_fixed_usdt") or 0
    discount = f"{pct}%" if pct > 0 else f"{fixed:.2f}$"
    uses = f"{p['used_count']}/{p['max_uses']}" if p["max_uses"] else f"{p['used_count']}/∞"
    expires = ""
    if p.get("expires_at"):
        expires = f" (до {p['expires_at'][:10]})"
    return f"{status} <code>{safe_html(p['code'])}</code> — {discount} (исп: {uses}){expires}\n"


def _promo_list_page_data(promos: list[dict], page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст + клавиатура одной страницы списка промокодов (UX №6)."""
    total_pages = max(1, math.ceil(len(promos) / PROMOS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    chunk = promos[page * PROMOS_PER_PAGE:(page + 1) * PROMOS_PER_PAGE]

    text = f"{ce('discount')}  <b>Промокоды</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    if chunk:
        for p in chunk:
            text += _promo_list_row(p)
    else:
        text += "Промокодов пока нет.\n"

    text += f"\n{ce('plus')} Для создания: /newpromo КОД СКИДКА_ПРОЦ [МАКС_ИСПОЛЬЗОВАНИЙ]\n"
    text += f"<i>Пример: /newpromo WELCOME 15 100</i>\n"
    text += f"<i>Пример: /newpromo LAUNCH20 20 0 (без лимита)</i>"

    from keyboards.keyboards import admin_promo_list_kb
    if chunk:
        kb = admin_promo_list_kb(chunk, page=page, total_pages=total_pages)
    else:
        # Пустой список: пустую инлайн-клавиатуру Telegram отклоняет —
        # отдаём админ-меню (как раньше)
        kb = admin_menu_kb()
    return text, kb


@router.message(StateFilter("*"), F.text.contains("Промокоды"))
async def admin_promo_codes(message: Message, state: FSMContext):
    await state.clear()
    promos = await _fetch_promos()
    text, kb = _promo_list_page_data(promos, 0)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("promolist_"))
async def admin_promo_list_page(callback: CallbackQuery, state: FSMContext):
    """UX №6: листание списка промокодов — тем же сообщением
    (страница передаётся номером в callback, например promolist_2)."""
    await state.clear()
    try:
        page = max(0, int(callback.data[len("promolist_"):]))
    except ValueError:
        await safe_answer(callback)
        return
    promos = await _fetch_promos()
    text, kb = _promo_list_page_data(promos, page)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        # «message is not modified» и т.п. — уже на нужной странице
        pass
    await safe_answer(callback)


@router.message(Command("newpromo"))
async def admin_create_promo(message: Message, state: FSMContext):
    await state.clear()
    
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            f"{ce('cross')} Формат: /newpromo КОД СКИДКА_ПРОЦ [МАКС_ИСПОЛЬЗОВАНИЙ] [ОПИСАНИЕ]\n"
            f"Пример: /newpromo WELCOME 15 100 \"Скидка для новых\"",
            parse_mode="HTML",
        )
        return
    
    code = parts[1].upper()
    # v17: валидация кода. Раньше код любой длины/символов уходил в БД,
    # а promo_toggle_<код> в кнопках списка превышал лимит Telegram 64 байта
    # → Telegram отклонял ВСЁ сообщение списка промокодов (BUTTON_DATA_INVALID)
    # и раздел умирал целиком, пока код не удаляли вручную из БД.
    if not re.fullmatch(r"[A-Z0-9_-]{2,30}", code):
        await message.answer(
            f"{ce('cross')} Код промокода: 2–30 символов, только латиница, "
            f"цифры, «_» и «-».\n\nПример: /newpromo WELCOME 15 100",
            parse_mode="HTML",
        )
        return
    try:
        discount_pct = float(parts[2])
    except ValueError:
        err_msg = await message.answer(f"{ce('cross')} Скидка должна быть числом (например, 15 для 15%)", parse_mode="HTML")
        return

    # UX №4: скидка ≥100% обнуляет стоимость заказа — способы оплаты на
    # нулевую сумму не рассчитаны (TON-счёт, минимальный чек карты, юниты
    # Digiseller). Такой код создаём не: юзер упрётся в 0 ₽ на оплате.
    if not (0 < discount_pct < 100):
        err_msg = await message.answer(
            f"{ce('cross')} Скидка должна быть от 1 до 99%.\n"
            f"100% и больше обнуляет стоимость заказа — оплатить его будет невозможно.\n"
            f"<i>Для бесплатных активаций оформляйте заказ вручную через тикет.</i>",
            parse_mode="HTML",
        )
        return
    
    # v17: нечисловой лимит больше НЕ молча превращается в «без лимита»
    # (0 = ∞): админ думал, что ставит 100, а получал бесконечный код.
    max_uses = 0
    if len(parts) > 3:
        if parts[3].isdigit():
            max_uses = int(parts[3])
        else:
            err_msg = await message.answer(
                f"{ce('cross')} МАКС_ИСПОЛЬЗОВАНИЙ должен быть целым числом "
                f"(0 или пропущен — без лимита). Получено: «{safe_html(parts[3])}»",
                parse_mode="HTML",
            )
            return
    # v17: описание — ВСЕ оставшиеся слова (раньше бралось только parts[4],
    # хотя подсказка показывает его в кавычках как единый 4-й аргумент)
    description = " ".join(parts[4:]) if len(parts) > 4 else ""
    
    # First-order only for WELCOME-style codes
    first_order = 1 if discount_pct >= 15 else 0
    
    async with db._get_connection() as conn:
        try:
            await conn.execute(
                """INSERT INTO promo_codes (code, discount_pct, max_uses, per_user_limit, first_order_only, description, active)
                   VALUES (?, ?, ?, 1, ?, ?, 1)""",
                (code, discount_pct, max_uses, first_order, description),
            )
            await conn.commit()
        except Exception as e:
            err_msg = await message.answer(f"{ce('cross')} Промокод {code} уже существует!", parse_mode="HTML")
            return
    
    limit_text = f"до {max_uses} использований" if max_uses else "без лимита"
    first_text = " (только первый заказ)" if first_order else ""
    
    done_msg = await message.answer(
        f"{ce('check')} Промокод <b>{code}</b> создан!\n"
        f"{ce('discount')} Скидка: {discount_pct}%{first_text}\n"
        f"{ce('chart')} Лимит: {limit_text}",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )
    logger.info(f"Admin {message.from_user.id} created promo code {code}")


@router.callback_query(F.data.startswith("promo_toggle_"))
async def admin_toggle_promo(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # v16: навигация гасит FSM-ввод
    """Toggle promo code active/inactive."""

    code = callback.data[len("promo_toggle_"):]
    async with db._get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT active FROM promo_codes WHERE code = ?", (code,)
        )
        row = await cursor.fetchone()
        if not row:
            await safe_answer(callback, "❌ Промокод не найден")
            return
        new_active = 0 if row["active"] else 1
        await conn.execute(
            "UPDATE promo_codes SET active = ? WHERE code = ?",
            (new_active, code),
        )
        await conn.commit()

    status = "включён ✅" if new_active else "выключен ❌"  # callback.answer — plain emoji only
    await safe_answer(callback, f"Промокод {code} {status}")
    # UX №5: остаёмся на ЭКРАНЕ промокода — сообщение РЕДАКТИРУЕТСЯ на месте,
    # новые не плодим. Фолбэк — перерисовать список, если edit не удался.
    promo = await _fetch_promo(code)
    if promo:
        try:
            await callback.message.edit_text(
                _promo_detail_text(promo),
                reply_markup=admin_promo_detail_kb(code, promo["active"]),
                parse_mode="HTML",
            )
            return
        except Exception:
            pass
    await admin_promo_codes_refresh(callback.message)


@router.callback_query(F.data.startswith("promo_del_"))
async def admin_delete_promo(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # v16: навигация гасит FSM-ввод
    """Delete a promo code."""

    code = callback.data[len("promo_del_"):]
    async with db._get_connection() as conn:
        await conn.execute("DELETE FROM promo_uses WHERE promo_code = ?", (code,))
        await conn.execute("DELETE FROM promo_codes WHERE code = ?", (code,))
        await conn.commit()

    await safe_answer(callback, f"Промокод {code} удалён")
    await admin_promo_codes_refresh(callback.message)


async def admin_promo_codes_refresh(message: Message):
    """Refresh promo codes list message (стр. 0; UX №6 — общие хелперы)."""
    promos = await _fetch_promos()
    text, kb = _promo_list_page_data(promos, 0)
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── Promo Detail: экран одного кода + редактирование (UX №5) ────────

async def _fetch_promo(code: str) -> dict | None:
    """Промокод по коду (или None)."""
    async with db._get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM promo_codes WHERE code = ?", (code,))
        row = await cursor.fetchone()
    return dict(row) if row else None


def _promo_detail_text(promo: dict) -> str:
    """Экран одного промокода: все параметры + подсказка, что менять."""
    code = promo.get("code", "")
    active = bool(promo.get("active"))
    pct = promo.get("discount_pct") or 0
    fixed = promo.get("discount_fixed_usdt") or 0
    if pct > 0:
        discount = f"{pct:.0f}%"
    elif fixed > 0:
        discount = f"{fixed:.2f} USDT"
    else:
        discount = "—"
    used = promo.get("used_count", 0) or 0
    max_uses = promo.get("max_uses", 0) or 0
    uses = f"{used} из {max_uses}" if max_uses else f"{used} (без лимита)"
    expires_raw = promo.get("expires_at") or ""
    if expires_raw:
        try:
            expires = datetime.fromisoformat(expires_raw).strftime("%d.%m.%Y")
        except ValueError:
            expires = expires_raw[:10]
        expires_line = f"{ce('calendar')} Срок действия: до <b>{expires}</b>"
    else:
        expires_line = f"{ce('calendar')} Срок действия: <b>бессрочно</b>"
    first_line = "только первый заказ" if promo.get("first_order_only") else "любой заказ"
    status = f"{ce('check')} включён" if active else f"{ce('cross')} выключен"
    text = (
        f"{ce('discount')}  <b>Промокод <code>{safe_html(code)}</code></b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Состояние: {status}\n"
        f"{ce('chart')} Скидка: <b>{discount}</b>\n"
        f"{ce('package')} Использований: <b>{uses}</b>\n"
        f"{expires_line}\n"
        f"{ce('profile')} Применим: {first_line}\n"
    )
    desc = (promo.get("description") or "").strip()
    if desc:
        text += f"{ce('info')} Описание: {safe_html(desc)}\n"
    text += f"\n<i>{ce('eyes')} Выберите, что изменить:</i>"
    return text


async def _rerender_promo_detail(bot, chat_id, message_id, code: str, note: str = "") -> None:
    """Перерисовать экран промокода РЕДАКТИРОВАНИЕМ (UX №5).

    Если исходное сообщение не отредактировать (старше 48 ч и т.п.) —
    отправляем новый экран, чтобы админ всё равно увидел результат.
    """
    promo = await _fetch_promo(code)
    if not promo:
        return
    text = f"{note}{_promo_detail_text(promo)}" if note else _promo_detail_text(promo)
    kb = admin_promo_detail_kb(code, promo["active"])
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id,
            reply_markup=kb, parse_mode="HTML",
        )
        return
    except Exception:
        pass
    try:
        await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


def parse_discount_pct_input(raw: str) -> float | None:
    """Скидка в процентах: только 1..99 (≥100% даёт 0 ₽ — UX №4)."""
    try:
        val = float(str(raw).strip().replace("%", "").replace(",", "."))
    except (ValueError, TypeError):
        return None
    if not (0 < val < 100):
        return None
    return round(val, 2)


def parse_max_uses_input(raw: str) -> int | None:
    """Лимит использований: целое ≥0 (0 — без лимита)."""
    try:
        val = int(str(raw).strip())
    except (ValueError, TypeError):
        return None
    return val if val >= 0 else None


def parse_expiry_input(raw: str) -> tuple[str | None, str | None]:
    """Срок действия: дата ДД.ММ.ГГГГ → ISO, «нет» → None (убрать срок).

    Возвращает (expires_at_iso или None, текст ошибки или None).
    """
    raw = (raw or "").strip().lower()
    if raw in ("нет", "неограничен", "бессрочно", "убрать", "-"):
        return None, None
    try:
        d = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        return None, "Введите дату в формате ДД.ММ.ГГГГ (например 01.10.2026) или слово «нет»."
    return d.strftime("%Y-%m-%d") + " 23:59:59", None


@router.callback_query(F.data.startswith("promodet_"))
async def admin_promo_detail_screen(callback: CallbackQuery, state: FSMContext):
    """Экран одного промокода — открывается ИЗ СПИСКА редактированием."""
    code = callback.data[len("promodet_"):]
    await state.clear()
    promo = await _fetch_promo(code)
    if not promo:
        await safe_answer(callback, "Промокод не найден")
        return
    try:
        await callback.message.edit_text(
            _promo_detail_text(promo),
            reply_markup=admin_promo_detail_kb(code, promo["active"]),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            _promo_detail_text(promo),
            reply_markup=admin_promo_detail_kb(code, promo["active"]),
            parse_mode="HTML",
        )
    await safe_answer(callback)


@router.callback_query(F.data == "promolist")
async def admin_promo_back_to_list(callback: CallbackQuery, state: FSMContext):
    """«Назад к списку» — тем же сообщением, без новых."""
    await state.clear()
    await admin_promo_codes_refresh(callback.message)
    await safe_answer(callback)


@router.callback_query(F.data.startswith("promodask_"))
async def admin_promo_delete_ask(callback: CallbackQuery, state: FSMContext):
    await state.clear()  # v16: навигация гасит FSM-ввод
    """Ступень подтверждения удаления промокода (та же схема, что у планов)."""
    code = callback.data[len("promodask_"):]
    promo = await _fetch_promo(code)
    if not promo:
        await safe_answer(callback, "Промокод не найден")
        return
    try:
        await callback.message.edit_text(
            f"{ce('warning')}  <b>Удалить промокод <code>{safe_html(code)}</code>?</b>\n\n"
            f"Код и история его использований будут удалены безвозвратно.",
            reply_markup=admin_promo_delask_kb(code),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await safe_answer(callback)


@router.callback_query(F.data.startswith("promodisc_"))
async def admin_promo_discount_start(callback: CallbackQuery, state: FSMContext):
    """Правка скидки — старт FSM."""
    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    code = callback.data[len("promodisc_"):]
    promo = await _fetch_promo(code)
    if not promo:
        await safe_answer(callback, "Промокод не найден")
        return
    await state.set_state(EditPromo.entering_discount)
    await state.update_data(
        promo_edit_code=code,
        detail_chat_id=callback.message.chat.id,
        detail_msg_id=callback.message.message_id,
    )
    pct = promo.get("discount_pct") or 0
    fixed = promo.get("discount_fixed_usdt") or 0
    cur = f"{pct:.0f}%" if pct > 0 else (f"{fixed:.2f} USDT" if fixed else "—")
    await callback.message.answer(
        f"{ce('discount')}  Промокод <code>{safe_html(code)}</code> — текущая скидка: <b>{cur}</b>\n\n"
        f"Введите новую скидку в процентах (от 1 до 99).\n"
        f"<i>100% и больше запрещено: заказ с нулевой стоимостью невозможно оплатить.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditPromo.entering_discount, F.text.contains("Отмена"))
async def admin_promo_discount_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    code = data.get("promo_edit_code", "")
    if code and data.get("detail_chat_id") and data.get("detail_msg_id"):
        await _rerender_promo_detail(message.bot, data["detail_chat_id"], data["detail_msg_id"], code)
        return
    await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditPromo.entering_discount)
async def admin_promo_discount_save(message: Message, state: FSMContext):
    val = parse_discount_pct_input(message.text or "")
    if val is None:
        await message.answer(
            f"{ce('cross')} Введите число от 1 до 99 (скидка ≥100% запрещена — заказ станет бесплатным):",
            parse_mode="HTML",
        )
        return
    data = await state.get_data()
    await state.clear()
    code = data.get("promo_edit_code", "")
    if not code:
        await message.answer(f"{ce('cross')} Контекст потерян. Откройте «Промокоды» заново.", reply_markup=admin_menu_kb())
        return
    async with db._get_connection() as conn:
        await conn.execute(
            "UPDATE promo_codes SET discount_pct = ?, discount_fixed_usdt = 0 WHERE code = ?",
            (val, code),
        )
        await conn.commit()
    logger.info(f"Admin {message.from_user.id} set promo {code} discount to {val}%")
    await _rerender_promo_detail(
        message.bot, data.get("detail_chat_id"), data.get("detail_msg_id"), code,
        note=f"{ce('check')} Скидка обновлена: <b>{val:.0f}%</b>\n\n",
    )


@router.callback_query(F.data.startswith("promolim_"))
async def admin_promo_limit_start(callback: CallbackQuery, state: FSMContext):
    """Правка лимита использований — старт FSM."""
    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    code = callback.data[len("promolim_"):]
    promo = await _fetch_promo(code)
    if not promo:
        await safe_answer(callback, "Промокод не найден")
        return
    await state.set_state(EditPromo.entering_max_uses)
    await state.update_data(
        promo_edit_code=code,
        detail_chat_id=callback.message.chat.id,
        detail_msg_id=callback.message.message_id,
    )
    max_uses = promo.get("max_uses", 0) or 0
    cur = f"{max_uses}" if max_uses else "без лимита"
    await callback.message.answer(
        f"{ce('chart')}  Промокод <code>{safe_html(code)}</code> — текущий лимит: <b>{cur}</b>\n\n"
        f"Введите новый лимит (целое число, 0 — без лимита):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditPromo.entering_max_uses, F.text.contains("Отмена"))
async def admin_promo_limit_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    code = data.get("promo_edit_code", "")
    if code and data.get("detail_chat_id") and data.get("detail_msg_id"):
        await _rerender_promo_detail(message.bot, data["detail_chat_id"], data["detail_msg_id"], code)
        return
    await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditPromo.entering_max_uses)
async def admin_promo_limit_save(message: Message, state: FSMContext):
    val = parse_max_uses_input(message.text or "")
    if val is None:
        await message.answer(
            f"{ce('cross')} Введите целое число ≥ 0 (0 — без лимита):",
            parse_mode="HTML",
        )
        return
    data = await state.get_data()
    await state.clear()
    code = data.get("promo_edit_code", "")
    if not code:
        await message.answer(f"{ce('cross')} Контекст потерян. Откройте «Промокоды» заново.", reply_markup=admin_menu_kb())
        return
    async with db._get_connection() as conn:
        await conn.execute("UPDATE promo_codes SET max_uses = ? WHERE code = ?", (val, code))
        await conn.commit()
    logger.info(f"Admin {message.from_user.id} set promo {code} max_uses to {val}")
    lim = f"{val}" if val else "без лимита"
    await _rerender_promo_detail(
        message.bot, data.get("detail_chat_id"), data.get("detail_msg_id"), code,
        note=f"{ce('check')} Лимит обновлён: <b>{lim}</b>\n\n",
    )


@router.callback_query(F.data.startswith("promoexp_"))
async def admin_promo_expiry_start(callback: CallbackQuery, state: FSMContext):
    """Правка срока действия — старт FSM."""
    await state.clear()  # v16: старт флоу — чистим устаревшие данные
    code = callback.data[len("promoexp_"):]
    promo = await _fetch_promo(code)
    if not promo:
        await safe_answer(callback, "Промокод не найден")
        return
    await state.set_state(EditPromo.entering_expires)
    await state.update_data(
        promo_edit_code=code,
        detail_chat_id=callback.message.chat.id,
        detail_msg_id=callback.message.message_id,
    )
    expires_raw = promo.get("expires_at") or ""
    if expires_raw:
        try:
            cur = datetime.fromisoformat(expires_raw).strftime("%d.%m.%Y")
        except ValueError:
            cur = expires_raw[:10]
    else:
        cur = "бессрочно"
    await callback.message.answer(
        f"{ce('calendar')}  Промокод <code>{safe_html(code)}</code> — текущий срок: <b>{cur}</b>\n\n"
        f"Введите новую дату в формате ДД.ММ.ГГГГ\n"
        f"или слово «нет», чтобы сделать код бессрочным:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.message(EditPromo.entering_expires, F.text.contains("Отмена"))
async def admin_promo_expiry_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    code = data.get("promo_edit_code", "")
    if code and data.get("detail_chat_id") and data.get("detail_msg_id"):
        await _rerender_promo_detail(message.bot, data["detail_chat_id"], data["detail_msg_id"], code)
        return
    await message.answer("Отменено.", reply_markup=admin_menu_kb())


@router.message(EditPromo.entering_expires)
async def admin_promo_expiry_save(message: Message, state: FSMContext):
    expires_iso, err = parse_expiry_input(message.text or "")
    if err:
        await message.answer(f"{ce('cross')} {err}", parse_mode="HTML")
        return
    data = await state.get_data()
    await state.clear()
    code = data.get("promo_edit_code", "")
    if not code:
        await message.answer(f"{ce('cross')} Контекст потерян. Откройте «Промокоды» заново.", reply_markup=admin_menu_kb())
        return
    async with db._get_connection() as conn:
        await conn.execute(
            "UPDATE promo_codes SET expires_at = ? WHERE code = ?",
            (expires_iso, code),
        )
        await conn.commit()
    if expires_iso:
        shown = expires_iso[:10]
        logger.info(f"Admin {message.from_user.id} set promo {code} expiry to {shown}")
        note = f"{ce('check')} Срок обновлён: до <b>{shown}</b>\n\n"
    else:
        logger.info(f"Admin {message.from_user.id} removed promo {code} expiry")
        note = f"{ce('check')} Срок снят — промокод теперь <b>бессрочный</b>\n\n"
    await _rerender_promo_detail(
        message.bot, data.get("detail_chat_id"), data.get("detail_msg_id"), code, note=note,
    )


# ─── Referral Stats ──────────────────────────────────────────────────

@router.message(StateFilter("*"), F.text.contains("Рефералы"))
async def admin_referrals(message: Message, state: FSMContext):
    await state.clear()
    
    async with db._get_connection() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """SELECT r.referrer_id, r.referral_code, r.referral_count, r.bonus_earned, u.first_name, u.username
               FROM referrals r
               LEFT JOIN users u ON r.referrer_id = u.user_id
               ORDER BY r.referral_count DESC LIMIT 10"""
        )
        refs = await cursor.fetchall()
    
    text = f"{ce('link')}  <b>Реферальная программа</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if refs:
        for r in refs:
            name = r["first_name"] or r["username"] or f"ID:{r['referrer_id']}"
            text += f"  {ce('profile')} <b>{name}</b> — код: <code>{r['referral_code']}</code>\n"
            text += f"     Пригласил: {r['referral_count']} чел. | Бонус: {r['bonus_earned']:.2f}$\n"
    else:
        text += "Рефералов пока нет.\n"
    
    # Your own referral code
    my_code = await generate_referral_code(message.from_user.id)
    bot_username = await get_bot_username(message.bot)
    text += f"\n{ce('referral_badge')} Ваш реферальный код: <code>{my_code}</code>\n"
    text += f"Ссылка: <code>https://t.me/{bot_username}?start=ref_{my_code}</code>\n"
    
    ref_msg = await message.answer(text, reply_markup=admin_menu_kb(), parse_mode="HTML")
