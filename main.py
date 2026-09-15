import asyncio
import logging
import sys
import os
import traceback
from datetime import datetime

# ─── Windows fix: aiodns requires SelectorEventLoop ─────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from config import BOT_TOKEN, PROXY_URL, ADMIN_IDS, CATALOG_PATH, APP_VERSION
from config import TON_CHECK_INTERVAL
from models.database import db
from handlers.user_handlers import router as user_router
from handlers.support_handlers import router as support_router
from handlers.admin_handlers import router as admin_router
from handlers.fallback_handlers import router as fallback_router
from utils.chat_dedup import DuplicateCleanupMiddleware, UserCommandDedupMiddleware
from utils.ephemeral import UserCommandCleanupMiddleware

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def notify_admins(bot: Bot, text: str):
    """Send notification to all admin users. Used for critical alerts."""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass


def renewal_kb(service_id: str):
    """Inline keyboard for subscription renewal reminders.

    «Продлить» открывает тарифы того же сервиса (обработчик svc_),
    «Каталог» — общий каталог. Если service_id неизвестен (старый заказ),
    остаётся только «Каталог».
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    if service_id:
        rows.append([InlineKeyboardButton(text="Продлить подписку", callback_data=f"svc_{service_id}")])
    rows.append([InlineKeyboardButton(text="Каталог", callback_data="back_catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cleanup_expired_orders(bot: Bot):
    """Background task: cancel orders with expired payment timeout.

    GRACE PERIOD: an order is cancelled only if it expired MORE than
    10 minutes ago. This protects against the race where a user paid
    at minute 29 but the blockchain/Digiseller confirmation only became
    visible at minute 31 — cancelling too early would kill a PAID order.
    The payment pollers can also revive a cancelled order if the payment
    is later found (see pollers below).
    """
    from emojis import ce
    GRACE_MINUTES = 10
    while True:
        try:
            from datetime import timedelta
            cutoff = (datetime.utcnow() - timedelta(minutes=GRACE_MINUTES)).isoformat()
            expired = await db.get_expired_pending_orders()
            for order in expired:
                expires_at = order.get("payment_expires_at") or ""
                if expires_at and str(expires_at) > cutoff:
                    continue  # expired recently — give payment a chance to land
                # v17: только CAS-переход pending_payment→cancelled. Раньше
                # был безусловный update_order_status: poller мог
                # ПОДТВЕРДИТЬ оплату между нашей выборкой и апдейтом —
                # оплаченный заказ оказывался «отменён» (revive не срабатывал:
                # он ловит только НЕуспешный CAS).
                transitioned = await db.try_transition_order_status(
                    order["order_id"], "pending_payment", "cancelled"
                )
                if not transitioned:
                    continue  # статус уже сменился (оплачен/активирован) — не трогаем
                logger.info(f"Auto-cancelled expired order #{order['order_id']}")
                # UX №2: проактивно гасим платёжные экраны заказа (экран
                # TON/USDT/Digiseller и QR-фото редактируются в «⌛ Время
                # оплаты истекло», кнопки деактивируются). РЕДАКТИРОВАНИЕ,
                # не удаление — политика чата не нарушается.
                try:
                    from utils.payment_ux import extinguish_expired
                    await extinguish_expired(bot, order["user_id"], order["order_id"])
                except Exception as e:
                    logger.debug(f"extinguish_expired #{order['order_id']}: {e}")
                # Notify user (остаётся в чате — стираются только дубли)
                try:
                    await bot.send_message(
                        order["user_id"],
                        f"{ce('clock')} Время оплаты заказа #{order['order_id']} истекло. "
                        f"Вы можете оформить новый через каталог.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")
        await asyncio.sleep(60)  # Check every minute


async def cleanup_account_data(bot: Bot):
    """Background task: periodically delete old account_data for security."""
    while True:
        try:
            await db.cleanup_old_account_data()
        except Exception as e:
            logger.error(f"Account data cleanup error: {e}")
        await asyncio.sleep(3600)  # Check every hour


async def rate_fallback_monitor(bot: Bot):
    """Background task: alert admins when using fallback exchange rates."""
    from services.ton_payments import _using_fallback_rate, get_gram_usd_rate, get_usd_rub_rate
    
    last_alert_time = 0
    ALERT_COOLDOWN = 3600  # Don't spam admins more than once per hour
    
    while True:
        try:
            # Force a fresh rate check
            await get_gram_usd_rate()
            await get_usd_rub_rate()
            
            if (_using_fallback_rate.get("gram_usd") or _using_fallback_rate.get("usd_rub")):
                now = asyncio.get_event_loop().time()
                if now - last_alert_time > ALERT_COOLDOWN:
                    from emojis import ce
                    gram_status = f"{ce('warning')} FALLBACK" if _using_fallback_rate.get("gram_usd") else f"{ce('check')} OK"
                    rub_status = f"{ce('warning')} FALLBACK" if _using_fallback_rate.get("usd_rub") else f"{ce('check')} OK"
                    await notify_admins(
                        bot,
                        f"{ce('warning')} <b>Фолбэк курсов!</b>\n\n"
                        f"Gram/USD: {gram_status}\n"
                        f"USD/RUB: {rub_status}\n\n"
                        f"Реальные курсы недоступны — используются запасные значения. "
                        f"Проверьте API CoinGecko/Toncenter."
                    )
                    last_alert_time = now
        except Exception as e:
            logger.error(f"Rate monitor error: {e}")
        
        await asyncio.sleep(300)  # Check every 5 minutes


async def send_renewal_reminder(bot: Bot, sub: dict) -> str | None:
    """Send ONE renewal reminder for an active subscription order.

    Stages (each delivered exactly ONCE, flags stored in DB):
      - '72h'     — подписка истекает в пределах AUTO_RENEWAL_REMINDER_HOURS;
      - '24h'     — осталось меньше суток (замещает 72ч-напоминание);
      - 'expired' — срок уже вышел (бот мог быть выключен в окно 24ч).

    Returns the stage that was sent, or None if this stage was already
    delivered (защита от спама: задача крутится каждые 30 минут).
    """
    from emojis import ce

    hours_left = float(sub.get("hours_left", 0) or 0)
    order_id = sub["order_id"]

    stage_72h_sent = bool(sub.get("renewal_reminder_sent", 0))
    stage_24h_sent = bool(sub.get("renewal_reminder_24h_sent", 0))

    # Выбор стадии напоминания
    if hours_left <= 0:
        stage, headline = "expired", "Подписка истекла"
        mark_both = True
    elif hours_left <= 24:
        stage, headline = "24h", "Подписка истекает уже завтра!"
        mark_both = True
    else:
        stage, headline = "72h", "Подписка заканчивается!"
        mark_both = False

    # Защита от спама: каждая стадия отправляется один раз
    if stage == "72h" and stage_72h_sent:
        return None
    if stage in ("24h", "expired") and stage_24h_sent:
        return None

    rub_rate = 0
    try:
        from services.ton_payments import usdt_to_rub
        rub_rate = await usdt_to_rub(1)
    except Exception:
        pass
    price_str = f"{sub['price_usdt'] * rub_rate:.0f} ₽" if rub_rate > 0 else f"{sub['price_usdt']:.2f} USDT"

    # Сколько осталось — человекочитаемо (без «-2 дн.»)
    if hours_left <= 0:
        left_str = "Срок действия истёк"
    elif hours_left < 1:
        left_str = "Осталось меньше часа"
    elif hours_left < 24:
        left_str = f"Осталось {int(hours_left)} ч."
    else:
        left_str = f"Осталось {sub.get('days_left', 0)} дн."

    await bot.send_message(
        sub["user_id"],
        f"{ce('clock')} <b>{headline}</b>\n\n"
        f"{sub['service_name']} — {sub['plan_name']}\n"
        f"{left_str}\n\n"
        f"{ce('urgency')} Продлите сейчас — {price_str}\n"
        f"Жмите кнопку ниже для продления.",
        reply_markup=renewal_kb(sub.get("service_id") or ""),
        parse_mode="HTML",
    )

    # Mark reminder sent (stage flags in DB protect from re-sends even
    # after bot restart)
    if mark_both:
        await db.update_order_status(
            order_id, "active",
            renewal_reminder_sent=1, renewal_reminder_24h_sent=1,
        )
    else:
        await db.update_order_status(order_id, "active", renewal_reminder_sent=1)

    return stage


async def marketing_background_tasks(bot: Bot):
    """Background task: drip follow-ups, renewal reminders, social proof updates."""
    from emojis import ce
    from services.marketing import (
        update_social_proof, get_drip_messages, mark_drip_sent,
        get_expiring_subscriptions,
    )
    from models.database import db
    
    # Wait 30 seconds for bot to start
    await asyncio.sleep(30)
    
    while True:
        try:
            # 1. Update social proof cache
            await update_social_proof()
            
            # 2. Drip follow-ups (every 30 minutes)
            drip_msgs = await get_drip_messages(bot)
            for msg in drip_msgs[:10]:  # Limit batch
                try:
                    await bot.send_message(
                        msg["user_id"],
                        msg["text"],
                        parse_mode="HTML",
                    )
                    await mark_drip_sent(msg["user_id"], msg["template_id"])
                    logger.info(f"Drip sent: {msg['template_id']} to user {msg['user_id']}")
                    await asyncio.sleep(2)  # Rate limit
                except Exception as e:
                    logger.warning(f"Drip failed for user {msg['user_id']}: {e}")
            
            # 3. Renewal reminders — 3 stages (72ч / 24ч / истекла),
            #    каждая стадия ровно один раз (флаги в БД)
            expiring = await get_expiring_subscriptions(bot)
            for sub in expiring[:10]:
                try:
                    stage = await send_renewal_reminder(bot, sub)
                    if stage:
                        logger.info(
                            f"Renewal reminder ({stage}) sent for order #{sub['order_id']}"
                        )
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"Renewal reminder failed for user {sub['user_id']}: {e}")
            
        except Exception as e:
            logger.error(f"Marketing background task error: {e}\n{traceback.format_exc()}")
        
        await asyncio.sleep(1800)  # Run every 30 minutes


async def _send_account_input_prompt(bot: Bot, order: dict, headline: str) -> bool:
    """Resiliently send the 'введите данные аккаунта' prompt to the buyer.

    This is THE critical message of the whole sale: if it is not delivered,
    the user is stuck with a paid order and no next step. Therefore:
      - 3 delivery attempts;
      - on HTML parse errors (bad service/plan names from catalog) fall
        back to a PLAIN-TEXT version that cannot fail;
      - marks order.account_prompt_sent = 1 on success so the recovery
        task (pending_account_notifier) stops retrying;
      - if the service has no account_fields, transitions the order to
        pending_activation instead and reports success.

    Returns True when the user was properly handled (prompt delivered or
    order moved to pending_activation), False only if user blocked the bot.
    """
    from emojis import ce
    from utils.html_utils import safe_html
    from aiogram.exceptions import TelegramForbiddenError

    order_id = order["order_id"]
    service = None
    try:
        from models.database import get_service_by_id
        service = get_service_by_id(order.get("service_id", ""))
    except Exception as e:
        logger.warning(f"Prompt #{order_id}: get_service_by_id failed: {e}")

    account_fields = (service or {}).get("account_fields", [])

    # ── No fields needed → straight to activation ──
    if not account_fields:
        await db.update_order_status(order_id, "pending_activation")
        text = (
            f"{ce('check')}   <b>{headline}</b>\n\n"
            f"{ce('clock')}   Заказ #{order_id} передан на активацию."
        )
        try:
            await bot.send_message(order["user_id"], text, parse_mode="HTML")
        except TelegramForbiddenError:
            logger.warning(f"Prompt #{order_id}: user blocked the bot")
            await db.mark_account_prompt_sent(order_id)
            return False
        except Exception as e:
            logger.warning(f"Prompt #{order_id}: HTML send failed ({e}), trying plain text")
            try:
                await bot.send_message(
                    order["user_id"],
                    f"✅ {headline}\n\nЗаказ #{order_id} передан на активацию.",
                )
            except Exception as e2:
                logger.error(f"Prompt #{order_id}: plain-text send also failed: {e2}")
                return True  # leave for recovery task to retry
        await db.mark_account_prompt_sent(order_id)
        # Гасим платёжные экраны заказа — оплата подтверждена (Б4/А2)
        await _extinguish_order_screens(bot, order)
        return True

    # ── Normal path: ask for the first account field ──
    first_field = account_fields[0]
    svc_name = safe_html(str(order.get("service_name", "")))
    plan_name = safe_html(str(order.get("plan_name", "")))
    field_label = safe_html(str(first_field.get("label", "Данные аккаунта")))
    field_hint = safe_html(str(first_field.get("placeholder", "")))

    html_text = (
        f"{ce('check')}   <b>{headline}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Заказ #{order_id}: {svc_name} — {plan_name}\n\n"
        f"{ce('key')}   <b>Введите данные аккаунта</b> для активации подписки.\n\n"
        f"<b>{field_label}</b>\n"
        f"<i>Пример: {field_hint}</i>\n\n"
        f"<i>{ce('shield')}  Данные используются только для активации и удаляются после.</i>\n"
        f"<i>{ce('mail')}  Если при входе сервис запросит код из письма — наша команда попросит его в этом чате.</i>"
    )
    plain_text = (
        f"✅ {headline}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Заказ #{order_id}: {order.get('service_name', '')} — {order.get('plan_name', '')}\n\n"
        f"🔑 Введите данные аккаунта для активации подписки.\n\n"
        f"{first_field.get('label', 'Данные аккаунта')}\n"
        f"Пример: {first_field.get('placeholder', '')}\n\n"
        f"🛡 Данные используются только для активации и удаляются после.\n"
        f"✉️ Если при входе сервис запросит код из письма — наша команда попросит его в этом чате."
    )

    from keyboards.keyboards import cancel_inline_kb

    for attempt in (1, 2, 3):
        try:
            await bot.send_message(
                order["user_id"], html_text,
                reply_markup=cancel_inline_kb(),
                parse_mode="HTML",
            )
            await db.mark_account_prompt_sent(order_id)
            logger.info(f"Prompt #{order_id}: delivered (attempt {attempt}, HTML)")
            # Гасим платёжные экраны заказа — оплата подтверждена (Б4/А2)
            await _extinguish_order_screens(bot, order)
            return True
        except TelegramForbiddenError:
            logger.warning(f"Prompt #{order_id}: user blocked the bot — giving up")
            await db.mark_account_prompt_sent(order_id)
            return False
        except Exception as e:
            logger.warning(f"Prompt #{order_id}: HTML send attempt {attempt} failed: {e}")
            # If HTML itself is broken, Telegram answers instantly with
            # "can't parse entities" — try plain text on next attempt
            if attempt == 2:
                try:
                    await bot.send_message(
                        order["user_id"], plain_text,
                        reply_markup=cancel_inline_kb(),
                    )
                    await db.mark_account_prompt_sent(order_id)
                    logger.info(f"Prompt #{order_id}: delivered via PLAIN TEXT fallback")
                    # Гасим платёжные экраны заказа — оплата подтверждена (Б4/А2)
                    await _extinguish_order_screens(bot, order)
                    return True
                except TelegramForbiddenError:
                    await db.mark_account_prompt_sent(order_id)
                    return False
                except Exception as e2:
                    logger.warning(f"Prompt #{order_id}: plain-text attempt failed: {e2}")
            await asyncio.sleep(2)

    logger.error(f"Prompt #{order_id}: ALL delivery attempts failed — recovery task will retry")
    return True


async def _extinguish_order_screens(bot: Bot, order: dict) -> None:
    """Погасить мёртвые платёжные экраны оплаченного заказа (QR, кнопки).

    Вызывается из _send_account_input_prompt: фоновые поллеры (TON/USDT,
    Tribute, Digiseller) подтверждают оплату БЕЗ нажатия кнопки, поэтому
    платёжные экраны остаются в чате «живыми» с мёртвыми кнопками.
    Гашение = редактирование (не удаление!): текстовый экран становится
    коротким «✅ Оплата подтверждена» без кнопок, у QR-фото меняется
    подпись. История покупок остаётся в чате целиком.
    """
    from utils.payment_ux import extinguish
    try:
        await extinguish(bot, order["user_id"], order["order_id"])
    except Exception as e:
        logger.debug(f"Extinguish screens for order #{order.get('order_id')}: {e}")


async def crypto_payment_poller(bot: Bot):
    """Background task: automatically check pending crypto (TON/USDT) payments.

    Instead of requiring users to manually press «Проверить оплату», this
    task polls every TON_CHECK_INTERVAL seconds and notifies the user
    automatically when payment is detected.
    """
    from emojis import ce
    from services.ton_payments import verify_payment
    from models.database import get_service_by_id, get_active_services
    from handlers.user_handlers import _post_payment_actions

    # Заказы, по которым недоплата уже доложена админу — уведомлять один раз
    underpaid_reported: set[int] = set()

    # Wait 30 seconds for bot to start
    await asyncio.sleep(30)

    while True:
        try:
            # Find all orders in pending_payment with ton/usdt payment method
            pending_orders = await db.get_orders_by_status("pending_payment", limit=200)

            for order in pending_orders:
                method = order.get("payment_method", "")
                if method not in ("ton", "usdt"):
                    continue  # Skip card/stars orders

                order_id = order["order_id"]
                price_usdt = order.get("price_usdt", 0)
                ton_amount = order.get("ton_amount", 0)

                if method == "ton" and ton_amount <= 0:
                    continue
                if method == "usdt" and price_usdt <= 0:
                    continue

                # Check payment
                expected_amount = ton_amount if method == "ton" else price_usdt
                result = await verify_payment(
                    order_id, method, expected_amount,
                    db_memo=order.get("payment_memo"),
                )

                if not result.get("paid") and result.get("error") == "amount_mismatch" \
                        and order_id not in underpaid_reported:
                    # ── Недоплата/переплата: платёж с memo найден, но сумма
                    # вне допуска. Раньше молча отбрасывался — заказ висел,
                    # юзер видел «не найдена», админ ничего не знал.
                    underpaid_reported.add(order_id)
                    amount_in = float(result.get("amount") or 0)
                    expected_amt = float(result.get("expected") or expected_amount)
                    unit = "Gram" if method == "ton" else "USDT"
                    logger.warning(
                        f"Crypto poller: order #{order_id} {method.upper()} amount mismatch: "
                        f"{amount_in:.4f} vs expected {expected_amt:.4f} {unit}"
                    )
                    try:
                        await bot.send_message(
                            order["user_id"],
                            f"{ce('warning')} <b>Платёж найден, но сумма отличается</b>\n\n"
                            f"Заказ #{order_id}: {order.get('service_name')} — {order.get('plan_name')}\n"
                            f"Ожидается: {expected_amt:.4f} {unit}\n\n"
                            f"Администратор проверит платёж вручную и свяжется с вами.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await notify_admins(
                        bot,
                        f"{ce('warning')} <b>Недоплата ({method.upper()}) по заказу #{order_id}</b>\n\n"
                        f"Получено: {amount_in:.4f} {unit}\n"
                        f"Ожидалось: {expected_amt:.4f} {unit}\n\n"
                        f"Заказ остался в ожидании оплаты. Проверьте транзакцию вручную.",
                    )

                # v17: amount_unverifiable (USDT без TONAPI_KEY / недекодируемое
                # тело — memo совпал, сумму проверить нельзя) раньше молча
                # отбрасывался: юзер платил, заказ висел до авто-отмены, админ
                # не узнавал. Теперь разово сообщаем обеим сторонам.
                if not result.get("paid") and result.get("error") == "amount_unverifiable" \
                        and order_id not in underpaid_reported:
                    underpaid_reported.add(order_id)
                    unit = "Gram" if method == "ton" else "USDT"
                    logger.warning(
                        f"Crypto poller: order #{order_id} {method.upper()} payment "
                        f"with matching memo but UNVERIFIABLE amount"
                    )
                    try:
                        await bot.send_message(
                            order["user_id"],
                            f"{ce('warning')} <b>Платёж с вашим комментарием найден</b>\n\n"
                            f"Заказ #{order_id}: сумма платежа не удалось проверить автоматически.\n"
                            f"Администратор проверит транзакцию вручную — ожидайте.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await notify_admins(
                        bot,
                        f"{ce('warning')} <b>{method.upper()}: непроверяемая сумма по заказу #{order_id}</b>\n\n"
                        f"Платёж с memo найден, но сумму верифицировать не удалось "
                        f"(нет TONAPI_KEY или тело транзакции не декодируется).\n"
                        f"Проверьте вручную и активируйте через «Новые заказы».",
                    )

                if result.get("paid"):
                    # Payment confirmed! Use CAS to prevent race with manual check
                    from datetime import datetime as dt
                    tx_hash = result.get("tx_hash", "")
                    transitioned = await db.try_transition_order_status(
                        order_id,
                        from_status="pending_payment",
                        to_status="pending_account",
                        paid_at=dt.utcnow().isoformat(),
                        ton_tx_hash=str(tx_hash),
                    )

                    if not transitioned:
                        # Maybe the cleanup task cancelled the order while the
                        # payment was still confirming — revive it: the money
                        # IS received (verified tx + memo + amount).
                        fresh = await db.get_order(order_id)
                        if fresh and fresh.get("status") == "cancelled":
                            revived = await db.try_transition_order_status(
                                order_id,
                                from_status="cancelled",
                                to_status="pending_account",
                                paid_at=dt.utcnow().isoformat(),
                                ton_tx_hash=str(tx_hash),
                            )
                            if revived:
                                logger.warning(
                                    f"Crypto poller: order #{order_id} was auto-cancelled "
                                    f"after payment — REVIVED to pending_account"
                                )
                                await notify_admins(
                                    bot,
                                    f"⚠️ Заказ #{order_id} был отменён по таймауту, "
                                    f"но оплата ({method}) подтверждена — заказ восстановлен.",
                                )
                            if revived:
                                await _send_account_input_prompt(
                                    bot, fresh, "Оплата подтверждена!"
                                )
                                # v17: пост-экшены и при revive — раньше
                                # промокод-лимит/лояльность/реферал работали
                                # только при ручной проверке кнопкой
                                await _post_payment_actions(fresh, bot)
                        continue

                    # Notify user (resilient: retries + plain-text fallback)
                    await _send_account_input_prompt(bot, order, "Оплата подтверждена!")

                    # v17: маркетинговые пост-экшены после ЛЮБОГО авто-подтверждения
                    # (лояльность, реферал-бонус, списание лимита промокода).
                    # Раньше срабатывали только из ручных кнопок «Проверить оплату»,
                    # а поллеры — ОСНОВНОЙ путь подтверждения — их пропускали:
                    # лимитированные промокоды оставались «неиспользованными».
                    try:
                        await _post_payment_actions(order, bot)
                    except Exception as e:
                        logger.warning(f"Post-payment actions failed for #{order_id}: {e}")

                    # Notify admins
                    for admin_id in ADMIN_IDS:
                        try:
                            method_name = "Gram" if method == "ton" else "USDT"
                            amount_str = f"{ton_amount:.3f} Gram" if method == "ton" else f"{price_usdt:.2f} USDT"
                            method_icon = ce('diamond') if method == "ton" else ce('dollar')
                            admin_text = (
                                f"{method_icon} "
                                f"<b>Оплата {method_name} получена! (авто-проверка)</b>\n"
                                f"Заказ #{order_id}: {order['service_name']} — {order['plan_name']}\n"
                                f"{ce('wallet')} {amount_str}\n"
                            )
                            if tx_hash:
                                admin_text += f"TX: <code>{str(tx_hash)[:20]}...</code>"
                            await bot.send_message(admin_id, admin_text, parse_mode="HTML")
                        except Exception:
                            pass

                    logger.info(f"Crypto poller: order #{order_id} auto-confirmed ({method})")

                # Small delay between order checks to avoid rate limits
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Crypto payment poller error: {e}\n{traceback.format_exc()}")

        await asyncio.sleep(TON_CHECK_INTERVAL)


async def global_exception_handler(bot: Bot, task_name: str, error: Exception):
    """Watchdog: log unhandled exceptions in background tasks and notify admins."""
    logger.error(f"Unhandled exception in '{task_name}': {error}\n{traceback.format_exc()}")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"⚠️ <b>Ошибка в фоновой задаче '{task_name}'</b>\n"
                f"<code>{str(error)[:500]}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def digiseller_payment_poller(bot: Bot):
    """Background task: automatically check pending Digiseller card payments.

    Polls Digiseller API (seller-sells) for paid invoices matching our
    pending orders and confirms them automatically.
    """
    from emojis import ce
    from services.digiseller import find_payment_for_order
    from services.ton_payments import usdt_to_rub
    from models.database import try_transition_order_status, get_service_by_id
    from handlers.user_handlers import _post_payment_actions

    # Orders already reported as underpaid — notify only once
    underpaid_reported: set[int] = set()

    # Wait for bot to start
    await asyncio.sleep(45)

    while True:
        try:
            pending_orders = await db.get_orders_by_status("pending_payment", limit=200)
            digi_orders = [o for o in pending_orders if o.get("payment_method") == "digiseller"]

            if digi_orders:
                # Resolve expected amounts once per cycle
                rub_rate = 0
                try:
                    rub_rate = await usdt_to_rub(1)
                except Exception:
                    pass

                # Учёт округления до юнитов Digiseller (см. services/digiseller.py)
                from config import DIGISELLER_UNIT_PRICE

                for order in digi_orders:
                    order_id = order["order_id"]
                    price_usdt = order.get("price_usdt", 0)

                    # СУММА: фактическая сумма из заказа (сохранена при создании
                    # ссылки) — не зависит от текущего курса USDT/RUB. Fallback —
                    # расчёт по курсу с округлением до юнитов (старые заказы).
                    expected_rub = float(order.get("digiseller_amount") or 0.0)
                    if expected_rub <= 0:
                        expected_rub = round(price_usdt * rub_rate, 2) if rub_rate > 0 else 0.0
                        if expected_rub > 0 and DIGISELLER_UNIT_PRICE > 0:
                            unit_cnt = max(1, round(expected_rub / DIGISELLER_UNIT_PRICE))
                            expected_rub = round(unit_cnt * DIGISELLER_UNIT_PRICE, 2)

                    # EMAIL: реальный email юзера, на который оформлялась оплата —
                    # главный признак для поиска продажи на площадке Digiseller.
                    buyer_email = (order.get("digiseller_email") or "").strip()

                    try:
                        result = await find_payment_for_order(
                            order_id,
                            expected_rub,
                            hours=6,
                            buyer_email=buyer_email,
                            # Защита от ложного подтверждения: продажи,
                            # оплаченные до создания ссылки, — чужие/старые.
                            not_before=order.get("digiseller_link_created_at"),
                        )
                    except Exception as e:
                        logger.warning(f"Digiseller check failed for order #{order_id}: {e}")
                        continue

                    if not result.get("paid"):
                        # ── Underpaid: платёж найден, но сумма меньше ожидаемой.
                        # Раньше молча игнорировалось — заказ висел вечно, юзер
                        # и админ ничего не знали. Теперь уведомляем ОДИН раз.
                        if result.get("underpaid") and order_id not in underpaid_reported:
                            underpaid_reported.add(order_id)
                            amount_in = float(result.get("amount_in") or 0)
                            amount_exp = float(result.get("amount_expected") or expected_rub)
                            invoice_id = result.get("invoice_id")
                            logger.warning(
                                f"Digiseller: order #{order_id} UNDERPAID: "
                                f"{amount_in:.2f} < {amount_exp:.2f} (invoice {invoice_id})"
                            )
                            try:
                                await bot.send_message(
                                    order["user_id"],
                                    f"{ce('warning')} <b>Платёж найден, но сумма отличается</b>\n\n"
                                    f"Заказ #{order_id}: {order['service_name']} — {order['plan_name']}\n"
                                    f"Ожидается: {amount_exp:.0f} ₽\n\n"
                                    f"Администратор проверит платёж вручную и свяжется с вами.",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                            await notify_admins(
                                bot,
                                f"{ce('warning')} <b>Digiseller: недоплата по заказу #{order_id}</b>\n"
                                f"{order['service_name']} — {order['plan_name']}\n"
                                f"Получено: {amount_in:.2f} ₽ / Ожидалось: {amount_exp:.2f} ₽\n"
                                f"Инвойс: <code>{invoice_id}</code>\n\n"
                                f"Проверьте вручную: если платёж корректный — "
                                f"активируйте заказ через «Новые заказы».",
                            )
                            try:
                                # v17: только комментарий — БЕЗ смены статуса.
                                # Раньше update_order_status(order_id,
                                # "pending_payment", ...) мог ОТКАТИТЬ назад
                                # заказ, подтверждённый этим же циклом через CAS.
                                await db.set_order_admin_comment(
                                    order_id,
                                    f"UNDERPAID: {amount_in:.2f}/{amount_exp:.2f} RUB, invoice {invoice_id}",
                                )
                            except Exception:
                                pass
                        continue

                    invoice_id = result.get("invoice_id")
                    # v17: защита от двойного подтверждения: один платёж с
                    # email юзера не должен подтверждать два заказа (два
                    # pending-заказа с одним email / общий email у юзеров)
                    # и не должен «переиспользоваться» новым заказом.
                    if invoice_id:
                        used_by = await db.get_order_by_digiseller_invoice(invoice_id)
                        if used_by and used_by["order_id"] != order_id:
                            if order_id not in underpaid_reported:
                                underpaid_reported.add(order_id)
                                logger.warning(
                                    f"Digiseller: invoice {invoice_id} уже привязан к заказу "
                                    f"#{used_by['order_id']} — подтверждение заказа #{order_id} отклонено"
                                )
                                await notify_admins(
                                    bot,
                                    f"{ce('warning')} <b>Digiseller: конфликт инвойсов</b>\n"
                                    f"Инвойс <code>{invoice_id}</code> уже засчитан заказу "
                                    f"#{used_by['order_id']}, но совпадает с оплатой заказа "
                                    f"#{order_id}. Проверьте вручную (два заказа с одним email?).",
                                )
                            continue
                    transitioned = await try_transition_order_status(
                        order_id, "pending_payment", "pending_account",
                        paid_at=datetime.utcnow().isoformat(),
                        payment_method="digiseller",
                        digiseller_invoice_id=int(invoice_id) if invoice_id else None,
                    )
                    if not transitioned:
                        # Возможно, cleanup отменил заказ, пока платёж
                        # подтверждался — деньги же получены: восстанавливаем.
                        fresh = await db.get_order(order_id)
                        if fresh and fresh.get("status") == "cancelled":
                            revived = await try_transition_order_status(
                                order_id, "cancelled", "pending_account",
                                paid_at=datetime.utcnow().isoformat(),
                                payment_method="digiseller",
                                digiseller_invoice_id=int(invoice_id) if invoice_id else None,
                            )
                            if revived:
                                logger.warning(
                                    f"Digiseller: order #{order_id} was auto-cancelled "
                                    f"after payment — REVIVED to pending_account"
                                )
                                await notify_admins(
                                    bot,
                                    f"⚠️ Заказ #{order_id} был отменён по таймауту, "
                                    f"но оплата картой подтверждена — заказ восстановлен.",
                                )
                                await _send_account_input_prompt(
                                    bot, fresh, "Оплата картой подтверждена!"
                                )
                                # v17: пост-экшены и при revive
                                await _post_payment_actions(fresh, bot)
                        continue

                    logger.info(f"Digiseller: order #{order_id} auto-confirmed (invoice {invoice_id})")

                    # Notify user (resilient: retries + plain-text fallback)
                    await _send_account_input_prompt(
                        bot, order, "Оплата картой подтверждена!"
                    )

                    # v17: пост-экшены при авто-подтверждении (см. crypto poller)
                    try:
                        await _post_payment_actions(order, bot)
                    except Exception as e:
                        logger.warning(f"Post-payment actions failed for #{order_id}: {e}")

                    # Notify admins
                    for admin_id in ADMIN_IDS:
                        try:
                            amount_str = f"{result.get('amount', 0):.2f} {result.get('currency', '')}".strip()
                            await bot.send_message(
                                admin_id,
                                f"{ce('credit_card')} <b>Оплата картой получена (Digiseller)!</b>\n"
                                f"Заказ #{order_id}: {order['service_name']} — {order['plan_name']}\n"
                                f"{ce('wallet')} {amount_str} ({price_usdt:.2f} USDT)\n"
                                f"Инвойс: <code>{invoice_id}</code>",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                    await asyncio.sleep(1)  # gentle pacing between orders
        except Exception as e:
            logger.error(f"Digiseller poller error: {e}\n{traceback.format_exc()}")

        from config import DIGISELLER_CHECK_INTERVAL
        await asyncio.sleep(DIGISELLER_CHECK_INTERVAL)


def _parse_tribute_shops(raw: list) -> list[tuple]:
    """Разобрать ответ GET /shops в список (id, name) без падений на полях."""
    out: list[tuple] = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("shopId") or s.get("uuid")
        name = s.get("name") or s.get("title") or s.get("shopName") or "?"
        if sid is not None:
            out.append((sid, str(name)))
    return out


async def tribute_preflight(bot: Bot):
    """Проверка конфигурации Tribute Shop API при старте (до первого покупателя).

    Ловит ошибку «shop not found» ЗАРАНЕЕ: без созданного магазина
    в @tribute POST /shop/orders отвечает 404 — покупатель видит отказ,
    админ — голый JSON в логе. Теперь:
      - магазин не создан → админ получает инструкцию по созданию;
      - TRIBUTE_SHOP_ID не совпадает ни с одним магазином ключа →
        админ получает список доступных ID;
      - магазинов несколько без TRIBUTE_SHOP_ID → предупреждение в лог
        (используется первый).
    Любая сетевая ошибка проверки НЕ мешает старту бота.
    """
    from services.tribute import shops, TributeError
    from config import TRIBUTE_SHOP_ID  # локально: на уровне модуля не импортируется
    from emojis import ce  # локальный импорт — как в соседних функциях
    try:
        raw = await shops()
    except TributeError as e:
        # Ключ может быть валиден, но /shops недоступен — не мешаем старту,
        # просто помечаем в логе (первый же заказ создаст понятную ошибку).
        logger.warning(f"Tribute preflight: список магазинов недоступен: {e}")
        return
    except Exception as e:
        logger.warning(f"Tribute preflight: {e}")
        return

    found = _parse_tribute_shops(raw)
    if not found:
        msg = (
            f"{ce('warning')} <b>Tribute: активный магазин не найден!</b>\n\n"
            "API-ключ работает, но за ним нет ни одного активного магазина —\n"
            "поэтому API и отвечал «shop not found». Оплата картой работать не будет.\n\n"
            "<b>Как исправить:</b>\n"
            "1. Открой веб-приложение @tribute → раздел «Магазин» → создай магазин\n"
            "2. Перезапусти бота — в логе появится список магазинов\n"
            "3. TRIBUTE_SHOP_ID задавать НЕ нужно (опция только для нескольких\n"
            "   магазинов — заказы идут в первый по умолчанию)"
        )
        logger.error("Tribute preflight: активный магазин не найден (см. инструкцию админам)")
        await notify_admins(bot, msg)
        return

    known = ", ".join(f"{sid} ({name})" for sid, name in found)
    if TRIBUTE_SHOP_ID and not any(str(sid) == str(TRIBUTE_SHOP_ID) for sid, _ in found):
        msg = (
            f"{ce('warning')} <b>Tribute: TRIBUTE_SHOP_ID={TRIBUTE_SHOP_ID} не найден</b>\n\n"
            f"Магазины этого API-ключа: {known}\n\n"
            f"Исправь TRIBUTE_SHOP_ID в .env (или убери строку — возьмётся первый) "
            f"и перезапусти бота."
        )
        logger.error(f"Tribute preflight: TRIBUTE_SHOP_ID={TRIBUTE_SHOP_ID} не найден; доступно: {known}")
        await notify_admins(bot, msg)
        return

    if not TRIBUTE_SHOP_ID and len(found) > 1:
        logger.warning(
            f"Tribute preflight: магазинов несколько ({known}) — заказы пойдут "
            f"в первый. Рекомендую задать TRIBUTE_SHOP_ID в .env"
        )
    else:
        logger.info(f"Tribute preflight OK: магазины: {known}")


async def tribute_payment_poller(bot: Bot):
    """Background task: автоматическая проверка оплат через Tribute Shop API.

    Каждые TRIBUTE_CHECK_INTERVAL секунд опрашивает статус КАЖДОГО
    заказа с payment_method='tribute' точным запросом по его UUID
    (GET /shop/orders/{uuid}). Никаких эвристик и подбо́ров: paid —
    только если сам этот заказ в Tribute оплачен. Публичный домен
    и вебхуки не нужны — работает на локальной машине.
    """
    from emojis import ce
    from services.tribute import get_order, TributeError
    from models.database import try_transition_order_status
    from config import TRIBUTE_CHECK_INTERVAL
    from handlers.user_handlers import _post_payment_actions

    # Wait for bot to start
    await asyncio.sleep(30)

    while True:
        try:
            pending_orders = await db.get_orders_by_status("pending_payment", limit=200)
            trib_orders = [
                o for o in pending_orders
                if o.get("payment_method") == "tribute"
                and (o.get("tribute_order_uuid") or "").strip()
            ]

            for order in trib_orders:
                order_id = order["order_id"]
                tribute_uuid = order["tribute_order_uuid"].strip()

                try:
                    data = await get_order(tribute_uuid)
                except TributeError as e:
                    logger.warning(f"Tribute check failed for order #{order_id}: {e}")
                    continue
                except Exception as e:
                    # v17: битый JSON/неожиданная ошибка сети НЕ должен прерывать
                    # весь цикл опроса остальных заказов (раньше ловили только
                    # TributeError — один битый ответ отменял проверки всех)
                    logger.warning(f"Tribute check UNEXPECTED error for order #{order_id}: {e}")
                    continue

                status = str(data.get("status", "")).lower()

                if status == "failed":
                    logger.info(
                        f"Tribute: order #{order_id} payment FAILED "
                        f"(uuid {tribute_uuid}) — ждём юзера, возможно повторит"
                    )
                    continue

                if status != "paid":
                    continue

                transitioned = await try_transition_order_status(
                    order_id, "pending_payment", "pending_account",
                    paid_at=datetime.utcnow().isoformat(),
                    payment_method="tribute",
                )
                if not transitioned:
                    # cleanup мог отменить заказ, пока шла проверка —
                    # но деньги получены: восстанавливаем (как в Digiseller)
                    fresh = await db.get_order(order_id)
                    if fresh and fresh.get("status") == "cancelled":
                        revived = await try_transition_order_status(
                            order_id, "cancelled", "pending_account",
                            paid_at=datetime.utcnow().isoformat(),
                            payment_method="tribute",
                        )
                        if revived:
                            logger.warning(
                                f"Tribute: order #{order_id} was auto-cancelled "
                                f"after payment — REVIVED to pending_account"
                            )
                            await notify_admins(
                                bot,
                                f"⚠️ Заказ #{order_id} был отменён по таймауту, "
                                f"но оплата картой (Tribute) подтверждена — "
                                f"заказ восстановлен.",
                            )
                            await _send_account_input_prompt(
                                bot, fresh, "Оплата картой подтверждена!"
                            )
                    continue

                logger.info(
                    f"Tribute: order #{order_id} auto-confirmed (uuid {tribute_uuid})"
                )

                # Notify user (resilient: retries + plain-text fallback)
                await _send_account_input_prompt(
                    bot, order, "Оплата картой подтверждена!"
                )

                # v17: пост-экшены при авто-подтверждении (см. crypto poller)
                try:
                    await _post_payment_actions(order, bot)
                except Exception as e:
                    logger.warning(f"Post-payment actions failed for #{order_id}: {e}")

                # Notify admins
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"{ce('credit_card')} <b>Оплата картой получена (Tribute)!</b>\n"
                            f"Заказ #{order_id}: {order['service_name']} — {order['plan_name']}\n"
                            f"{ce('wallet')} {order.get('price_usdt', 0):.2f} USDT\n"
                            f"UUID: <code>{tribute_uuid}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

                await asyncio.sleep(1)  # gentle pacing between orders

            # ── Перезапуски/офлайн: отменённые по таймауту tribute-заказы,
            # оплаченные пока бот был выключен. Окно 2 часа с момента
            # истечения — старые отмены не проверяем бесконечно. ──
            from datetime import timedelta as _td
            cancelled = await db.get_orders_by_status("cancelled", limit=100)
            now_utc = datetime.utcnow()
            for order in cancelled:
                if order.get("payment_method") != "tribute":
                    continue
                tribute_uuid = (order.get("tribute_order_uuid") or "").strip()
                if not tribute_uuid:
                    continue
                exp = order.get("payment_expires_at")
                if exp:
                    try:
                        exp_dt = datetime.fromisoformat(str(exp))
                        if now_utc - exp_dt > _td(hours=2):
                            continue  # слишком старая отмена — не проверяем
                    except ValueError:
                        pass
                order_id = order["order_id"]
                try:
                    data = await get_order(tribute_uuid)
                except TributeError as e:
                    logger.warning(f"Tribute revive-check failed for order #{order_id}: {e}")
                    continue
                if str(data.get("status", "")).lower() != "paid":
                    continue
                revived = await try_transition_order_status(
                    order_id, "cancelled", "pending_account",
                    paid_at=datetime.utcnow().isoformat(),
                    payment_method="tribute",
                )
                if revived:
                    logger.warning(
                        f"Tribute: cancelled order #{order_id} was PAID during "
                        f"downtime — REVIVED to pending_account"
                    )
                    await notify_admins(
                        bot,
                        f"⚠️ Заказ #{order_id} был отменён по таймауту, "
                        f"но оплата картой (Tribute) подтверждена — "
                        f"заказ восстановлен.",
                    )
                    await _send_account_input_prompt(
                        bot, order, "Оплата картой подтверждена!"
                    )
                    # v17: пост-экшены и при офлайн-revive
                    await _post_payment_actions(order, bot)
        except Exception as e:
            logger.error(f"Tribute poller error: {e}\n{traceback.format_exc()}")

        await asyncio.sleep(TRIBUTE_CHECK_INTERVAL)


async def pending_account_notifier(bot: Bot):
    """SAFETY NET: guarantee every PAID order gets its account-data prompt.

    If the payment poller confirmed the payment but crashed / lost network
    BEFORE delivering the 'введите данные аккаунта' message, the order
    would stay in pending_account forever and the user would be stuck
    with no next step. This task re-checks such orders every minute and
    re-delivers the prompt until it succeeds (flag account_prompt_sent).
    """
    # Wait for the bot to start and for the payment pollers to do their job
    await asyncio.sleep(90)

    while True:
        try:
            unnotified = await db.get_unnotified_account_orders(limit=50)
            for order in unnotified:
                logger.info(
                    f"Recovery: order #{order['order_id']} is paid but the "
                    f"account prompt was never delivered — re-sending"
                )
                await _send_account_input_prompt(
                    bot, order, "Оплата подтверждена!"
                )
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Pending account notifier error: {e}\n{traceback.format_exc()}")

        await asyncio.sleep(60)


# ─── Health server для бесплатного Render (Web Service) ────────────
# Бесплатные Web Service на Render требуют открытый HTTP-порт, иначе
# убивают процесс через ~3 минуты («No open ports detected» → SIGTERM).
# Поднимаем крошечный aiohttp-сервер, который просто отвечает 200.
# PORT задаёт сама платформа Render; локально/на воркере он пуст —
# сервер не стартует и поведение не меняется.

async def _health_ok(request):
    """v27.3: версия + ЛИЧНОСТЬ бота этого сервиса.

    Откройте https://<сервис>.onrender.com/health в браузере:
      «ok v27.3 bot=@MyShopBot (id 123456789)» — бот ДОЛЖЕН совпадать с
      тем, из чата которого открывается магазин. Если показан другой бот
      или «bot=? (getMe failed)» — в Environment ЭТОГО сервиса чужой /
      неверный BOT_TOKEN → все подписи initData отвергаются с «bad
      signature» (та самая «сессия устарела / личность не подтверждена»).
    """
    from aiohttp import web
    app = request.app
    bid = app.get("bot_id") or ""
    if bid:
        bot_part = f" bot=@{(app.get('bot_username') or '').lstrip('@')} (id {bid})"
    elif app.get("bot_checked"):
        bot_part = " bot=? (getMe failed — проверьте BOT_TOKEN)"
    else:
        bot_part = ""
    return web.Response(text=f"ok v{APP_VERSION}{bot_part}")


# v27: корень больше не «пустой ok» — люди, открывшие URL сервиса на Render,
# сразу попадают в магазин. Машинам (health-check платформы) по-прежнему
# 200 на / и /health; людям — авто-переход на /app/.
_ROOT_HTML = (
    "<!doctype html><html lang=ru><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<meta http-equiv=refresh content='0;url=./app/'>"
    "<title>SUBSTORE</title>"
    "<body style=\"margin:0;font-family:system-ui;background:#0f1c2e;"
    "color:#e8f1fb;display:flex;min-height:100vh;align-items:center;"
    "justify-content:center\">"
    "<p>Открываем магазин… <a href='./app/' style='color:#5eb1ff'>"
    "открыть вручную</a></p>"
)


async def _root_ok(request):
    from aiohttp import web
    return web.Response(text=_ROOT_HTML, content_type="text/html")


async def start_health_server(bot=None):
    from aiohttp import web
    port = int(os.getenv("PORT", "0") or 0)
    if not port:
        logger.info("PORT not set — health server skipped (local run / worker)")
        return
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", _root_ok)
    app.router.add_get("/health", _health_ok)

    # v23: Mini App — статика (/app/) и API (/api/*) в том же приложении.
    # Username для /api/session: из конфига, иначе через get_me (один раз).
    # Блок health-сервера должен работать даже в изоляции (test_health_server
    # исполняет его без bot_fixed в sys.path) — поэтому всё в try/except.
    app["bot_username"] = ""
    app["bot_id"] = ""
    app["bot_checked"] = False
    try:
        from config import BOT_USERNAME
        _bu = BOT_USERNAME
        if bot is not None:
            # v27.3: getMe при старте — СЕБЯЛИЧНОСТЬ сервиса для /health.
            # Если BOT_TOKEN чужой/битый, getMe упадёт и /health покажет
            # «bot=?» — мгновенная диагностика «bad signature».
            app["bot_checked"] = True
            try:
                me = await bot.get_me()
                _bu = _bu or me.username
                app["bot_id"] = str(me.id)
                logger.info(
                    f"Shop service identity: bot=@{me.username} (id {me.id}) — "
                    f"тот же бот виден в /health; initData подписывается ЭТИМ ботом"
                )
            except Exception as e:
                logger.error(
                    f"getMe failed — BOT_TOKEN недействителен ДЛЯ ЭТОГО СЕРВИСА: {e}. "
                    f"Магазин будет отдавать 401 «bad signature» при авторизации."
                )
        app["bot_username"] = (_bu or "").lstrip("@")
    except Exception:
        pass
    try:
        from webapp_api import setup_webapp_routes
        setup_webapp_routes(app)
    except Exception as e:
        logger.warning(f"Mini App mount failed (bot runs without web store): {e}")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server listening on 0.0.0.0:{port} — port check satisfied")


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set! Please set it in .env file.")
        sys.exit(1)

    # Initialize database
    await db.init()
    logger.info("Database initialized")

    # v17: предупреждение о шифровании данных аккаунтов. utils/crypto.py
    # без ENCRYPTION_KEY молча сохраняет логины/пароли ОТКРЫТЫМ текстом.
    # Алерт админам уходит ниже, после создания bot (см. _encryption_warning).
    from config import ENCRYPTION_KEY
    _encryption_warning = not bool(ENCRYPTION_KEY)
    if _encryption_warning:
        logger.warning(
            "ENCRYPTION_KEY не задан — данные аккаунтов покупателей "
            "сохраняются в БД БЕЗ шифрования! Задайте ENCRYPTION_KEY в .env "
            "(любая длинная случайная строка) и перезапустите бота."
        )

    # UX №10: восстановить дедуп-реестр из БД (столбик «Каталог» не растёт
    # заново после рестарта). Только при включённом дедупе.
    from config import CHAT_DEDUP_ENABLED
    from utils.chat_dedup import load_registry as _dedup_load
    if CHAT_DEDUP_ENABLED:
        try:
            await _dedup_load(db)
            logger.info("Chat dedup registry restored from DB")
        except Exception as e:
            logger.warning(f"Chat dedup registry load skipped: {e}")

    # Restore pending payment memos from DB (survives bot restart)
    from services.ton_payments import restore_memos_at_startup
    await restore_memos_at_startup()

    logger.info(f"Catalog path: {CATALOG_PATH}")
    
    # Verify catalog is readable
    from models.database import load_catalog
    try:
        catalog = load_catalog()
        svc_count = len(catalog.get('services', []))
        logger.info(f"Catalog loaded: {svc_count} services")
    except Exception as e:
        logger.error(f"FAILED to load catalog from {CATALOG_PATH}: {e}")
    
    # Restore memos for pending orders (after bot restart)
    try:
        from services.ton_payments import restore_memo_from_db
        pending = await db.get_orders_by_status('pending_payment')
        restored = 0
        for order in pending:
            memo = order.get('payment_memo')
            if memo:
                restore_memo_from_db(order['order_id'], memo)
                restored += 1
        if restored:
            logger.info(f"Restored {restored} memos for pending orders")
    except Exception as e:
        logger.warning(f"Memo restoration skipped: {e}")

    # Create bot and dispatcher
    # Setup session with proxy if needed (supports HTTP and SOCKS5)
    def _build_session():
        """Build aiogram aiohttp session with optional proxy.

        aiogram 3.x natively supports HTTP and SOCKS5/SOCKS4 proxies through
        `aiohttp-socks` (already in requirements.txt). Just pass the URL:

          - HTTP/HTTPS:  http://user:pass@host:port
          - SOCKS5:      socks5://user:pass@host:1080
          - SOCKS4:      socks4://host:1080
          - Chain (tuple): see aiogram docs for advanced use

        Returns None when PROXY_URL is empty (aiogram uses default session).
        """
        if not PROXY_URL:
            return None

        from aiogram.client.session.aiohttp import AiohttpSession

        # aiogram internally calls aiohttp_socks.ProxyConnector when needed.
        # If aiohttp-socks is missing, aiogram raises a clear RuntimeError
        # instructing the user to install it.
        session = AiohttpSession(proxy=PROXY_URL)

        # Mask credentials before logging: keep scheme + host:port only
        masked = PROXY_URL
        if "@" in PROXY_URL:
            scheme, rest = PROXY_URL.split("://", 1)
            _, host = rest.split("@", 1)
            masked = f"{scheme}://***@{host}"
        logger.info(f"Using proxy: {masked}")
        return session

    session = _build_session()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )

    # Дедупликация: стираются ТОЛЬКО повторные одинаковые сообщения
    # (юзер заново открыл каталог / повторил команду / заново открыл
    # оплату того же заказа). DEDUP_ADMINS=true (Task 35) — дедуп работает
    # и в админских чатах; критичные экраны уникальны и не совпадают.
    bot.session.middleware.register(DuplicateCleanupMiddleware())

    dp = Dispatcher()

    # Гигиена чата: команды юзера стираются ТОЛЬКО как дубли — повторная
    # /catalog стирает предыдущую /catalog. DEDUP_ADMINS=false исключает
    # админские чаты (по умолчанию дедуп работает во всех чатах, Task 35).
    dp.message.outer_middleware(UserCommandDedupMiddleware())

    # Task 35 (Ephemeral-растворение, группа G): КАЖДАЯ команда юзера и
    # нажатие reply-кнопки меню растворяются через ~5 с — во всех личных
    # чатах, включая админские (решение владельца «во всех чатах»).
    # Регистрация ПОСЛЕ дедуп-middleware: тот сначала запоминает команду
    # в реестре. Выключается в .env: EPHEMERAL_ENABLED=false
    dp.message.outer_middleware(UserCommandCleanupMiddleware())

    # Global error handler: expired callback queries, blocked users, etc.
    # must never crash update processing (keeps logs clean on flaky networks).
    @dp.error()
    async def on_update_error(event: ErrorEvent):
        exc = event.exception
        msg = str(exc)
        if isinstance(exc, TelegramBadRequest) and (
            "query is too old" in msg or "query ID is invalid" in msg
        ):
            # Click delivered late (e.g. after proxy/network reconnect) —
            # nothing to answer anymore, business logic already ran.
            logger.warning("Expired callback query ignored: %s", msg)
            return True
        if isinstance(exc, TelegramBadRequest) and "message is not modified" in msg:
            logger.debug("Ignored redundant message edit: %s", msg)
            return True
        if isinstance(exc, TelegramForbiddenError):
            logger.warning("User blocked the bot; update skipped")
            return True
        logger.error(
            "Update processing failed: %s\n%s",
            exc,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        return True  # handled — keep polling loop quiet

    # Register routers — admin first so admin-specific handlers take priority
    # (e.g., "Ред. каталог" must match before "Каталог", "Новые заказы" before "Заказы")
    dp.include_router(admin_router)
    # Support tickets BEFORE user_router: FSM handlers here must beat the
    # generic account_input_fallback in user_handlers.
    dp.include_router(support_router)
    dp.include_router(user_router)
    # Fallback LAST: вежливый ответ на устаревшие инлайн-кнопки — сюда
    # попадают только callback'и, не совпавшие ни с одним хендлером выше.
    dp.include_router(fallback_router)

    # Start background tasks with error watchdog
    def _start_task(coro, name: str):
        """Start background task and attach global_exception_handler callback."""
        task = asyncio.create_task(coro)
        def _on_done(t: asyncio.Task):
            if not t.cancelled() and (exc := t.exception()):
                asyncio.create_task(global_exception_handler(bot, name, exc))
        task.add_done_callback(_on_done)
        return task

    _start_task(cleanup_expired_orders(bot), "cleanup_expired_orders")
    # UX №10: периодический сброс дедуп-реестра в SQLite (write-behind)
    if CHAT_DEDUP_ENABLED:
        from utils.chat_dedup import flusher_loop as _dedup_flusher
        _start_task(_dedup_flusher(db), "chat_dedup_flusher")
    _start_task(marketing_background_tasks(bot), "marketing_background_tasks")
    _start_task(cleanup_account_data(bot), "cleanup_account_data")
    _start_task(rate_fallback_monitor(bot), "rate_fallback_monitor")
    _start_task(crypto_payment_poller(bot), "crypto_payment_poller")
    # SAFETY NET: re-delivers the account-data prompt for paid orders
    # whose prompt delivery failed (network/HTML errors in pollers)
    _start_task(pending_account_notifier(bot), "pending_account_notifier")
    # Start Digiseller card payment poller (if configured)
    from config import (
        DIGISELLER_SELLER_ID, DIGISELLER_API_KEY, DIGISELLER_PRODUCT_ID,
        DIGISELLER_UNIT_PRICE, DIGISELLER_PRODUCT_CURRENCY, DIGISELLER_CURRENCY,
        DIGISELLER_SKIP_AMOUNT_CHECK, USDT_RUB_RATE,
    )
    if DIGISELLER_SELLER_ID and DIGISELLER_API_KEY and DIGISELLER_PRODUCT_ID:
        logger.info(
            f"Digiseller config: "
            f"product_id={DIGISELLER_PRODUCT_ID}, "
            f"currency={DIGISELLER_CURRENCY}, "
            f"product_currency={DIGISELLER_PRODUCT_CURRENCY}, "
            f"unit_price={DIGISELLER_UNIT_PRICE}, "
            f"usdt_rub_rate={USDT_RUB_RATE}, "
            f"skip_amount_check={DIGISELLER_SKIP_AMOUNT_CHECK}"
        )
        if not DIGISELLER_SKIP_AMOUNT_CHECK:
            logger.warning(
                "Digiseller: DIGISELLER_SKIP_AMOUNT_CHECK=false — жёсткая "
                "проверка суммы. КАРТОЧНЫЕ ПЛАТЕЖИ ЧЕРЕЗ PAYMASTER БУДУТ "
                "ОТКЛОНЯТЬСЯ из-за конвертации RUB→WMT. "
                "Установите DIGISELLER_SKIP_AMOUNT_CHECK=true в .env"
            )
        _start_task(digiseller_payment_poller(bot), "digiseller_payment_poller")

    # Start Tribute card payment poller (if configured) — точное
    # подтверждение по UUID заказа, работает без публичного домена
    from config import TRIBUTE_API_KEY, TRIBUTE_SHOP_ID, TRIBUTE_CHECK_INTERVAL
    if TRIBUTE_API_KEY:
        logger.info(
            f"Tribute config: shop_id={TRIBUTE_SHOP_ID or '(первый магазин)'}, "
            f"check_interval={TRIBUTE_CHECK_INTERVAL}s"
        )
        _start_task(tribute_payment_poller(bot), "tribute_payment_poller")
        # Preflight: проверить магазин ДО первого покупателя — иначе
        # конфиг-ошибка проявится только как 404 на кассе
        _start_task(tribute_preflight(bot), "tribute_preflight")

    # Notify admins that bot started
    try:
        from emojis import ce
        await notify_admins(bot, f"{ce('check')} Бот запущен и готов к работе!")
    except Exception:
        pass

    # v17: алерт о нешифрованных данных аккаунтов — после создания bot
    if _encryption_warning:
        try:
            await notify_admins(
                bot,
                "⚠️ <b>ENCRYPTION_KEY не задан!</b>\n\n"
                "Данные аккаунтов покупателей (логины/пароли) хранятся в БД "
                "<b>открытым текстом</b>.\n\n"
                "Добавьте в .env строку\n"
                "<code>ENCRYPTION_KEY=длинная-случайная-строка</code>\n"
                "и перезапустите бота — новые заказы будут шифроваться.",
            )
        except Exception:
            pass

    # v23: Mini App — кнопка «Магазин» в меню чата (круглая кнопка слева
    # от поля ввода). Ставится только если задан WEBAPP_URL в .env.
    from config import WEBAPP_URL
    if WEBAPP_URL:
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🛍 Магазин",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/app/"),
                )
            )
            logger.info(f"Mini App menu button set → {WEBAPP_URL}/app/")
        except Exception as e:
            logger.warning(f"Mini App menu button setup failed: {e}")

    logger.info("Bot starting...")

    # Открываем HTTP-порт, если платформа требует (Render Free Web Service)
    try:
        await start_health_server(bot)
    except Exception as e:
        logger.warning(f"Health server failed to start: {e}")

    # Start polling
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
