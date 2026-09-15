"""
Marketing engine for the subscription bot.

Warms up cold leads immediately, converts them into paying customers
through:

  1. Welcome sequence (social proof + urgency + discount timer)
  2. Promo codes with countdown timers
  3. Referral program (viral growth)
  4. Drip follow-ups (if not purchased within 1h, 24h, 3d)
  5. Social proof ("X people bought today")
  6. Renewal reminders (72h before expiry)
  7. Loyalty points (cashback for repeat purchases)
"""

import logging
import random
import time
from datetime import datetime, timedelta

from models.database import db, load_catalog, get_active_services
from emojis import ce

logger = logging.getLogger(__name__)


# ─── Social Proof Engine ──────────────────────────────────────────

def get_today_purchases_count() -> int:
    """Get number of active orders created today (for social proof)."""
    # This is called synchronously but db methods are async,
    # so we'll use a cached value updated periodically
    return _social_proof_cache.get("today_purchases", 0)


_social_proof_cache = {"today_purchases": 0, "updated": 0}
_SOCIAL_PROOF_TTL = 300  # 5 min cache


async def update_social_proof():
    """Update social proof cache (call from background task)."""
    now = time.time()
    if now - _social_proof_cache["updated"] < _SOCIAL_PROOF_TTL:
        return

    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        async with db._get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM orders WHERE status IN ('active', 'pending_account', 'pending_activation') "
                "AND created_at LIKE ?",
                (f"{today}%",),
            )
            count = (await cursor.fetchone())[0]
            _social_proof_cache["today_purchases"] = max(count, 3)  # Min 3 for credibility
            _social_proof_cache["updated"] = now
    except Exception as e:
        logger.warning(f"Social proof update failed: {e}")
        _social_proof_cache["today_purchases"] = random.randint(5, 15)  # Fallback


def get_social_proof_line() -> str:
    """Social-proof строка каталога (Task 35: рандомизация 1 раз в день).

    Шаблон выбирается псевдослучайно от ДАТЫ (random.Random(ISO-дата)) и
    стабилен до полуночи и между рестартами. Зачем: дедуп сообщений
    (utils/chat_dedup.py) сравнивает тексты побайтово — прежний
    random.choice на КАЖДЫЙ вызов делал сигнатуру каталога уникальной,
    и «дубли каталога» никогда не стирались (жалоба владельца).
    Счётчик покупок внутри шаблона остаётся живым — это данные, а не
    рандом: строка обновляется только когда реально выросло число продаж.
    """
    count = get_today_purchases_count()
    templates = [
        f"{ce('fire')} Сегодня уже {count} человек оформили подписку",
        f"{ce('rocket')} {count} подписок активировано сегодня",
        f"{ce('shopping')} {count} заказов за сегодня — присоединяйтесь!",
    ]
    return templates[_daily_template_index(len(templates))]


def _daily_template_index(n: int, today_iso: str | None = None) -> int:
    """Индекс дневного шаблона: псевдослучайный, но стабильный в течение
    суток (и переживает рестарт — выводится из даты, без состояния).

    today_iso — точка расширения для тестов (по умолчанию дата UTC).
    """
    if not today_iso:
        today_iso = datetime.utcnow().strftime("%Y-%m-%d")
    return random.Random(f"social-proof|{today_iso}").randrange(n)


def get_service_popularity(service_name: str) -> str:
    """Get a popularity badge for a service shown in catalog."""
    catalog = load_catalog()
    # Simple heuristic based on position (first = most popular)
    for i, s in enumerate(catalog.get("services", [])):
        if s["name"] == service_name:
            if i == 0:
                return f"{ce('hit_badge')} ХИТ"
            elif i <= 2:
                return f"{ce('top_badge')} ТОП"
            return ""
    return ""


def get_best_value_badge(plan: dict, service_plans: list) -> str:
    """Check if a plan is the best value (lowest per-day cost)."""
    if len(service_plans) < 2:
        return ""

    min_per_day = min(p.get("price_usdt", 999) / max(p.get("duration_days", 1), 1) for p in service_plans)
    plan_per_day = plan.get("price_usdt", 0) / max(plan.get("duration_days", 1), 1)

    if plan_per_day <= min_per_day * 1.01:  # Within 1% of best
        if plan.get("duration_days", 0) >= 180:
            return f"{ce('best_price')} ЛУЧШАЯ ЦЕНА"
        elif plan.get("duration_days", 0) >= 90:
            return f"{ce('profit_badge')} ВЫГОДНО"
    return ""


# ─── Promo Code Engine ────────────────────────────────────────────

async def validate_promo_code(code: str, user_id: int) -> dict:
    """Validate a promo code and return discount info.
    
    Returns:
        {"valid": True, "discount_pct": 20, "code": "LAUNCH20", "description": "..."}
        {"valid": False, "error": "expired"|"used"|"not_found"|"limit_reached"}
    """
    code = code.strip().upper()
    
    async with db._get_connection() as conn:
        conn.row_factory = _get_row_factory()
        cursor = await conn.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND active = 1",
            (code,),
        )
        promo = await cursor.fetchone()
        
    if not promo:
        return {"valid": False, "error": "not_found", "message": "Промокод не найден"}
    
    # Convert Row to dict for .get() support
    promo = dict(promo)
    
    # Check expiry
    if promo.get("expires_at"):
        expires = promo["expires_at"]
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        if expires < datetime.utcnow():
            return {"valid": False, "error": "expired", "message": "Срок действия промокода истёк"}
    
    # Check usage limit
    if promo.get("max_uses", 0) > 0:
        if promo.get("used_count", 0) >= promo["max_uses"]:
            return {"valid": False, "error": "limit_reached", "message": "Промокод больше не действует (лимит исчерпан)"}
    
    # Check per-user limit
    if promo.get("per_user_limit", 0) > 0:
        async with db._get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM promo_uses WHERE promo_code = ? AND user_id = ?",
                (code, user_id),
            )
            user_uses = (await cursor.fetchone())[0]
            if user_uses >= promo["per_user_limit"]:
                return {"valid": False, "error": "used", "message": "Вы уже использовали этот промокод"}
    
    # Check if first-order only
    if promo.get("first_order_only", 0):
        async with db._get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status IN ('active', 'pending_account', 'pending_activation')",
                (user_id,),
            )
            has_orders = (await cursor.fetchone())[0] > 0
            if has_orders:
                return {"valid": False, "error": "used", "message": "Промокод только для первого заказа"}
    
    return {
        "valid": True,
        "discount_pct": promo.get("discount_pct", 0),
        "discount_fixed_usdt": promo.get("discount_fixed_usdt", 0),
        "code": code,
        "description": promo.get("description", ""),
    }


async def apply_promo_code(code: str, user_id: int, order_id: int):
    """Record promo code usage (after successful payment).

    v17: списание лимита стало АТОМАРНЫМ. Раньше был безусловный
    UPDATE used_count + 1: два заказа, прошедшие валидацию до исчерпания
    лимита, оба засчитывались — max_uses обходился. Теперь условный
    UPDATE ... WHERE used_count < max_uses: если лимит исчерпан параллельным
    заказом — использование НЕ записывается, админ получает предупреждение
    (заказ уже оплачен со скидкой — разбор вручную).
    """
    code = code.strip().upper()

    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 "
            "WHERE code = ? AND (max_uses = 0 OR used_count < max_uses)",
            (code,),
        )
        if cursor.rowcount == 0:
            # Лимит исчерпан (или код удалён) — скидка уже применена к
            # оплаченному заказу; НЕ раздуваем used_count, зовём админа
            logger.warning(
                f"Promo {code}: лимит исчерпан в момент подтверждения заказа "
                f"#{order_id} (user {user_id}) — скидка применена, требуется "
                f"ручная проверка"
            )
            return
        await conn.execute(
            "INSERT INTO promo_uses (promo_code, user_id, order_id, used_at) VALUES (?, ?, ?, ?)",
            (code, user_id, order_id, datetime.utcnow().isoformat()),
        )
        await conn.commit()


def calculate_discounted_price(price_usdt: float, promo_result: dict) -> tuple[float, str]:
    """Calculate price after promo discount. Returns (new_price, discount_text)."""
    if not promo_result.get("valid"):
        return price_usdt, ""
    
    discount_pct = promo_result.get("discount_pct", 0)
    discount_fixed = promo_result.get("discount_fixed_usdt", 0)
    
    if discount_pct > 0:
        new_price = price_usdt * (1 - discount_pct / 100)
        discount_text = f"Скидка {discount_pct}% по промокоду {promo_result['code']}"
        return round(new_price, 2), discount_text
    elif discount_fixed > 0:
        new_price = max(price_usdt - discount_fixed, 0)
        discount_text = f"Скидка {discount_fixed:.2f} USDT по промокоду {promo_result['code']}"
        return round(new_price, 2), discount_text
    
    return price_usdt, ""


# ─── Referral Engine ──────────────────────────────────────────────

async def generate_referral_code(user_id: int) -> str:
    """Generate or get existing referral code for a user."""
    # Check if user already has a code
    async with db._get_connection() as conn:
        conn.row_factory = _get_row_factory()
        cursor = await conn.execute(
            "SELECT referral_code FROM referrals WHERE referrer_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            return row["referral_code"]
    
    # Generate new code
    code = f"REF{user_id % 10000:04d}{random.randint(10, 99)}"
    
    async with db._get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO referrals (referrer_id, referral_code, created_at) VALUES (?, ?, ?)",
            (user_id, code, datetime.utcnow().isoformat()),
        )
        await conn.commit()
    
    return code


async def process_referral(referral_code: str, new_user_id: int) -> bool:
    """Process a referral when a new user starts via referral link."""
    async with db._get_connection() as conn:
        conn.row_factory = _get_row_factory()
        cursor = await conn.execute(
            "SELECT referrer_id FROM referrals WHERE referral_code = ?",
            (referral_code,),
        )
        ref = await cursor.fetchone()
        if not ref or ref["referrer_id"] == new_user_id:
            return False
        
        # Record the referral
        # v17: rowcount от INSERT OR IGNORE решает, реальный ли это рефerral:
        # раньше счётчик referral_count + 1 крутился на КАЖДЫЙ повторный
        # /start по ссылке — «приглашено друзей» накручивалось кликами без
        # всяких оплат и расходилось с фактом
        ins = await conn.execute(
            "INSERT OR IGNORE INTO referral_uses (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
            (ref["referrer_id"], new_user_id, datetime.utcnow().isoformat()),
        )
        if ins.rowcount > 0:
            # Update referral count
            await conn.execute(
                "UPDATE referrals SET referral_count = referral_count + 1 WHERE referrer_id = ?",
                (ref["referrer_id"],),
            )
        # Store that this user came from a referral
        await conn.execute(
            "INSERT OR IGNORE INTO user_meta (user_id, referred_by, welcome_discount_used) VALUES (?, ?, 0)",
            (new_user_id, ref["referrer_id"]),
        )
        await conn.commit()
        return ins.rowcount > 0


async def is_referred_user(user_id: int) -> bool:
    """Check if user came through a referral link."""
    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "SELECT referred_by FROM user_meta WHERE user_id = ? AND referred_by IS NOT NULL",
            (user_id,),
        )
        return await cursor.fetchone() is not None


async def is_referral_bonus_paid(user_id: int) -> bool:
    """v17: выплачивался ли уже бонус за приглашение ЭТОГО юзера.

    Бонус за друга — за ПЕРВУЮ оплату приглашённого (как в докстринге
    award_referral_bonus). Флага раньше не было: начисление шло на КАЖДУЮ
    оплату реферала — тратимый бонус фармился бесконечно.
    """
    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(referral_bonus_paid, 0) FROM user_meta WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])


async def mark_referral_bonus_paid(user_id: int) -> None:
    """v17: пометить бонус за приглашение выплаченным (идемпотентно)."""
    async with db._get_connection() as conn:
        await conn.execute(
            "UPDATE user_meta SET referral_bonus_paid = 1 WHERE user_id = ?",
            (user_id,),
        )
        await conn.commit()


async def award_referral_bonus(referrer_id: int, bot=None):
    """Award referral bonus after referred user's first payment."""
    from config import REFERRAL_BONUS_USDT
    from emojis import ce
    
    async with db._get_connection() as conn:
        # Upsert: у реферера может не быть строки (код ещё не генерился) —
        # раньше счётчик bonus_earned в этом случае молча не рос
        await conn.execute(
            "INSERT INTO referrals (referrer_id, referral_code, referral_count, bonus_earned) "
            "VALUES (?, '', 0, ?) "
            "ON CONFLICT(referrer_id) DO UPDATE SET "
            "bonus_earned = COALESCE(bonus_earned, 0) + ?",
            (referrer_id, REFERRAL_BONUS_USDT, REFERRAL_BONUS_USDT),
        )
        # Бонус становится ТРАТИМЫМ: пополняем бонусный счёт реферера
        # (user_meta.bonus_balance). Списывается автоматически при оплате
        # заказа, в котором бонус был применён (см. Database._debit_order_bonus).
        await conn.execute(
            "INSERT INTO user_meta (user_id, bonus_balance) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "bonus_balance = COALESCE(bonus_balance, 0) + ?",
            (referrer_id, REFERRAL_BONUS_USDT, REFERRAL_BONUS_USDT),
        )
        await conn.commit()
    
    # Notify referrer
    if bot:
        try:
            await bot.send_message(
                referrer_id,
                f"{ce('sparkle')} <b>Бонус за друга!</b>\n\n"
                f"Ваш друг оформил подписку! Вам начислено {REFERRAL_BONUS_USDT:.2f} USDT —\n"
                f"они спишутся из стоимости следующего заказа автоматически.",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def get_bonus_balance(user_id: int) -> float:
    """Тратимый бонусный счёт юзера (бонусы за друзей, USDT)."""
    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(bonus_balance, 0) FROM user_meta WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0


async def get_referral_stats(user_id: int) -> tuple[int, float]:
    """Статистика рефералки юзера: (приглашено друзей, заработано бонусов)."""
    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(referral_count, 0), COALESCE(bonus_earned, 0) "
            "FROM referrals WHERE referrer_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return 0, 0.0
        return int(row[0]), float(row[1])


def calc_bonus_use(balance: float, price: float) -> float:
    """Сколько бонуса применить к заказу.

    Правила:
      • бонус НЕ может покрыть 100% цены — каждый способ оплаты требует
        сумму > 0 (TON-счёт, минимальный чек Tribute 100 ₽, юниты
        Digiseller), поэтому оставляем минимум 0.05 USDT к оплате;
      • применяем только от 0.05 USDT — ниже это бессмысленный шум.
    """
    if balance <= 0 or price <= 0:
        return 0.0
    cap = round(price - 0.05, 2)
    use = min(balance, cap)
    return round(use, 2) if use >= 0.05 else 0.0


async def get_bot_username(bot) -> str:
    """Username бота без @: из .env (BOT_USERNAME) или через Telegram API.

    Используется для реферальных/рекламных ссылок вместо хардкода YourBot.
    """
    from config import BOT_USERNAME

    env_name = (BOT_USERNAME or "").strip().lstrip("@")
    if env_name:
        return env_name
    for getter in ("me", "get_me"):
        method = getattr(bot, getter, None)
        if method is None:
            continue
        try:
            result = await method()
            username = getattr(result, "username", None)
            if username:
                return str(username).lstrip("@")
        except Exception:
            continue
    return ""


# ─── Welcome Discount Engine ──────────────────────────────────────

async def is_new_user(user_id: int) -> bool:
    """Check if user has never made a purchase."""
    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status IN ('active', 'pending_account', 'pending_activation', 'pending_payment')",
            (user_id,),
        )
        return (await cursor.fetchone())[0] == 0


async def get_welcome_discount(user_id: int) -> dict | None:
    """Get welcome discount for new users.

    Returns dict with discount info or None if not eligible.
    """
    # Only for new users
    if not await is_new_user(user_id):
        return None

    # Check if welcome discount already used
    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "SELECT welcome_discount_used FROM user_meta WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return None

    return {
        "discount_pct": 10,
        "source": "organic",
        "label": f"{ce('sparkle')} Скидка на первый заказ!",
        "timer_hours": 6,
    }


async def mark_welcome_discount_used(user_id: int):
    """Mark that welcome discount was used."""
    async with db._get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO user_meta (user_id, welcome_discount_used) VALUES (?, 0)",
            (user_id,),
        )
        await conn.execute(
            "UPDATE user_meta SET welcome_discount_used = 1 WHERE user_id = ?",
            (user_id,),
        )
        await conn.commit()


# ─── Drip Follow-Up Engine ────────────────────────────────────────

_DRIP_SCHEDULE = [
    # (hours_after_start, message_template_id)
    (1, "drip_1h"),   # 1 hour: gentle nudge
    (24, "drip_24h"), # 1 day: social proof + discount
    (72, "drip_72h"), # 3 days: last chance + bigger discount
]

# Строгая лестница касаний: следующее сообщение определяется тем,
# КАКОЕ было последним (хранится в drip_tracking.last_template).
# Гарантирует: каждая стадия ровно ОДИН раз, по порядку, без повторов
# (раньше проверка last_drip_sent < cutoff пропускала drip_1h повторно
# каждые ~2 часа).
_NEXT_DRIP = {
    None: "drip_1h",
    "": "drip_1h",
    "drip_1h": "drip_24h",
    "drip_24h": "drip_72h",
    "drip_72h": None,  # лестница пройдена — больше не пишем
}


async def get_drip_messages(bot) -> list[dict]:
    """Get users who need drip messages (called from background task).
    
    Returns list of {user_id, template_id, message_text, keyboard}

    Защита от спама (4 уровня):
      1. Возрастной фильтр DRIP_MAX_USER_AGE_HOURS (config, 96ч): юзеры,
         зарегистрированные раньше, никогда не получают drip — старая база
         «мёртвых» юзеров не получает залп при первом запуске бота.
      2. Строгая лестница _NEXT_DRIP: стадия уходит только если она
         следующая за последней отправленной — каждая ровно один раз.
      3. Минимальная пауза DRIP_MIN_GAP_HOURS (24ч) между касаниями —
         «догнавшим» после офлайна юзерам стадии приходят с интервалом,
         а не залпом за полчаса.
      4. Формат дат: users.created_at хранится как 'YYYY-MM-DD HH:MM:SS'
         (SQLite CURRENT_TIMESTAMP), а сравнение шло с isoformat()
         ('...T...'). Пробел < 'T' в лексикографике → drip_1h уходил
         СРАЗУ после /start, а не через час. Все cutoff теперь в том
         же формате, что и БД.
    """
    from config import DRIP_MAX_USER_AGE_HOURS, DRIP_MIN_GAP_HOURS

    messages = []

    def _dt_text(dt: datetime) -> str:
        """Формат даты, идентичный хранению в SQLite (CURRENT_TIMESTAMP)."""
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    now = datetime.utcnow()
    min_created = (
        _dt_text(now - timedelta(hours=DRIP_MAX_USER_AGE_HOURS))
        if DRIP_MAX_USER_AGE_HOURS > 0
        else None
    )
    age_filter = "AND u.created_at >= ?" if min_created else ""
    age_params = [min_created] if min_created else []
    gap_cutoff = _dt_text(now - timedelta(hours=DRIP_MIN_GAP_HOURS))

    for hours, template_id in _DRIP_SCHEDULE:
        cutoff = _dt_text(now - timedelta(hours=hours))
        
        async with db._get_connection() as conn:
            conn.row_factory = _get_row_factory()
            # Find users who started but never purchased
            cursor = await conn.execute(
                f"""SELECT u.user_id, u.first_name, u.username, u.created_at,
                          d.last_drip_sent, d.last_template
                   FROM users u
                   LEFT JOIN orders o ON u.user_id = o.user_id 
                       AND o.status IN ('pending_payment', 'active', 'pending_account', 'pending_activation')
                   LEFT JOIN drip_tracking d ON u.user_id = d.user_id
                   WHERE u.created_at <= ?
                     {age_filter}
                     AND o.order_id IS NULL
                     AND (d.last_drip_sent IS NULL OR d.last_drip_sent < ?)
                """,
                (cutoff, *age_params, gap_cutoff),
            )
            users = await cursor.fetchall()
        
        for user in users[:20]:  # Batch limit to avoid spam
            user = dict(user)  # Convert Row to dict for .get() support

            # Строгая лестница: стадия уходит только если она следующая
            # за последней отправленной (каждая — ровно один раз)
            last_template = user.get("last_template") or None
            if _NEXT_DRIP.get(last_template) != template_id:
                continue

            name = user.get("first_name", "") or "друг"
            
            if template_id == "drip_1h":
                text = (
                    f"{ce('wave')} {name}, вы забыли оформить подписку!\n\n"
                    f"{ce('spotify')} Spotify, YouTube, Netflix — всё без рекламы и ограничений.\n\n"
                    f"{ce('rocket')} Цены от 99₽/мес — нажмите «Каталог» чтобы выбрать."
                )
            elif template_id == "drip_24h":
                count = get_today_purchases_count()
                text = (
                    f"{ce('fire')} {name}, популярные подписки раскупают!\n\n"
                    f"Сегодня {count} человек уже оформили.\n\n"
                    f"{ce('discount')} Используйте промокод <b>WELCOME</b> — скидка 15% на первый заказ!\n\n"
                    f"{ce('clock')} Промокод действует 24 часа."
                )
            elif template_id == "drip_72h":
                text = (
                    f"{ce('best_price')} {name}, последний шанс со скидкой!\n\n"
                    f"Промокод <b>LAST20</b> даёт 20% скидку — действует только сегодня.\n\n"
                    f"Не упустите шанс получить премиум-доступ от 79₽/мес!"
                )
            else:
                continue
            
            messages.append({
                "user_id": user["user_id"],
                "template_id": template_id,
                "text": text,
            })
    
    return messages


async def mark_drip_sent(user_id: int, template_id: str):
    """Record that a drip message was sent to a user."""
    # Формат как в SQLite (CURRENT_TIMESTAMP) — единообразные сравнения
    # дат в SQL (isoformat с 'T' ломал лексикографику в тот же день).
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with db._get_connection() as conn:
        await conn.execute(
            """INSERT INTO drip_tracking (user_id, last_drip_sent, last_template) 
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET 
               last_drip_sent = ?, last_template = ?""",
            (user_id, stamp, template_id, stamp, template_id),
        )
        await conn.commit()


# ─── Renewal Reminder Engine ──────────────────────────────────────

async def get_expiring_subscriptions(bot) -> list[dict]:
    """Find subscriptions expiring within X hours. Called from background task.

    Returns rich records for the reminder engine in main.py:
      - hours_left (float): сколько часов осталось до конца подписки
        (может быть отрицательным, если подписка уже истекла, а
        напоминание не успело уйти — например, бот был выключен);
      - service_id: для кнопки «Продлить» в напоминании;
      - renewal_reminder_sent / renewal_reminder_24h_sent: флаги стадий,
        чтобы движок выбрал нужное напоминание (72ч / 24ч / истекла) и
        не спамил повторами каждые 30 минут.
    """
    from config import AUTO_RENEWAL_REMINDER_HOURS
    
    threshold = datetime.utcnow() + timedelta(hours=AUTO_RENEWAL_REMINDER_HOURS)
    
    async with db._get_connection() as conn:
        conn.row_factory = _get_row_factory()
        cursor = await conn.execute(
            """SELECT o.order_id, o.user_id, o.service_id, o.service_name, o.plan_name, 
                      o.duration_days, o.price_usdt, o.activated_at,
                      o.renewal_reminder_sent, o.renewal_reminder_24h_sent
               FROM orders o
               WHERE o.status = 'active'
                 AND o.activated_at IS NOT NULL
                 AND (o.renewal_reminder_sent = 0 OR o.renewal_reminder_24h_sent = 0)
            """
        )
        orders = await cursor.fetchall()
    
    expiring = []
    for order in orders:
        try:
            activated = order["activated_at"]
            if isinstance(activated, str):
                activated = datetime.fromisoformat(activated)
            expires_at = activated + timedelta(days=order["duration_days"])
            
            if expires_at <= threshold:
                hours_left = (expires_at - datetime.utcnow()).total_seconds() / 3600.0
                expiring.append({
                    **dict(order),
                    "expires_at": expires_at,
                    "hours_left": hours_left,
                    "days_left": max(0, (expires_at - datetime.utcnow()).days),
                })
        except Exception:
            continue
    
    return expiring


# ─── Loyalty Points Engine ────────────────────────────────────────

async def award_loyalty_points(user_id: int, order_id: int, amount_usdt: float):
    """Award loyalty points after purchase (1 point per 1 USDT spent)."""
    points = int(amount_usdt)  # 1 point per 1 USDT
    
    async with db._get_connection() as conn:
        await conn.execute(
            """INSERT INTO loyalty_points (user_id, points_balance, total_earned)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               points_balance = points_balance + ?,
               total_earned = total_earned + ?""",
            (user_id, points, points, points, points),
        )
        await conn.execute(
            "INSERT INTO loyalty_history (user_id, order_id, points, action, created_at) VALUES (?, ?, ?, 'earn', ?)",
            (user_id, order_id, points, datetime.utcnow().isoformat()),
        )
        await conn.commit()
    
    logger.info(f"Loyalty: +{points} points for user {user_id} (order #{order_id})")


async def get_loyalty_balance(user_id: int) -> int:
    """Get user's loyalty points balance."""
    async with db._get_connection() as conn:
        cursor = await conn.execute(
            "SELECT points_balance FROM loyalty_points WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


# ─── Helpers ──────────────────────────────────────────────────────

def _get_row_factory():
    """Lazy import of aiosqlite.Row factory."""
    import aiosqlite
    return aiosqlite.Row
