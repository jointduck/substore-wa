"""v25 — API и статика для ПОЛНОФУНКЦИОНАЛЬНОГО Telegram Mini App.

История архитектуры:
  v23 — «тонкий клиент»: Mini App показывал каталог и создавал заказ,
        а весь платёжный путь (выбор способа, проверка, ожидание) шёл
        в чате бота.
  v25 — ПОЛНЫЙ ПАРИТЕТ с ботом: каталог, оформление, промокоды,
        все способы оплаты (Gram / USDT / карта Tribute/Digiseller /
        Telegram Stars), ручная проверка оплаты, ввод данных аккаунта,
        отмена, история заказов и реферальный профиль — всё работает
        ВНУТРИ Mini App. Чат остаётся параллельным каналом: поллеры
        бота по-прежнему подтверждают оплаты и шлют подсказки, заказы
        из TMA и из чата живут в одной таблице orders.

Монтируется в health-сервер main.start_health_server():
    /app/               — статика Mini App (webapp/index.html и т.д.)
    /api/catalog        — GET  публичный каталог (активные сервисы)
    /api/session        — POST валидация initData + профиль + конфиг способов оплаты
    /api/order          — POST создание заказа (та же логика, что select_plan)
    /api/orders         — POST история заказов пользователя
    /api/order/{id}/info    — POST карточка заказа (реквизиты, статусы, поля)
    /api/order/{id}/promo   — POST применить промокод (та же логика, что apply_promo)
    /api/order/{id}/pay     — POST начать оплату выбранным способом
    /api/order/{id}/check   — POST ручная проверка оплаты (как кнопка «Проверить»)
    /api/order/{id}/account — POST сохранить данные аккаунта (как input_account_field)
    /api/order/{id}/cancel  — POST отменить неоплаченный заказ
    /api/profile        — POST бонус, рефералка, скидка новичка

Безопасность: все /api/* кроме /api/catalog принимают ТОЛЬКО валидный
Telegram initData (HMAC-SHA256(bot_token), окно 60 минут) и проверяют
владение заказом (order.user_id == telegram id). На /api/order —
rate-limit (5 заказов / 10 минут на пользователя), на /check —
антифлуд 10 секунд (тот же, что у кнопок бота).
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, unquote_plus

from aiohttp import web
from aiogram.types import LabeledPrice

from config import (
    ADMIN_IDS,
    APP_VERSION,
    BOT_TOKEN,
    DIGISELLER_CURRENCY,
    PROMO_CODE_ENABLED,
    STARS_PER_USDT,
    STORE_NAME,
    USDT_RUB_RATE,
)
from config import ENABLED_PAYMENT_METHODS
from models.database import db, load_catalog, get_service_by_id, get_plan_from_service
from services.marketing import (
    apply_promo_code,
    calc_bonus_use,
    calculate_discounted_price,
    generate_referral_code,
    get_bonus_balance,
    get_referral_stats,
    get_welcome_discount,
    validate_promo_code,
)
from services.ton_payments import (
    generate_ton_payment_link,
    generate_tonkeeper_link,
    generate_usdt_payment_link,
    get_deposit_address,
    generate_memo,
    restore_memo_from_db,
    usdt_to_rub,
    usdt_to_ton,
    verify_payment,
)
from services.tribute import (
    MAX_AMOUNT_KOP,
    MIN_AMOUNT_KOP,
    TributeError,
    create_order as tribute_create_order,
    get_order as tribute_get_order,
    is_configured as tribute_configured,
    is_order_paid as tribute_is_paid,
    payment_url_of,
    rub_to_kopecks,
)
from services.digiseller import (
    create_payment_url,
    find_payment_for_order,
    is_configured as digiseller_configured,
)
from keyboards.keyboards import payment_method_kb, promo_code_kb
from emojis import ce
from utils.html_utils import safe_html, safe_code
from utils.order_status import effective_status, subscription_note
from utils.payment_ux import check_cooldown_left, extinguish, note_check_press

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"

# Окно жизни initData (сек). Telegram рекомендует проверять свежесть,
# иначе украденный initData валиден вечно.
#
# v27.2: было 60 минут — слишком агрессивно для реальных сценариев:
# Mini App держат открытым часами, а мобильные клиенты при повторном
# открытии из чата часто подсовывают тот же (старый) initData вместо
# свежего → юзер видел «сессия устарела» сразу после открытия.
# Подпись при этом ВСЕГДА проверяется полностью; окно защищает только
# от replay украденной строки, а initData авторизует лишь самого юзера.
# 24 часа — практичный компромисс (настройка INITDATA_MAX_AGE_HOURS).
try:
    INITDATA_MAX_AGE = int(float(os.getenv("INITDATA_MAX_AGE_HOURS", "24")) * 3600)
except ValueError:
    INITDATA_MAX_AGE = 24 * 3600
if INITDATA_MAX_AGE < 300:  # sanity: меньше 5 минут не даём ставить
    INITDATA_MAX_AGE = 24 * 3600


# ─── Сессионные токены (v27.3) ──────────────────────────────────────
# Подпись initData ВСЕГДА проверяется, когда она приходит. Но мобильные
# клиенты при повторном открытии Mini App подсовывают старый initData,
# а location.reload() не всегда обновляет его — юзер ловил «сессия
# устарела» на ровном месте (например при оформлении заказа).
# Решение: после первой успешной валидации выдаём подписанный токен
# (HMAC от BOT_TOKEN) на SESSION_TOKEN_TTL. Все /api/* авторизуются
# И initData (если свеж/валиден), И токеном — «сессия устарела»
# исчезает как класс ошибок. Токен без данных (только user_id+срок),
# компрометация равна компрометации initData и кончается через TTL.
SESSION_TOKEN_TTL = 7 * 24 * 3600


def _session_secret() -> bytes:
    return hmac.new(b"WebAppSession", BOT_TOKEN.encode(), hashlib.sha256).digest()


def make_session_token(user_id: int) -> str:
    """Подписанный токен сессии: uid.exp.sig (HMAC-SHA256/32 hex)."""
    exp = int(time.time()) + SESSION_TOKEN_TTL
    msg = f"{user_id}:{exp}".encode()
    sig = hmac.new(_session_secret(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{exp}.{sig}"


def verify_session_token(tok: str) -> int | None:
    """user_id или None (битый / подделанный / просроченный)."""
    if not tok:
        return None
    try:
        uid_s, exp_s, sig = str(tok).split(".", 2)
        uid, exp = int(uid_s), int(exp_s)
    except (ValueError, AttributeError):
        return None
    if exp < time.time():
        return None
    expect = hmac.new(
        _session_secret(), f"{uid}:{exp}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(expect, sig):
        return None
    return uid


def _user_from_signed_initdata(init_data: str) -> dict | None:
    """user из initData, чья подпись УЖЕ проверена (reason='expired'):
    hash сверён, окно — нет. Раз подпись валидна, user полю доверять можно."""
    try:
        for chunk in str(init_data).split("&"):
            key, _, value = chunk.partition("=")
            if key == "user":
                u = json.loads(unquote_plus(value))
                if isinstance(u, dict) and u.get("id"):
                    return u
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def authenticate_user(body: dict) -> tuple[dict | None, str | None]:
    """Единая авторизация для всех /api/*: initData + сессионный токен.

    Порядок:
      1) свежий валидный initData — обычный путь;
      2) подпись валидна, но старше окна (expired) + живой токен с тем
         же user_id — пропускаем (подпись Telegram проверена);
      3) initData пуст/бит, но живой токен — пропускаем (токен выдан
         только после успешной валидации);
      4) иначе отказ с причиной (invalid/expired).
    """
    init_data = str(body.get("initData", "") or "")
    user, reason = validate_init_data_ex(init_data)
    if user:
        return user, None

    tok_uid = verify_session_token(str(body.get("sessionToken", "") or ""))
    if tok_uid is None:
        return None, reason

    if reason == "expired":
        stale = _user_from_signed_initdata(init_data)
        if stale and int(stale["id"]) == tok_uid:
            return stale, None
        return None, "expired"  # подпись и токен от разных юзеров

    # invalid или пустой initData — токен сам по себе достаточен
    return {"id": tok_uid, "first_name": "", "username": ""}, None


# Rate-limit создания заказов: не более 5 заказов за 10 минут на юзера.
_ORDER_WINDOW = 600
_ORDER_LIMIT = 5
_order_hits: dict[int, list[float]] = {}

# Окно оплаты карточных заказов (Tribute/Digiseller) — как в боте
# (CARD_PAYMENT_WINDOW_MINUTES в handlers/user_handlers.py).
CARD_WINDOW_MINUTES = 60

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

METHOD_NAMES = {
    "ton": "Gram",
    "usdt": "USDT",
    "digiseller": "Карта (Digiseller)",
    "tribute": "Карта (Tribute)",
    "stars": "Stars ⭐",
}


# ─── Валидация initData ────────────────────────────────────────────

def validate_init_data_ex(init_data: str, bot_token: str = BOT_TOKEN) -> tuple[dict | None, str | None]:
    """Проверить initData → (user, None) | (None, "expired") | (None, "invalid").

    expired = подпись валидна, но initData старше INITDATA_MAX_AGE (Mini App
    долго был открыт) — фронту надо молча перезагрузиться: Telegram при
    перезагрузке выдаёт свежий initData. invalid = подпись/формат битые.

    Алгоритм из офиц. доков (core.telegram.org/bots/webapps):
    secret = HMAC-SHA256(key="WebAppData", msg=bot_token)
    hash   = HMAC-SHA256(key=secret,   msg=data_check_string)
    где data_check_string — все поля кроме hash, отсортированные по ключу,
    разделитель '\n'.

    v27.4 — НАЙДЕНА ПРИЧИНА «bad signature» при верном токене: Telegram
    подписывает строку из ДЕКОДИРОВАННЫХ значений (user — обычный JSON,
    а не %7B%22id%22...) — таков и пример в доках, и parse_qsl в aiogram.
    Раньше мы строили строку из raw-urlencoded значений — hash не сходился
    НИ НА ОДНОМ реальном initData, а до v27.3 ошибка маскировалась текстом
    «сессия устарела». Теперь пробуем декодированный вариант (основной) и
    raw (фолбэк для экзотических транспортиров).
    """
    if not init_data or not bot_token:
        return None, "invalid"

    received_hash = None
    pairs: list[tuple[str, str]] = []
    for chunk in init_data.split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        if key == "hash":
            received_hash = value
        else:
            pairs.append((key, value))

    if not received_hash or not pairs:
        return None, "invalid"
    pairs.sort(key=lambda kv: kv[0])

    # v27.4: основной вариант — декодированные значения (конвенция Telegram:
    # aiogram/parse_qsl и офиц. PHP-пример с parse_str это подтверждают),
    # фолбэк — raw, если initData пришёл уже декодированным.
    dcs_dec = "\n".join(f"{k}={unquote_plus(v)}" for k, v in pairs)
    dcs_raw = "\n".join(f"{k}={v}" for k, v in pairs)
    variants = [dcs_dec] if dcs_dec == dcs_raw else [dcs_dec, dcs_raw]

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    matched_dcs = None
    for i, dcs in enumerate(variants):
        calc_hash = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calc_hash, received_hash):
            matched_dcs = "decoded" if i == 0 else "raw"
            break
    if matched_dcs is None:
        # v27.3: тихие отказы не оставляли следов в логах — юзер видел
        # «сессия устарела», а в Render было пусто. Теперь видно причину.
        logger.warning(
            f"initData: bad signature (len={len(init_data)}, "
            f"token_set={bool(bot_token)}) — initData подписан ДРУГИМ ботом "
            f"или повреждён при передаче; токен сервиса проверьте через "
            f"/health (bot=@...)"
        )
        return None, "invalid"
    logger.debug(f"initData: signature OK (dcs={matched_dcs})")

    # Свежесть подписи — защита от replay старых initData.
    # Поля после валидации читаем в ДЕКОДИРОВАННОМ виде (та же конвенция).
    fields = {k: unquote_plus(v) for k, v in pairs}
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        return None, "invalid"
    if auth_date < time.time() - INITDATA_MAX_AGE:
        logger.info(
            f"initData: expired (age={int(time.time()) - auth_date}s, "
            f"window={INITDATA_MAX_AGE}s) — пробуем сессионный токен"
        )
        return None, "expired"

    try:
        user = json.loads(fields.get("user", ""))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None, "invalid"
    return user, None


def validate_init_data(init_data: str, bot_token: str = BOT_TOKEN) -> dict | None:
    """Совместимая обёртка: только user или None (без причины)."""
    return validate_init_data_ex(init_data, bot_token)[0]


# ─── Мелкие помощники ──────────────────────────────────────────────

def _err(status: int, message: str):
    return web.json_response({"ok": False, "error": message}, status=status)


def _auth_err(reason: str | None):
    """401 для TMA. expired=true — фронту можно молча перезагрузиться
    (свежий initData), иначе подпись реально битая."""
    if reason == "expired":
        payload = {
            "ok": False,
            "error": "Сессия устарела. Переоткройте магазин через бота.",
            "expired": True,
        }
    else:
        # v27.3: раньше и «битая подпись» подписывалась как «сессия
        # устарела» — вводило в заблуждение при диагностике
        payload = {
            "ok": False,
            "error": "Не удалось подтвердить личность. Переоткройте магазин через бота.",
        }
    return web.json_response(payload, status=401)


def _rate_limited(user_id: int) -> bool:
    now = time.time()
    hits = [t for t in _order_hits.get(user_id, []) if now - t < _ORDER_WINDOW]
    if len(hits) >= _ORDER_LIMIT:
        _order_hits[user_id] = hits
        return True
    hits.append(now)
    _order_hits[user_id] = hits
    return False


async def _read_body(request: web.Request) -> dict | None:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else None
    except Exception:
        return None


async def _notify_admins(bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass


def _expires_iso(minutes: int) -> str:
    return (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()


async def _rub(usdt_amount: float) -> float:
    try:
        return float(await usdt_to_rub(usdt_amount))
    except Exception:
        return 0.0


async def _rub_rate() -> float:
    """Текущий курс USDT→RUB — фронтенду для отображения цен в рублях (v27.7).

    Живой курс с кэшем (usdt_to_rub); если источники недоступны —
    константа USDT_RUB_RATE из конфига, чтобы цены никогда не были нулевыми.
    """
    try:
        rate = float(await usdt_to_rub(1))
        if rate > 0:
            return rate
    except Exception:
        pass
    return float(USDT_RUB_RATE)


async def _auth_order(request: web.Request, body: dict) -> tuple[dict | None, dict | None, web.Response | None]:
    """Общий вход для /api/order/{id}/*: initData + заказ + владение.

    Возвращает (user, order, None) при успехе или (None, None, ответ-ошибку).
    """
    user, _reason = authenticate_user(body)
    if not user:
        return None, None, _auth_err(_reason)

    try:
        order_id = int(str(request.match_info.get("order_id", "")))
    except (TypeError, ValueError):
        return None, None, _err(400, "Некорректный номер заказа")

    order = await db.get_order(order_id)
    if not order or order.get("user_id") != int(user["id"]):
        return None, None, _err(404, "Заказ не найден")
    return user, order, None


# ─── Хендлеры: каталог / сессия ────────────────────────────────────

async def api_catalog(request: web.Request) -> web.Response:
    """Публичный каталог активных сервисов для Mini App."""
    try:
        services = load_catalog().get("services", [])
    except Exception as e:
        logger.error(f"webapp api_catalog: catalog read failed: {e}")
        return _err(500, "Каталог временно недоступен")
    active = [s for s in services if s.get("active", True)]
    # v27: bot_username публично — демо-шлюзу нужен линк «Открыть в Telegram»;
    # v27.2: версия — мгновенная проверка, что на хостинге свежий код
    return web.json_response({
        "ok": True,
        "services": active,
        "bot_username": request.app["bot_username"],
        "version": APP_VERSION,
        # v27.7: курс для показа цен в рублях (каталог рендерится первым)
        "rub_rate": await _rub_rate(),
    })


def _pay_config() -> dict:
    """Доступные способы оплаты для TMA — зеркало payment_method_kb."""
    card_provider = ""
    if "card" in ENABLED_PAYMENT_METHODS:
        if tribute_configured():
            card_provider = "tribute"
        elif digiseller_configured():
            card_provider = "digiseller"
    return {
        "methods": list(ENABLED_PAYMENT_METHODS),
        "card_provider": card_provider,
        "stars_per_usdt": STARS_PER_USDT,
        "wallet_configured": bool(get_deposit_address()),
        "promo_enabled": bool(PROMO_CODE_ENABLED),
    }


async def api_session(request: web.Request) -> web.Response:
    """Валидация initData + профиль пользователя + конфиг для Mini App."""
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")

    user, _reason = authenticate_user(body)
    if not user:
        if _reason == "expired":
            return _auth_err(_reason)
        return _err(401, "Не удалось подтвердить личность. Откройте магазин через бота.")

    user_id = int(user["id"])

    welcome = None
    try:
        welcome = await get_welcome_discount(user_id)
    except Exception as e:
        logger.warning(f"webapp api_session: welcome discount check failed: {e}")

    bonus_balance = 0.0
    try:
        bonus_balance = float(await get_bonus_balance(user_id) or 0.0)
    except Exception:
        pass

    # v27.7: курс USDT→RUB в конфиге — фронтенд показывает цены в рублях
    pay_cfg = _pay_config()
    pay_cfg["rub_rate"] = await _rub_rate()

    return web.json_response({
        "ok": True,
        "user": {
            "id": user_id,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
        },
        "store_name": STORE_NAME,
        # v27.3: токен сессии на 7 дней — все /api/* дальше авторизуются
        # им, если мобильный клиент подсовывает старый initData
        "session_token": make_session_token(user_id),
        "bot_username": request.app["bot_username"],
        "offer_url": "https://disk.yandex.ru/i/HWpCZ1blH8fyUw",
        "welcome_discount": {"discount_pct": welcome["discount_pct"]} if welcome else None,
        "bonus_balance": round(bonus_balance, 2),
        "config": pay_cfg,
    })


# ─── Хендлеры: создание заказа (как select_plan) ───────────────────

async def api_order(request: web.Request) -> web.Response:
    """Создать заказ из Mini App — ТА ЖЕ логика, что select_plan в боте.

    Скидка новичка + бонус за друзей применяются идентично; заказ уходит
    в ту же таблицу orders. Дополнительно в БД пишутся original_price_usdt
    и discount_pct — тогда промокод из TMA считает «суммарную скидку от
    исходной цены» ровно как бот. Покупателю в чат дублируется карточка
    заказа — чат остаётся запасным каналом оплаты.
    """
    bot = request.app["bot"]
    if bot is None:
        return _err(503, "Бот перезапускается, попробуйте через минуту")

    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")

    user, _reason = authenticate_user(body)
    if not user:
        return _auth_err(_reason)

    user_id = int(user["id"])
    if _rate_limited(user_id):
        return _err(429, "Слишком много заказов подряд. Подождите немного.")

    service_id = str(body.get("service_id", "")).strip()
    plan_id = str(body.get("plan_id", "")).strip()

    service = get_service_by_id(service_id)
    if not service:
        return _err(404, "Сервис не найден")
    plan = get_plan_from_service(service, plan_id)
    if not plan:
        return _err(404, "Тариф не найден")

    original_price_usdt = float(plan.get("price_usdt", 0))
    price_usdt = original_price_usdt
    discount_pct = 0

    # ── Скидка новичка (как в select_plan) ──
    try:
        welcome = await get_welcome_discount(user_id)
        if welcome and welcome.get("discount_pct", 0) > 0:
            discount_pct = int(welcome["discount_pct"])
            price_usdt = round(original_price_usdt * (1 - discount_pct / 100), 2)
    except Exception as e:
        logger.warning(f"webapp api_order: welcome discount failed for {user_id}: {e}")

    # ── Бонус за друзей (как в select_plan: резерв живых заказов) ──
    bonus_applied = 0.0
    try:
        balance = await get_bonus_balance(user_id)
        reserved = await db.get_pending_bonus_reserved(user_id)
        bonus_applied = calc_bonus_use(max(0.0, balance - reserved), price_usdt)
        if bonus_applied > 0:
            price_usdt = round(price_usdt - bonus_applied, 2)
    except Exception as e:
        logger.warning(f"webapp api_order: bonus calc failed for {user_id}: {e}")

    # ── Создание заказа (та же таблица и тот же пайплайн) ──
    order_id = await db.create_order(
        user_id=user_id,
        service_id=service_id,
        service_name=service["name"],
        plan_id=plan_id,
        plan_name=plan["name"],
        duration_days=int(plan["duration_days"]),
        price_usdt=price_usdt,
        currency="USDT",
        bonus_applied=bonus_applied,
    )
    # v24: исходная цена и скидка — в БД (нужно для промо и истории)
    if discount_pct > 0:
        await db.update_order_status(
            order_id, "pending_payment",
            original_price_usdt=original_price_usdt,
            discount_pct=discount_pct,
        )

    # ── Карточка заказа в чат (запасной канал — как раньше) ──
    rub_price = await _rub(price_usdt)

    lines = [
        f"{ce('ticket')}   <b>Оформление заказа #{order_id}</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{ce('shopping')}  <b>{safe_html(str(service['name']))}</b>",
        f"{ce('plan_badge')}   Тариф: <b>{safe_html(str(plan['name']))}</b> ({plan['duration_days']} дн.)",
    ]
    if discount_pct > 0:
        lines.append(
            f"{ce('discount')}  Скидка {discount_pct}% на первый заказ: "
            f"<s>{original_price_usdt:.2f} USDT</s> → <b>{price_usdt:.2f} USDT</b>"
        )
    if bonus_applied > 0:
        lines.append(f"{ce('referral_badge')}  Бонус за друзей: <b>−{bonus_applied:.2f} USDT</b>")
    if rub_price > 0:
        lines.append(f"{ce('wallet')}   Стоимость: <b>{rub_price:.0f} ₽</b>")
    lines.append(f"{ce('chart')}   ≈ {price_usdt:.2f} USDT")
    lines.append("")
    if PROMO_CODE_ENABLED:
        lines.append(f"{ce('discount')}  Есть промокод? Кнопка ниже")
    lines.append(f"<i>{ce('lock')}  Выберите способ оплаты — в приложении или здесь:</i>")

    text = "\n".join(lines)
    kb = promo_code_kb(order_id) if PROMO_CODE_ENABLED else payment_method_kb(order_id)

    try:
        await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        # Заказ создан, но доставить сообщение не смогли — TMA всё равно
        # продолжает оформление внутри приложения.
        logger.warning(f"webapp api_order: send_message to {user_id} failed: {e}")

    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "price_usdt": price_usdt,
        "original_price_usdt": original_price_usdt,
        "discount_pct": discount_pct,
        "bonus_applied": bonus_applied,
        "service_name": service["name"],
        "plan_name": plan["name"],
        "rub_price": round(rub_price),
    })


# ─── Хендлеры: история и карточка заказа ───────────────────────────

def _order_view(order: dict, service: dict | None = None) -> dict:
    """Безопасное представление заказа для фронтенда.

    Никаких внутренних служебных полей; реквизиты платежа — только
    сохранённые в заказе (сумма, memo, ссылки на кошелёк).
    """
    oid = order["order_id"]
    eff = effective_status(order)
    method = (order.get("payment_method") or "").strip()

    view = {
        "order_id": oid,
        "service_id": order.get("service_id", ""),
        "service_name": order.get("service_name", ""),
        "plan_name": order.get("plan_name", ""),
        "duration_days": int(order.get("duration_days") or 0),
        "price_usdt": float(order.get("price_usdt") or 0),
        "original_price_usdt": float(order.get("original_price_usdt") or 0) or None,
        "discount_pct": round(float(order.get("discount_pct") or 0), 1),
        "promo_code": order.get("promo_code") or "",
        "bonus_applied": float(order.get("bonus_applied") or 0),
        "status": eff,
        "payment_method": method,
        "created_at": str(order.get("created_at") or "")[:16],
        "note": subscription_note(order),
        "account_filled": bool(order.get("account_data")),
    }

    # Поля формы данных аккаунта — только когда оплата прошла и данных ещё нет
    if eff == "pending_account" and service:
        fields = service.get("account_fields", [])
        if fields:
            view["account_fields"] = [
                {
                    "id": f.get("id", ""),
                    "label": f.get("label", ""),
                    "placeholder": f.get("placeholder", ""),
                    "type": f.get("type", "text"),
                }
                for f in fields
            ]

    # Платёжные реквизиты (для экрана «Продолжить оплату»)
    if method in ("ton", "usdt"):
        addr = get_deposit_address()
        if addr:
            memo = (order.get("payment_memo") or "").strip()
            if memo:
                restore_memo_from_db(oid, memo)
            pay: dict = {"address": addr, "memo": memo}
            pexp = order.get("payment_expires_at")
            if pexp:
                pay["expires_at"] = str(pexp)
            if method == "ton":
                amt = float(order.get("ton_amount") or 0)
                pay["amount_ton"] = round(amt, 4)
                if amt > 0:
                    pay["pay_url"] = generate_tonkeeper_link(amt, oid, addr)
                    pay["ton_url"] = generate_ton_payment_link(amt, oid, addr)
            else:
                amt = float(order.get("price_usdt") or 0)
                pay["amount_usdt"] = round(amt, 2)
                if amt > 0:
                    pay["pay_url"] = generate_usdt_payment_link(amt, oid, addr)
            view["payment"] = pay
    elif method in ("tribute", "digiseller"):
        view["payment"] = {
            "provider": method,
            "expires_at": str(order.get("payment_expires_at") or ""),
        }
    return view


async def api_order_info(request: web.Request) -> web.Response:
    """Карточка заказа: статус, реквизиты, форма данных аккаунта."""
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, order, err = await _auth_order(request, body)
    if err is not None:
        return err

    service = get_service_by_id(order.get("service_id", ""))
    return web.json_response({
        "ok": True,
        "order": _order_view(order, service),
        # v27.7: курс для показа цены заказа в рублях
        "rub_rate": await _rub_rate(),
    })


async def api_orders_list(request: web.Request) -> web.Response:
    """История заказов пользователя (как «Заказы» в боте)."""
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, _reason = authenticate_user(body)
    if not user:
        return _auth_err(_reason)

    try:
        limit = min(int(body.get("limit", 20)), 50)
    except (TypeError, ValueError):
        limit = 20

    orders = await db.get_user_orders(int(user["id"]), limit=limit)
    rows = []
    for o in orders:
        eff = effective_status(o)
        rows.append({
            "order_id": o["order_id"],
            "service_name": o.get("service_name", ""),
            "plan_name": o.get("plan_name", ""),
            # v27.8: duration_days нужен фронту («30 дн.» в строке истории)
            "duration_days": int(o.get("duration_days") or 0),
            "price_usdt": float(o.get("price_usdt") or 0),
            "discount_pct": round(float(o.get("discount_pct") or 0), 1),
            "status": eff,
            "payment_method": o.get("payment_method") or "",
            "created_at": str(o.get("created_at") or "")[:16],
            "note": subscription_note(o),
        })
    return web.json_response({"ok": True, "orders": rows, "rub_rate": await _rub_rate()})


# ─── Хендлеры: промокод (как apply_promo) ──────────────────────────

async def api_order_promo(request: web.Request) -> web.Response:
    """Применить промокод к заказу — та же логика, что apply_promo."""
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, order, err = await _auth_order(request, body)
    if err is not None:
        return err

    if order["status"] != "pending_payment":
        if effective_status(order) == "payment_over":
            return _err(410, "Время оплаты истекло — оформите новый заказ")
        return _err(409, "Оплата уже подтверждена — промокод применить нельзя")

    code = str(body.get("code", "")).strip()
    if not code:
        return _err(400, "Введите промокод")

    result = await validate_promo_code(code, int(user["id"]))
    if not result.get("valid"):
        return _err(400, result.get("message", "Промокод недействителен"))

    original_price_usdt = float(order.get("original_price_usdt") or 0) or float(order.get("price_usdt") or 0)
    current_price = float(order.get("price_usdt", 0) or 0)
    base_for_promo = current_price if current_price > 0 else original_price_usdt
    new_price, discount_desc = calculate_discounted_price(base_for_promo, result)

    # Как в боте (UX №4): промокод не должен обнулять стоимость
    if new_price <= 0:
        return _err(400, "Промокод делает стоимость нулевой — введите другой код")

    total_discount_pct = round((1 - new_price / original_price_usdt) * 100, 1) if original_price_usdt > 0 else 0

    await db.update_order_status(
        order["order_id"], "pending_payment",
        price_usdt=new_price,
        discount_pct=total_discount_pct,
        original_price_usdt=original_price_usdt,
        promo_code=code.upper(),
    )

    updated = await db.get_order(order["order_id"])
    service = get_service_by_id(updated.get("service_id", ""))
    return web.json_response({
        "ok": True,
        "message": f"Промокод {code.upper()} применён!",
        "discount_desc": discount_desc,
        "order": _order_view(updated, service),
    })


# ─── Хендлеры: подтверждение оплаты (общая часть) ──────────────────

async def _confirm_payment(request: web.Request, order: dict, method: str,
                           tx_hash: str = "", invoice_id=None) -> bool:
    """Перевести заказ в pending_account + все действия бота после оплаты.

    Зеркало финальных блоков check_ton/checkusdt/checktrib/checkdigi:
      • CAS-переход pending_payment → pending_account (идемпотентность);
      • списание бонуса (внутри try_transition_order_status);
      • уведомление админов;
      • маркетинг (лоялти, реферальный бонус, промокод) — _post_payment_actions;
      • гашение платёжных экранов в чате (extinguish — редактирование).
    """
    bot = request.app["bot"]
    kwargs = dict(paid_at=datetime.utcnow().isoformat(), payment_method=method)
    if tx_hash:
        kwargs["ton_tx_hash"] = str(tx_hash)
    if invoice_id:
        kwargs["digiseller_invoice_id"] = int(invoice_id)

    transitioned = await db.try_transition_order_status(
        order["order_id"], "pending_payment", "pending_account", **kwargs
    )
    if not transitioned:
        return False

    text = (
        f"{ce('check')} <b>Оплата получена ({METHOD_NAMES.get(method, method)}) — Mini App</b>\n"
        f"Заказ #{order['order_id']}: {safe_html(str(order['service_name']))} — "
        f"{safe_html(str(order['plan_name']))}\n"
        f"{ce('wallet')} {float(order.get('price_usdt') or 0):.2f} USDT"
    )
    if tx_hash:
        text += f"\nTX: <code>{str(tx_hash)[:24]}…</code>"
    if invoice_id:
        text += f"\nИнвойс: <code>{invoice_id}</code>"
    await _notify_admins(bot, text)

    try:
        from handlers.user_handlers import _post_payment_actions
        updated = await db.get_order(order["order_id"])
        await _post_payment_actions(updated, bot)
    except Exception as e:
        logger.warning(f"webapp _confirm_payment: post-payment actions failed: {e}")

    try:
        await extinguish(bot, order["user_id"], order["order_id"])
    except Exception as e:
        logger.debug(f"webapp _confirm_payment: extinguish: {e}")

    return True


# ─── Хендлер: начать оплату (как payton/payusdt/paycard/paystars) ──

async def api_order_pay(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, order, err = await _auth_order(request, body)
    if err is not None:
        return err

    if order["status"] != "pending_payment":
        if effective_status(order) == "payment_over":
            return _err(410, "Время оплаты истекло — оформите новый заказ")
        return _err(409, "Заказ уже оплачен или отменён")

    method = str(body.get("method", "")).strip().lower()
    if method == "ton":
        return await _pay_ton(request, order)
    if method == "usdt":
        return await _pay_usdt(request, order)
    if method == "card":
        return await _pay_card(request, order, body)
    if method == "stars":
        return await _pay_stars(request, order)
    return _err(400, "Неизвестный способ оплаты")


async def _pay_ton(request: web.Request, order: dict) -> web.Response:
    """Экран Gram-оплаты: сумма, адрес, memo, ссылки на кошелёк (как pay_with_ton)."""
    price_usdt = float(order.get("price_usdt", 0) or 0)
    wallet_address = get_deposit_address()
    if not wallet_address:
        return _err(503, "Крипто-кошелёк не настроен. Обратитесь в поддержку.")

    # v17-правило бота: при повторном открытии НЕ пересчитываем Gram —
    # сохранённая сумма и есть сумма для проверки поллером (допуск 5%).
    saved_gram = float(order.get("ton_amount") or 0)
    gram_amount = saved_gram if saved_gram > 0 else await usdt_to_ton(price_usdt)
    if gram_amount <= 0:
        return _err(503, "Не удалось получить курс Gram. Попробуйте позже.")

    memo = (order.get("payment_memo") or "").strip()
    if memo:
        restore_memo_from_db(order["order_id"], memo)
    else:
        memo = generate_memo(order["order_id"])

    expires_at = _expires_iso(30)
    await db.update_order_status(
        order["order_id"], "pending_payment",
        payment_method="ton",
        ton_amount=gram_amount,
        payment_expires_at=expires_at,
        payment_memo=memo,
    )

    rub_price = await _rub(price_usdt)
    return web.json_response({
        "ok": True,
        "method": "ton",
        "amount_ton": round(gram_amount, 4),
        "address": wallet_address,
        "memo": memo,
        "pay_url": generate_tonkeeper_link(gram_amount, order["order_id"], wallet_address),
        "ton_url": generate_ton_payment_link(gram_amount, order["order_id"], wallet_address),
        "expires_at": expires_at,
        "price_usdt": price_usdt,
        "rub_price": round(rub_price),
    })


async def _pay_usdt(request: web.Request, order: dict) -> web.Response:
    """Экран USDT (Tether в сети Gram) — как pay_with_usdt."""
    price_usdt = float(order.get("price_usdt", 0) or 0)
    wallet_address = get_deposit_address()
    if not wallet_address:
        return _err(503, "Крипто-кошелёк не настроен. Обратитесь в поддержку.")

    memo = (order.get("payment_memo") or "").strip()
    if memo:
        restore_memo_from_db(order["order_id"], memo)
    else:
        memo = generate_memo(order["order_id"])

    expires_at = _expires_iso(30)
    await db.update_order_status(
        order["order_id"], "pending_payment",
        payment_method="usdt",
        ton_amount=0,
        payment_expires_at=expires_at,
        payment_memo=memo,
    )

    rub_price = await _rub(price_usdt)
    return web.json_response({
        "ok": True,
        "method": "usdt",
        "amount_usdt": round(price_usdt, 2),
        "address": wallet_address,
        "memo": memo,
        "pay_url": generate_usdt_payment_link(price_usdt, order["order_id"], wallet_address),
        "expires_at": expires_at,
        "price_usdt": price_usdt,
        "rub_price": round(rub_price),
    })


async def _pay_card(request: web.Request, order: dict, body: dict) -> web.Response:
    """Карта: Tribute (Mini App-оплата) или Digiseller (email → ссылка)."""
    order_id = order["order_id"]
    price_usdt = float(order.get("price_usdt", 0) or 0)

    if tribute_configured():
        rub_amount = await _rub(price_usdt)
        if rub_amount <= 0:
            return _err(503, "Не удалось определить сумму в рублях. Попробуйте позже.")

        kop = rub_to_kopecks(rub_amount)
        if kop < MIN_AMOUNT_KOP:
            return _err(400, "Оплата картой доступна от 100 ₽ — используйте другие способы.")
        if kop > MAX_AMOUNT_KOP:
            return _err(400, "Оплата картой доступна до 300 000 ₽ — напишите в поддержку.")

        expires_at = _expires_iso(CARD_WINDOW_MINUTES)
        await db.update_order_status(
            order_id, "pending_payment",
            payment_method="tribute",
            payment_expires_at=expires_at,
        )

        # Уже есть живой заказ в Tribute — переиспользуем (как в боте)
        existing_uuid = (order.get("tribute_order_uuid") or "").strip()
        if existing_uuid:
            try:
                existing = await tribute_get_order(existing_uuid)
                if str(existing.get("status", "")).lower() in ("pending", "prepaid"):
                    return web.json_response({
                        "ok": True, "method": "card", "provider": "tribute",
                        "pay_url": payment_url_of(existing),
                        "rub_price": round(rub_amount), "price_usdt": price_usdt,
                        "expires_at": expires_at,
                    })
            except TributeError:
                pass  # не найден/недоступен — создадим новый

        title = f"Заказ #{order_id}: {order['service_name']} — {order['plan_name']}"
        description = (
            f"Подписка {order['service_name']} на {order['plan_name']} "
            f"({order.get('duration_days', 0)} дн.)"
        )
        try:
            tribute_order = await tribute_create_order(
                rub_amount,
                title=title,
                description=description,
                customer_id=str(order["user_id"]),
                comment=f"bot_order_id:{order_id}",
            )
        except TributeError as e:
            logger.error(f"Tribute create_order failed for order #{order_id}: {e}")
            await _notify_admins(
                request.app["bot"],
                f"{ce('warning')} <b>Tribute: не удалось создать заказ #{order_id}</b>\n"
                f"<code>{safe_html(str(e)[:500])}</code>",
            )
            return _err(503, "Оплата картой временно недоступна. Попробуйте другой способ.")

        await db.set_order_tribute_info(order_id, tribute_order["uuid"])
        return web.json_response({
            "ok": True, "method": "card", "provider": "tribute",
            "pay_url": payment_url_of(tribute_order),
            "rub_price": round(rub_amount), "price_usdt": price_usdt,
            "expires_at": expires_at,
        })

    if digiseller_configured():
        email = str(body.get("email", "")).strip()
        if not _EMAIL_RE.match(email):
            return _err(400, "Укажите корректный email — на него придёт чек об оплате")

        rub_amount = await _rub(price_usdt)
        expires_at = _expires_iso(CARD_WINDOW_MINUTES)
        await db.update_order_status(
            order_id, "pending_payment",
            payment_method="digiseller",
            payment_expires_at=expires_at,
        )

        try:
            payment_url, actual_amount, id_po = await create_payment_url(
                order_id,
                amount=rub_amount if rub_amount > 0 else None,
                buyer_email=email,
            )
        except Exception as e:
            logger.error(f"Digiseller create_payment_url failed for order #{order_id}: {e}")
            await _notify_admins(
                request.app["bot"],
                f"{ce('warning')} <b>Digiseller: не удалось создать ссылку оплаты "
                f"для заказа #{order_id}</b>\n<code>{safe_html(str(e)[:500])}</code>",
            )
            return _err(503, "Оплата картой временно недоступна. Попробуйте другой способ.")

        await db.set_order_digiseller_info(
            order_id, email=email, amount=actual_amount, id_po=id_po
        )
        return web.json_response({
            "ok": True, "method": "card", "provider": "digiseller",
            "pay_url": payment_url,
            "rub_price": round(actual_amount), "price_usdt": price_usdt,
            "expires_at": expires_at,
        })

    return _err(503, "Оплата картой временно недоступна. Используйте Gram/USDT/Stars.")


async def _pay_stars(request: web.Request, order: dict) -> web.Response:
    """Telegram Stars: ссылка инвойса для WebApp.openInvoice (как pay_with_stars)."""
    bot = request.app["bot"]
    if bot is None:
        return _err(503, "Бот перезапускается, попробуйте через минуту")

    price_usdt = float(order.get("price_usdt", 0) or 0)
    stars_amount = int(price_usdt * STARS_PER_USDT)
    if stars_amount <= 0:
        return _err(400, "Ошибка: некорректная сумма")

    await db.update_order_status(order["order_id"], "pending_payment", payment_method="stars")

    try:
        invoice_link = await bot.create_invoice_link(
            title=f"{order['service_name']} — {order['plan_name']}",
            description=(
                f"Подписка {order['service_name']} на {order['plan_name']} "
                f"({order.get('duration_days', 0)} дн.)"
            ),
            payload=str(order["order_id"]),
            provider_token="",   # Stars не требуют провайдера
            currency="XTR",
            prices=[LabeledPrice(label=str(order["plan_name"]), amount=stars_amount)],
        )
    except Exception as e:
        logger.error(f"create_invoice_link failed for order #{order['order_id']}: {e}")
        return _err(503, "Не удалось создать счёт Stars. Попробуйте позже.")

    return web.json_response({
        "ok": True,
        "method": "stars",
        "stars": stars_amount,
        "price_usdt": price_usdt,
        "invoice_link": invoice_link,
    })


# ─── Хендлер: ручная проверка оплаты (как checkton/checkusdt/…) ────

async def api_order_check(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, order, err = await _auth_order(request, body)
    if err is not None:
        return err

    order_id = order["order_id"]

    # Уже обработанные заказы — честный статус без повторной проверки
    if order["status"] != "pending_payment":
        if order["status"] in ("pending_account", "pending_activation", "active"):
            return web.json_response({
                "ok": True, "paid": True, "status": order["status"],
                "message": "Оплата уже подтверждена!",
            })
        return web.json_response({
            "ok": True, "paid": False, "status": effective_status(order),
            "message": "Заказ отменён или время оплаты истекло",
        })

    if effective_status(order) == "payment_over":
        return web.json_response({
            "ok": True, "paid": False, "status": "payment_over",
            "message": "Время оплаты истекло — оформите новый заказ",
        })

    left = check_cooldown_left(int(user["id"]))
    if left:
        return _err(429, f"Подождите {left} с — проверка выполняется не чаще раза в 10 секунд.")

    method = (order.get("payment_method") or "").strip()

    # ── Gram / USDT ──
    if method in ("ton", "usdt"):
        expected = float(order.get("ton_amount") or 0) if method == "ton" \
            else float(order.get("price_usdt", 0) or 0)
        if method == "ton" and expected <= 0:
            return _err(400, "Сумма Gram не указана — откройте оплату заново")

        note_check_press(int(user["id"]))
        result = await verify_payment(
            order_id, method, expected, db_memo=order.get("payment_memo")
        )
        if result.get("error") == "check_in_progress":
            return _err(429, "Проверка уже выполняется, секунду…")

        if result.get("paid"):
            confirmed = await _confirm_payment(request, order, method, tx_hash=result.get("tx_hash", ""))
            if confirmed:
                return web.json_response({
                    "ok": True, "paid": True, "status": "pending_account",
                    "message": "Оплата подтверждена!",
                })
            return web.json_response({
                "ok": True, "paid": True, "status": "pending_account",
                "message": "Оплата уже подтверждена!",
            })
        return web.json_response({
            "ok": True, "paid": False, "status": "pending_payment",
            "message": "Оплата не найдена. Если оплатили недавно — подождите минуту и проверьте снова.",
        })

    # ── Tribute ──
    if method == "tribute":
        tribute_uuid = (order.get("tribute_order_uuid") or "").strip()
        if not tribute_uuid:
            return _err(400, "Заказ не привязан к Tribute — нажмите «Оплатить» заново")

        note_check_press(int(user["id"]))
        try:
            paid = await tribute_is_paid(tribute_uuid)
        except TributeError as e:
            logger.warning(f"Tribute status check failed for order #{order_id}: {e}")
            return _err(503, "Сервис оплаты не ответил. Попробуйте ещё раз через пару секунд.")

        if paid:
            confirmed = await _confirm_payment(request, order, "tribute")
            if confirmed:
                return web.json_response({
                    "ok": True, "paid": True, "status": "pending_account",
                    "message": "Оплата подтверждена!",
                })
        return web.json_response({
            "ok": True, "paid": False, "status": "pending_payment",
            "message": "Оплата не найдена. Завершите оплату и попробуйте через 30 секунд.",
        })

    # ── Digiseller ──
    if method == "digiseller":
        from config import DIGISELLER_UNIT_PRICE

        price_usdt = float(order.get("price_usdt", 0) or 0)
        expected_amount = float(order.get("digiseller_amount") or 0.0)
        if expected_amount <= 0:
            expected_amount = await _rub(price_usdt)
            if expected_amount > 0 and DIGISELLER_UNIT_PRICE > 0:
                unit_cnt = max(1, round(expected_amount / DIGISELLER_UNIT_PRICE))
                expected_amount = round(unit_cnt * DIGISELLER_UNIT_PRICE, 2)

        buyer_email = (order.get("digiseller_email") or "").strip()
        note_check_press(int(user["id"]))
        result = await find_payment_for_order(
            order_id,
            expected_amount,
            hours=6,
            buyer_email=buyer_email,
            not_before=order.get("digiseller_link_created_at"),
        )

        if result.get("paid"):
            invoice_id = result.get("invoice_id")
            # v17-защита бота: один платёж не должен подтвердить два заказа
            if invoice_id:
                used_by = await db.get_order_by_digiseller_invoice(invoice_id)
                if used_by and used_by["order_id"] != order_id:
                    logger.warning(
                        f"Digiseller: invoice {invoice_id} уже привязан к заказу "
                        f"#{used_by['order_id']} — повторное подтверждение #{order_id} отклонено"
                    )
                    return _err(409, "Этот платёж уже засчитан другому заказу. Напишите в поддержку — разберёмся.")
            confirmed = await _confirm_payment(request, order, "digiseller", invoice_id=invoice_id)
            if confirmed:
                return web.json_response({
                    "ok": True, "paid": True, "status": "pending_account",
                    "message": "Оплата подтверждена!",
                })
        elif result.get("underpaid"):
            expected = result.get("amount_expected") or expected_amount
            await _notify_admins(
                request.app["bot"],
                f"{ce('warning')} <b>Digiseller: недоплата по заказу #{order_id}</b>\n"
                f"{safe_html(str(order['service_name']))} — {safe_html(str(order['plan_name']))}\n"
                f"Получено: {float(result.get('amount_in') or 0):.2f} / Ожидалось: {expected:.2f}\n"
                f"Инвойс: <code>{result.get('invoice_id')}</code>\n"
                f"Клиент: <code>{user['id']}</code>\n\n"
                f"Проверьте вручную: если платёж корректный — активируйте заказ.",
            )
            return web.json_response({
                "ok": True, "paid": False, "underpaid": True, "status": "pending_payment",
                "message": "Платёж найден, но сумма не совпала — администратор проверит вручную.",
            })
        return web.json_response({
            "ok": True, "paid": False, "status": "pending_payment",
            "message": "Оплата не найдена. Подтверждение приходит автоматически в течение минуты.",
        })

    # ── Stars: подтверждение автоматическое (successful_payment у бота) ──
    if method == "stars":
        return web.json_response({
            "ok": True, "paid": False, "status": "pending_payment",
            "message": "Оплата Stars подтверждается автоматически — обычно в течение секунды.",
        })

    return _err(400, "Сначала выберите способ оплаты")


# ─── Хендлер: данные аккаунта (как input_account_field) ────────────

async def api_order_account(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, order, err = await _auth_order(request, body)
    if err is not None:
        return err

    order_id = order["order_id"]

    if order["status"] in ("pending_activation", "active"):
        return web.json_response({"ok": True, "status": order["status"], "message": "Данные уже получены"})

    if order["status"] != "pending_account":
        return _err(409, "Сначала нужно оплатить заказ")

    service = get_service_by_id(order.get("service_id", ""))
    fields = (service or {}).get("account_fields", [])

    if not fields:
        # Сервис без полей — сразу на активацию (как в боте)
        await db.update_order_status(order_id, "pending_activation")
        return web.json_response({"ok": True, "status": "pending_activation"})

    values = body.get("fields")
    if not isinstance(values, dict):
        return _err(400, "Заполните поля формы")

    account_data: dict[str, str] = {}
    for f in fields:
        val = str(values.get(f.get("id", ""), "")).strip()
        if not val:
            return _err(400, f"Заполните поле: {f.get('label', '—')}")
        account_data[f["id"]] = val[:300]

    await db.update_order_status(order_id, "pending_activation", account_data=account_data)

    # Уведомление админам — зеркало input_account_field
    fields_detail = "\n".join(
        f"  {ce('key')}  {safe_html(f['label'])}: <code>{safe_code(account_data.get(f['id'], ''))}</code>"
        for f in fields
    )
    uname = str(user.get("username") or "N/A")
    fname = safe_html(str(user.get("first_name") or ""))
    method = order.get("payment_method", "")
    await _notify_admins(
        request.app["bot"],
        f"{ce('bell')}   <b>Новый заказ #{order_id} (данные из Mini App)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ce('profile')} Клиент: {fname} (@{safe_html(uname)})\n"
        f"{ce('shopping')}  Сервис: <b>{safe_html(str(order.get('service_name', '')))}</b>\n"
        f"{ce('plan_badge')}  Тариф: {safe_html(str(order.get('plan_name', '')))}\n"
        f"{ce('wallet')}  Сумма: <b>{float(order.get('price_usdt', 0) or 0):.2f} USDT</b> "
        f"({METHOD_NAMES.get(method, method)})\n\n"
        f"{ce('key')}  Данные аккаунта:\n{fields_detail}",
    )

    return web.json_response({"ok": True, "status": "pending_activation"})


# ─── Хендлер: отмена заказа (как cancel_order_confirm) ─────────────

async def api_order_cancel(request: web.Request) -> web.Response:
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, order, err = await _auth_order(request, body)
    if err is not None:
        return err

    if order["status"] == "pending_account":
        return _err(409, "Заказ уже оплачен — отмена невозможна. Напишите в поддержку, если ошиблись.")

    if order["status"] != "pending_payment":
        return _err(409, "Заказ уже обработан — статус изменился")

    transitioned = await db.try_transition_order_status(
        order["order_id"], "pending_payment", "cancelled"
    )
    if not transitioned:
        return _err(409, "Статус заказа уже изменился")

    return web.json_response({"ok": True, "status": "cancelled"})


# ─── Хендлер: профиль (скидка, бонус, рефералка) ───────────────────

async def api_profile(request: web.Request) -> web.Response:
    """Профиль: скидка новичка, бонусный счёт, реферальная программа.

    Зеркало страницы «Рефералы» + маркетинговых полей /api/session.
    """
    body = await _read_body(request)
    if body is None:
        return _err(400, "Ожидался JSON")
    user, _reason = authenticate_user(body)
    if not user:
        return _auth_err(_reason)

    user_id = int(user["id"])
    bot_username = request.app["bot_username"]

    welcome = None
    try:
        welcome = await get_welcome_discount(user_id)
    except Exception:
        pass

    balance = 0.0
    reserved = 0.0
    try:
        balance = float(await get_bonus_balance(user_id) or 0.0)
        reserved = float(await db.get_pending_bonus_reserved(user_id) or 0.0)
    except Exception:
        pass

    code = await generate_referral_code(user_id)
    invited, earned = await get_referral_stats(user_id)
    link = f"https://t.me/{bot_username}?start=ref_{code}" if bot_username else ""

    return web.json_response({
        "ok": True,
        "user": {
            "id": user_id,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
        },
        "store_name": STORE_NAME,
        "bot_username": bot_username,
        "offer_url": "https://disk.yandex.ru/i/HWpCZ1blH8fyUw",
        "welcome_discount": {"discount_pct": welcome["discount_pct"]} if welcome else None,
        "bonus_balance": round(balance, 2),
        "bonus_reserved": round(reserved, 2),
        "rub_rate": await _rub_rate(),
        "referral": {
            "code": code,
            "link": link,
            "invited": int(invited),
            "earned": round(float(earned), 2),
            "bonus_per_friend": 0.50,
        },
    })


# ─── Монтаж ────────────────────────────────────────────────────────

async def _app_redirect(request: web.Request) -> web.Response:
    raise web.HTTPFound("/app/")


async def _app_index(request: web.Request) -> web.FileResponse:
    """aiohttp add_static не умеет index.html — отдаём вручную."""
    return web.FileResponse(WEBAPP_DIR / "index.html")


def setup_webapp_routes(app: web.Application) -> None:
    """Подключить Mini App (статику и API) к aiohttp-приложению бота."""
    app.router.add_get("/app", _app_redirect)
    app.router.add_get("/app/", _app_index)
    app.router.add_static("/app/", path=str(WEBAPP_DIR))
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_post("/api/session", api_session)
    app.router.add_post("/api/order", api_order)
    app.router.add_post("/api/orders", api_orders_list)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/order/{order_id}/info", api_order_info)
    app.router.add_post("/api/order/{order_id}/promo", api_order_promo)
    app.router.add_post("/api/order/{order_id}/pay", api_order_pay)
    app.router.add_post("/api/order/{order_id}/check", api_order_check)
    app.router.add_post("/api/order/{order_id}/account", api_order_account)
    app.router.add_post("/api/order/{order_id}/cancel", api_order_cancel)
    logger.info(
        "Mini App mounted: /app/ + /api/catalog|session|order|orders|profile "
        "|order/{id}/info|promo|pay|check|account|cancel (v24 full parity)"
    )
