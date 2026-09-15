import json
import logging
import math
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, PreCheckoutQuery, LabeledPrice, BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from models.database import db, get_service_by_id, get_plan_from_service, get_active_services, try_transition_order_status
from keyboards.keyboards import (
    main_menu_kb,
    admin_menu_kb,
    cancel_kb,
    cancel_inline_kb,
    catalog_kb,
    empty_catalog_kb,
    service_plans_kb,
    payment_method_kb,
    ton_payment_kb,
    usdt_payment_kb,
    user_orders_kb,
    order_detail_kb,
    promo_code_kb,
    back_kb,
)
from config import (
    ADMIN_IDS, CURRENCY, CURRENCY_SYMBOL,
    ENABLED_PAYMENT_METHODS,
    SHOW_RUB_EQUIVALENT, TON_WALLET_ADDRESS, PROMO_CODE_ENABLED,
    STARS_PER_USDT, USDT_RUB_RATE, REFERRAL_BONUS_USDT,
)
from emojis import ce
from utils.html_utils import safe_html, safe_code
from utils.safe_callback import safe_answer
from utils import ephemeral as eph  # Task 35: растворение переходных сообщений (группы D/E)
from utils.payment_ux import (
    remember_screen, extinguish_for_callback,
    check_cooldown_left, note_check_press,
)
from utils.order_status import effective_status, subscription_note, parse_db_dt
from services.ton_payments import (
    usdt_to_ton, usdt_to_rub, get_ton_usd_rate,
    verify_payment, format_price_rub, get_deposit_address,
    generate_qr_code, generate_memo,
)
from services.marketing import (
    get_social_proof_line, get_service_popularity, get_best_value_badge,
    validate_promo_code, calculate_discounted_price, apply_promo_code,
    generate_referral_code, process_referral, is_referred_user,
    is_referral_bonus_paid, mark_referral_bonus_paid,
    get_welcome_discount, mark_welcome_discount_used,
    is_new_user,
    award_loyalty_points, get_loyalty_balance,
    award_referral_bonus, get_bonus_balance, get_referral_stats,
    calc_bonus_use, get_bot_username,
)

logger = logging.getLogger(__name__)
router = Router()

# Окно оплаты для КАРТОЧНЫХ заказов (Digiseller / Tribute), минут.
# Раньше карточным заказам окно не выставлялось вообще — брошенный заказ
# висел «Ожидает оплату» вечно, cleanup его не отменял (фильтр
# get_expired_pending_orders берёт только payment_expires_at IS NOT NULL),
# а фоновый поллер гонял его проверками бесконечно (UX №1).
# 60 минут вместо 30: оплата картой занимает больше времени, чем крипта.
CARD_PAYMENT_WINDOW_MINUTES = 60

# UX №6: заказов на странице «Заказы» и потолок выборки на юзера
# (список без лимита ломал сообщение о превышении 4096 символов;
# 5 × ~3 строки на карточку ≈ безопасно, 200 заказов = 40 страниц).
ORDERS_PER_PAGE = 5
ORDERS_FETCH_CAP = 200


# ─── Helpers ───────────────────────────────────────────────────────

async def _post_payment_actions(order: dict, bot, state: FSMContext = None):
    """Run post-payment marketing actions: loyalty, referral, welcome discount.
    
    Call this after ANY successful payment confirmation.
    """
    user_id = order["user_id"]
    order_id = order["order_id"]
    price_usdt = order.get("price_usdt", 0)
    
    try:
        # 1. Award loyalty points
        await award_loyalty_points(user_id, order_id, price_usdt)
    except Exception as e:
        logger.warning(f"Loyalty award failed for order #{order_id}: {e}")
    
    try:
        # 2. Referral bonus — ЗА ПЕРВУЮ оплату приглашённого (v17).
        # Раньше начислялся на КАЖДУЮ оплату реферала: тратимый бонус
        # фармился бесконечно. Флаг referral_bonus_paid в user_meta
        # гарантирует ровно одну выплату (идемпотентен и после рестарта).
        # v17-КРИТИЧНО: раньше SQL запрашивал несуществующую колонку
        # referrer_id (в user_meta она называется referred_by) — запрос
        # падал, исключение молча глоталось, и бонус НЕ ВЫПЛАЧИВАЛСЯ ВООБЩЕ
        # ни на одном пути подтверждения. Обещание «получи бонус за друга»
        # было сломано всегда; теперь колонка правильная.
        if await is_referred_user(user_id) and not await is_referral_bonus_paid(user_id):
            async with db._get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT referred_by FROM user_meta WHERE user_id = ?",
                    (user_id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    await mark_referral_bonus_paid(user_id)
                    await award_referral_bonus(row[0], bot)
    except Exception as e:
        logger.warning(f"Referral bonus failed for order #{order_id}: {e}")
    
    try:
        # 3. Mark welcome discount as used (if was applied)
        if order.get("discount_pct", 0) > 0:
            await mark_welcome_discount_used(user_id)
    except Exception as e:
        logger.warning(f"Welcome discount mark failed for order #{order_id}: {e}")
    
    try:
        # 4. Record promo code usage (if promo was applied to this order)
        promo_code = order.get("promo_code", "")
        if promo_code:
            await apply_promo_code(promo_code, user_id, order_id)
            logger.info(f"Promo code {promo_code} usage recorded for order #{order_id}")
    except Exception as e:
        logger.warning(f"Promo code usage record failed for order #{order_id}: {e}")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_menu(user_id: int):
    return admin_menu_kb() if is_admin(user_id) else main_menu_kb()


STATUS_NAMES = {
    "pending_payment": f"{ce('credit_card')} Ожидает оплату",
    "pending_account": f"{ce('clock')} Ожидает данные аккаунта",
    "pending_activation": f"{ce('clock')} Ожидает активации",
    "active": f"{ce('check')} Подписка активна",
    "cancelled": f"{ce('cross')} Отменён",
    "failed": f"{ce('warning')} Ошибка активации",
    # Виртуальные статусы — вычисляются на момент показа (utils.order_status),
    # статус в БД не меняется (на него завязана drip-рассылка)
    "expired": f"{ce('clock')} Подписка закончилась",
    "payment_over": f"{ce('clock')} Время оплаты истекло",
}

STATUS_ICON = {
    "pending_payment": ce('credit_card'),
    "pending_account": ce('pencil'),
    "pending_activation": ce('clock'),
    "active": ce('sparkle'),
    "cancelled": ce('cross'),
    "failed": ce('warning'),
    "expired": ce('clock'),
    "payment_over": ce('clock'),
}


# ─── Menu button texts (for FSM escape detection) ─────────────────

MENU_BUTTONS = {"Каталог", "Заказы", "Поддержка", "Помощь", "Обращения", "Новые заказы", "Статистика", "Ред. каталог", "Рассылка", "Промокоды", "Рефералы", "Отмена"}


async def _escape_if_menu(message: Message, state: FSMContext) -> bool:
    """If message text matches a menu button, clear FSM and return True.
    
    This prevents FSM catch-all handlers from swallowing menu button presses.
    When a user is in OrderFlow/SupportFlow and presses a menu button,
    we want to exit the flow and let the menu handler process it.
    """
    if message.text and message.text.strip() in MENU_BUTTONS:
        await state.clear()
        # Don't answer here — let the menu handler process it
        return True
    return False


# ─── FSM States ────────────────────────────────────────────────────

class OrderFlow(StatesGroup):
    selecting_plan = State()
    waiting_for_card_email = State()  # Ввод email перед оплатой картой через Digiseller
    waiting_for_account_fields = State()
    entering_promo_code = State()


class SupportFlow(StatesGroup):
    # DEPRECATED: replaced by the ticket system in handlers/support_handlers.py.
    # Kept only so old FSM references don't break; not used by handlers anymore.
    waiting_for_message = State()


# ─── UX №8: возврат к неоплаченному заказу на /start ──────────────

async def _find_resumable_order(user_id: int) -> dict | None:
    """Самый свежий заказ юзера, который ещё МОЖНО оплатить.

    Условия: статус pending_payment И окно оплаты не истекло
    (эффективный статус — не payment_over; grace 10 мин тот же, что у
    cleanup). Заказы без выбранного способа оплаты
    (payment_expires_at IS NULL) тоже подходят — окно на них ещё
    не тикало. get_user_orders сортирует по created_at DESC — берём
    первый подходящий, карточка на /start всегда одна (без спама).
    """
    try:
        orders = await db.get_user_orders(user_id, limit=20)
    except Exception:
        return None
    for order in orders:
        if order.get("status") != "pending_payment":
            continue
        if effective_status(order) == "payment_over":
            continue  # окно истекло — cleanup отменит заказ в течение grace
        return order
    return None


def _unpaid_window_line(order: dict) -> str:
    """Строка про окно оплаты для карточки неоплаченного заказа."""
    expires = parse_db_dt(order.get("payment_expires_at"))
    if not expires:
        return f"{ce('clock')}  Способ оплаты ещё не выбран — окно не тикает"
    left_min = int((expires - datetime.utcnow()).total_seconds() // 60)
    if left_min <= 0:
        # Внутри grace-периода (10 мин): cleanup вот-вот отменит заказ
        return f"{ce('clock')}  Окно оплаты почти истекло — успейте оплатить"
    if left_min >= 60:
        return f"{ce('clock')}  Окно оплаты: осталось ~{left_min // 60} ч {left_min % 60} мин"
    return f"{ce('clock')}  Окно оплаты: осталось ~{left_min} мин"


async def _send_unpaid_order_card(message: Message, state: FSMContext) -> None:
    """Карточка «У вас есть неоплаченный заказ» ПЕРЕД приветствием /start.

    Пользователь, потерявший экран оплаты (закрыл чат, дедуп стёр старую
    копию, перезапустил бота), больше не должен догадываться идти в
    «Заказы»: /start сам возвращает его в воронку. Карточка — одна,
    для самого свежего оплачиваемого заказа (см. _find_resumable_order).
    Кнопка resumepay_ переоткрывает экран выбора способа оплаты.
    """
    order = await _find_resumable_order(message.from_user.id)
    if not order:
        return

    price_usdt = order.get("price_usdt", 0)
    rub_rate = 0
    try:
        rub_rate = await usdt_to_rub(1)
    except Exception:
        pass
    if rub_rate > 0:
        price_str = f"{(price_usdt * rub_rate):.0f} ₽ (~{price_usdt:.2f} USDT)"
    else:
        price_str = f"{price_usdt:.2f} USDT"

    text = (
        f"{ce('warning')}  <b>У вас есть неоплаченный заказ</b>\n\n"
        f"{ce('package')}  Заказ <b>#{order['order_id']}</b>: "
        f"{safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
        f"{ce('wallet')}  К оплате: <b>{price_str}</b>\n"
        f"{_unpaid_window_line(order)}\n\n"
        f"<i>Продолжите оплату — заказ уже зарезервирован за вами.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="💳 Продолжить оплату",
            callback_data=f"resumepay_{order['order_id']}",
        )
    ]])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── /start ────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    # Parse deep link payload
    payload = ""
    if message.text and len(message.text.split()) > 1:
        payload = message.text.split(maxsplit=1)[1].strip()

    # ── Referral processing ──
    if payload.startswith("ref_"):
        await process_referral(payload[4:], message.from_user.id)

    # ── Deep link to specific service ──
    deep_link = None
    if payload.startswith("svc_"):
        deep_link = payload[4:]

    if deep_link:
        service = get_service_by_id(deep_link)
        if service and service.get("active", True):
            # DIRECT LINK TO SERVICE — warm up + show plans
            welcome = await get_welcome_discount(message.from_user.id)
            await state.update_data(service_id=deep_link)
            rub_rate = 0
            try:
                rub_rate = await usdt_to_rub(1)
            except Exception:
                pass

            plans_text = ""
            for p in service["plans"]:
                price_usdt = p.get("price_usdt", 0)
                badge = get_best_value_badge(p, service["plans"])
                badge_str = f"  {badge}" if badge else ""
                if rub_rate > 0:
                    price_rub = price_usdt * rub_rate
                    per_day = price_rub / p["duration_days"]
                    plans_text += f"  {ce('best_price')}   {p['name']} — <b>{price_rub:.0f} ₽</b> (~{per_day:.0f} ₽/дн.){badge_str}\n"
                else:
                    per_day = round(price_usdt / p["duration_days"], 2)
                    plans_text += f"  {ce('best_price')}   {p['name']} — <b>{price_usdt:.2f} USDT</b>{badge_str}\n"

            social_proof = get_social_proof_line()
            text = (
                f"{service_emoji_html(service)}  <b>{service['name']}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{service['description']}\n\n"
            )
            # Welcome discount (urgency!)
            if welcome:
                timer_text = f"{ce('clock')} Скидка {welcome['discount_pct']}% действует {welcome['timer_hours']} часа!"
                text += f"{welcome['label']}\n{timer_text}\n\n"
                await state.update_data(welcome_discount=welcome)

            text += (
                f"<b>Тарифы:</b>\n{plans_text}\n"
                f"{social_proof}\n\n"
                f"<i>{ce('fire')}  Выберите срок подписки:</i>"
            )
            kb = await service_plans_kb(deep_link)
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

    # ── UX №8: неоплаченный заказ с живым окном — вернём в воронку.
    # Карточка отправляется ПЕРЕД приветствием: это самое actionable.
    # (В deep-link ветке не показываем — не мешаем продающему сценарию.)
    try:
        await _send_unpaid_order_card(message, state)
    except Exception as e:
        logger.warning(f"Unpaid order card skipped: {e}")

    # ── STANDARD /start — Warm-up sequence for cold traffic ──
    name = safe_html(message.from_user.first_name or message.from_user.username or "друг")
    is_new = await is_new_user(message.from_user.id)
    welcome = await get_welcome_discount(message.from_user.id) if is_new else None
    social_proof = get_social_proof_line()
    
    # Build warm welcome that pushes to purchase
    # Dynamically list services from catalog.json (not hardcoded!)
    active_services = get_active_services()
    service_lines = ""
    for s in active_services[:5]:  # Show max 5 to keep message compact
        s_emoji = s.get("custom_emoji_id") or s.get("emoji", "📦")
        if s.get("custom_emoji_id"):
            # Telegram HTML format for custom emoji — same as ce() in emojis.py
            s_emoji = f'<tg-emoji emoji-id="{s["custom_emoji_id"]}">{s.get("emoji", "📦")}</tg-emoji>'
        else:
            s_emoji = s.get("emoji", "📦")
        # Short tagline from first part of description
        desc = s.get("description", "")
        tagline = desc.split(".")[0].strip() if desc else s["name"]
        if len(tagline) > 40:
            tagline = tagline[:37] + "..."
        service_lines += f"   {s_emoji}  {tagline}\n"
    
    text = (
        f"{ce('wave')}  <b>{name}, добро пожаловать!</b>\n\n"
        f"{ce('rocket')}  Активируем <b>премиум-подписки</b> на ваших аккаунтах:\n"
        f"{service_lines}\n"
    )
    
    # Social proof
    text += f"{social_proof}\n\n"
    
    # Welcome discount (URGENCY for Yandex Direct traffic)
    if welcome:
        text += (
            f"{welcome['label']}\n"
            f"{ce('clock')} Скидка <b>{welcome['discount_pct']}%</b> на первый заказ — "
            f"действует {welcome['timer_hours']} часа!\n\n"
        )
        await state.update_data(welcome_discount=welcome)
    
    # Push to catalog
    text += (
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('shopping')}  <b>Каталог</b> — выберите подписку\n"
        f"{ce('package')}  <b>Заказы</b> — статус заказов\n"
        f"{ce('speech')}  <b>Поддержка</b> — связь с нами\n\n"
        f"<i>{ce('fire')}  Цены от 99₽/мес — жмите «Каталог»!</i>"
    )
    await message.answer(text, reply_markup=get_menu(message.from_user.id), parse_mode="HTML")


def service_emoji_html(s: dict) -> str:
    """
    Эмодзи сервиса для HTML-сообщений (каталог, /start, экран тарифов).

    Если у сервиса в catalog.json задан custom_emoji_id — рендерит
    кастомный эмодзи через <tg-emoji> (анимированный для Premium-юзеров,
    обычный fallback для остальных). Иначе возвращает Unicode-эмодзи
    из catalog.json, а если и его нет — заглушку 📦.
    """
    fallback = s.get("emoji") or ce("package")
    custom_id = s.get("custom_emoji_id") or ""
    if custom_id:
        return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'
    return fallback


# ─── Catalog ───────────────────────────────────────────────────────

@router.message(StateFilter("*"), F.text.contains("Каталог"))
@router.message(Command("catalog"))
async def cmd_catalog(message: Message, state: FSMContext):
    await state.clear()
    social_proof = get_social_proof_line()
    
    # Get services with popularity badges
    from models.database import get_active_services
    services = get_active_services()

    # UX №4: пустой каталог — не пустой экран без кнопок, а честная
    # заглушка с кнопкой поддержки
    if not services:
        await message.answer(
            f"{ce('shopping')}  <b>Каталог временно обновляется</b>\n\n"
            f"Сейчас мы готовим для вас новые предложения.\n"
            f"Пожалуйста, загляните чуть позже — или напишите нам,\n"
            f"оформим подписку вручную.",
            reply_markup=empty_catalog_kb(),
            parse_mode="HTML",
        )
        return

    catalog_text = ""
    for s in services:
        badge = get_service_popularity(s["name"])
        badge_str = f"  {badge}" if badge else ""
        catalog_text += f"  {service_emoji_html(s)}  {s['name']}{badge_str}\n"
    
    # Check for welcome discount
    is_new = await is_new_user(message.from_user.id)
    welcome = await get_welcome_discount(message.from_user.id) if is_new else None
    
    text = (
        f"{ce('shopping')}  <b>Каталог подписок</b>\n\n"
    )
    if welcome:
        text += f"{welcome['label']}\n{ce('clock')} Скидка {welcome['discount_pct']}% — действует {welcome['timer_hours']} часа!\n\n"
        await state.update_data(welcome_discount=welcome)
    
    text += (
        f"{catalog_text}\n"
        f"{social_proof}\n\n"
        f"<i>{ce('eyes')}  Нажмите на сервис, чтобы выбрать тариф:</i>"
    )
    await message.answer(text, reply_markup=catalog_kb(), parse_mode="HTML")


@router.callback_query(F.data == "back_catalog")
async def back_to_catalog(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    services = get_active_services()
    # UX №4: пустой каталог — та же заглушка, что и в /catalog
    if not services:
        await callback.message.edit_text(
            f"{ce('shopping')}  <b>Каталог временно обновляется</b>\n\n"
            f"Сейчас мы готовим для вас новые предложения.\n"
            f"Пожалуйста, загляните чуть позже — или напишите нам,\n"
            f"оформим подписку вручную.",
            reply_markup=empty_catalog_kb(),
            parse_mode="HTML",
        )
        await safe_answer(callback)
        return
    text = (
        f"{ce('shopping')}  <b>Каталог подписок</b>\n\n"
        f"<i>{ce('eyes')}  Выберите сервис для активации:</i>"
    )
    await callback.message.edit_text(text, reply_markup=catalog_kb(), parse_mode="HTML")
    await safe_answer(callback)


@router.callback_query(F.data.startswith("svc_"))
async def select_service(callback: CallbackQuery, state: FSMContext):
    service_id = callback.data[4:]  # len("svc_") = 4
    service = get_service_by_id(service_id)
    if not service:
        await safe_answer(callback, "Сервис не найден")
        return

    await state.update_data(service_id=service_id)

    svc_emoji_html = service_emoji_html(service)

    # Get live RUB rate
    rub_rate = 0
    try:
        rub_rate = await usdt_to_rub(1)
    except Exception:
        pass

    plans_text = ""
    for p in service["plans"]:
        price_usdt = p.get("price_usdt", 0)
        badge = get_best_value_badge(p, service["plans"])
        badge_str = f"  {badge}" if badge else ""
        if rub_rate > 0:
            price_rub = price_usdt * rub_rate
            per_day = price_rub / p["duration_days"]
            plans_text += f"  {ce('best_price')}   {p['name']} — <b>{price_rub:.0f} ₽</b> (~{per_day:.0f} ₽/дн.){badge_str}\n"
        else:
            per_day = round(price_usdt / p["duration_days"], 2)
            plans_text += f"  {ce('best_price')}   {p['name']} — <b>{price_usdt:.2f} USDT</b> (~{per_day} USDT/дн.){badge_str}\n"

    social_proof = get_social_proof_line()
    
    # Check for welcome discount
    is_new = await is_new_user(callback.from_user.id)
    welcome = await get_welcome_discount(callback.from_user.id) if is_new else None

    text = (
        f"{svc_emoji_html}  <b>{service['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{service['description']}\n\n"
    )
    if welcome:
        text += f"{welcome['label']}\n{ce('clock')} Скидка {welcome['discount_pct']}% — действует {welcome['timer_hours']} часа!\n\n"
        await state.update_data(welcome_discount=welcome)

    text += (
        f"<b>Тарифы:</b>\n{plans_text}\n"
        f"{social_proof}\n\n"
        f"<i>{ce('eyes')}  Выберите срок подписки:</i>"
    )
    if rub_rate > 0:
        text += f"\n\n{ce('chart')}  <i>Цены в ₽, привязаны к USDT. Курс обновляется автоматически.</i>"

    kb = await service_plans_kb(service_id)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await safe_answer(callback)


@router.callback_query(F.data.startswith("plan_"))
async def select_plan(callback: CallbackQuery, state: FSMContext):
    # Remove ONLY the "plan_" prefix (not all occurrences)
    payload = callback.data[5:]  # len("plan_") = 5
    parts = payload.split(":", 1)
    service_id = parts[0]
    plan_id = parts[1] if len(parts) > 1 else ""

    logger.info(f"select_plan: callback_data={callback.data}, service_id={service_id}, plan_id={plan_id}")

    service = get_service_by_id(service_id)
    if not service:
        logger.warning(f"select_plan: Service not found for id={service_id}, callback_data={callback.data}")
        await safe_answer(callback, "Сервис не найден")
        return

    plan = get_plan_from_service(service, plan_id)
    if not plan:
        available = [p['id'] for p in service.get('plans', [])]
        logger.error(
            f"select_plan: Plan not found! service_id={service_id}, plan_id={plan_id}, "
            f"callback_data={callback.data}, available_plans={available}, "
            f"plans_count={len(service.get('plans', []))}"
        )
        # Re-read catalog to double-check (maybe file was just updated)
        service2 = get_service_by_id(service_id)
        plan2 = get_plan_from_service(service2, plan_id) if service2 else None
        if plan2:
            plan = plan2
            logger.info(f"select_plan: Plan found on second read — race condition resolved")
        else:
            await safe_answer(callback, "План не найден. Попробуйте зайти в каталог заново.")
            return

    await state.update_data(service_id=service_id, plan_id=plan_id)

    price_usdt = plan.get("price_usdt", 0)
    original_price_usdt = price_usdt
    
    # ── Apply welcome discount if available ──
    state_data = await state.get_data()
    welcome = state_data.get("welcome_discount")
    discount_pct = 0
    discount_text = ""
    
    if welcome and welcome.get("discount_pct", 0) > 0:
        discount_pct = welcome["discount_pct"]
        price_usdt = round(original_price_usdt * (1 - discount_pct / 100), 2)
        discount_text = f"{ce('discount')}  Скидка {discount_pct}% на первый заказ!\n"
        discount_text += f"   {ce('cross')}  ~~{original_price_usdt:.2f} USDT~~  →  {ce('check')}  <b>{price_usdt:.2f} USDT</b>\n"
    
    # ── Apply referral bonus (тратимый счёт за друзей) ──
    # Бонус уменьшает цену заказа — все способы оплаты (TON, Tribute,
    # Digiseller) берут сумму из order.price_usdt, поэтому скидка
    # автоматически попадает в любой платёж. Списание с баланса — при
    # подтверждении оплаты (Database._debit_order_bonus).
    # v17: учитываем РЕЗЕРВ бонуса живыми заказами — раньше каждый новый
    # заказ резервировал ВЕСЬ баланс: два заказа на один баланс давали
    # двойную скидку (оба оплачивались, баланс уходил в 0 дважды).
    bonus_applied = 0.0
    try:
        _balance = await get_bonus_balance(callback.from_user.id)
        _reserved = await db.get_pending_bonus_reserved(callback.from_user.id)
        bonus_applied = calc_bonus_use(max(0.0, _balance - _reserved), price_usdt)
    except Exception as e:
        logger.warning(f"Referral bonus calc failed for user {callback.from_user.id}: {e}")
    if bonus_applied > 0:
        price_usdt = round(price_usdt - bonus_applied, 2)
    bonus_text = ""
    if bonus_applied > 0:
        bonus_text = (
            f"{ce('referral_badge')}  Бонус за друзей: <b>−{bonus_applied:.2f} USDT</b>\n"
        )
    
    # Get live RUB price (after discount)
    rub_price = 0
    try:
        rub_price = await usdt_to_rub(price_usdt)
    except Exception:
        pass

    # Create order (with discount info)
    order_id = await db.create_order(
        user_id=callback.from_user.id,
        service_id=service_id,
        service_name=service["name"],
        plan_id=plan_id,
        plan_name=plan["name"],
        duration_days=plan["duration_days"],
        price_usdt=price_usdt,
        currency="USDT",
        bonus_applied=bonus_applied,
    )

    await state.update_data(order_id=order_id, original_price_usdt=original_price_usdt, discount_pct=discount_pct)

    # Format price in RUB primary (with discount if applied)
    if rub_price > 0:
        per_day_rub = rub_price / plan["duration_days"]
        original_rub = 0
        if original_price_usdt != price_usdt:
            try:
                original_rub = await usdt_to_rub(original_price_usdt)
            except Exception:
                pass
        
        text = (
            f"{ce('ticket')}   <b>Оформление заказа #{order_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{ce('shopping')}  <b>{service['name']}</b>\n"
            f"{ce('plan_badge')}   Тариф: <b>{plan['name']}</b> ({plan['duration_days']} дн.)\n"
        )
        if discount_text:
            text += f"{discount_text}\n"
            if original_rub > 0:
                text += f"{ce('wallet')}   <s>{original_rub:.0f} ₽</s>  →  <b>{rub_price:.0f} ₽</b> (~{per_day_rub:.0f} ₽/день)\n"
            else:
                text += f"{ce('wallet')}   Стоимость: <b>{rub_price:.0f} ₽</b> (~{per_day_rub:.0f} ₽/день)\n"
        else:
            text += f"{ce('wallet')}   Стоимость: <b>{rub_price:.0f} ₽</b> (~{per_day_rub:.0f} ₽/день)\n"
        if bonus_text:
            text += bonus_text
        text += (
            f"{ce('chart')}   ≈ {price_usdt:.2f} USDT\n\n"
        )
    else:
        per_day = round(price_usdt / plan["duration_days"], 2)
        text = (
            f"{ce('ticket')}   <b>Оформление заказа #{order_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{ce('shopping')}  <b>{service['name']}</b>\n"
            f"{ce('plan_badge')}   Тариф: <b>{plan['name']}</b> ({plan['duration_days']} дн.)\n"
        )
        if discount_text:
            text += f"{discount_text}\n"
        if bonus_text:
            text += bonus_text
        text += (
            f"{ce('wallet')}   Стоимость: <b>{price_usdt:.2f} USDT</b>\n"
            f"{ce('chart')}   ~{per_day} USDT/день\n\n"
        )
    
    # Add promo code prompt
    if PROMO_CODE_ENABLED:
        text += f"{ce('discount')}  Есть промокод? Нажмите кнопку ниже\n\n"
    
    text += f"<i>{ce('lock')}  Выберите способ оплаты:</i>\n"
    text += f"<a href=\"https://disk.yandex.ru/i/HWpCZ1blH8fyUw\">Оферта</a>"

    # Build keyboard with promo code option
    kb = payment_method_kb(order_id)
    if PROMO_CODE_ENABLED:
        from keyboards.keyboards import promo_code_kb
        kb = promo_code_kb(order_id)

    await callback.message.edit_text(
        text,
        reply_markup=kb,
        parse_mode="HTML",
    )
    await safe_answer(callback)


# ─── Promo Code ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("promo_"))
async def enter_promo_code(callback: CallbackQuery, state: FSMContext):
    """Start promo code entry."""
    await state.set_state(OrderFlow.entering_promo_code)
    promo_prompt = await callback.message.answer(
        f"{ce('discount')}  <b>Введите промокод:</b>\n\n"
        f"<i>Например: WELCOME, LAUNCH20.\n"
        f"Передумали — отправьте «Отмена» или нажмите кнопку меню.</i>",
        parse_mode="HTML",
        **eph.native_kwargs(callback.from_user.id, callback_query_id=callback.id),
    )
    # Task 35, группа D: промпт растворяется после завершения ввода
    # (пара с вводом юзера), а оставленный без ответа — сам через 10 мин.
    await state.update_data(eph_promo_prompt=promo_prompt.message_id)
    eph.dissolve(promo_prompt, eph.PROMPT_TTL)
    await safe_answer(callback)


# UX №9: явный выход из ввода промокода. Без этого хендлера «Отмена»
# проваливалась бы в apply_promo (отзыв «Промокод недействителен»),
# а нажатие кнопки меню молча гасило состояние без ответа.
@router.message(OrderFlow.entering_promo_code, F.text.lower() == "отмена")
async def promo_cancel(message: Message, state: FSMContext):
    fsm_data = await state.get_data()
    await state.clear()
    # Task 35, группа D: ввод отменён — промпт больше не нужен
    # (само «Отмена» растворит UserCommandCleanupMiddleware — это кнопка меню).
    eph.dissolve_ids(
        message.bot, message.chat.id, [fsm_data.get("eph_promo_prompt")], eph.PAIR_DELAY
    )
    await message.answer(
        f"{ce('check')}  Ввод промокода отменён.\n\n"
        f"<i>Выберите способ оплаты на экране заказа выше — он по-прежнему активен.</i>",
        parse_mode="HTML",
    )


@router.message(OrderFlow.entering_promo_code)
async def apply_promo(message: Message, state: FSMContext):
    """Apply promo code to current order."""
    if await _escape_if_menu(message, state):
        return
    
    code = message.text.strip()
    data = await state.get_data()
    order_id = data.get("order_id")
    
    if not order_id:
        await state.clear()
        err_msg = await message.answer("Заказ не найден. Оформите заново через каталог.", reply_markup=get_menu(message.from_user.id))
        # Группа E: пара «ввод + ошибка» уходит сама (ERROR_DELAY)
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, err_msg.message_id], eph.ERROR_DELAY)
        return
    
    # Validate promo
    result = await validate_promo_code(code, message.from_user.id)
    
    if not result.get("valid"):
        err_msg = await message.answer(
            f"{ce('cross')}  {result.get('message', 'Промокод недействителен')}",
            parse_mode="HTML",
        )
        # Task 35, группа E: промпт живёт (юзер может повторить ввод),
        # а сам неверный ввод + ошибка растворяются через ERROR_DELAY.
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, err_msg.message_id], eph.ERROR_DELAY)
        # Stay in promo state to let user try again
        return
    
    # Calculate discounted price
    order = await db.get_order(order_id)
    if not order:
        await state.clear()
        err_msg = await message.answer("Заказ не найден. Оформите заново через каталог.", reply_markup=get_menu(message.from_user.id))
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, err_msg.message_id], eph.ERROR_DELAY)
        return
    if order["status"] != "pending_payment":
        # v17: оплата могла подтвердиться поллером, пока юзер вводил промокод.
        # Раньше update_order_status вернул бы статус назад в pending_payment
        # (гонка с поллером) и перезаписал цену уже оплаченного заказа.
        await state.clear()
        info_msg = await message.answer(
            f"{ce('info')}  Оплата этого заказа уже подтверждена — промокод применить "
            f"нельзя. Следуйте инструкции бота выше."
        )
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, info_msg.message_id], eph.ERROR_DELAY)
        return
    original_price_usdt = data.get("original_price_usdt", order.get("price_usdt", 0))
    existing_discount = data.get("discount_pct", 0)
    
    # Apply promo on top of the CURRENT order price — она уже включает
    # welcome-скидку и применённый бонус за друзей. Прежний пересчёт от
    # original стёр бы бонус: order.price_usdt вернулся бы к цене без него,
    # а bonus_applied остался бы на заказе → списание без скидки.
    current_price = float(order.get("price_usdt", 0) or 0)
    base_for_promo = current_price if current_price > 0 else original_price_usdt
    new_price, discount_desc = calculate_discounted_price(base_for_promo, result)

    # UX №4: промокод не должен обнулять стоимость — способы оплаты
    # (TON-счёт, минимальный чек карты, юниты Digiseller) на 0 не рассчитаны.
    # Заказ НЕ меняем; FSM остаётся активным — можно ввести другой код.
    if new_price <= 0:
        warn_msg = await message.answer(
            f"{ce('warning')}  Промокод <b>{code.upper()}</b> делает стоимость заказа "
            f"<b>нулевой</b> — оплата на 0 невозможна.\n\n"
            f"Заказ остался без изменений. Введите другой промокод "
            f"или нажмите «Отмена».\n\n"
            f"<i>Считаете, что это ошибка? Напишите в «Поддержку».</i>",
            parse_mode="HTML",
        )
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, warn_msg.message_id], eph.ERROR_DELAY)
        return
    
    total_discount_pct = round((1 - new_price / original_price_usdt) * 100, 1) if original_price_usdt > 0 else 0
    
    # Update order price in DB (but DON'T record usage yet — only after payment)
    await db.update_order_status(order_id, "pending_payment",
        price_usdt=new_price, discount_pct=total_discount_pct,
        original_price_usdt=original_price_usdt, promo_code=code.upper())
    
    # Store promo code in state — will be recorded AFTER successful payment
    await state.update_data(discount_pct=total_discount_pct, promo_code=code.upper())
    
    # Clear the promo FSM state — user is back to selecting payment method
    await state.set_state(None)

    # Task 35, группа D: ввод завершён успешно — пара «промпт + ввод юзера»
    # растворяется батчем (промокод не должен forever висеть в чате)
    eph.dissolve_ids(
        message.bot, message.chat.id,
        [data.get("eph_promo_prompt"), message.message_id],
        eph.PAIR_DELAY,
    )
    
    # Show updated order with discount
    rub_price = 0
    try:
        rub_price = await usdt_to_rub(new_price)
    except Exception:
        pass
    original_rub = 0
    try:
        original_rub = await usdt_to_rub(original_price_usdt)
    except Exception:
        pass
    
    service = get_service_by_id(order.get("service_id", ""))
    service_name = service["name"] if service else order.get("service_name", "")
    
    text = (
        f"{ce('check')}  Промокод <b>{code.upper()}</b> применён!\n"
        f"{ce('discount')}  {discount_desc}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('ticket')}   <b>Заказ #{order_id}</b>\n"
        f"{ce('shopping')}  <b>{service_name}</b>\n"
        f"{ce('plan_badge')}   Тариф: <b>{order.get('plan_name', '')}</b>\n"
    )
    
    if rub_price > 0 and original_rub > 0 and original_rub != rub_price:
        text += f"{ce('wallet')}   <s>{original_rub:.0f} ₽</s>  →  <b>{rub_price:.0f} ₽</b>\n"
    elif rub_price > 0:
        text += f"{ce('wallet')}   Стоимость: <b>{rub_price:.0f} ₽</b>\n"
    else:
        text += f"{ce('wallet')}   Стоимость: <b>{new_price:.2f} USDT</b>\n"
    
    text += f"\n<i>{ce('lock')}  Выберите способ оплаты:</i>"
    
    from keyboards.keyboards import promo_code_kb
    await message.answer(text, reply_markup=promo_code_kb(order_id), parse_mode="HTML")


# ─── Payment: Select Method ────────────────────────────────────────

@router.callback_query(F.data.startswith("payton_"))
async def pay_with_ton(callback: CallbackQuery, state: FSMContext):
    """Start Gram payment flow with QR code."""
    order_id = int(callback.data.replace("payton_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    if order["status"] != "pending_payment":
        await safe_answer(callback, "Заказ уже оплачен или отменён")
        return

    price_usdt = order.get("price_usdt", 0)
    # v17: при повторном открытии экрана НЕ пересчитываем Gram по текущему
    # курсу — сохранённая сумма и есть сумма для проверки поллером (допуск
    # всего 5%): пересчёт мог дать «Ожидается: X, отправлено Y» по СТАРОМУ QR.
    saved_gram = float(order.get("ton_amount") or 0)
    if saved_gram > 0:
        gram_amount = saved_gram
    else:
        gram_amount = await usdt_to_ton(price_usdt)
    wallet_address = get_deposit_address()

    if not wallet_address:
        await safe_answer(callback, "Gram-кошелёк не настроен. Обратитесь в поддержку.")
        return

    if gram_amount <= 0:
        await safe_answer(callback, "Не удалось получить курс Gram. Попробуйте позже.")
        return

    # Generate random memo
    memo = generate_memo(order_id)

    # Update order
    expires_at = (datetime.utcnow() + timedelta(minutes=30)).isoformat()
    await db.update_order_status(
        order_id, "pending_payment",
        payment_method="ton",
        ton_amount=gram_amount,
        payment_expires_at=expires_at,
        payment_memo=memo,
    )

    # Get RUB equivalent
    rub_amount = 0
    try:
        rub_amount = await usdt_to_rub(price_usdt)
    except Exception:
        pass

    price_info = await format_price_rub(price_usdt)

    text = (
        f"{ce('diamond')}   <b>Оплата Gram</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('package')}  Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
        f"{ce('wallet')}  Сумма: <b>{gram_amount:.3f} Gram</b>\n"
        f"{ce('chart')}   ≈ {price_info}\n\n"
        f"{ce('key')}  Кошелёк для оплаты:\n<code>{wallet_address}</code>\n\n"
        f"{ce('pencil')}  Комментарий (обязательно!):\n<code>{memo}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<i>{ce('warning')}  Внимание!</i>\n"
        f"• Отправляйте <b>точно</b> указанную сумму Gram\n"
        f"• Обязательно укажите комментарий <code>{memo}</code>\n"
        f"• Без комментария мы не сможем зачесть оплату\n"
        f"• Оплата действительна 30 минут\n\n"
        f"<i>Нажмите кнопку ниже, чтобы открыть кошелёк, или отсканируйте QR-код:</i>"
    )

    # Generate QR code for payment URL
    from services.ton_payments import generate_tonkeeper_link
    payment_url = generate_tonkeeper_link(gram_amount, order_id, wallet_address)
    
    await callback.message.edit_text(
        text,
        reply_markup=ton_payment_kb(order_id, gram_amount, wallet_address, memo),
        parse_mode="HTML",
    )

    # Экран оплаты запомнен: после успешной оплаты он будет погашен
    # (см. utils/payment_ux.py — редактирование, не удаление).
    remember_screen(callback.message.chat.id, order_id, callback.message.message_id, "text")

    # Send QR code as separate image
    # Подпись содержит номер заказа: дедупликация различает QR разных
    # заказов, а повторное открытие оплаты того же заказа стирает старый QR.
    if payment_url:
        qr_buf = generate_qr_code(payment_url)
        if qr_buf:
            qr_file = BufferedInputFile(qr_buf.read(), filename="payment_qr.png")
            qr_msg = await callback.message.answer_photo(
                qr_file,
                caption=f"{ce('camera')}  QR-код оплаты заказа #{order_id} — наведите камеру телефона:",
                parse_mode="HTML",
            )
            remember_screen(callback.message.chat.id, order_id, qr_msg.message_id, "photo")

    await safe_answer(callback)


@router.callback_query(F.data.startswith("payusdt_"))
async def pay_with_usdt(callback: CallbackQuery, state: FSMContext):
    """Start USDT (Tether on Gram) payment flow with QR code."""
    order_id = int(callback.data.replace("payusdt_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    if order["status"] != "pending_payment":
        await safe_answer(callback, "Заказ уже оплачен или отменён")
        return

    price_usdt = order.get("price_usdt", 0)
    wallet_address = get_deposit_address()

    if not wallet_address:
        await safe_answer(callback, "Gram-кошелёк не настроен. Обратитесь в поддержку.")
        return

    memo = generate_memo(order_id)

    # Update order
    expires_at = (datetime.utcnow() + timedelta(minutes=30)).isoformat()
    await db.update_order_status(
        order_id, "pending_payment",
        payment_method="usdt",
        ton_amount=0,
        payment_expires_at=expires_at,
        payment_memo=memo,
    )

    price_info = await format_price_rub(price_usdt)

    text = (
        f"{ce('dollar')}   <b>Оплата USDT (Tether)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('package')}  Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
        f"{ce('wallet')}  Сумма: <b>{price_usdt:.2f} USDT</b>\n"
        f"{ce('chart')}   ≈ {price_info}\n\n"
        f"{ce('key')}  Кошелёк для оплаты (сеть Gram):\n<code>{wallet_address}</code>\n\n"
        f"{ce('pencil')}  Комментарий (обязательно!):\n<code>{memo}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<i>{ce('warning')}  Внимание!</i>\n"
        f"• Отправляйте USDT <b>только в сети Gram</b>\n"
        f"• Обязательно укажите комментарий <code>{memo}</code>\n"
        f"• Без комментария мы не сможем зачесть оплату\n"
        f"• Оплата действительна 30 минут\n\n"
        f"<i>Нажмите кнопку ниже, чтобы отправить USDT, или отсканируйте QR-код:</i>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=usdt_payment_kb(order_id, price_usdt, wallet_address, memo),
        parse_mode="HTML",
    )

    # Экран оплаты запомнен: после успешной оплаты он будет погашен
    # (см. utils/payment_ux.py — редактирование, не удаление).
    remember_screen(callback.message.chat.id, order_id, callback.message.message_id, "text")

    # Send QR code
    # Подпись содержит номер заказа: дедупликация различает QR разных
    # заказов, а повторное открытие оплаты того же заказа стирает старый QR.
    from services.ton_payments import generate_usdt_payment_link
    usdt_url = generate_usdt_payment_link(price_usdt, order_id, wallet_address)
    if usdt_url:
        qr_buf = generate_qr_code(usdt_url)
        if qr_buf:
            qr_file = BufferedInputFile(qr_buf.read(), filename="usdt_qr.png")
            qr_msg = await callback.message.answer_photo(
                qr_file,
                caption=f"{ce('camera')}  QR-код оплаты USDT, заказ #{order_id} — наведите камеру телефона:",
                parse_mode="HTML",
            )
            remember_screen(callback.message.chat.id, order_id, qr_msg.message_id, "photo")

    await safe_answer(callback)


@router.callback_query(F.data.startswith("paycard_"))
async def pay_with_card(callback: CallbackQuery, state: FSMContext):
    """Bank card payment: Tribute (если задан TRIBUTE_API_KEY) или Digiseller."""
    order_id = int(callback.data.replace("paycard_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    if order["status"] != "pending_payment":
        await safe_answer(callback, "Заказ уже оплачен или отменён")
        return

    from services.tribute import is_configured as tribute_configured
    if tribute_configured():
        await _start_tribute_payment(callback, order)
        return

    from services.digiseller import is_configured as digi_configured
    if not digi_configured():
        await safe_answer(callback, "Оплата картой временно недоступна. Используйте Gram/USDT.")
        return

    price_usdt = order.get("price_usdt", 0)
    rub_amount = 0
    try:
        rub_amount = await usdt_to_rub(price_usdt)
    except Exception:
        pass

    from config import DIGISELLER_CURRENCY
    currency_label = "₽" if DIGISELLER_CURRENCY == "RUB" else DIGISELLER_CURRENCY

    # Окно оплаты стартует сразу при выборе карты: если юзер бросит
    # оформление (не введёт email), заказ через час отменится сам (UX №1).
    card_expires_at = (
        datetime.utcnow() + timedelta(minutes=CARD_PAYMENT_WINDOW_MINUTES)
    ).isoformat()
    await db.update_order_status(
        order_id, "pending_payment",
        payment_method="digiseller",
        payment_expires_at=card_expires_at,
    )

    # Спрашиваем у юзера его реальный email — он попадёт на страницу оплаты
    # и на чек от Digiseller. Без этого Digiseller показывает фиктивный email
    # вида {order_id}@{domain}, что сбивает юзера с толку.
    ask_email_text = (
        f"{ce('credit_card')}   <b>Оплата банковской картой</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('package')}  Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
        f"{ce('wallet')}  Сумма: <b>{rub_amount:.0f} {currency_label}</b> ({price_usdt:.2f} USDT)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('mail')}  <b>Введите ваш email</b> для чека и уведомлений об оплате.\n"
        f"Он будет подставлен на страницу оплаты автоматически.\n\n"
        f"<i>Пример: ivan@mail.ru</i>\n\n"
        f"<i>{ce('clock')}   Оплата действительна {CARD_PAYMENT_WINDOW_MINUTES} минут после перехода на страницу.</i>"
    )

    await state.set_state(OrderFlow.waiting_for_card_email)
    await state.update_data(
        card_order_id=order_id,
        card_price_usdt=price_usdt,
        card_rub_amount=rub_amount,
    )
    await callback.message.edit_text(
        ask_email_text,
        reply_markup=cancel_inline_kb(),
        parse_mode="HTML",
    )
    await safe_answer(callback)


# UX №9: «Отмена» во время ввода email. Раньше текст ошибки советовал
# «нажмите Отмена», но хендлера не существовало — «Отмена» валидировалась
# как email и получала «Некорректный email» по кругу.
@router.message(OrderFlow.waiting_for_card_email, F.text.lower() == "отмена")
async def card_email_cancel(message: Message, state: FSMContext):
    fsm_data = await state.get_data()
    order_id = fsm_data.get("card_order_id")
    await state.clear()
    text = f"{ce('check')}  Ввод email отменён."
    if order_id:
        text += (
            f"\n\nЗаказ <b>#{order_id}</b> остался неоплаченным — вернуться к оплате "
            f"можно из «Заказы» или карточкой при следующем /start\n"
            f"<i>(пока не истекло окно оплаты; сама оплата — на экране выше).</i>"
        )
    await message.answer(text, reply_markup=get_menu(message.from_user.id), parse_mode="HTML")


@router.message(OrderFlow.waiting_for_card_email, F.text)
async def card_email_entered(message: Message, state: FSMContext):
    """Юзер ввёл email — создаём ссылку на оплату Digiseller и показываем кнопку."""
    import re

    fsm_data = await state.get_data()
    order_id = fsm_data.get("card_order_id")
    price_usdt = fsm_data.get("card_price_usdt", 0)
    rub_amount = fsm_data.get("card_rub_amount", 0)

    if not order_id:
        notfound_msg = await message.answer("Заказ не найден. Начните заново через Каталог.")
        await state.clear()
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, notfound_msg.message_id], eph.ERROR_DELAY)
        return

    # Валидация email
    email = message.text.strip()
    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(email_pattern, email):
        bad_email_msg = await message.answer(
            f"{ce('warning')}  <b>Некорректный email</b>\n\n"
            f"Пример правильного: <code>ivan@mail.ru</code>\n"
            f"Попробуйте ещё раз или нажмите «Отмена».",
            parse_mode="HTML",
        )
        # Task 35, группа E: неверный ввод + ошибка уйдут сами
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, bad_email_msg.message_id], eph.ERROR_DELAY)
        return

    order = await db.get_order(order_id)
    if not order or order["user_id"] != message.from_user.id:
        notfound_msg = await message.answer("Заказ не найден.")
        await state.clear()
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, notfound_msg.message_id], eph.ERROR_DELAY)
        return
    if order["status"] == "pending_account":
        # Оплата уже подтверждена авто-проверкой. То, что юзер сейчас ввёл —
        # это ДАННЫЕ АККАУНТА (он следует подсказке бота), а не email для чека.
        # Мягко переходим в обычный ввод данных аккаунта.
        await state.clear()
        service = get_service_by_id(order["service_id"])
        account_fields = service.get("account_fields", []) if service else []
        if account_fields:
            await state.set_state(OrderFlow.waiting_for_account_fields)
            await state.update_data(
                account_fields=account_fields,
                current_field_index=0,
                account_data={},
                order_id=order_id,
            )
            transition_msg = await message.answer(
                f"{ce('check')}  Оплата уже подтверждена — продолжим ввод данных аккаунта.",
                parse_mode="HTML",
            )
            await input_account_field(message, state)
            return
        await db.update_order_status(order_id, "pending_activation")
        activated_msg = await message.answer("Заказ уже оплачен и передан на активацию.")
        await state.clear()
        return
    if order["status"] != "pending_payment":
        already_msg = await message.answer("Заказ уже оплачен или отменён.")
        await state.clear()
        return

    from config import DIGISELLER_CURRENCY
    currency_label = "₽" if DIGISELLER_CURRENCY == "RUB" else DIGISELLER_CURRENCY

    from services.digiseller import create_payment_url

    # Создаём подписанный URL с email юзера и фиксированной суммой.
    # create_payment_url возвращает кортеж (url, фактическая_сумма, id_po).
    # Фактическая сумма может слегка отличаться от желаемой из-за округления
    # до целого числа юнитов (DIGISELLER_UNIT_PRICE × unit_cnt).
    try:
        payment_url, actual_amount, id_po = await create_payment_url(
            order_id,
            amount=rub_amount if rub_amount > 0 else None,
            buyer_email=email,
        )
    except Exception as e:
        logger.error(f"Digiseller create_payment_url failed for order #{order_id}: {e}")
        # Покупателю — понятный текст без JSON; детали с подсказкой — админу
        pay_err_msg = await message.answer(
            f"{ce('warning')}  Оплата картой временно недоступна.\n\n"
            f"Попробуйте другой способ оплаты или зайдите позже.",
            parse_mode="HTML",
        )
        admin_text = (
            f"{ce('warning')} <b>Digiseller: не удалось создать ссылку оплаты "
            f"для заказа #{order_id}</b>\n"
            f"<code>{safe_html(str(e)[:500])}</code>\n\n"
            f"Заказ #{order_id} отменён — покупателю придётся оформить заново "
            f"после исправления конфигурации (см. подсказку в тексте ошибки)."
        )
        for admin_id in ADMIN_IDS:
            try:
                await message.bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception:
                pass
        await db.update_order_status(order_id, "cancelled")
        await state.clear()
        # Группа E: пара «email + ошибка» растворяется; заказ уже отменён,
        # актуальный статус виден в «Заказы»
        eph.dissolve_ids(message.bot, message.chat.id,
                         [message.message_id, pay_err_msg.message_id], eph.ERROR_DELAY)
        return

    # СОХРАНЯЕМ платёжные данные в заказе — без них бот НЕ найдёт оплату:
    #   email — попадает в ссылку и записывается на продажу в Digiseller;
    #   amount — фактическая сумма (после округления до юнитов), по ней
    #           сверяется оплата без привязки к курсу USDT/RUB;
    #   id_po — подписанный предзаказ (для диагностики).
    try:
        await db.set_order_digiseller_info(
            order_id,
            email=email,
            amount=actual_amount if actual_amount > 0 else None,
            id_po=id_po,
        )
    except Exception as e:
        logger.error(f"Digiseller: не удалось сохранить данные оплаты заказа #{order_id}: {e}")

    # Окно оплаты отсчитывается заново от момента создания ссылки (UX №1):
    # юзер мог потратить время на ввод email — даём полные 60 минут на оплату.
    try:
        link_expires_at = (
            datetime.utcnow() + timedelta(minutes=CARD_PAYMENT_WINDOW_MINUTES)
        ).isoformat()
        await db.update_order_status(
            order_id, "pending_payment",
            payment_expires_at=link_expires_at,
        )
    except Exception as e:
        logger.warning(f"Digiseller: не удалось обновить окно оплаты заказа #{order_id}: {e}")

    # Состояние больше не нужно — email получен
    await state.clear()

    # Task 35, группа D: email — персональные данные; ввод растворяется
    # после показа экрана оплаты (сам email остаётся на экране и в чеке)
    eph.dissolve_ids(message.bot, message.chat.id, [message.message_id], eph.PAIR_DELAY)

    # Показываем юзеру ФАКТИЧЕСКУЮ сумму оплаты (после округления до юнитов)
    display_amount = actual_amount if actual_amount > 0 else rub_amount

    # Если сумма изменилась после округления — предупредим юзера
    rounding_note = ""
    if rub_amount > 0 and abs(display_amount - rub_amount) > 0.01:
        rounding_note = (
            f"{ce('info')}  Сумма округлена с {rub_amount:.0f} до {display_amount:.0f} {currency_label} "
            f"(минимальный шаг цены товара на Digiseller).\n\n"
        )

    text = (
        f"{ce('credit_card')}   <b>Оплата банковской картой</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('package')}  Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
        f"{ce('wallet')}  Сумма к оплате: <b>{display_amount:.0f} {currency_label}</b>"
        + (f" <i>(≈{price_usdt:.2f} USDT)</i>" if price_usdt else "")
        + "\n\n"
        + rounding_note
        + f"{ce('mail')}  Email для чека: <code>{email}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('link')}  Нажмите кнопку ниже для перехода на страницу оплаты.\n"
        f"Сумма и email уже подставлены — оплачивайте как есть.\n"
        f"Принимаются карты, СБП и другие способы.\n\n"
        f"{ce('clock')}   Оплата действительна {CARD_PAYMENT_WINDOW_MINUTES} минут.\n\n"
        f"<i>После оплаты нажмите «Проверить оплату» — подтверждение приходит автоматически в течение минуты.</i>"
    )

    from keyboards.keyboards import digiseller_payment_kb
    digi_msg = await message.answer(
        text,
        reply_markup=digiseller_payment_kb(order_id, payment_url),
        parse_mode="HTML",
    )
    # Экран оплаты запомнен: после успешной оплаты будет погашен
    # (см. utils/payment_ux.py — редактирование, не удаление).
    remember_screen(message.chat.id, order_id, digi_msg.message_id, "text")


# ─── Payment: Tribute (карты через Tribute Shop API) ───────────────
#
# Включается автоматически, когда в .env задан TRIBUTE_API_KEY.
# Преимущества перед Digiseller: точное подтверждение по UUID заказа
# (нет подбо́ров платежей по email), оплата внутри Telegram (Mini App),
# email юзера не требуется.

async def _start_tribute_payment(callback: CallbackQuery, order: dict):
    """Создать заказ в Tribute и показать кнопку оплаты (Mini App)."""
    from services.tribute import (
        TributeError, create_order, get_order as tribute_get_order,
        payment_url_of, rub_to_kopecks, MIN_AMOUNT_KOP, MAX_AMOUNT_KOP,
    )

    order_id = order["order_id"]
    price_usdt = order.get("price_usdt", 0)

    try:
        rub_amount = await usdt_to_rub(price_usdt)
    except Exception:
        rub_amount = 0.0

    if rub_amount <= 0:
        await safe_answer(
            callback,
            "Не удалось определить сумму в рублях. Попробуйте позже.",
        )
        return

    kop = rub_to_kopecks(rub_amount)
    if kop < MIN_AMOUNT_KOP:
        await safe_answer(
            callback,
            "Оплата картой доступна от 100 ₽ — используйте другие способы.",
        )
        return
    if kop > MAX_AMOUNT_KOP:
        await safe_answer(
            callback,
            "Оплата картой доступна до 300 000 ₽ — напишите администратору.",
        )
        return

    # Окно оплаты и для карточных заказов Tribute (UX №1): без него
    # брошенный заказ висел «Ожидает оплату» вечно. 60 минут — как у Digiseller.
    tribute_expires_at = (
        datetime.utcnow() + timedelta(minutes=CARD_PAYMENT_WINDOW_MINUTES)
    ).isoformat()
    await db.update_order_status(
        order_id, "pending_payment",
        payment_method="tribute",
        payment_expires_at=tribute_expires_at,
    )

    title = f"Заказ #{order_id}: {order['service_name']} — {order['plan_name']}"
    description = (
        f"Подписка {order['service_name']} на {order['plan_name']} "
        f"({order['duration_days']} дн.)"
    )

    # Если заказ уже создавался в Tribute и всё ещё не оплачен —
    # переиспользуем его (не плодим дубли на стороне Tribute).
    existing_uuid = (order.get("tribute_order_uuid") or "").strip()
    if existing_uuid:
        try:
            existing = await tribute_get_order(existing_uuid)
            if str(existing.get("status", "")).lower() in ("pending", "prepaid"):
                await _show_tribute_payment(
                    callback, order, rub_amount, price_usdt,
                    existing_uuid, payment_url_of(existing),
                )
                return
        except TributeError:
            pass  # заказ не найден/недоступен — создадим новый

    try:
        tribute_order = await create_order(
            rub_amount,
            title=title,
            description=description,
            customer_id=str(order["user_id"]),
            comment=f"bot_order_id:{order_id}",
        )
    except TributeError as e:
        logger.error(f"Tribute create_order failed for order #{order_id}: {e}")
        # Покупателю — понятный текст без JSON; детали с подсказкой — админу
        err_msg = await callback.message.answer(
            f"{ce('warning')}  Оплата картой временно недоступна.\n\n"
            f"Попробуйте другой способ оплаты или зайдите позже.",
            parse_mode="HTML",
        )
        admin_text = (
            f"{ce('warning')} <b>Tribute: не удалось создать заказ #{order_id}</b>\n"
            f"<code>{safe_html(str(e)[:500])}</code>\n\n"
            f"Оплата картой сейчас недоступна покупателям. Исправь конфигурацию "
            f"и перезапусти бота (подсказка — в тексте ошибки)."
        )
        for admin_id in ADMIN_IDS:
            try:
                await callback.bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception:
                pass
        await safe_answer(callback)
        return

    tribute_uuid = tribute_order["uuid"]
    # UUID сохраняем ДО отправки сообщения: если доставка упадёт,
    # poller всё равно увидит заказ и подтвердит оплату.
    await db.set_order_tribute_info(order_id, tribute_uuid)

    await _show_tribute_payment(
        callback, order, rub_amount, price_usdt,
        tribute_uuid, payment_url_of(tribute_order),
    )


async def _show_tribute_payment(
    callback: CallbackQuery,
    order: dict,
    rub_amount: float,
    price_usdt: float,
    tribute_uuid: str,
    pay_url: str,
):
    """Показать сообщение с кнопкой оплаты Tribute."""
    order_id = order["order_id"]
    text = (
        f"{ce('credit_card')}   <b>Оплата банковской картой</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('package')}  Заказ #{order_id}: "
        f"{safe_html(str(order['service_name']))} — "
        f"{safe_html(str(order['plan_name']))}\n\n"
        f"{ce('wallet')}  Сумма к оплате: <b>{rub_amount:.0f} ₽</b>"
        + (f" <i>(≈{price_usdt:.2f} USDT)</i>" if price_usdt else "")
        + "\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('link')}  Нажмите кнопку ниже — оплата откроется "
        f"прямо в Telegram. Карты, СБП и другие способы.\n\n"
        f"{ce('clock')}   Оплата действительна {CARD_PAYMENT_WINDOW_MINUTES} минут.\n\n"
        f"<i>После оплаты подтвердим автоматически в течение минуты — "
        f"затем попросим данные аккаунта. Кнопка «Проверить оплату» — "
        f"если не хотите ждать.</i>"
    )

    from keyboards.keyboards import tribute_payment_kb
    try:
        await callback.message.edit_text(
            text,
            reply_markup=tribute_payment_kb(order_id, pay_url),
            parse_mode="HTML",
        )
        # Экран оплаты запомнен: после успешной оплаты будет погашен
        # (см. utils/payment_ux.py — редактирование, не удаление).
        remember_screen(callback.message.chat.id, order_id, callback.message.message_id, "text")
    except Exception:
        # сообщение нельзя отредактировать (старое/удалено) — шлём новое
        tribute_msg = await callback.message.answer(
            text,
            reply_markup=tribute_payment_kb(order_id, pay_url),
            parse_mode="HTML",
        )
        remember_screen(callback.message.chat.id, order_id, tribute_msg.message_id, "text")
    await safe_answer(callback)


@router.callback_query(F.data.startswith("checktrib_"))
async def check_trib_payment(callback: CallbackQuery, state: FSMContext):
    """Ручная проверка оплаты Tribute (кнопка «Проверить оплату»)."""
    from services.tribute import TributeError, is_order_paid

    # Антифлуд: дорогая проверка (запрос к Tribute API) — не чаще раза в 10 с.
    left = check_cooldown_left(callback.from_user.id)
    if left:
        await safe_answer(
            callback,
            f"⏳ Подождите {left} с — проверка выполняется не чаще раза в 10 секунд.",
        )
        return

    order_id = int(callback.data.replace("checktrib_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    # Уже обработанные / отменённые заказы не переподтверждаем
    if order["status"] not in ("pending_payment",):
        if order["status"] in ("pending_account", "pending_activation", "active"):
            await safe_answer(callback, "Оплата уже подтверждена!")
        else:
            # отменён/просрочен — гасим мёртвый экран, а не пугаем попапом (UX №2)
            await _dim_expired_payment_screen(callback, order_id)
        return

    # Окно оплаты вышло (+ grace) — гасим экран (UX №2)
    if effective_status(order) == "payment_over":
        await _dim_expired_payment_screen(callback, order_id)
        return

    tribute_uuid = (order.get("tribute_order_uuid") or "").strip()
    if not tribute_uuid:
        await safe_answer(
            callback, "Заказ не привязан к Tribute. Нажмите «Оплатить» заново."
        )
        return

    note_check_press(callback.from_user.id)
    try:
        paid = await is_order_paid(tribute_uuid)
    except TributeError as e:
        logger.warning(f"Tribute status check failed for order #{order_id}: {e}")
        await safe_answer(
            callback,
            "⚠️ Сервис оплаты не ответил. Попробуйте ещё раз через пару секунд.",
        )
        return

    if not paid:
        await safe_answer(
            callback,
            "Оплата не найдена. Завершите оплату и попробуйте через 30 сек.",
        )
        return

    # CAS: атомарный переход исключает двойную обработку
    transitioned = await try_transition_order_status(
        order_id, "pending_payment", "pending_account",
        paid_at=datetime.utcnow().isoformat(),
        payment_method="tribute",
    )
    if not transitioned:
        await safe_answer(callback, "Оплата уже подтверждена!")
        return

    service = get_service_by_id(order["service_id"])
    account_fields = service.get("account_fields", []) if service else []

    if account_fields:
        first_field = account_fields[0]
        text = (
            f"{ce('check')}   <b>Оплата подтверждена!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — "
            f"{safe_html(str(order['plan_name']))}\n\n"
            f"{ce('key')}   <b>Теперь введите данные аккаунта</b> "
            f"для активации подписки.\n\n"
            f"<b>{first_field['label']}</b>\n"
            f"<i>Пример: {first_field['placeholder']}</i>\n\n"
            f"<i>{ce('shield')}  Данные используются только для активации "
            f"и удаляются после.</i>\n"
            f"<i>{ce('mail')}  Если при входе сервис запросит код из письма — "
            f"наша команда попросит его в этом чате.</i>"
        )
        await state.set_state(OrderFlow.waiting_for_account_fields)
        await state.update_data(
            account_fields=account_fields,
            current_field_index=0,
            account_data={},
            order_id=order_id,
        )
        await callback.message.edit_text(
            text, reply_markup=cancel_inline_kb(), parse_mode="HTML"
        )
    else:
        await db.update_order_status(order_id, "pending_activation")
        text = (
            f"{ce('check')}   <b>Оплата подтверждена!</b>\n\n"
            f"{ce('clock')}   Заказ #{order_id} передан на активацию."
        )
        await callback.message.edit_text(
            text,
            reply_markup=get_menu(callback.from_user.id),
            parse_mode="HTML",
        )

    # Погасить оставшиеся мёртвые экраны заказа (QR и т.п.) — оплата подтверждена.
    # Основной экран уже переоформлен выше — extinguish исключает его сам.
    await extinguish_for_callback(callback.bot, callback, order_id)

    # Уведомление админам
    for admin_id in ADMIN_IDS:
        try:
            await callback.bot.send_message(
                admin_id,
                f"{ce('credit_card')} <b>Оплата картой получена (Tribute)!</b>\n"
                f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — "
                f"{safe_html(str(order['plan_name']))}\n"
                f"{ce('wallet')} {order.get('price_usdt', 0):.2f} USDT\n"
                f"UUID: <code>{tribute_uuid}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    updated_order = await db.get_order(order_id)
    await _post_payment_actions(updated_order, callback.bot, state)

    await safe_answer(callback, "Оплата подтверждена!")



# ─── Payment: Telegram Stars ───────────────────────────────────────

@router.callback_query(F.data.startswith("paystars_"))
async def pay_with_stars(callback: CallbackQuery, state: FSMContext):
    """Telegram Stars payment — native Telegram currency.

    Course: STARS_PER_USDT stars = 1 USDT (default: 100 stars = 1 USDT).
    Stars payments use currency="XTR" and do NOT require a provider_token.
    """
    order_id = int(callback.data.replace("paystars_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    if order["status"] != "pending_payment":
        await safe_answer(callback, "Заказ уже оплачен или отменён")
        return

    price_usdt = order.get("price_usdt", 0)
    stars_amount = int(price_usdt * STARS_PER_USDT)

    if stars_amount <= 0:
        await safe_answer(callback, "Ошибка: некорректная сумма")
        return

    await db.update_order_status(order_id, "pending_payment", payment_method="stars")

    await callback.message.answer_invoice(
        title=f"{safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}",
        description=f"Подписка {order['service_name']} на {order['plan_name']} ({order['duration_days']} дн.)",
        provider_token="",  # Stars do NOT use a provider token
        currency="XTR",     # XTR = Telegram Stars
        prices=[LabeledPrice(label=order["plan_name"], amount=stars_amount)],
        payload=str(order_id),
    )
    await safe_answer(callback)


# ─── Payment: Check Gram/USDT ───────────────────────────────────────

async def _dim_expired_payment_screen(callback: CallbackQuery, order_id: int) -> None:
    """Гасим мёртвый экран оплаты (UX №2).

    Окно оплаты истекло (или заказ уже отменён) — вместо мёртвых кнопок
    «Проверить оплату» экран переоформляется в честное состояние:
    «Время оплаты истекло» + кнопка в каталог. РЕДАКТИРОВАНИЕ, не удаление:
    политика стирания не затрагивается (гасятся только юзерские экраны
    оплаты, админские сообщения и тут неприкосновенны).
    """
    from keyboards.keyboards import back_catalog_kb
    text = (
        f"{ce('clock')}  <b>Время оплаты заказа #{order_id} истекло</b>\n\n"
        f"Если вы уже отправили деньги — не переживайте: напишите в «Поддержку»,\n"
        f"платёж проверят вручную и оформят заказ.\n\n"
        f"Иначе просто оформите новый заказ через каталог."
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=back_catalog_kb(),
            parse_mode="HTML",
        )
    except Exception:
        # Экран старше 48 ч / уже погашен — попапа ниже достаточно
        pass
    await safe_answer(callback, "Время оплаты истекло — оформите заказ заново")


@router.callback_query(F.data.startswith("checkton_"))
async def check_ton_payment(callback: CallbackQuery, state: FSMContext):
    """Check if Gram payment has been received."""
    # Антифлуд: дорогая проверка (запрос к блокчейну) — не чаще раза в 10 с.
    left = check_cooldown_left(callback.from_user.id)
    if left:
        await safe_answer(
            callback,
            f"⏳ Подождите {left} с — проверка выполняется не чаще раза в 10 секунд.",
        )
        return

    order_id = int(callback.data.replace("checkton_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    # Prevent re-confirmation of already processed / cancelled orders
    if order["status"] not in ("pending_payment",):
        if order["status"] in ("pending_account", "pending_activation", "active"):
            await safe_answer(callback, "Оплата уже подтверждена!")
        else:
            # отменён/просрочен — гасим мёртвый экран, а не пугаем попапом (UX №2)
            await _dim_expired_payment_screen(callback, order_id)
        return

    # Окно оплаты вышло (+ grace) — экран больше не должен предлагать
    # «Проверить оплату»: до этого юзер получал бессмысленное
    # «Оплата не найдена» через час после истечения окна (UX №2)
    if effective_status(order) == "payment_over":
        await _dim_expired_payment_screen(callback, order_id)
        return

    ton_amount = order.get("ton_amount", 0)
    if ton_amount <= 0:
        await safe_answer(callback, "Ошибка: сумма Gram не указана")
        return

    note_check_press(callback.from_user.id)
    result = await verify_payment(order_id, "ton", ton_amount, db_memo=order.get("payment_memo"))

    if result.get("paid"):
        tx_hash = result.get("tx_hash", "")
        # CAS: atomic transition prevents double-processing
        transitioned = await try_transition_order_status(
            order_id, "pending_payment", "pending_account",
            paid_at=datetime.utcnow().isoformat(),
            ton_tx_hash=str(tx_hash),
        )
        if not transitioned:
            await safe_answer(callback, "Оплата уже подтверждена!")
            return

        service = get_service_by_id(order["service_id"])
        account_fields = service.get("account_fields", []) if service else []

        if account_fields:
            first_field = account_fields[0]
            text = (
                f"{ce('check')}   <b>Оплата подтверждена!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
                f"{ce('key')}   <b>Теперь введите данные аккаунта</b> для активации подписки.\n\n"
                f"<b>{first_field['label']}</b>\n"
                f"<i>Пример: {first_field['placeholder']}</i>\n\n"
                f"<i>{ce('shield')}  Данные используются только для активации и удаляются после.</i>\n"
                f"<i>{ce('mail')}  Если при входе сервис запросит код из письма — наша команда попросит его в этом чате.</i>"
            )
            await state.set_state(OrderFlow.waiting_for_account_fields)
            await state.update_data(
                account_fields=account_fields,
                current_field_index=0,
                account_data={},
                order_id=order_id,
            )
            await callback.message.edit_text(text, reply_markup=cancel_inline_kb(), parse_mode="HTML")
        else:
            await db.update_order_status(order_id, "pending_activation")
            text = (
                f"{ce('check')}   <b>Оплата подтверждена!</b>\n\n"
                f"{ce('clock')}   Заказ #{order_id} передан на активацию."
            )
            await callback.message.edit_text(
                text,
                reply_markup=get_menu(callback.from_user.id),
                parse_mode="HTML",
            )

        # Погасить оставшиеся мёртвые экраны заказа (QR-фото) — оплата подтверждена.
        # Основной экран уже переоформлен выше — extinguish исключает его сам.
        await extinguish_for_callback(callback.bot, callback, order_id)

        # Notify admins
        for admin_id in ADMIN_IDS:
            try:
                admin_text = (
                    f"{ce('diamond')} <b>Оплата Gram получена!</b>\n"
                    f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
                    f"{ce('wallet')} {ton_amount:.3f} Gram ({order.get('price_usdt', 0):.2f} USDT)\n"
                )
                if tx_hash:
                    admin_text += f"TX: <code>{tx_hash[:20]}...</code>"
                await callback.bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception:
                pass

        # Post-payment marketing actions (loyalty, referral, welcome discount)
        updated_order = await db.get_order(order_id)
        await _post_payment_actions(updated_order, callback.bot, state)

        await safe_answer(callback, "Оплата подтверждена!")
    else:
        error = result.get("error", "")
        if error == "check_in_progress":
            await safe_answer(callback, "⏳ Проверка уже выполняется — подождите пару секунд.")
        elif error == "amount_mismatch":
            await safe_answer(callback, "⚠️ Сумма не совпадает! Отправьте точную сумму.")
        elif error == "amount_unverifiable":
            await safe_answer(callback, "⚠️ Не удалось проверить сумму. Обратитесь в поддержку или попробуйте позже.")
        else:
            await safe_answer(callback, "Оплата не найдена. Проверьте комментарий и сумму, попробуйте через 30 сек.")


@router.callback_query(F.data.startswith("checkusdt_"))
async def check_usdt_payment(callback: CallbackQuery, state: FSMContext):
    """Check if USDT payment has been received."""
    # Антифлуд: дорогая проверка (запрос к блокчейну) — не чаще раза в 10 с.
    left = check_cooldown_left(callback.from_user.id)
    if left:
        await safe_answer(
            callback,
            f"⏳ Подождите {left} с — проверка выполняется не чаще раза в 10 секунд.",
        )
        return

    order_id = int(callback.data.replace("checkusdt_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    # Prevent re-confirmation of already processed / cancelled orders
    if order["status"] not in ("pending_payment",):
        if order["status"] in ("pending_account", "pending_activation", "active"):
            await safe_answer(callback, "Оплата уже подтверждена!")
        else:
            # отменён/просрочен — гасим мёртвый экран, а не пугаем попапом (UX №2)
            await _dim_expired_payment_screen(callback, order_id)
        return

    # Окно оплаты вышло (+ grace) — гасим экран (UX №2)
    if effective_status(order) == "payment_over":
        await _dim_expired_payment_screen(callback, order_id)
        return

    price_usdt = order.get("price_usdt", 0)
    if price_usdt <= 0:
        await safe_answer(callback, "Ошибка: сумма USDT не указана")
        return

    note_check_press(callback.from_user.id)
    result = await verify_payment(order_id, "usdt", price_usdt, db_memo=order.get("payment_memo"))

    if result.get("paid"):
        tx_hash = result.get("tx_hash", "")
        # CAS: atomic transition prevents double-processing
        transitioned = await try_transition_order_status(
            order_id, "pending_payment", "pending_account",
            paid_at=datetime.utcnow().isoformat(),
            ton_tx_hash=str(tx_hash),
        )
        if not transitioned:
            await safe_answer(callback, "Оплата уже подтверждена!")
            return

        service = get_service_by_id(order["service_id"])
        account_fields = service.get("account_fields", []) if service else []

        if account_fields:
            first_field = account_fields[0]
            text = (
                f"{ce('check')}   <b>Оплата подтверждена!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
                f"{ce('key')}   <b>Теперь введите данные аккаунта</b> для активации подписки.\n\n"
                f"<b>{first_field['label']}</b>\n"
                f"<i>Пример: {first_field['placeholder']}</i>\n\n"
                f"<i>{ce('shield')}  Данные используются только для активации и удаляются после.</i>\n"
                f"<i>{ce('mail')}  Если при входе сервис запросит код из письма — наша команда попросит его в этом чате.</i>"
            )
            await state.set_state(OrderFlow.waiting_for_account_fields)
            await state.update_data(
                account_fields=account_fields,
                current_field_index=0,
                account_data={},
                order_id=order_id,
            )
            await callback.message.edit_text(text, reply_markup=cancel_inline_kb(), parse_mode="HTML")
        else:
            await db.update_order_status(order_id, "pending_activation")
            text = (
                f"{ce('check')}   <b>Оплата подтверждена!</b>\n\n"
                f"{ce('clock')}   Заказ #{order_id} передан на активацию."
            )
            await callback.message.edit_text(
                text,
                reply_markup=get_menu(callback.from_user.id),
                parse_mode="HTML",
            )

        # Погасить оставшиеся мёртвые экраны заказа (QR-фото) — оплата подтверждена.
        # Основной экран уже переоформлен выше — extinguish исключает его сам.
        await extinguish_for_callback(callback.bot, callback, order_id)

        for admin_id in ADMIN_IDS:
            try:
                admin_text = (
                    f"{ce('dollar')} <b>Оплата USDT получена!</b>\n"
                    f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
                    f"{ce('wallet')} {price_usdt:.2f} USDT\n"
                )
                if tx_hash:
                    admin_text += f"TX: <code>{tx_hash[:20]}...</code>"
                await callback.bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception:
                pass

        # Post-payment marketing actions (loyalty, referral, welcome discount)
        updated_order = await db.get_order(order_id)
        await _post_payment_actions(updated_order, callback.bot, state)

        await safe_answer(callback, "Оплата подтверждена!")
    else:
        error = result.get("error", "")
        if error == "check_in_progress":
            await safe_answer(callback, "⏳ Проверка уже выполняется — подождите пару секунд.")
        elif error == "amount_mismatch":
            await safe_answer(callback, "⚠️ Сумма USDT не совпадает! Отправьте точную сумму.")
        elif error == "amount_unverifiable":
            await safe_answer(callback, "⚠️ Не удалось верифицировать сумму USDT. Попробуйте позже или обратитесь в поддержку.")
        else:
            await safe_answer(callback, "Оплата не найдена. Отправьте точную сумму с комментарием, попробуйте через 30 сек.")


# ─── Check Digiseller Card Payment ────────────────────────────────

@router.callback_query(F.data.startswith("checkdigi_"))
async def check_digi_payment(callback: CallbackQuery, state: FSMContext):
    """Check if Digiseller card payment has been received."""
    # Антифлуд: дорогая проверка (запрос к Digiseller API) — не чаще раза в 10 с.
    left = check_cooldown_left(callback.from_user.id)
    if left:
        await safe_answer(
            callback,
            f"⏳ Подождите {left} с — проверка выполняется не чаще раза в 10 секунд.",
        )
        return

    order_id = int(callback.data.replace("checkdigi_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    # Prevent re-confirmation of already processed / cancelled orders
    if order["status"] not in ("pending_payment",):
        if order["status"] in ("pending_account", "pending_activation", "active"):
            await safe_answer(callback, "Оплата уже подтверждена!")
        else:
            # отменён/просрочен — гасим мёртвый экран, а не пугаем попапом (UX №2)
            await _dim_expired_payment_screen(callback, order_id)
        return

    # Окно оплаты вышло (+ grace) — гасим экран (UX №2)
    if effective_status(order) == "payment_over":
        await _dim_expired_payment_screen(callback, order_id)
        return

    from services.digiseller import find_payment_for_order
    from config import DIGISELLER_UNIT_PRICE

    price_usdt = order.get("price_usdt", 0)

    # СУММА: берём фактическую сумму из заказа (сохранена при создании
    # ссылки) — она не зависит от текущего курса USDT/RUB. Если её нет
    # (старый заказ) — считаем по курсу и округляем до юнитов.
    expected_amount = float(order.get("digiseller_amount") or 0.0)
    if expected_amount <= 0:
        try:
            expected_amount = await usdt_to_rub(price_usdt)
        except Exception:
            expected_amount = 0.0
        if expected_amount > 0 and DIGISELLER_UNIT_PRICE > 0:
            unit_cnt = max(1, round(expected_amount / DIGISELLER_UNIT_PRICE))
            expected_amount = round(unit_cnt * DIGISELLER_UNIT_PRICE, 2)

    # EMAIL: реальный email юзера, на который оформлялась оплата
    # (сохранён в заказе при создании ссылки). Это ГЛАВНЫЙ признак,
    # по которому оплата находится на площадке Digiseller.
    buyer_email = (order.get("digiseller_email") or "").strip()

    note_check_press(callback.from_user.id)
    result = await find_payment_for_order(
        order_id,
        expected_amount,
        hours=6,  # окно поиска — на случай, если юзер платил заранее
        buyer_email=buyer_email,
        # Платёж не может быть старше момента создания ссылки: продажи
        # раньше — прошлые покупки с тем же email, а не этот заказ.
        not_before=order.get("digiseller_link_created_at"),
    )

    if result.get("paid"):
        invoice_id = result.get("invoice_id")
        # v17: защита от ДВОЙНОГО подтверждения. Один платёж с email юзера
        # раньше мог подтвердить ДВА заказа (два pending-заказа с одним
        # email — или общий/опечатанный email у разных юзеров), а также
        # «переиспользоваться» новым заказом того же юзера.
        if invoice_id:
            used_by = await db.get_order_by_digiseller_invoice(invoice_id)
            if used_by and used_by["order_id"] != order_id:
                logger.warning(
                    f"Digiseller: invoice {invoice_id} уже привязан к заказу "
                    f"#{used_by['order_id']} — повторное подтверждение заказа "
                    f"#{order_id} отклонено"
                )
                await safe_answer(
                    callback,
                    "⚠️ Этот платёж уже засчитан другому заказу. "
                    "Напишите в «Поддержку» — разберёмся вручную.",
                    show_alert=True,
                )
                return
        # CAS: atomic transition prevents double-processing
        transitioned = await try_transition_order_status(
            order_id, "pending_payment", "pending_account",
            paid_at=datetime.utcnow().isoformat(),
            payment_method="digiseller",
            digiseller_invoice_id=int(invoice_id) if invoice_id else None,
        )
        if not transitioned:
            await safe_answer(callback, "Оплата уже подтверждена!")
            return

        service = get_service_by_id(order["service_id"])
        account_fields = service.get("account_fields", []) if service else []

        if account_fields:
            first_field = account_fields[0]
            text = (
                f"{ce('check')}   <b>Оплата подтверждена!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n\n"
                f"{ce('key')}   <b>Теперь введите данные аккаунта</b> для активации подписки.\n\n"
                f"<b>{first_field['label']}</b>\n"
                f"<i>Пример: {first_field['placeholder']}</i>\n\n"
                f"<i>{ce('shield')}  Данные используются только для активации и удаляются после.</i>\n"
                f"<i>{ce('mail')}  Если при входе сервис запросит код из письма — наша команда попросит его в этом чате.</i>"
            )
            await state.set_state(OrderFlow.waiting_for_account_fields)
            await state.update_data(
                account_fields=account_fields,
                current_field_index=0,
                account_data={},
                order_id=order_id,
            )
            await callback.message.edit_text(text, reply_markup=cancel_inline_kb(), parse_mode="HTML")
        else:
            await db.update_order_status(order_id, "pending_activation")
            text = (
                f"{ce('check')}   <b>Оплата подтверждена!</b>\n\n"
                f"{ce('clock')}   Заказ #{order_id} передан на активацию."
            )
            await callback.message.edit_text(
                text,
                reply_markup=get_menu(callback.from_user.id),
                parse_mode="HTML",
            )

        # Погасить оставшиеся мёртвые экраны заказа — оплата подтверждена.
        # Основной экран уже переоформлен выше — extinguish исключает его сам.
        await extinguish_for_callback(callback.bot, callback, order_id)

        # Notify admins
        for admin_id in ADMIN_IDS:
            try:
                amount_str = f"{result.get('amount', 0):.2f} {result.get('currency', '')}".strip()
                admin_text = (
                    f"{ce('credit_card')} <b>Оплата картой получена (Digiseller)!</b>\n"
                    f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
                    f"{ce('wallet')} {amount_str} ({order.get('price_usdt', 0):.2f} USDT)\n"
                )
                if result.get("invoice_id"):
                    admin_text += f"Инвойс: <code>{result['invoice_id']}</code>"
                await callback.bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception:
                pass

        # Post-payment marketing
        updated_order = await db.get_order(order_id)
        await _post_payment_actions(updated_order, callback.bot, state)

        await safe_answer(callback, "Оплата подтверждена!")
    elif result.get("underpaid"):
        # ВАЖНО: amount_in из Digiseller — это сумма, ПОЛУЧЕННАЯ ПРОДАВЦОМ
        # после конвертации RUB→WMT и комиссии банка/PayMaster. Юзер реально
        # заплатил больше (например, 13 ₽ с карты, а продавец получил 0.04 WMT).
        # Поэтому НЕ показываем amount_in как "вы оплатили" — это путает юзера.
        # Вместо этого говорим "платёж найден, сумма не совпала" и предлагаем
        # проверить вручную.
        expected = result.get("amount_expected") or expected_amount
        try:
            await safe_answer(callback, 
                f"⚠️ Платёж найден, но сумма не совпала. Админ проверит.",
                show_alert=True,
            )
        except Exception:
            await safe_answer(callback, "⚠️ Платёж найден, проверка суммы.", show_alert=True)
        # Развёрнутое объяснение в чат
        try:
            from emojis import ce
            await callback.message.answer(
                f"{ce('warning')} <b>Платёж найден, но требует проверки</b>\n\n"
                f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
                f"Стоимость: {expected:.2f} ₽\n\n"
                f"Платёж прошёл на стороне Digiseller, но из-за конвертации "
                f"RUB→WMT (банк + комиссия PayMaster) полученная продавцом сумма "
                f"отличается от стоимости заказа. Это нормально для карточных "
                f"платежей через Digiseller.\n\n"
                f"{ce('clock')} Администратор проверит платёж вручную и активирует "
                f"подписку, либо свяжется с вами для уточнения.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        # Уведомляем админов — раньше обещали «админ проверит», но админ
        # ничего не получал и заказ висел без присмотра
        try:
            from emojis import ce as _ce
            amount_in = float(result.get("amount_in") or 0)
            for admin_id in ADMIN_IDS:
                try:
                    await callback.bot.send_message(
                        admin_id,
                        f"{_ce('warning')} <b>Digiseller: недоплата по заказу #{order_id}</b>\n"
                        f"{safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
                        f"Получено: {amount_in:.2f} ₽ / Ожидалось: {expected:.2f} ₽\n"
                        f"Инвойс: <code>{result.get('invoice_id')}</code>\n"
                        f"Клиент: <code>{callback.from_user.id}</code>\n\n"
                        f"Проверьте вручную: если платёж корректный — "
                        f"активируйте заказ через «Новые заказы».",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except Exception:
            pass
    else:
        await safe_answer(callback, 
            "Оплата не найдена. Подтверждение приходит автоматически в течение минуты — "
            "если оплатили недавно, подождите и попробуйте снова.",
            show_alert=True,
        )


# ─── Telegram Pre-Checkout & Successful Payment ────────────────────

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Pre-checkout handler for Telegram Stars payments."""
    order_id = int(query.invoice_payload)
    order = await db.get_order(order_id)
    if not order:
        await query.answer(ok=False, error_message="Заказ не найден")
        return
    if order["status"] != "pending_payment":
        await query.answer(ok=False, error_message="Заказ уже оплачен или отменён")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, state: FSMContext):
    """Handle successful payment from Telegram Stars Invoice."""
    order_id = int(message.successful_payment.invoice_payload)
    order = await db.get_order(order_id)
    if not order:
        return

    # Detect payment method from order
    payment_method = order.get("payment_method", "")
    currency = message.successful_payment.currency
    
    # If Stars payment, update method just in case
    if currency == "XTR" and payment_method != "stars":
        payment_method = "stars"
        await db.update_order_status(order_id, "pending_payment", payment_method="stars")

    await db.update_order_status(order_id, "pending_account", paid_at=datetime.utcnow().isoformat())
    service = get_service_by_id(order["service_id"])
    account_fields = service.get("account_fields", []) if service else []

    # Build payment confirmation message
    method_name = {"ton": "Gram", "usdt": "USDT", "digiseller": "Карта", "tribute": "Карта", "stars": f"Stars {ce('star')}"}.get(payment_method, payment_method)
    
    if currency == "XTR":
        stars_paid = message.successful_payment.total_amount
        price_str = f"{stars_paid} {ce('star')} ({order.get('price_usdt', 0):.2f} USDT)"
    else:
        price_str = f"{order.get('price_usdt', 0):.2f} USDT"

    if account_fields:
        first_field = account_fields[0]
        text = (
            f"{ce('check')}   <b>Оплата прошла успешно!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{ce('shopping')}  {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
            f"{ce('wallet')}  {price_str} ({method_name})\n\n"
            f"{ce('key')}   <b>Введите данные аккаунта</b> для активации.\n\n"
            f"<b>{first_field['label']}</b>\n"
            f"<i>Пример: {first_field['placeholder']}</i>\n\n"
            f"<i>{ce('shield')}  Данные используются только для активации и удаляются после.</i>\n"
                f"<i>{ce('mail')}  Если при входе сервис запросит код из письма — наша команда попросит его в этом чате.</i>"
        )
        await state.set_state(OrderFlow.waiting_for_account_fields)
        await state.update_data(
            account_fields=account_fields,
            current_field_index=0,
            account_data={},
            order_id=order_id,
        )
        await message.answer(text, reply_markup=cancel_kb(), parse_mode="HTML")
    else:
        await db.update_order_status(order_id, "pending_activation")
        await message.answer(
            f"{ce('check')}   <b>Оплата прошла успешно!</b>\n\n"
            f"{ce('clock')}  Заказ #{order_id} передан на активацию.",
            reply_markup=get_menu(message.from_user.id),
            parse_mode="HTML",
        )

    # Notify admins about payment
    for admin_id in ADMIN_IDS:
        try:
            stars_paid = message.successful_payment.total_amount
            admin_text = (
                f"{ce('star')} <b>Оплата Stars получена!</b>\n"
                f"Заказ #{order_id}: {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
                f"{ce('wallet')} {stars_paid} {ce('star')} ({order.get('price_usdt', 0):.2f} USDT)\n"
            )
            await message.bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception:
            pass

    # Post-payment marketing actions (loyalty, referral, welcome discount)
    updated_order = await db.get_order(order_id)
    await _post_payment_actions(updated_order, message.bot, state)

    # Гасим платёжные экраны заказа (если были) — оплата прошла
    await extinguish_for_callback(message.bot, message, order_id)


# ─── Account Data Input ────────────────────────────────────────────

@router.message(OrderFlow.waiting_for_account_fields, F.text.contains("Отмена"))
async def cancel_account_input(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if order_id:
        order = await db.get_order(order_id)
        if order and order["status"] == "pending_account":
            # Заказ УЖЕ ОПЛАЧЕН — отменять нельзя (иначе юзер теряет деньги).
            # Выходим из ввода данных, но заказ остаётся в силе.
            await state.clear()
            cancel_msg = await message.answer(
                f"{ce('shield')}   Заказ #{order_id} <b>уже оплачен</b> — отмена невозможна.\n\n"
                f"Если вы ошиблись при вводе данных или хотите отказаться от покупки — "
                f"напишите в «Поддержку», мы поможем.",
                reply_markup=get_menu(message.from_user.id),
                parse_mode="HTML",
            )
            return
        await db.update_order_status(order_id, "cancelled")
    await state.clear()

    cancel_msg = await message.answer(
        f"{ce('cross')}   Заказ отменён. Вы можете оформить новый через каталог.",
        reply_markup=get_menu(message.from_user.id),
        parse_mode="HTML",
    )


@router.message(OrderFlow.waiting_for_account_fields, F.text)
async def input_account_field(message: Message, state: FSMContext):
    # Escape FSM if user pressed a menu button
    if await _escape_if_menu(message, state):
        return

    data = await state.get_data()
    account_fields = data.get("account_fields", [])
    field_index = data.get("current_field_index", 0)
    account_data = data.get("account_data", {})
    order_id = data.get("order_id")

    if field_index >= len(account_fields):
        # All fields collected
        await db.update_order_status(order_id, "pending_activation", account_data=account_data)
        await state.clear()

        fields_text = "\n".join(
            f"  {ce('key')}   {f['label']}: <tg-spoiler>{'•' * 8}</tg-spoiler>"
            for f in account_fields
        )
        await message.answer(
            f"{ce('check')}   <b>Данные получены!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{ce('package')}  Заказ #{order_id}\n\n"
            f"{fields_text}\n\n"
            f"{ce('clock')}   Мы активируем подписку на вашем аккаунте и уведомим вас.\n"
            f"<i>Обычно это занимает от 5 минут до нескольких часов.</i>",
            reply_markup=get_menu(message.from_user.id),
            parse_mode="HTML",
        )

        # Notify admins
        order = await db.get_order(order_id)
        for admin_id in ADMIN_IDS:
            try:
                fields_detail = "\n".join(
                    f"  {ce('key')}  {safe_html(f['label'])}: <code>{safe_code(str(account_data.get(f['id'], '')))}</code>"
                    for f in account_fields
                )
                method = order.get("payment_method", "не указан")
                method_name = {"ton": "Gram", "usdt": "USDT", "digiseller": "Карта", "tribute": "Карта", "stars": f"Stars {ce('star')}"}.get(method, method)
                await message.bot.send_message(
                    admin_id,
                    f"{ce('bell')}   <b>Новый заказ #{order_id}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{ce('profile')} Клиент: {safe_html(message.from_user.first_name or '')} (@{safe_html(message.from_user.username or 'N/A')})\n"
                    f"{ce('shopping')}  Сервис: <b>{safe_html(str(order.get('service_name', '')))}</b>\n"
                    f"{ce('plan_badge')}  Тариф: {safe_html(str(order.get('plan_name', '')))}\n"
                    f"{ce('wallet')}  Сумма: <b>{order.get('price_usdt', 0):.2f} USDT</b> ({method_name})\n\n"
                    f"{ce('key')}  Данные аккаунта:\n{fields_detail}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Failed to notify admin {admin_id}: {e}")
        return

    # Save current field value
    current_field = account_fields[field_index]
    field_id = current_field["id"]
    account_data[field_id] = message.text.strip()

    # Task 35, группа D: логины/пароли не должны forever висеть в чате —
    # каждый принятый ввод растворяется (данные уже в заказе и у админа)
    eph.dissolve_ids(message.bot, message.chat.id, [message.message_id], eph.PAIR_DELAY)

    # Move to next field
    next_index = field_index + 1
    await state.update_data(current_field_index=next_index, account_data=account_data)

    if next_index < len(account_fields):
        next_field = account_fields[next_index]
        text = (
            f"{ce('check')}  Сохранено!\n\n"
            f"<b>{next_field['label']}</b>\n"
            f"<i>Пример: {next_field['placeholder']}</i>"
        )
        await message.answer(text, reply_markup=cancel_kb(), parse_mode="HTML")
    else:
        # Recurse to finish
        await input_account_field(message, state)


@router.message(OrderFlow.waiting_for_account_fields)
async def account_field_not_text(message: Message, state: FSMContext):
    """Non-text message (sticker/photo/voice) while entering account data."""
    not_text_msg = await message.answer(
        f"{ce('warning')}  Отправьте данные обычным <b>текстовым сообщением</b>. "
        f"Стикеры, фото и голосовые не подойдут.",
        parse_mode="HTML",
    )
    # Группа E: стикер/фото + подсказка уйдут сами
    eph.dissolve_ids(message.bot, message.chat.id,
                     [message.message_id, not_text_msg.message_id], eph.ERROR_DELAY)


# ─── Cancel order (с подтверждением, UX №3) ─────────────────────

@router.callback_query(F.data == "cancel_order")
async def cancel_order(callback: CallbackQuery, state: FSMContext):
    """Первое нажатие «Отмена» — только СПРОСИТ подтверждение.

    Раньше одно нажатие сразу ставило заказу cancelled: случайный тап
    уничтожал оформленный заказ вместе с применённым промокодом (UX №3).
    """
    # Try to get order_id from state first (set during plan selection)
    data = await state.get_data()
    order_id = data.get("order_id")
    order = await db.get_order(order_id) if order_id else None

    # Fallback: try to find the most recent pending_payment / pending_account
    # order for this user (state may be empty after bot restart or when the
    # payment was auto-confirmed by the background poller)
    if not order:
        orders = await db.get_user_orders(callback.from_user.id, limit=5)
        for o in orders:
            if o["status"] in ("pending_payment", "pending_account"):
                order = o
                order_id = o["order_id"]
                break

    if order and order["status"] == "pending_account":
        # Заказ УЖЕ ОПЛАЧЕН — отменять нельзя (иначе юзер теряет деньги)
        await safe_answer(callback,
            "Заказ уже оплачен — отмена невозможна.\n"
            "Если ошиблись в данных — напишите в «Поддержку».",
            show_alert=True,
        )
        return

    from keyboards.keyboards import cancel_confirm_kb
    target = order_id or 0
    if target:
        ask_text = (
            f"{ce('warning')}  <b>Точно отменить заказ #{target}?</b>\n\n"
            f"Оформленный заказ и все применённые скидки будут удалены.\n"
            f"Для новой покупки придётся оформить заказ заново."
        )
    else:
        ask_text = (
            f"{ce('warning')}  <b>Точно отменить оформление?</b>\n\n"
            f"Незавершённый заказ будет отменён."
        )
    await callback.message.edit_text(
        ask_text,
        reply_markup=cancel_confirm_kb(target),
        parse_mode="HTML",
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("cancelyes_"))
async def cancel_order_confirm(callback: CallbackQuery, state: FSMContext):
    """«Да, отменить» — вот теперь действительно отменяем."""
    raw = callback.data[len("cancelyes_"):]
    try:
        order_id = int(raw)
    except ValueError:
        order_id = 0

    # Заказ могли успеть оплатить (поллер подтвердил), пока юзер думал
    if order_id:
        order = await db.get_order(order_id)
        if order and order["user_id"] == callback.from_user.id \
                and order["status"] == "pending_account":
            await safe_answer(callback,
                "Заказ уже оплачен — отмена невозможна.",
                show_alert=True,
            )
            return
        if order and order["user_id"] == callback.from_user.id:
            await db.update_order_status(order_id, "cancelled")
    await state.clear()

    await callback.message.edit_text(
        f"{ce('cross')}  Заказ отменён.",
        parse_mode="HTML",
    )
    await callback.message.answer(
        "Вы можете оформить новый через каталог.",
        reply_markup=get_menu(callback.from_user.id),
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("cancelno_"))
async def cancel_order_keep(callback: CallbackQuery, state: FSMContext):
    """«Нет, вернуться» — заказ остаётся; перерисовываем выбор способа оплаты."""
    raw = callback.data[len("cancelno_"):]
    try:
        order_id = int(raw)
    except ValueError:
        order_id = 0

    order = await db.get_order(order_id) if order_id else None
    if order_id and (not order or order["user_id"] != callback.from_user.id):
        await safe_answer(callback, "Заказ не найден")
        return

    if order and order["status"] == "pending_account":
        await safe_answer(callback,
            "Оплата уже подтверждена — отправьте данные аккаунта в чат.",
            show_alert=True,
        )
        return
    if order and order["status"] != "pending_payment":
        await safe_answer(callback, "Статус заказа изменился — откройте «Заказы».")
        return

    # Перерисовываем экран выбора способа оплаты (заказ и скидки сохранены)
    price_usdt = float(order.get("price_usdt", 0) or 0) if order else 0
    price_info = ""
    if order and price_usdt > 0:
        try:
            price_info = f"\n{ce('chart')}   ≈ {await format_price_rub(price_usdt)}"
        except Exception:
            price_info = f"\n{ce('chart')}   ≈ {price_usdt:.2f} USDT"
    head = (
        f"{ce('check')}  <b>Заказ #{order_id} остаётся активным</b>\n\n"
        f"{ce('shopping')}  {safe_html(str(order['service_name']))} — "
        f"{safe_html(str(order['plan_name']))}"
        + (f"\n{ce('wallet')}   Стоимость: <b>{price_usdt:.2f} USDT</b>" if order else "")
        + price_info
        + "\n\n"
    ) if order else f"{ce('check')}  <b>Оформление продолжается</b>\n\n"

    from keyboards.keyboards import promo_code_kb, payment_method_kb
    kb = promo_code_kb(order_id) if order else payment_method_kb(0)
    await callback.message.edit_text(
        head + f"<i>{ce('lock')}  Выберите способ оплаты:</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await safe_answer(callback)


# ─── My Orders ─────────────────────────────────────────────────────

def _order_list_row(order: dict, rub_rate: float) -> str:
    """Строка одного заказа в списке «Заказы» (переиспользуется пагинацией)."""
    eff = effective_status(order)
    status_name = STATUS_NAMES.get(eff, order["status"])
    icon = STATUS_ICON.get(eff, ce('package'))
    price_usdt = order.get("price_usdt", 0)
    if rub_rate > 0:
        price_rub = price_usdt * rub_rate
        price_str = f"{price_rub:.0f} ₽"
    else:
        price_str = f"{price_usdt:.2f} USDT"
    row = (
        f"{icon}  <b>#{order['order_id']}</b> {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
        f"     {status_name} · {price_str}"
    )
    note = subscription_note(order)
    if note:
        row += f"\n     {ce('calendar')} {note}"
    return row


def _orders_page_text(orders: list[dict], page: int, per_page: int, rub_rate: float) -> str:
    """Текст одной страницы «Заказов» (стр. 0-based, срез внутри)."""
    text = f"{ce('package')}   <b>Ваши заказы</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for order in orders[page * per_page:(page + 1) * per_page]:
        text += _order_list_row(order, rub_rate) + "\n\n"
    return text


@router.message(StateFilter("*"), F.text.contains("Заказы"))
@router.message(Command("orders"))
async def cmd_my_orders(message: Message, state: FSMContext):
    await state.clear()
    # UX №6: раньше — ВСЕ заказы одним сообщением: при большой истории
    # превышался лимит Telegram 4096 символов и «Заказы» не отправлялись
    # вовсе. Теперь — страницы по ORDERS_PER_PAGE с листанием.
    orders = await db.get_user_orders(message.from_user.id, limit=ORDERS_FETCH_CAP)
    if not orders:
        await message.answer(
            f"{ce('package')}   <b>У вас пока нет заказов</b>\n\n"
            f"<i>{ce('shopping')}  Оформите подписку через каталог!</i>",
            reply_markup=get_menu(message.from_user.id),
            parse_mode="HTML",
        )
        return

    # Get RUB rate for order list
    rub_rate = 0
    try:
        rub_rate = await usdt_to_rub(1)
    except Exception:
        pass

    total_pages = max(1, math.ceil(len(orders) / ORDERS_PER_PAGE))
    text = _orders_page_text(orders, 0, ORDERS_PER_PAGE, rub_rate)
    await message.answer(
        text,
        reply_markup=user_orders_kb(
            orders[:ORDERS_PER_PAGE], page=0, total_pages=total_pages
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("myord_"))
async def my_orders_switch_page(callback: CallbackQuery):
    """UX №6: листание «Заказов» — экран РЕДАКТИРУЕТСЯ на месте,
    новые сообщения не плодятся (политика чата). FSM НЕ трогаем:
    клик по ◀/▶ не должен убивать активный ввод (данные аккаунта и т.п.)."""
    try:
        page = max(0, int(callback.data[len("myord_"):]))
    except ValueError:
        await safe_answer(callback)
        return

    orders = await db.get_user_orders(callback.from_user.id, limit=ORDERS_FETCH_CAP)
    if not orders:
        try:
            await callback.message.edit_text(
                f"{ce('package')}   <b>У вас пока нет заказов</b>\n\n"
                f"<i>{ce('shopping')}  Оформите подписку через каталог!</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await safe_answer(callback)
        return

    total_pages = max(1, math.ceil(len(orders) / ORDERS_PER_PAGE))
    page = min(page, total_pages - 1)
    rub_rate = 0
    try:
        rub_rate = await usdt_to_rub(1)
    except Exception:
        pass
    text = _orders_page_text(orders, page, ORDERS_PER_PAGE, rub_rate)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=user_orders_kb(
                orders[page * ORDERS_PER_PAGE:(page + 1) * ORDERS_PER_PAGE],
                page=page,
                total_pages=total_pages,
            ),
            parse_mode="HTML",
        )
    except Exception:
        # «message is not modified» и т.п. — уже на нужной странице
        pass
    await safe_answer(callback)


@router.callback_query(F.data.startswith("uorder_"))
async def view_order(callback: CallbackQuery):
    order_id = int(callback.data.replace("uorder_", ""))
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    eff = effective_status(order)
    status_name = STATUS_NAMES.get(eff, order["status"])
    icon = STATUS_ICON.get(eff, ce('package'))
    
    rub_rate = 0
    try:
        rub_rate = await usdt_to_rub(1)
    except Exception:
        pass
    
    price_usdt = order.get("price_usdt", 0)
    if rub_rate > 0:
        price_rub = price_usdt * rub_rate
        price_str = f"{price_rub:.0f} ₽ ({price_usdt:.2f} USDT)"
    else:
        price_str = f"{price_usdt:.2f} USDT"
    
    method = order.get("payment_method", "")
    method_name = {"ton": "Gram", "usdt": "USDT", "digiseller": "Карта", "tribute": "Карта", "stars": f"Stars {ce('star')}"}.get(method, method)

    text = (
        f"{icon}  <b>Заказ #{order_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('shopping')}  {safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
        f"{ce('calendar')}  {order['duration_days']} дн.\n"
        f"{ce('wallet')}  {price_str}\n"
        f"{ce('credit_card')}  {method_name}\n"
        f"{ce('chart')}  {status_name}\n"
    )
    note = subscription_note(order)
    if note:
        if eff == "expired":
            text += f"{ce('calendar')}  Подписка закончилась {note}\n"
        else:
            text += f"{ce('calendar')}  {note}\n"
    if order.get("created_at"):
        text += f"\n{ce('clock')}  {order['created_at'][:16]}"

    await callback.message.edit_text(text, reply_markup=order_detail_kb(order), parse_mode="HTML")
    await safe_answer(callback)


@router.callback_query(F.data.startswith("resumepay_"))
async def resume_payment(callback: CallbackQuery, state: FSMContext):
    """UX №8: «Продолжить оплату» с карточки на /start.

    Переоткрывает экран выбора способа оплаты для СУЩЕСТВУЮЩЕГО заказа
    (тот же вид, что после выбора тарифа). Все pay* хендлеры работают
    от order_id в callback — FSM им не нужен, но промокодам нужен:
    apply_promo берёт order_id из состояния, поэтому восстанавливаем его.
    """
    try:
        order_id = int(callback.data[len("resumepay_"):])
    except ValueError:
        await safe_answer(callback)
        return
    order = await db.get_order(order_id)

    if not order or order["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Заказ не найден")
        return

    if order["status"] != "pending_payment" or effective_status(order) == "payment_over":
        await safe_answer(
            callback,
            "Время оплаты заказа истекло — оформите заказ заново через Каталог",
        )
        return

    await state.update_data(
        order_id=order_id,
        original_price_usdt=order.get("original_price_usdt") or order.get("price_usdt", 0),
        discount_pct=order.get("discount_pct", 0),
    )
    await state.set_state(None)

    price_usdt = order.get("price_usdt", 0)
    rub_price = 0
    try:
        rub_price = await usdt_to_rub(price_usdt)
    except Exception:
        pass

    text = (
        f"{ce('ticket')}   <b>Оформление заказа #{order_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('shopping')}  {safe_html(str(order['service_name']))}\n"
        f"{ce('plan_badge')}   Тариф: <b>{safe_html(str(order['plan_name']))}</b> ({order['duration_days']} дн.)\n"
    )
    if rub_price > 0:
        per_day_rub = rub_price / max(1, order["duration_days"])
        text += (
            f"{ce('wallet')}   Стоимость: <b>{rub_price:.0f} ₽</b> (~{per_day_rub:.0f} ₽/день)\n"
            f"{ce('chart')}   ≈ {price_usdt:.2f} USDT\n\n"
        )
    else:
        text += f"{ce('wallet')}   Стоимость: <b>{price_usdt:.2f} USDT</b>\n\n"

    if PROMO_CODE_ENABLED:
        text += f"{ce('discount')}  Есть промокод? Нажмите кнопку ниже\n\n"

    text += f"<i>{ce('lock')}  Выберите способ оплаты:</i>\n"
    text += f"<a href=\"https://disk.yandex.ru/i/HWpCZ1blH8fyUw\">Оферта</a>"

    kb = promo_code_kb(order_id) if PROMO_CODE_ENABLED else payment_method_kb(order_id)

    # Редактируем карточку /start в экран оформления; если она уже
    # недоступна для правки — отправляем экран отдельным сообщением.
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await safe_answer(callback)


# ─── Support ───────────────────────────────────────────────────────
# Support moved to handlers/support_handlers.py — full ticket system
# (statuses, order binding, thread history, admin reply buttons).
# The «Поддержка» menu button is handled there (registered before this router).


# ─── Help ──────────────────────────────────────────────────────────

@router.message(StateFilter("*"), F.text.contains("Помощь"))
@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    text = (
        f"{ce('question')}   <b>Как работает сервис</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('one')}   <b>Выберите подписку</b> в каталоге\n"
        f"{ce('two')}   <b>Оплатите</b> удобным способом:\n"
        f"     {ce('diamond')}  Gram — криптовалюта (быстро, без комиссии)\n"
        f"     {ce('dollar')}  USDT — стейблкоин в сети Gram\n"
        f"     {ce('star')}  Telegram Stars — оплата внутри Telegram\n"
        f"     {ce('credit_card')}  Банковская карта — оплата картой/СБП через Digiseller\n\n"
        f"{ce('three')}   <b>Введите данные аккаунта</b> для активации\n"
        f"{ce('four')}   <b>Готово!</b> Подписка активируется вручную нашей командой в течение 24 часов\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('shield')}  <b>Безопасность:</b>\n"
        f"• Ваши данные используются только для активации\n"
        f"• Рекомендуем сменить пароль после активации\n"
        f"• Иногда при входе сервис запрашивает код из письма — "
        f"тогда мы попросим его прямо в этом боте\n"
        f"• Менеджеры пишут только из этого бота: в других местах "
        f"от нашего имени никто не пишет и кодов не спрашивает\n\n"
        f"{ce('chart')}  <b>О курсах:</b>\n"
        f"• Цены привязаны к USDT (стейблкоин = 1$)\n"
        f"• Сумма в ₽ пересчитывается по актуальному курсу\n"
        f"• Сумма в Gram пересчитывается при выборе оплаты\n"
        f"• Telegram Stars: 100 {ce('star')} = 1 USDT"
    )
    await message.answer(text, reply_markup=get_menu(message.from_user.id), parse_mode="HTML")


# ─── Реферальная страница юзера ────────────────────────────────────
#
# Покупатель видит свою ссылку, счётчики и кнопку «Поделиться» — без
# этого вирусная петля не работает: код существовал только в БД, а
# обработчик «Рефералы» был доступен лишь админам. Админам страница не
# мешает: admin-роутер подключён в main.py ПЕРВЫМ и перехватывает
# текст «Рефералы» раньше (там своя страница со статистикой).


def referral_share_url(bot_username: str, code: str) -> str:
    """Ссылка «Поделиться» через штатный диалог Telegram (t.me/share).

    Работает у ЛЮБОГО бота (switch_inline_query требует включённого
    inline-режима через BotFather — не полагаемся на это).
    """
    from urllib.parse import quote

    link = f"https://t.me/{bot_username}?start=ref_{code}"
    share_text = "Дешёвые подписки в этом боте — заходи!"
    return (
        f"https://t.me/share/url?url={quote(link, safe='')}"
        f"&text={quote(share_text)}"
    )


def referral_page_kb(bot_username: str, code: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Поделиться ссылкой",
            url=referral_share_url(bot_username, code),
        )],
        [InlineKeyboardButton(text="Каталог", callback_data="back_catalog")],
    ])


@router.message(StateFilter("*"), F.text.contains("Рефералы"))
async def show_referral_page(message: Message, state: FSMContext):
    await state.clear()

    uid = message.from_user.id
    bot_username = await get_bot_username(message.bot)
    if not bot_username:
        # Username не задан ни в .env, ни через Telegram API — ссылка была бы битой
        await message.answer(
            f"{ce('warning')}  Реферальная программа временно недоступна.\n\n"
            f"Попробуйте позже или напишите в поддержку."
        )
        return

    code = await generate_referral_code(uid)
    invited, earned = await get_referral_stats(uid)
    balance = await get_bonus_balance(uid)

    link = f"https://t.me/{bot_username}?start=ref_{code}"
    text = (
        f"{ce('referral_badge')}  <b>Пригласи друга — получи бонус</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Друг оплачивает подписку по твоей ссылке — тебе начисляется "
        f"<b>{REFERRAL_BONUS_USDT:.2f} USDT</b> бонусом. "
        f"Бонус автоматически вычитается из стоимости следующего заказа.\n\n"
        f"{ce('link')}  <b>Твоя ссылка:</b>\n"
        f"<code>{safe_html(link)}</code>\n\n"
        f"{ce('profile')}  Приглашено друзей: <b>{invited}</b>\n"
        f"{ce('wallet')}  Заработано бонусов: <b>{earned:.2f} USDT</b>\n"
        f"{ce('sparkle')}  Доступно к оплате: <b>{balance:.2f} USDT</b>\n\n"
        f"<i>{ce('info')}  Ссылку можно отправить другу в личку или опубликовать где угодно.</i>"
    )
    await message.answer(
        text,
        reply_markup=referral_page_kb(bot_username, code),
        parse_mode="HTML",
    )


# ─── Safety net: ввод данных аккаунта без FSM-состояния ────────────
#
# Фоновые авто-проверщики оплат в main.py (crypto_payment_poller и
# digiseller_payment_poller) подтверждают оплату и присылают юзеру
# «Введите данные аккаунта», но они НЕ МОГУТ установить FSM-состояние
# (FSMContext доступен только обработчикам апдейтов). Из-за этого ответ
# юзера (почта/логин) не совпадал ни с одним обработчиком и молча
# терялся. Этот хвостовой обработчик ловит такие сообщения: если у юзера
# есть заказ в статусе pending_account — текст воспринимается как ввод
# данных аккаунта. Также спасает после /start и перезапуска бота
# (MemoryStorage теряет все состояния).

@router.message(StateFilter("*"), F.text)
async def account_input_fallback(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    # Команды и кнопки меню не трогаем — у них свои обработчики
    if not text or text.startswith("/") or text in MENU_BUTTONS:
        return

    # Если мы уже в нужном состоянии — обрабатывает предыдущий хендлер,
    # сюда попасть не должны; но на всякий случай обработаем корректно
    if await state.get_state() == OrderFlow.waiting_for_account_fields:
        await input_account_field(message, state)
        return

    # Ищем последний заказ юзера, который ОПЛАЧЕН, но данные ещё не введены
    orders = await db.get_user_orders(message.from_user.id, limit=10)
    target = next((o for o in orders if o["status"] == "pending_account"), None)
    if not target:
        return  # нечего вводить — молчим (прежнее поведение)

    service = get_service_by_id(target["service_id"])
    account_fields = service.get("account_fields", []) if service else []
    if not account_fields:
        return

    # Вводим юзера в обычный сценарий ввода данных и считаем это
    # сообщение ответом на первое поле
    await state.set_state(OrderFlow.waiting_for_account_fields)
    await state.update_data(
        account_fields=account_fields,
        current_field_index=0,
        account_data={},
        order_id=target["order_id"],
    )
    await input_account_field(message, state)
