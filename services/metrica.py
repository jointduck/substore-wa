"""Выгрузка офлайн-конверсий в Яндекс Метрику (v19) — канал до Директа.

ЗАЧЕМ: автостратегии Директа («Оплата за конверсии») optimizируют по
целям, которые Директ берёт ИЗ МЕТРИКИ. Покупки происходят в боте —
Метрика про них не знает. Решение — официальный механизм «офлайн-
конверсии»: бот выгружает покупки CSV-файлом по yclid (клик ID из
рекламной ссылки), Метрика клэимит их к визитам из рекламы, Директ
видит цель (например "purchase") с доходом и optimizирует.

ПОТОК ДАННЫХ:
  Реклама Директа → клик (yclid) → редирект /r/ этого же сервера →
  короткий код c_<id> → t.me/<bot>?start=c_<id> → бот сохраняет yclid в
  utm_tracking → оплата → заказ active → фоновой цикл каждые
  METRICA_UPLOAD_INTERVAL сек выгружает CSV в Метрику →
  mark_metrica_uploaded(1).

API: POST /management/v1/counter/{id}/offline_conversions/upload
     (Authorization: OAuth <token>, body — CSV).
CSV-колонки: yclid,target,timestamp,price,currency
  - timestamp — unix-время конверсии (активация заказа, иначе paid_at);
  - price/currency — необязательны, но дают «конверсии с доходом»;
  - заказы БЕЗ yclid (органика) помечаются флагом 2 и не выгружаются —
    клэймить их не к чему.

Настройка владельцем — см. config.py (METRICA_*) и .env.example.
"""
import asyncio
import calendar
import logging
from datetime import datetime

import aiohttp

from config import (
    METRICA_COUNTER_ID, METRICA_OAUTH_TOKEN, METRICA_TARGET_NAME,
    METRICA_CURRENCY, METRICA_UPLOAD_INTERVAL, METRICA_ENABLED,
)
from models.database import db
from utils.order_status import parse_db_dt

logger = logging.getLogger(__name__)

_METRICA_API = "https://api-metrica.yandex.net/management/v1"

CSV_HEADER = "yclid,target,timestamp,price,currency"


def _to_unix(dt_str) -> int:
    """DB-дата (ISO/CURRENT_TIMESTAMP) → unix-секунды UTC. Мусор → now.

    parse_db_dt возвращает наивную дату в UTC — .timestamp() брал бы
    локальную зону сервера, поэтому timegm (строго UTC, детерминизм).
    """
    dt = parse_db_dt(dt_str) if dt_str else None
    if dt is None:
        dt = datetime.utcnow()
    return calendar.timegm(dt.timetuple())


def build_conversions_csv(rows: list[dict], target: str = METRICA_TARGET_NAME,
                          currency: str = METRICA_CURRENCY) -> tuple[str, list[int], list[int]]:
    """Собрать CSV офлайн-конверсий из строк get_pending_metrica_conversions().

    Returns: (csv_text, upload_ids, skip_ids) — заказы без yclid уходят в
    skip_ids (помечаются флагом 2, чтобы не пересканировать каждую итерацию).
    """
    assert "," not in target and '"' not in target, (
        f"METRICA_TARGET_NAME не должен содержать запятых/кавычек: {target!r}")
    lines = [CSV_HEADER]
    upload_ids: list[int] = []
    skip_ids: list[int] = []
    for r in rows:
        yclid = str(r.get("yclid") or "").strip()
        if not yclid:
            skip_ids.append(r["order_id"])
            continue
        # Момент конверсии: активация → оплата → создание заказа.
        ts = _to_unix(r.get("activated_at") or r.get("paid_at") or r.get("created_at"))
        try:
            price = float(r.get("price_usdt") or 0)
        except (TypeError, ValueError):
            price = 0.0
        lines.append(f"{yclid},{target},{ts},{price:.2f},{currency}")
        upload_ids.append(r["order_id"])
    return "\n".join(lines), upload_ids, skip_ids


async def upload_offline_conversions(counter_id: int, token: str, csv_text: str,
                                     timeout_s: float = 30.0) -> dict:
    """POST CSV в Метрику. Возвращает {upload_id} либо кидает RuntimeError
    с понятным описанием (для лога владельца)."""
    url = f"{_METRICA_API}/counter/{counter_id}/offline_conversions/upload"
    headers = {"Authorization": f"OAuth {token}", "Content-Type": "text/csv"}
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, data=csv_text.encode("utf-8")) as resp:
            body = await resp.text()
            if resp.status == 200:
                import json
                try:
                    data = json.loads(body)
                    return {"upload_id": data.get("upload_id")}
                except Exception:
                    return {"upload_id": None}
            if resp.status == 403:
                raise RuntimeError(
                    "Метрика 403: проверьте METRICA_OAUTH_TOKEN (нужен доступ "
                    "metrica:write) и что счётчик ваш.")
            if resp.status == 400:
                raise RuntimeError(
                    f"Метрика 400: скорее всего не включена «Загрузка данных → "
                    f"Офлайн-конверсии» в настройках счётчика, либо CSV "
                    f"невалиден. Ответ: {body[:300]}")
            raise RuntimeError(f"Метрика HTTP {resp.status}: {body[:300]}")


async def upload_pending_conversions() -> dict:
    """Один цикл выгрузки: активные заказы без флага → CSV → Метрика.

    Idempotent: выгруженные получают metrica_uploaded=1, заказы без
    yclid — флаг 2 (органика, неклеймить). Возвращает сводку для лога.
    """
    if not METRICA_ENABLED:
        return {"enabled": False, "uploaded": 0, "skipped": 0}

    rows = await db.get_pending_metrica_conversions()
    if not rows:
        return {"enabled": True, "uploaded": 0, "skipped": 0}

    csv_text, upload_ids, skip_ids = build_conversions_csv(rows)
    if skip_ids:
        await db.mark_metrica_uploaded(skip_ids, flag=2)
    if not upload_ids:
        return {"enabled": True, "uploaded": 0, "skipped": len(skip_ids)}

    try:
        result = await upload_offline_conversions(METRICA_COUNTER_ID, METRICA_OAUTH_TOKEN, csv_text)
    except RuntimeError as e:
        # НЕ помечаем выгруженными — следующий цикл повторит попытку.
        logger.error(f"Metrica upload failed ({len(upload_ids)} orders): {e}")
        return {"enabled": True, "uploaded": 0, "skipped": len(skip_ids), "error": str(e)}

    await db.mark_metrica_uploaded(upload_ids, flag=1)
    logger.info(
        f"Metrica: {len(upload_ids)} конверсий выгружено "
        f"(upload_id={result.get('upload_id')}), органики пропущено: {len(skip_ids)}")
    return {"enabled": True, "uploaded": len(upload_ids), "skipped": len(skip_ids),
            "upload_id": result.get("upload_id")}


async def metrica_upload_loop():
    """Фоновый цикл выгрузки офлайн-конверсий (запуск из main._start_task)."""
    if not METRICA_ENABLED:
        logger.info(
            "METRICA_COUNTER_ID/METRICA_OAUTH_TOKEN не заданы — выгрузка "
            "офлайн-конверсий в Метрику выключена (Директ не получит цели).")
        return
    logger.info(
        f"Metrica upload loop: счётчик {METRICA_COUNTER_ID}, цель "
        f"«{METRICA_TARGET_NAME}», интервал {METRICA_UPLOAD_INTERVAL}с")
    # Небольшая задержка на старте: даем боту подняться и БД прогреться.
    await asyncio.sleep(20)
    while True:
        try:
            await upload_pending_conversions()
        except Exception as e:
            logger.error(f"Metrica upload loop error: {e}")
        await asyncio.sleep(max(60, METRICA_UPLOAD_INTERVAL))
