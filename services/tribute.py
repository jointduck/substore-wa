"""Клиент Tribute Shop API (https://wiki.tribute.tg/ru/for-shops/api-magazina).

Принимает карточные платежи вместо/вместе с Digiseller. Ключевые
отличия от Digiseller-интеграции:

  1. Каждый заказ имеет СОБСТВЕННЫЙ UUID — подтверждение оплаты
     точное, никаких подбо́ров продаж по email (устраняет класс
     бага «логин/пароль до оплаты»).
  2. Оплата происходит внутри Telegram (Mini App) — email юзера
     не нужен вовсе.
  3. Подтверждение — опросом GET /shop/orders/{uuid}/status:
     публичный домен/вебхуки не нужны, работает на локальной машине.

API: https://tribute.tg/api/v1, авторизация — заголовок Api-Key.
Суммы — в минимальных единицах валюты (копейки для RUB).
Лимиты RUB: 100 – 300 000 ₽ за заказ.
"""
import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

API_BASE = "https://tribute.tg/api/v1"

# Лимиты суммы для обычных заказов (копейки)
MIN_AMOUNT_KOP = 100 * 100          # 100 ₽
MAX_AMOUNT_KOP = 300_000 * 100      # 300 000 ₽

# Лимиты строк из API (UTF-16 code units)
_TITLE_MAX_UTF16 = 100
_DESC_MAX_UTF16 = 300

# Таймауты запросов
_CONNECT_TIMEOUT = 15
_TOTAL_TIMEOUT = 30


class TributeError(Exception):
    """Ошибка взаимодействия с Tribute Shop API."""


def is_configured() -> bool:
    """True, если задан TRIBUTE_API_KEY — карта идёт через Tribute."""
    from config import TRIBUTE_API_KEY
    return bool(TRIBUTE_API_KEY)


def _utf16_len(s: str) -> int:
    """Длина строки в UTF-16 code units (как считает API Tribute)."""
    return len(s.encode("utf-16-le")) // 2


def truncate_utf16(s: str, limit: int) -> str:
    """Обрезать строку до limit UTF-16 units, не разрывая символы."""
    if _utf16_len(s) <= limit:
        return s
    # Постепенно укорачиваем, пока не влезет (эмодзи = 2 units)
    while s and _utf16_len(s) > limit:
        s = s[:-1]
    return s


def rub_to_kopecks(amount_rub: float) -> int:
    """Рубли → копейки (минимальные единицы RUB), с защитой от float-шума."""
    return int(round(float(amount_rub) * 100))


def _headers() -> dict:
    from config import TRIBUTE_API_KEY
    if not TRIBUTE_API_KEY:
        raise TributeError("TRIBUTE_API_KEY не задан (в .env)")
    return {"Api-Key": TRIBUTE_API_KEY, "Content-Type": "application/json"}


# Подсказки к типовым ошибкам API — чтобы в логе/уведомлении админу
# сразу было видно, ЧТО исправить, а не голый JSON от Tribute.
# (404 «shop not found» задокументирован: у ключа нет активного магазина.
# shopId опционален — без него берётся первый магазин аккаунта, поэтому
# причина всегда одна: магазин не создан/неактивен.)
_ERROR_HINTS = (
    ("shop not found",
     "на аккаунте Tribute нет АКТИВНОГО магазина: создай его в "
     "веб-приложении @tribute (раздел «Магазин») и перезапусти бота. "
     "TRIBUTE_SHOP_ID задавать не нужно — он только для нескольких магазинов"),
)


def hint_for_status(text: str) -> str:
    """Подсказка к ошибке API по её тексту (пусто — типовой ошибки нет)."""
    low = (text or "").lower()
    for marker, hint in _ERROR_HINTS:
        if marker in low:
            return f" → {hint}"
    return ""


def _build_body(
    amount_rub: float,
    title: str,
    description: str,
    customer_id: str | None = None,
    email: str | None = None,
    comment: str | None = None,
) -> dict:
    """Тело POST /shop/orders (currency и amount обязательны)."""
    from config import TRIBUTE_SHOP_ID

    kop = rub_to_kopecks(amount_rub)
    if kop < MIN_AMOUNT_KOP:
        raise TributeError(f"Сумма {amount_rub:.0f} ₽ ниже минимума Tribute (100 ₽)")
    if kop > MAX_AMOUNT_KOP:
        raise TributeError(f"Сумма {amount_rub:.0f} ₽ выше максимума Tribute (300 000 ₽)")

    body: dict[str, Any] = {
        "currency": "rub",
        "amount": kop,
        "title": truncate_utf16(title.strip(), _TITLE_MAX_UTF16),
        "description": truncate_utf16(description.strip(), _DESC_MAX_UTF16),
        "period": "onetime",
    }
    if TRIBUTE_SHOP_ID:
        body["shopId"] = TRIBUTE_SHOP_ID
    if customer_id:
        body["customerId"] = str(customer_id)[:256]
    if email:
        body["email"] = email.strip()
    if comment:
        body["comment"] = truncate_utf16(comment.strip(), _DESC_MAX_UTF16)
    return body


async def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    """Выполнить запрос к Tribute API с ретраями на сетевые сбои."""
    url = f"{API_BASE}{path}"
    timeout = aiohttp.ClientTimeout(
        connect=_CONNECT_TIMEOUT, total=_TOTAL_TIMEOUT
    )
    last_exc: Exception | None = None

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method, url, headers=_headers(), json=json_body
                ) as resp:
                    text = await resp.text()
                    if resp.status in (200, 201):
                        return await resp.json(content_type=None)
                    # 4xx — наша ошибка, ретраить бессмысленно
                    if resp.status < 500:
                        raise TributeError(
                            f"Tribute API {resp.status}: {text[:300]}"
                            f"{hint_for_status(text)}"
                        )
                    # 5xx — возможно, временный сбой: ретрай
                    last_exc = TributeError(
                        f"Tribute API {resp.status}: {text[:300]}"
                        f"{hint_for_status(text)}"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))

    raise TributeError(f"Tribute API недоступен после 3 попыток: {last_exc}")


async def create_order(
    amount_rub: float,
    title: str,
    description: str,
    customer_id: str | None = None,
    email: str | None = None,
    comment: str | None = None,
) -> dict:
    """Создать заказ в магазине Tribute.

    Возвращает данные заказа: uuid, webappPaymentUrl (Mini App —
    основной вариант для кнопки), paymentUrl (браузер), status.

    Период всегда onetime (разовая оплата подписки).
    """
    body = _build_body(amount_rub, title, description, customer_id, email, comment)
    data = await _request("POST", "/shop/orders", body)

    if not data.get("uuid"):
        raise TributeError(f"Tribute: в ответе нет uuid заказа: {str(data)[:300]}")
    if not data.get("webappPaymentUrl") and not data.get("paymentUrl"):
        raise TributeError("Tribute: в ответе нет ссылки на оплату")

    logger.info(
        f"Tribute: order created uuid={data['uuid']} amount={amount_rub:.0f}₽"
    )
    return data


async def get_order(order_uuid: str) -> dict:
    """Получить заказ по UUID (поля status/amount/currency/...)."""
    if not order_uuid:
        raise TributeError("get_order: пустой uuid")
    return await _request("GET", f"/shop/orders/{order_uuid}")


async def is_order_paid(order_uuid: str) -> bool:
    """Оплачен ли заказ (status == 'paid').

    Возможные статусы: pending → prepaid (только card-to-Stars) →
    paid / failed. Для карточных разовых заказов ждём 'paid'.
    """
    data = await get_order(order_uuid)
    return str(data.get("status", "")).lower() == "paid"


def payment_url_of(order_data: dict) -> str:
    """Ссылка для кнопки оплаты: Mini App, фолбэк — веб-страница."""
    return (
        order_data.get("webappPaymentUrl")
        or order_data.get("paymentUrl")
        or ""
    )


async def shops() -> list[dict]:
    """Список магазинов аккаунта (для проверки ключа при запуске)."""
    data = await _request("GET", "/shops")
    return data if isinstance(data, list) else []
