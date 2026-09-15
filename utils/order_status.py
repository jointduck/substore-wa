"""Актуальный жизненный цикл заказа/подписки НА МОМЕНТ ПОКАЗА.

Проблема: статус в БД не отражает реальное время. Подписка со статусом
'active' остаётся 'active' навсегда, даже когда срок (activated_at +
duration_days) уже вышел; неоплаченный заказ остаётся 'pending_payment',
пока фоновый cleanup не отменит его (а если бот был офлайн — то до
следующего запуска). Личный кабинет показывал «Подписка активна» вечно.

Решение: экраны вычисляют ЭФФЕКТИВНЫЙ статус на момент показа:
  active           → 'expired'      если activated_at + duration_days <= now
  pending_payment  → 'payment_over' если payment_expires_at + grace <= now
Остальные статусы проходят насквозь.

ВАЖНО: статус в БД намеренно НЕ переводится в 'expired':
  - drip-рассылка выбирает подписки строго по status = 'active' и сама
    вычисляет expires_at (финальное письмо «Подписка истекла»);
  - статистика/бонусы считают оплаченные заказы по своим комбинациям.
Перевод статуса сломал бы их, поэтому 'expired'/'payment_over' —
виртуальные статусы только для отображения.
"""
from datetime import datetime, timedelta

# Тот же grace-период, что у cleanup_expired_orders в main.py:
# пока окно оплаты вышло менее 10 минут назад, платёж ещё может догнать.
PAYMENT_GRACE_MINUTES = 10


def parse_db_dt(value) -> datetime | None:
    """Парсит дату из БД в UTC. Форматы: ISO (utcnow().isoformat(),
    SQLite CURRENT_TIMESTAMP 'YYYY-MM-DD HH:MM:SS'), пусто/мусор → None."""
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    s = s.split("+", 1)[0].split(".", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def subscription_ends_at(order: dict) -> datetime | None:
    """Момент окончания подписки: activated_at + duration_days. None —
    если заказ не активирован или данные некорректны."""
    activated = parse_db_dt(order.get("activated_at"))
    if activated is None:
        return None
    try:
        days = int(order.get("duration_days") or 0)
    except (TypeError, ValueError):
        return None
    return activated + timedelta(days=days)


def effective_status(order: dict, now: datetime | None = None) -> str:
    """Статус заказа с учётом текущего времени (см. докстринг модуля)."""
    st = order.get("status", "")
    now = now or datetime.utcnow()
    if st == "active":
        ends = subscription_ends_at(order)
        if ends and now >= ends:
            return "expired"
    elif st == "pending_payment":
        pexp = parse_db_dt(order.get("payment_expires_at"))
        if pexp and now >= pexp + timedelta(minutes=PAYMENT_GRACE_MINUTES):
            return "payment_over"
    return st


def fmt_date(d: datetime) -> str:
    return d.strftime("%d.%m.%Y")


def days_left(ends: datetime, now: datetime | None = None) -> int:
    """Полных суток до конца подписки (0 — осталось менее суток)."""
    now = now or datetime.utcnow()
    return int((ends - now).total_seconds() // 86400)


def subscription_note(order: dict, now: datetime | None = None) -> str:
    """Короткая строка со сроком для карточки/списка заказов:
      active  → 'до 24.09.2026 · осталось 12 дн.' (или 'менее суток')
      expired → '24.09.2026' (когда закончилась)
      прочее  → ''
    """
    eff = effective_status(order, now)
    now = now or datetime.utcnow()
    ends = subscription_ends_at(order)
    if ends is None:
        return ""
    if eff == "active":
        left = days_left(ends, now)
        tail = "осталось менее суток" if left == 0 else f"осталось {left} дн."
        return f"до {fmt_date(ends)} · {tail}"
    if eff == "expired":
        return fmt_date(ends)
    return ""
