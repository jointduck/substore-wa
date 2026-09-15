"""
Интеграция оплаты через Digiseller (digiseller.ru / oplata.info).

Документация (проверено 2026-09):
  https://my.digiseller.com/inside/api_payment.asp      — платёжный цикл
  https://my.digiseller.com/inside/api_statistics.asp   — статистика продаж
  https://my.digiseller.com/inside/api_general.asp      — авторизация (token)

Как работает оплата (ПОЛНЫЙ ЦИКЛ):
  1. Продавец создаёт товар с НЕФИКСИРОВАННОЙ ценой в панели Digiseller
     («цена задаётся количеством юнитов», минимальная цена юнита — в .env
     как DIGISELLER_UNIT_PRICE). API-ключ должен иметь право
     [Статистика]: Статистика продаж.
  2. Бот спрашивает у юзера email (FSM), затем:
     а) POST /api/purchases/options → создаёт «предзаказ» с нужным
        unit_cnt (кол-во юнитов) → возвращает id_po (подписанный ID);
     б) строит ссылку на страницу оплаты С ЯВНЫМИ ПАРАМЕТРАМИ:
          https://oplata.info/asp2/pay_wm.asp
              ?id_d={product_id}      — ID товара
              &id_po={id_po}          — подписанный предзаказ (сумма+опции)
              &unit_cnt={unit_cnt}    — кол-во юнитов (для нефикс. цены)
              &email={email_юзера}    — email покупателя (подставится на страницу)
              &typecurr=RUB&lang=ru-RU
     ВАЖНО: без email в ссылке Digiseller запишет на продажу тот email,
     который юзер введёт сам (или пустой) — и мы НЕ сможем найти оплату!
  3. Юзер платит картой / СБП на oplata.info.
  4. Бот ищет оплату: POST /api/seller-sells/v2 (список продаж, даты в
     МОСКОВСКОМ времени, лимит частоты запросов — возможен HTTP 429).
     Совпадение: email продажи == email, сохранённый в заказе при создании
     ссылки (order.digiseller_email), ИЛИ фиктивный {order_id}@{домен}
     (старые заказы). Сумма: amount_in >= ожидаемая - допуск.
  5. Заказ помечается оплаченным, юзеру выдаётся следующий шаг.

Авторизация:
  POST {BASE}apilogin  {"seller_id": int, "timestamp": int,
                        "sign": sha256(api_key + str(timestamp))}
  → {"token": "..."} действует ~120 минут, обновляется автоматически.
  Base URL: https://api.digiseller.ru/api/  (api.digiseller.com тоже работает)

Коды статуса покупки (purchase/info):
  1 — ожидает оплаты, 2 — отклонена, 3 — оплачена, 4 — истекла,
  5 — возвращена, 35 — возврат в процессе
"""

import asyncio
import hashlib
import logging
import time

import aiohttp

from datetime import datetime, timedelta

from config import (
    DIGISELLER_SELLER_ID,
    DIGISELLER_API_KEY,
    DIGISELLER_PRODUCT_ID,
    DIGISELLER_CURRENCY,
    DIGISELLER_UNIT_PRICE,
    DIGISELLER_PRODUCT_CURRENCY,
    DIGISELLER_EMAIL_DOMAIN,
    DIGISELLER_PARAM_ID,
    DIGISELLER_SKIP_AMOUNT_CHECK,
    USDT_RUB_RATE,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.digiseller.ru/api/"

# Payment page (documented form action; GET params are widely supported)
PAYMENT_PAGE_URL = "https://oplata.info/asp2/pay_wm.asp"

# ─── Token cache ─────────────────────────────────────────────────────

_token: str | None = None
_token_expires: float = 0.0
_token_lock = asyncio.Lock()

TOKEN_LIFETIME = 110 * 60  # API says 120 min; refresh a bit earlier


def is_configured() -> bool:
    """Check if Digiseller credentials are set."""
    return bool(DIGISELLER_SELLER_ID and DIGISELLER_API_KEY and DIGISELLER_PRODUCT_ID)


async def _get_token() -> str:
    """Get (and cache/refresh) the Digiseller API token.

    Signature: sha256(api_key + str(timestamp))
    See: https://my.digiseller.com/inside/api_general.asp
    """
    global _token, _token_expires

    async with _token_lock:
        now = time.time()
        if _token and now < _token_expires:
            return _token

        timestamp = int(now)
        # api_key is already stripped in config.py, but strip again defensively
        api_key = (DIGISELLER_API_KEY or "").strip()
        sign_input = api_key + str(timestamp)
        sign = hashlib.sha256(sign_input.encode("utf-8")).hexdigest()
        payload = {
            "seller_id": int(DIGISELLER_SELLER_ID),
            "timestamp": timestamp,
            "sign": sign,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{BASE_URL}apilogin",
                    json=payload,
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json(content_type=None)
        except Exception as e:
            raise RuntimeError(f"Digiseller auth network error: {e}") from e

        token = data.get("token")
        if not token:
            # Detailed error for diagnostics — mask api_key in logs
            masked_key = api_key[:4] + "***" + api_key[-4:] if len(api_key) > 8 else "***"
            raise RuntimeError(
                f"Digiseller auth failed: {data} "
                f"[seller_id={DIGISELLER_SELLER_ID}, api_key={masked_key}, "
                f"timestamp={timestamp}, sign={sign[:16]}...]"
            )

        _token = token
        _token_expires = time.time() + TOKEN_LIFETIME
        logger.info("Digiseller API token acquired")
        return _token


async def _api_post(endpoint: str, payload: dict) -> dict | None:
    """POST request to Digiseller API with token in query string."""
    token = await _get_token()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BASE_URL}{endpoint}",
            params={"token": token},
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Digiseller {endpoint}: HTTP {resp.status}")
                return None
            return await resp.json(content_type=None)


async def _api_get(endpoint: str, params: dict) -> dict | None:
    """GET request to Digiseller API with token in query string."""
    token = await _get_token()
    q = dict(params)
    q["token"] = token
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BASE_URL}{endpoint}",
            params=q,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Digiseller {endpoint}: HTTP {resp.status}")
                return None
            return await resp.json(content_type=None)


# ─── Payment link ────────────────────────────────────────────────────

def build_payment_url(order_id: int, amount: float | None = None, buyer_email: str = "") -> str:
    """Build a BASIC Digiseller payment page URL (without locked amount).

    WARNING: The `amount` GET parameter on pay_wm.asp is NOT reliably
    honored by oplata.info — the page usually falls back to the product's
    minimum price. Use `create_payment_url()` instead, which calls the
    Digiseller API to obtain a signed `id_po` that locks in the amount.

    This function is kept only as a fallback for cases where the API call
    fails and we want to give the user a payment link with a manual amount.
    """
    from urllib.parse import urlencode

    email = buyer_email or order_email(order_id)
    params = {
        "id_d": int(DIGISELLER_PRODUCT_ID),
        "typecurr": DIGISELLER_CURRENCY,
        "lang": "ru-RU",
        "email": email,
    }
    if amount is not None and amount > 0:
        params["amount"] = f"{float(amount):.2f}"
    return f"{PAYMENT_PAGE_URL}?{urlencode(params)}"


async def create_payment_url(
    order_id: int,
    amount: float | None = None,
    buyer_email: str = "",
    user_ip: str = "",
) -> tuple[str, float, str]:
    """Создать ссылку на оплату Digiseller с ЗАКРЕПЛЁННОЙ суммой и email юзера.

    ВАЖНО ПРО МЕХАНИКУ ОПЛАТЫ DIGISELLER:
      Товар с «произвольной ценой» на Digiseller имеет минимальную цену одного
      юнита (задаётся в панели как «Минимальная цена», у нас — .env
      DIGISELLER_UNIT_PRICE). Покупатель платит: цена_юнита × unit_cnt,
      где unit_cnt — целое число юнитов.

      Поле `amount` в /api/purchases/options НЕ работает (API его игнорирует).
      Правильный параметр — `unit_cnt` (количество юнитов). Причём id_po
      надёжно закрепляет сумму ТОЛЬКО у товаров с обязательными параметрами,
      поэтому unit_cnt дополнительно передаётся прямо в ссылке на оплату.

      Пример: минимальная цена юнита 1 ₽, нужно получить 475.50 ₽ →
      unit_cnt = round(475.50 / 1) = 476 → юзер заплатит 476 ₽.

      EMAIL обязательно включается в ссылку (&email=...) — он подставляется
      на страницу оплаты и попадает в продажу на площадке. По этому email
      бот потом находит оплату. Без него поиск оплаты НЕ РАБОТАЕТ.

    Args:
        order_id: ID нашего заказа.
        amount: желаемая сумма оплаты в DIGISELLER_CURRENCY (RUB по умолчанию).
            Будет конвертирована в unit_cnt = round(amount / DIGISELLER_UNIT_PRICE).
            Фактическая сумма может слегка отличаться из-за округления.
            Pass None only if the product has a fixed price.
        buyer_email: email покупателя (вводится юзером через FSM).
        user_ip: buyer IP (best-effort, used by Digiseller for fraud checks).

    Returns:
        Кортеж (payment_url, фактическая_сумма_оплаты, id_po).
        Фактическая сумма = unit_cnt × DIGISELLER_UNIT_PRICE.
        id_po сохраняется в заказе для диагностики.

    Raises:
        RuntimeError: if the API call fails or returns no id_po.
    """
    email = buyer_email or order_email(order_id)
    payload: dict = {
        "product_id": int(DIGISELLER_PRODUCT_ID),
        "lang": "ru-RU",
        "email": email,
        "ip": user_ip or "127.0.0.1",
    }

    # ── Конвертируем желаемую сумму (RUB) в количество юнитов товара ──────
    #
    # Товар на Digiseller имеет «минимальную цену юнита» (DIGISELLER_UNIT_PRICE)
    # в ВАЛЮТЕ ТОВАРА (DIGISELLER_PRODUCT_CURRENCY). Например, товар в WMT
    # с ценой юнита 0.01 WMT означает ~1 ₽ за юнит (при курсе 1 WMT = 95 ₽).
    #
    # Сумма, которую мы передаём ботом (amount) — в RUB. Чтобы получить
    # правильный unit_cnt, нужно сначала перевести amount в валюту товара:
    #   amount_in_product_currency = amount_rub / rate_to_rub(currency)
    #   unit_cnt = round(amount_in_product_currency / DIGISELLER_UNIT_PRICE)
    #
    # Если валюта товара = RUB, конвертация не нужна (rate=1).
    actual_amount_rub = 0.0
    unit_cnt = 0
    if amount is not None and amount > 0 and DIGISELLER_UNIT_PRICE > 0:
        product_cur = (DIGISELLER_PRODUCT_CURRENCY or "").strip().upper()
        # Курс валюты товара к RUB (для WMT/USD ~95, для RUB = 1)
        if product_cur in ("RUB", "RUR", ""):
            rate = 1.0
        elif product_cur in ("WMT", "WMZ", "USD", "USDT"):
            rate = float(USDT_RUB_RATE)
        elif product_cur in ("WME", "EUR"):
            rate = 100.0  # примерный курс EUR→RUB
        else:
            logger.warning(
                f"Digiseller: неизвестная валюта товара {product_cur!r} — "
                f"считаю unit_cnt как для RUB"
            )
            rate = 1.0

        # amount приходит в RUB → переводим в валюту товара
        amount_in_product_cur = float(amount) / rate
        unit_cnt = max(1, round(amount_in_product_cur / DIGISELLER_UNIT_PRICE))
        payload["unit_cnt"] = int(unit_cnt)

        # Фактическая сумма в RUB = unit_cnt × unit_price × rate
        actual_amount_rub = round(unit_cnt * DIGISELLER_UNIT_PRICE * rate, 2)

        logger.info(
            f"Digiseller: расчёт unit_cnt: "
            f"желаемая_сумма={amount} RUB → "
            f"{amount_in_product_cur:.4f} {product_cur} ÷ "
            f"unit_price={DIGISELLER_UNIT_PRICE} {product_cur} = "
            f"unit_cnt={unit_cnt} → фактическая_сумма={actual_amount_rub} RUB"
        )

    # Optional: bind order_id to a product text parameter for cleaner matching
    if DIGISELLER_PARAM_ID:
        payload["options"] = [
            {"id": int(DIGISELLER_PARAM_ID), "value": {"text": str(order_id)}}
        ]

    # Диагностика: логируем, что отправляем (без чувствительных данных)
    logger.info(
        f"Digiseller purchases/options request: product_id={payload['product_id']}, "
        f"unit_cnt={payload.get('unit_cnt', 'НЕ ЗАДАН')}, "
        f"unit_price={DIGISELLER_UNIT_PRICE} {DIGISELLER_PRODUCT_CURRENCY}, "
        f"желаемая_сумма={amount} RUB, фактическая_сумма={actual_amount_rub} RUB, "
        f"email={email[:3]}***@{email.split('@')[-1] if '@' in email else '?'}"
    )

    data = await _api_post("purchases/options", payload)
    if not data:
        raise RuntimeError("Digiseller purchases/options: пустой ответ от API")

    logger.info(f"Digiseller purchases/options response: retval={data.get('retval')}, retdesc={data.get('retdesc', '-')}, id_po={'есть' if data.get('id_po') else 'НЕТ'}")

    retval = data.get("retval")
    if retval not in (0, "0"):
        retdesc = data.get("retdesc") or data.get("desc") or "неизвестная ошибка"
        raise RuntimeError(
            f"Digiseller API вернул ошибку retval={retval}: {retdesc}. "
            f"Проверьте: 1) товар имеет тип «произвольная цена», "
            f"2) минимальная цена товара (DIGISELLER_UNIT_PRICE) задана верно в .env, "
            f"3) API-ключ имеет право на purchases/options"
        )

    id_po = data.get("id_po")
    if not id_po:
        raise RuntimeError(f"Digiseller purchases/options: no id_po in response: {data}")

    # ── Итоговая ссылка на страницу оплаты ──────────────────────────
    # По документации Digiseller («Jump to payment») страница оплаты принимает:
    #   id_d      — ID товара (обязательный);
    #   id_po     — подписанный набор значений (сумма + параметры товара);
    #   unit_cnt  — количество юнитов для товаров с НЕфиксированной ценой.
    #               ВАЖНО: id_po надёжно работает только для товаров с
    #               обязательными параметрами, поэтому unit_cnt дублируем
    #               прямо в ссылке — иначе страница показывает МИНИМУМ;
    #   email     — email покупателя, подставляется на страницу и попадает
    #               в продажу — по нему мы потом ИЩЕМ ОПЛАТУ.
    from urllib.parse import urlencode
    params = {
        "id_d": int(DIGISELLER_PRODUCT_ID),
        "typecurr": DIGISELLER_CURRENCY,
        "lang": "ru-RU",
        "email": email,
    }
    if unit_cnt > 0:
        params["unit_cnt"] = unit_cnt
    params["id_po"] = str(id_po)

    logger.info(
        f"Digiseller: создана ссылка для заказа #{order_id}: "
        f"id_po={id_po}, unit_cnt={unit_cnt or '-'}, "
        f"фактическая_сумма={actual_amount_rub} RUB, "
        f"email={email[:3]}***@{email.split('@')[-1] if '@' in email else '?'}"
    )
    return f"{PAYMENT_PAGE_URL}?{urlencode(params)}", actual_amount_rub, str(id_po)


def order_email(order_id: int) -> str:
    """Buyer email that uniquely identifies an order for matching."""
    return f"{order_id}@{DIGISELLER_EMAIL_DOMAIN}"


async def bind_order_parameter(order_id: int, user_ip: str = "") -> str | None:
    """Bind the order id to the product's required text parameter.

    Requires DIGISELLER_PARAM_ID — numeric id of the product parameter
    (from the product editor in the Digiseller panel). Returns id_po
    (add &id_po=... to the payment URL), or None on failure.
    """
    if not DIGISELLER_PARAM_ID:
        return None
    payload = {
        "product_id": int(DIGISELLER_PRODUCT_ID),
        "options": [
            {"id": int(DIGISELLER_PARAM_ID), "value": {"text": str(order_id)}}
        ],
        "lang": "ru-RU",
        "ip": user_ip or "127.0.0.1",
    }
    data = await _api_post("purchases/options", payload)
    if not data or data.get("retval") != 0:
        logger.warning(f"Digiseller purchases/options failed: {data}")
        return None
    return str(data.get("id_po") or "") or None


# ─── Проверка оплаты ───────────────────────────────────────────────

# Digiseller ограничивает частоту запросов seller-sells/v2 — при превышении
# возвращает HTTP 429. Поэтому: все проверки идут через общий лок с
# минимальным интервалом, а при 429 делаем одну повторную попытку через 10 с.
_sells_lock = asyncio.Lock()
_last_sells_ts: float = 0.0
SELLS_MIN_INTERVAL = 5.0   # секунды между запросами seller-sells/v2
SELLS_RETRY_DELAY = 10.0   # ожидание после HTTP 429 перед повтором


async def get_recent_sales(hours: float = 3.0, page_rows: int = 100) -> list[dict]:
    """Получить последние оплаченные продажи через seller-sells/v2.

    Args:
        hours: за сколько часов назад смотреть (окно поиска).
        page_rows: строк на странице (максимум по докам — 1000).

    Returns:
        Список строк продаж: invoice_id, product_id, product_name,
        date_put, date_pay, email, amount_in, amount_out,
        amount_currency, method_pay, aggregator_pay, returned, owner...
        Даты Дигиселлера — московское время (UTC+3).
    """
    global _last_sells_ts

    from datetime import datetime, timedelta

    now_utc = datetime.utcnow()
    date_end = now_utc + timedelta(hours=3)   # UTC → МСК
    date_start = now_utc - timedelta(hours=hours)

    payload = {
        "date_start": date_start.strftime("%Y-%m-%d %H:%M:%S"),
        "date_finish": date_end.strftime("%Y-%m-%d %H:%M:%S"),
        "returned": 1,  # 1 = исключить возвраты (0 = ВКЛЮЧАТЬ, 2 = только возвраты)
        "page": 1,
        "rows": page_rows,
    }
    if DIGISELLER_PRODUCT_ID:
        payload["product_ids"] = [int(DIGISELLER_PRODUCT_ID)]

    async with _sells_lock:
        # Троттлинг: не чаще SELLS_MIN_INTERVAL секунд между запросами
        wait = SELLS_MIN_INTERVAL - (time.time() - _last_sells_ts)
        if wait > 0:
            await asyncio.sleep(wait)

        token = await _get_token()
        data: dict | None = None

        for attempt in (1, 2):
            _last_sells_ts = time.time()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{BASE_URL}seller-sells/v2",
                        params={"token": token},
                        json=payload,
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 429 and attempt == 1:
                            logger.warning(
                                f"Digiseller seller-sells/v2: HTTP 429 (лимит частоты), "
                                f"повтор через {SELLS_RETRY_DELAY:.0f} с..."
                            )
                            await asyncio.sleep(SELLS_RETRY_DELAY)
                            continue
                        if resp.status != 200:
                            logger.warning(f"Digiseller seller-sells/v2: HTTP {resp.status}")
                            return []
                        data = await resp.json(content_type=None)
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Digiseller seller-sells/v2 ошибка сети: {e}")
                return []

    if not data:
        return []
    if data.get("retval") not in (0, "0", None):
        logger.warning(
            f"Digiseller seller-sells retval={data.get('retval')}: {data.get('retdesc')}"
        )
        return []

    rows = list(data.get("rows") or [])
    logger.info(f"Digiseller seller-sells/v2: получено {len(rows)} продаж за {hours} ч")
    return rows


async def get_purchase_info(invoice_id: int) -> dict | None:
    """Fetch purchase info by Digiseller invoice id (purchase/info).

    Возвращает СЛИЯНИЕ верхних полей + content, чтобы caller'у было удобно
    работать. Внутри content обычно лежат:
      invoice_state   — 3 = оплачена, 2 = отклонена, 5 = возврат
      amount          — сумма покупки в ВАЛЮТЕ ТОВАРА
      currency_type   — код валюты ("RUB", "WMZ", "WMT", "USD", "EUR", ...)
      options         — список опций товара (id, name, value)
      id_po           — подписанный предзаказ (если создавали)
      date_pay        — дата оплаты (МСК)
      email           — email покупателя
      ...
    """
    try:
        data = await _api_get(f"purchase/info/{int(invoice_id)}", {})
    except Exception as e:
        logger.warning(f"Digiseller purchase/info/{invoice_id}: network error: {e}")
        return None
    if not data:
        logger.warning(f"Digiseller purchase/info/{invoice_id}: пустой ответ")
        return None
    content = data.get("content") or {}
    # Сливаем верхний уровень с content — некоторые поля (retval, retdesc)
    # лежат на верхнем уровне, остальные (amount, options) — внутри content.
    merged = dict(data)
    merged.update(content)
    # Диагностика: показываем ключевые поля ответа (без email — он маскируется)
    if "email" in merged and merged["email"]:
        merged_for_log = dict(merged)
        merged_for_log["email"] = _mask_email(str(merged["email"]))
    else:
        merged_for_log = merged
    logger.info(
        f"Digiseller purchase/info/{invoice_id}: "
        f"retval={merged.get('retval')}, invoice_state={merged.get('invoice_state')}, "
        f"amount={merged.get('amount')}, currency={merged.get('currency_type')}, "
        f"options_count={len(merged.get('options') or [])}"
    )
    return merged or None


# Внутренний кеш курсов валют, чтобы не дёргать сторонние API слишком часто.
# Digiseller возвращает amount_in во внутренней валюте (часто WMZ/USD);
# нам нужно сравнивать с ожидаемой суммой в RUB. Курсы обновляются редко —
# держим в памяти ~1 час.
_fx_cache: dict[str, tuple[float, float]] = {}  # currency -> (rate_to_rub, fetched_at)
_FX_TTL = 3600.0


async def _rate_to_rub(currency: str) -> float | None:
    """Примерный курс валюты к RUB.

    Digiseller хранит продажи в разных валютах:
      - RUB / RUR     — российский рубль (rate = 1.0)
      - WMT           — WebMoney Transfer, ~эквивалент USD (PayMaster при
                        оплате картой конвертирует RUB→WMT по своему курсу)
      - WMZ / USD     — доллар США
      - USDT          — криптодоллар
      - WME / EUR     — евро

    Для всех USD-эквивалентов берём USDT_RUB_RATE из config (по умолчанию 95).
    Если валюта неизвестна — возвращаем None (сумма останется как есть,
    и в логе будет видно, что нужно добавить курс вручную).
    """
    cur = (currency or "").strip().upper()
    if cur in ("RUB", "RUR", "₽", ""):
        return 1.0
    if cur in ("WMT", "WMZ", "USD", "USDT"):
        try:
            from config import USDT_RUB_RATE
            return float(USDT_RUB_RATE)
        except Exception:
            return 95.0
    if cur in ("WME", "EUR"):
        return 100.0  # примерный курс
    if cur in ("WMR",):
        return 1.0
    return None


def _mask_email(addr: str) -> str:
    """Маскирует email для безопасного лога: ivan@mail.ru → iva***@mail.ru"""
    if not addr or "@" not in addr:
        return "?"
    local, domain = addr.split("@", 1)
    return f"{local[:3]}***@{domain}"


def _safe_row_for_log(row: dict) -> dict:
    """Копия row с замаскированным email — для безопасного лога."""
    out = dict(row or {})
    if "email" in out and out["email"]:
        out["email"] = _mask_email(str(out["email"]))
    return out


async def _resolve_amount_rub(row: dict, invoice_id: int | None) -> tuple[float, str, dict | None]:
    """Получить точную сумму оплаты в RUB.

    Стратегия (по приоритету):
      1. row.amount_in_usd × USDT_RUB_RATE — САМЫЙ НАДЁЖНЫЙ источник: Digiseller
         сам конвертирует платёж в USD и хранит его в amount_in_usd. Курсы
         Digiseller ≈ рыночные, расхождение минимально.
      2. purchase/info/{invoice_id} → content.amount + content.currency_type.
         Это запасной источник: amount возвращается в валюте товара (WMT/WMZ/RUB).
         Конвертируем через _rate_to_rub.
      3. row.amount_in × _rate_to_rub(amount_currency) — последний фолбэк.

    Returns:
      (amount_rub, source_label, purchase_info_or_None)
      source_label — для лога: "amount_in_usd" / "purchase/info" / "row.amount_in" / "none"
    """
    # ── 1. amount_in_usd — готовое поле Digiseller, конвертирует сам площадка ──
    amt_usd = row.get("amount_in_usd")
    if amt_usd is not None:
        try:
            usd = float(amt_usd)
            if usd > 0:
                try:
                    from config import USDT_RUB_RATE
                    rate = float(USDT_RUB_RATE)
                except Exception:
                    rate = 95.0
                amount_rub = usd * rate
                logger.info(
                    f"Digiseller: сумма через amount_in_usd: "
                    f"{usd} USD × {rate} = {amount_rub:.2f} RUB"
                )
                # purchase/info всё равно вызываем — для опций товара (матчинг по order_id)
                info: dict | None = None
                if invoice_id:
                    try:
                        info = await get_purchase_info(int(invoice_id))
                    except Exception as e:
                        logger.warning(f"Digiseller purchase/info/{invoice_id}: {e}")
                return amount_rub, "amount_in_usd", info
        except (TypeError, ValueError):
            pass

    # ── 2. purchase/info — точная сумма в валюте товара ──
    info: dict | None = None
    if invoice_id:
        try:
            info = await get_purchase_info(int(invoice_id))
        except Exception as e:
            logger.warning(f"Digiseller purchase/info/{invoice_id} error: {e}")
            info = None

    if info:
        # content.amount — в валюте товара (RUB, WMT, WMZ, ...)
        amt = info.get("amount")
        if amt is None:
            amt = info.get("amount_in")
        if amt is not None:
            try:
                amount = float(amt)
                currency = (info.get("currency_type") or info.get("amount_currency") or "").strip().upper()
                # Если валюта не RUB — конвертируем
                if currency and currency not in ("RUB", "RUR", ""):
                    rate = await _rate_to_rub(currency)
                    if rate:
                        amount = amount * rate
                        logger.info(
                            f"Digiseller: сумма через purchase/info: "
                            f"{amt} {currency} × {rate} = {amount:.2f} RUB"
                        )
                    else:
                        logger.warning(
                            f"Digiseller: неизвестная валюта {currency!r} — "
                            f"сумма оставлена как есть ({amount})"
                        )
                else:
                    logger.info(f"Digiseller: сумма через purchase/info: {amount} RUB")
                return amount, "purchase/info", info
            except (TypeError, ValueError):
                pass

    # ── 3. Фолбэк: row.amount_in × rate ──
    amt_in = row.get("amount_in")
    if amt_in is None:
        amt_in = row.get("amount")
    if amt_in is None:
        return 0.0, "none", info
    try:
        amount = float(amt_in)
        currency = (row.get("amount_currency") or "").strip().upper()
        if currency and currency not in ("RUB", "RUR", ""):
            rate = await _rate_to_rub(currency)
            if rate:
                amount = amount * rate
                logger.info(
                    f"Digiseller: сумма через row.amount_in (фолбэк): "
                    f"{amt_in} {currency} × {rate} = {amount:.2f} RUB"
                )
            else:
                logger.warning(
                    f"Digiseller: неизвестная валюта {currency!r} — "
                    f"сумма оставлена как есть ({amount})"
                )
        return amount, "row.amount_in", info
    except (TypeError, ValueError):
        return 0.0, "none", info


# ── Время продаж Digiseller ─────────────────────────────────────
# Даты Digiseller — московское время (UTC+3), формат "%Y-%m-%d %H:%M:%S".

_CLOCK_SKEW = timedelta(minutes=5)  # допуск на расхождение часов


def _sale_dt_utc(row: dict) -> datetime | None:
    """Дата продажи (date_pay, fallback date_put) из МСК в UTC-наивный."""
    raw = row.get("date_pay") or row.get("date_put") or ""
    if not raw:
        return None
    try:
        msk = datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return msk - timedelta(hours=3)  # MSK → UTC


def _coerce_utc(value: datetime | str | None) -> datetime | None:
    """ISO-строку или datetime → наивный UTC. None/мусор → None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


async def find_payment_for_order(
    order_id: int,
    expected_amount: float = 0.0,
    hours: float = 6.0,
    buyer_email: str = "",
    not_before: datetime | str | None = None,
) -> dict:
    """Найти оплату Digiseller для нашего заказа.

    Стратегия поиска (в порядке приоритета):
      1. Сначала проходим по всем продажам и для каждой вызываем
         purchase/info/{invoice_id}, который возвращает content.options —
         там мы ищем наш order_id (если настроен DIGISELLER_PARAM_ID).
         Это НАИБОЛЕЕ надёжный способ: одна продажа = один заказ.
      2. Если опции не заданы (нет DIGISELLER_PARAM_ID) — матчит по email:
         а) buyer_email — реальный email юзера, сохранённый в заказе;
         б) фиктивный "{order_id}@{DIGISELLER_EMAIL_DOMAIN}" (старые заказы).

    Проверка суммы:
      Берём amount через purchase/info (точная сумма в валюте товара = RUB).
      Сравниваем с expected_amount с допуском max(1.0, 5% суммы).
      Если expected_amount <= 0 — сумма не проверяется (платёж засчитывается).

    Args:
        order_id: ID нашего заказа.
        expected_amount: ожидаемая сумма в DIGISELLER_CURRENCY (RUB).
        hours: окно поиска в часах (сколько назад смотреть продажи).
        buyer_email: email юзера из заказа (order.digiseller_email).
        not_before: момент создания ссылки на оплату (UTC; datetime или
            ISO-строка из orders.digiseller_link_created_at). Продажи,
            оплаченные РАНЬШЕ этого момента (минус допуск 5 мин на
            расхождение часов), НЕ считаются оплатой заказа — это старые
            платежи с тем же email (прошлые покупки юзера). Без фильтра
            poller ложно подтверждал новый заказ по старому платежу.
            None — старое поведение (для заказов без штампа времени).

    Returns:
      {"paid": True,  "invoice_id": ..., "amount": ..., "method": ...}
      {"paid": False, "underpaid": True, "amount_in": ..., "invoice_id": ...}
      {"paid": False}
    """
    try:
        sales = await get_recent_sales(hours=hours)
    except Exception as e:
        logger.error(f"Digiseller get_recent_sales error: {e}")
        return {"paid": False}

    not_before_utc = _coerce_utc(not_before)

    if not sales:
        return {"paid": False}

    # ── СОРТИРОВКА ПО ДАТЕ (новые сначала) ─────────────────────────────
    # Если у юзера несколько платежей с тем же email (тестировал несколько
    # раз), бот должен брать САМЫЙ СВЕЖИЙ платёж, а не первый попавшийся.
    # Сортируем по date_pay (убывание) — самые новые в начале списка.
    def _sort_key(r: dict):
        # date_put может быть "0001-01-01 00:00:00" для неоплаченных —
        # используем date_pay, fallback на date_put
        d = r.get("date_pay") or r.get("date_put") or ""
        return str(d)  # лексикографическая сортировка работает для ISO-дат

    sales_sorted = sorted(sales, key=_sort_key, reverse=True)

    # Все email, по которым может быть оформлена оплата этого заказа
    expected_emails = {order_email(order_id).strip().lower()}
    if buyer_email:
        expected_emails.add(buyer_email.strip().lower())

    # Шаг 1: если настроен DIGISELLER_PARAM_ID, ищем по опции товара.
    # Digiseller возвращает в purchase/info список options; значение опции
    # с id=DIGISELLER_PARAM_ID = наш order_id. Это 100% надёжный матчинг.
    if DIGISELLER_PARAM_ID:
        for row in sales_sorted:
            try:
                if int(row.get("returned") or 0) != 0:
                    continue
                inv_id = row.get("invoice_id")
                if not inv_id:
                    continue
                info = await get_purchase_info(int(inv_id))
                if not info:
                    continue
                for opt in (info.get("options") or []):
                    try:
                        opt_id = int(opt.get("id") or 0)
                    except (TypeError, ValueError):
                        opt_id = 0
                    if opt_id != int(DIGISELLER_PARAM_ID):
                        continue
                    val = str(opt.get("value", "")).strip()
                    if val == str(order_id):
                        # Точное совпадение по опции — это наш платёж!
                        amount_rub, src, _ = await _resolve_amount_rub(row, inv_id)
                        logger.info(
                            f"Digiseller: найден платёж по опции товара "
                            f"(order_id={order_id}, invoice={inv_id}, "
                            f"amount_rub={amount_rub}, источник={src})"
                        )
                        return _build_paid_result(
                            order_id, row, inv_id, amount_rub, expected_amount, info
                        )
            except Exception as e:
                logger.warning(f"Digiseller purchase/info match error: {e}")
                continue

    # Шаг 2: матчит по email — собираем ВСЕ совпадения, потом берём свежий.
    # Логируем каждый найденный платёж, чтобы видеть, сколько их вообще.
    matched_rows: list[dict] = []
    skipped_stale = 0
    for row in sales_sorted:
        try:
            if int(row.get("returned") or 0) != 0:
                continue  # возврат — пропускаем

            email = (row.get("email") or "").strip().lower()
            if email not in expected_emails:
                continue

            # Платёж не может быть оплачен ДО создания ссылки этого заказа.
            # Продажа старше ссылки — прошлый платёж с тем же email
            # (например, предыдущая покупка/тест юзера), НЕ этот заказ.
            if not_before_utc is not None:
                sale_dt = _sale_dt_utc(row)
                if sale_dt is not None and sale_dt < not_before_utc - _CLOCK_SKEW:
                    skipped_stale += 1
                    logger.info(
                        f"Digiseller: пропущена СТАРАЯ продажа "
                        f"invoice={row.get('invoice_id')} "
                        f"(date_pay={row.get('date_pay')} MSK — раньше создания "
                        f"ссылки) — это НЕ оплата заказа #{order_id}"
                    )
                    continue

            matched_rows.append(row)
        except Exception as e:
            logger.warning(f"Digiseller sale row processing error: {e}")
            continue

    if not matched_rows:
        # Диагностика: продажи есть, но ни одна не совпала
        seen_emails = {
            _mask_email((r.get("email") or "").strip().lower())
            for r in sales
            if int(r.get("returned") or 0) == 0
        }
        logger.info(
            f"Digiseller: оплата для заказа #{order_id} НЕ найдена среди "
            f"{len(sales)} продаж (отброшено старых: {skipped_stale}). Ожидали email: "
            f"{sorted(_mask_email(e) for e in expected_emails)} | "
            f"На площадке: {sorted(seen_emails) or 'нет продаж без возвратов'}"
        )
        return {"paid": False}

    # Логируем ВСЕ совпадения — чтобы видеть, сколько платежей с этим email
    import json as _json
    logger.info(
        f"Digiseller: найдено {len(matched_rows)} платежей с email "
        f"{_mask_email(list(expected_emails)[0])}. Берём самый свежий."
    )
    for i, row in enumerate(matched_rows):
        logger.info(
            f"  [{i+1}/{len(matched_rows)}] invoice={row.get('invoice_id')}, "
            f"date_pay={row.get('date_pay')}, amount_in={row.get('amount_in')} "
            f"{row.get('amount_currency')}, amount_in_usd={row.get('amount_in_usd')}"
        )

    # Берём самый свежий (sales_sorted уже отсортирован по убыванию даты)
    row = matched_rows[0]
    inv_id = row.get("invoice_id")

    logger.info(
        f"Digiseller: выбран САМЫЙ СВЕЖИЙ платёж: invoice={inv_id}, "
        f"date_pay={row.get('date_pay')}, amount_in={row.get('amount_in')} "
        f"{row.get('amount_currency')}"
    )

    # Точная сумма через purchase/info (валюта товара)
    amount_rub, src, info = await _resolve_amount_rub(row, inv_id)

    return _build_paid_result(
        order_id, row, inv_id, amount_rub, expected_amount, info
    )


def _build_paid_result(
    order_id: int,
    row: dict,
    invoice_id,
    amount_rub: float,
    expected_amount: float,
    info: dict | None,
) -> dict:
    """Сформировать результат find_payment_for_order на основе суммы.

    Логика проверки суммы:
      1. Если DIGISELLER_SKIP_AMOUNT_CHECK=true — сумма НЕ проверяется.
         Платёж засчитывается, если совпал email и invoice_state=3.
         Это для случая, когда товар в RUB, но деньги приходят в WMT —
         конвертация через банк+PayMaster даёт непредсказуемый курс.
      2. Иначе: если amount_rub < expected_amount - tolerance — underpaid.
         tolerance = max(1.0, 5% суммы).
      3. Если expected_amount <= 0 — сумма не проверяется.

    Дополнительно проверяем invoice_state из purchase/info:
      3 = оплачена, 2 = отклонена, 5 = возврат. Если не 3 — не оплачено.
    """
    # Проверка статуса платежа через purchase/info (если есть)
    if info:
        state = info.get("invoice_state")
        if state is not None:
            try:
                state_int = int(state)
                if state_int != 3:
                    logger.info(
                        f"Digiseller: заказ #{order_id} — invoice_state={state_int} "
                        f"(не 3 = не оплачено). invoice={invoice_id}"
                    )
                    return {"paid": False}
            except (TypeError, ValueError):
                pass  # не удалось распарсить — продолжаем как обычно

    # Мягкая проверка: только по email + invoice_state, без суммы
    if DIGISELLER_SKIP_AMOUNT_CHECK:
        logger.info(
            f"Digiseller: оплата подтверждена (мягкая проверка, без суммы): "
            f"invoice={invoice_id}, amount_rub={amount_rub:.4f} "
            f"(ожидание {expected_amount}, но проверка суммы отключена)"
        )
        return {
            "paid": True,
            "invoice_id": invoice_id,
            "amount": amount_rub,
            "currency": row.get("amount_currency") or DIGISELLER_CURRENCY,
            "method": row.get("method_pay") or "",
            "aggregator": row.get("aggregator_pay") or "",
            "date_pay": row.get("date_pay") or "",
        }

    # Жёсткая проверка суммы (если DIGISELLER_SKIP_AMOUNT_CHECK=false)
    tolerance = max(1.0, expected_amount * 0.05) if expected_amount > 0 else 0.0

    if expected_amount > 0 and amount_rub < expected_amount - tolerance:
        logger.info(
            f"Digiseller: заказ #{order_id} оплачен НА МЕНЬШУЮ СУММУ: "
            f"{amount_rub:.4f} < {expected_amount} (допуск {tolerance:.2f}). "
            f"invoice={invoice_id}. "
            f"Включите DIGISELLER_SKIP_AMOUNT_CHECK=true, если товар в RUB, "
            f"но зачисление идёт в WMT (конвертация через банк+PayMaster)."
        )
        return {
            "paid": False,
            "underpaid": True,
            "amount_in": amount_rub,
            "amount_expected": expected_amount,
            "invoice_id": invoice_id,
        }

    logger.info(
        f"Digiseller: оплата найдена для заказа #{order_id}: "
        f"invoice={invoice_id}, amount_rub={amount_rub:.4f}, "
        f"expected={expected_amount}, method={row.get('method_pay')}"
    )
    return {
        "paid": True,
        "invoice_id": invoice_id,
        "amount": amount_rub,
        "currency": row.get("amount_currency") or DIGISELLER_CURRENCY,
        "method": row.get("method_pay") or "",
        "aggregator": row.get("aggregator_pay") or "",
        "date_pay": row.get("date_pay") or "",
    }
