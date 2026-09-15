from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CopyTextButton,
    DisabledButton,
)

from models.database import get_active_services, get_service_by_id
from config import CURRENCY_SYMBOL, ENABLED_PAYMENT_METHODS
from emojis import ce, ce_id
from utils.order_status import effective_status


# ─── Helpers ──────────────────────────────────────────────────────

def pbtn(text: str, callback_data: str, emoji_name: str = "", style: str | None = None) -> InlineKeyboardButton:
    kwargs = {
        "text": text,
        "callback_data": callback_data,
    }
    eid = ce_id(emoji_name) if emoji_name else ""
    if eid:
        kwargs["icon_custom_emoji_id"] = eid
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def rbtn(text: str, emoji_name: str = "", style: str | None = None) -> KeyboardButton:
    kwargs = {"text": text}
    eid = ce_id(emoji_name) if emoji_name else ""
    if eid:
        kwargs["icon_custom_emoji_id"] = eid
    if style:
        kwargs["style"] = style
    return KeyboardButton(**kwargs)


def pagination_row(prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    """Строка пагинации для инлайн-клавиатуры (UX №6 + Bot API 10.3).

    Возвращает ряд [«◀», «стр./всего», «▶»]; при единственной странице —
    пустой список (ряд не добавляется). prefix — callback-префикс, к которому
    приклеивается НОМЕР СТРАНИЦЫ (например "myord_" → callback "myord_2").

    Bot API 10.3 (Task 35): кнопки на границах и индикатор страницы
    помечены disabled=DisabledButton() — Telegram сам рисует их неактивными
    и не доставляет нажатия. callback_data «noop» сохранён для совместимости:
    кэшированные клавиатуры старых сообщений по-прежнему попадают в
    молчаливый fallback-хендлер.
    """
    if total_pages <= 1:
        return []
    at_start = page <= 0
    at_end = page >= total_pages - 1
    return [
        InlineKeyboardButton(
            text="◀",
            callback_data=f"{prefix}{page - 1}" if not at_start else "noop",
            **({"disabled": DisabledButton()} if at_start else {}),
        ),
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="noop",
            disabled=DisabledButton(),
        ),
        InlineKeyboardButton(
            text="▶",
            callback_data=f"{prefix}{page + 1}" if not at_end else "noop",
            **({"disabled": DisabledButton()} if at_end else {}),
        ),
    ]


# ─── Reply Keyboards (bottom menu) ─────────────────────────────────

def main_menu_kb() -> ReplyKeyboardMarkup:
    # v23: если задан WEBAPP_URL, первой строкой добавляем кнопку
    # «Магазин» — открывает Mini App не выходя из чата.
    webapp_row = []
    try:
        from aiogram.types import KeyboardButtonWebApp, WebAppInfo
        from config import WEBAPP_URL
        if WEBAPP_URL:
            webapp_row = [KeyboardButtonWebApp(
                text="🛍 Магазин",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/app/"),
            )]
    except Exception:
        webapp_row = []
    kb = []
    if webapp_row:
        kb.append(webapp_row)
    kb += [
        [rbtn("Каталог", "shopping", "primary"), rbtn("Заказы", "package", "primary")],
        [rbtn("Поддержка", "speech", "primary"), rbtn("Помощь", "question", "primary")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, persistent=True)


def admin_menu_kb() -> ReplyKeyboardMarkup:
    kb = [
        [rbtn("Каталог", "shopping", "primary"), rbtn("Заказы", "package", "primary")],
        [rbtn("Новые заказы", "bell", "success"), rbtn("Обращения", "ticket", "primary")],
        [rbtn("Статистика", "chart", "primary"), rbtn("Ред. каталог", "pencil", "primary")],
        [rbtn("Промокоды", "discount", "primary"), rbtn("Рефералы", "link", "primary")],
        [rbtn("Рассылка", "megaphone", "danger")],
        [rbtn("Помощь", "question", "primary")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, persistent=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    kb = [[rbtn("Отмена", "cross", "danger")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def cancel_inline_kb(callback_data: str = "cancel_order") -> InlineKeyboardMarkup:
    """Inline cancel button — for edit_text() and send_message from webhooks
    where ReplyKeyboardMarkup is not accepted or not appropriate."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Отмена", callback_data, "cross", "danger")]
    ])


def cancel_confirm_kb(order_id: int = 0) -> InlineKeyboardMarkup:
    """Ступень подтверждения отмены заказа ( UX №3):
    «Да, отменить» реально отменяет, «Нет, вернуться» перерисовывает
    выбор способа оплаты — случайный тап больше не уничтожает заказ."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Да, отменить", f"cancelyes_{order_id}", "cross", "danger"),
         pbtn("Нет, вернуться", f"cancelno_{order_id}", "refresh", "primary")],
    ])


def back_catalog_kb() -> InlineKeyboardMarkup:
    """Кнопка «В каталог» — гашеные экраны оплаты (UX №2)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("В каталог", "back_catalog", "shopping", "primary")],
    ])


def empty_catalog_kb() -> InlineKeyboardMarkup:
    """Каталог пуст (сервисы скрыты/каталог обновляется) — вместо пустого
    экрана без кнопок юзеру сразу доступна поддержка (UX №4)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Написать в поддержку", "sup:menu", "speech", "primary")],
    ])


# ─── Inline Keyboards — premium emoji icons + colored styles ──────

def catalog_kb() -> InlineKeyboardMarkup:
    """Main catalog — services with premium emoji icons + primary buttons."""
    services = get_active_services()
    buttons = []
    for s in services:
        custom_eid = s.get("custom_emoji_id", "")
        kwargs = {
            "text": s['name'],
            "callback_data": f"svc_{s['id']}",
            "style": "primary",
        }
        if custom_eid:
            kwargs["icon_custom_emoji_id"] = custom_eid
        buttons.append([InlineKeyboardButton(**kwargs)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def service_plans_kb(service_id: str) -> InlineKeyboardMarkup:
    """Plans — green buttons. Prices shown in RUB (converted from USDT at live rate)."""
    from services.ton_payments import usdt_to_rub

    service = get_service_by_id(service_id)
    if not service:
        return InlineKeyboardMarkup(inline_keyboard=[])

    rub_rate = 0
    try:
        rub_rate = await usdt_to_rub(1)
    except Exception:
        pass

    buttons = []
    for plan in service["plans"]:
        price_usdt = plan.get("price_usdt", 0)
        if rub_rate > 0:
            price_rub = price_usdt * rub_rate
            price_str = f"{price_rub:.0f} ₽"
        else:
            price_str = f"{price_usdt:.2f} USDT"
        buttons.append([pbtn(
            text=f"{plan['name']} — {price_str}",
            callback_data=f"plan_{service_id}:{plan['id']}",
            emoji_name="wallet",
            style="success",
        )])
    buttons.append([pbtn(
        text="Назад",
        callback_data="back_catalog",
        emoji_name="refresh",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payment_method_kb(order_id: int) -> InlineKeyboardMarkup:
    """Select payment method — Gram, USDT, or Card."""
    buttons = []

    if "ton" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="Gram",
            callback_data=f"payton_{order_id}",
            emoji_name="diamond",
            style="primary",
        )])

    if "usdt" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="USDT (Tether)",
            callback_data=f"payusdt_{order_id}",
            emoji_name="dollar",
            style="success",
        )])

    if "card" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="Банковская карта",
            callback_data=f"paycard_{order_id}",
            emoji_name="credit_card",
            style="primary",
        )])

    if "stars" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="Telegram Stars",
            callback_data=f"paystars_{order_id}",
            emoji_name="star",
            style="primary",
        )])

    buttons.append([pbtn(
        text="Отмена",
        callback_data="cancel_order",
        emoji_name="cross",
        style="danger",
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def promo_code_kb(order_id: int) -> InlineKeyboardMarkup:
    """Payment method selection WITH promo code option."""
    buttons = []

    # Promo code button at top
    buttons.append([pbtn(
        text="Ввести промокод",
        callback_data=f"promo_{order_id}",
        emoji_name="discount",
        style="primary",
    )])

    if "ton" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="Gram",
            callback_data=f"payton_{order_id}",
            emoji_name="diamond",
            style="primary",
        )])

    if "usdt" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="USDT (Tether)",
            callback_data=f"payusdt_{order_id}",
            emoji_name="dollar",
            style="success",
        )])

    if "card" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="Банковская карта",
            callback_data=f"paycard_{order_id}",
            emoji_name="credit_card",
            style="primary",
        )])

    if "stars" in ENABLED_PAYMENT_METHODS:
        buttons.append([pbtn(
            text="Telegram Stars",
            callback_data=f"paystars_{order_id}",
            emoji_name="star",
            style="primary",
        )])

    buttons.append([pbtn(
        text="Отмена",
        callback_data="cancel_order",
        emoji_name="cross",
        style="danger",
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def ton_payment_kb(order_id: int, ton_amount: float, wallet_address: str, memo: str = "") -> InlineKeyboardMarkup:
    """Gram payment — links to open wallet app + QR code."""
    buttons = []

    from services.ton_payments import generate_tonkeeper_link, generate_ton_payment_link
    tonkeeper_url = generate_tonkeeper_link(ton_amount, order_id, wallet_address)
    if tonkeeper_url:
        buttons.append([InlineKeyboardButton(
            text="Открыть Tonkeeper",
            url=tonkeeper_url,
            icon_custom_emoji_id=ce_id("wallet") or None,
            style="primary",
        )])

    ton_url = generate_ton_payment_link(ton_amount, order_id, wallet_address)
    if ton_url:
        buttons.append([InlineKeyboardButton(
            text="Открыть Gram Wallet",
            url=ton_url,
            icon_custom_emoji_id=ce_id("link") or None,
        )])

    # Check payment button
    buttons.append([pbtn(
        text="Проверить оплату",
        callback_data=f"checkton_{order_id}",
        emoji_name="refresh",
        style="success",
    )])

    buttons.append([pbtn(
        text="Отмена",
        callback_data="cancel_order",
        emoji_name="cross",
        style="danger",
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def usdt_payment_kb(order_id: int, usdt_amount: float, wallet_address: str, memo: str = "") -> InlineKeyboardMarkup:
    """USDT payment — link to open wallet app for Jetton transfer."""
    buttons = []

    from services.ton_payments import generate_usdt_payment_link
    usdt_url = generate_usdt_payment_link(usdt_amount, order_id, wallet_address)
    if usdt_url:
        buttons.append([InlineKeyboardButton(
            text="Отправить USDT",
            url=usdt_url,
            icon_custom_emoji_id=ce_id("dollar") or None,
            style="primary",
        )])

    # Check payment button
    buttons.append([pbtn(
        text="Проверить оплату",
        callback_data=f"checkusdt_{order_id}",
        emoji_name="refresh",
        style="success",
    )])

    buttons.append([pbtn(
        text="Отмена",
        callback_data="cancel_order",
        emoji_name="cross",
        style="danger",
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def tribute_payment_kb(order_id: int, payment_url: str) -> InlineKeyboardMarkup:
    """Tribute card payment — Mini App оплата + проверка + отмена."""
    buttons = []

    # Pay button — открывает Mini App Tribute прямо в Telegram
    buttons.append([InlineKeyboardButton(
        text="Оплатить картой",
        url=payment_url,
        icon_custom_emoji_id=ce_id("credit_card") or None,
        style="primary",
    )])

    # Check payment button
    buttons.append([pbtn(
        text="Проверить оплату",
        callback_data=f"checktrib_{order_id}",
        emoji_name="refresh",
        style="success",
    )])

    # Cancel
    buttons.append([pbtn(
        text="Отмена",
        callback_data="cancel_order",
        emoji_name="cross",
        style="danger",
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def digiseller_payment_kb(order_id: int, payment_url: str) -> InlineKeyboardMarkup:
    """Digiseller card payment — link to payment page + check + cancel."""
    buttons = []

    # Pay button — opens Digiseller payment page (oplata.info)
    buttons.append([InlineKeyboardButton(
        text="Оплатить картой",
        url=payment_url,
        icon_custom_emoji_id=ce_id("credit_card") or None,
        style="primary",
    )])

    # Check payment button
    buttons.append([pbtn(
        text="Проверить оплату",
        callback_data=f"checkdigi_{order_id}",
        emoji_name="refresh",
        style="success",
    )])

    # Cancel
    buttons.append([pbtn(
        text="Отмена",
        callback_data="cancel_order",
        emoji_name="cross",
        style="danger",
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)



# ─── Admin keyboards ──────────────────────────────────────────────

def admin_order_actions_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            pbtn("Активировано", f"a_done_{order_id}", "check", "success"),
            pbtn("Ошибка", f"a_fail_{order_id}", "cross", "danger"),
        ],
        [pbtn("Написать клиенту", f"a_msg_{order_id}", "speech", "primary")],
    ])


def admin_pending_orders_kb(orders: list[dict], rub_rate: float = 0, page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    """Admin: pending orders list (срез страницы) + пагинация (UX №6).
    Shows RUB prices when rate available."""
    buttons = []
    for order in orders[:10]:
        method_icon_name = {"ton": "diamond", "usdt": "dollar", "digiseller": "credit_card", "tribute": "credit_card", "stars": "star"}.get(order.get("payment_method", ""), "package")
        # Статус — словами, а не эмодзи-точками: Telegram не отображает
        # кастомные эмодзи в ТЕКСТЕ кнопки (единственный слот —
        # icon_custom_emoji_id, и он занят иконкой способа оплаты).
        status_word = {
            "pending_payment": "ждёт оплату",
            "pending_account": "ждёт данные",
            "pending_activation": "ждёт активации",
        }.get(order["status"], "")
        status_str = f" · {status_word}" if status_word else ""
        price_usdt = order.get("price_usdt", 0)
        if rub_rate > 0:
            price_rub = price_usdt * rub_rate
            price_str = f"{price_rub:.0f}₽"
        else:
            price_str = f"{price_usdt:.2f} USDT"
        buttons.append([pbtn(
            text=f"#{order['order_id']} — {order['service_name']} {order['plan_name']} {price_str}{status_str}",
            callback_data=f"aorder_{order['order_id']}",
            emoji_name=method_icon_name,
            style="primary",
        )])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="Нет заказов", callback_data="noop")])
    else:
        row = pagination_row("apend_", page, total_pages)
        if row:
            buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_orders_kb(orders: list[dict], page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    """Кнопки заказов юзера (срез текущей страницы) + строка пагинации (UX №6)."""
    status_style = {
        "pending_payment": "primary", "pending_account": "primary",
        "pending_activation": "primary", "active": "success",
        "cancelled": "danger", "failed": "danger",
        "expired": "primary", "payment_over": "danger",
    }
    status_emoji_name = {
        "pending_payment": "credit_card", "pending_account": "clock",
        "pending_activation": "clock", "active": "check",
        "cancelled": "cross", "failed": "warning",
        "expired": "clock", "payment_over": "clock",
    }
    buttons = []
    for order in orders[:10]:
        eff = effective_status(order)
        style = status_style.get(eff, "primary")
        emoji_name = status_emoji_name.get(eff, "")
        buttons.append([pbtn(
            text=f"#{order['order_id']} {order['service_name']} — {order['plan_name']}",
            callback_data=f"uorder_{order['order_id']}",
            emoji_name=emoji_name,
            style=style,
        )])
    row = pagination_row("myord_", page, total_pages)
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def order_detail_kb(order: dict) -> InlineKeyboardMarkup:
    buttons = []
    buttons.append([
        InlineKeyboardButton(
            text="Скопировать № заказа",
            copy_text=CopyTextButton(text=str(order["order_id"])),
            icon_custom_emoji_id=ce_id("ticket") or None,
        )
    ])
    if order.get("account_data"):
        buttons.append([
            InlineKeyboardButton(
                text="Скопировать данные аккаунта",
                copy_text=CopyTextButton(text=order["account_data"]),
                icon_custom_emoji_id=ce_id("key") or None,
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_catalog_kb() -> InlineKeyboardMarkup:
    from models.database import load_catalog
    catalog = load_catalog()
    buttons = []
    for s in catalog["services"]:
        is_active = s.get("active", True)
        style = "success" if is_active else "danger"
        emoji_name = "check" if is_active else "cross"
        buttons.append([pbtn(
            text=f"{s.get('emoji', '')} {s['name']}",
            callback_data=f"edit_{s['id']}",
            emoji_name=emoji_name,
            style=style,
        )])
    buttons.append([pbtn("Добавить сервис", "add_service", "plus", "primary")])
    buttons.append([pbtn("Назад", "back_menu", "refresh")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_service_kb(service_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Название", f"edname_{service_id}", "pencil", "primary")],
        [pbtn("Описание", f"eddesc_{service_id}", "gear", "primary")],
        [pbtn("Цены/планы", f"edprice_{service_id}", "wallet", "success")],
        [pbtn("Вкл/Выкл", f"edtoggle_{service_id}", "refresh", "danger")],
        [pbtn("Удалить сервис", f"delsvc_{service_id}", "cross", "danger")],
        [pbtn("Назад", "back_edit_catalog", "refresh")],
    ])


def edit_plans_kb(service_id: str) -> InlineKeyboardMarkup:
    """Admin: edit plans — shows USDT price (base)."""
    service = get_service_by_id(service_id)
    if not service:
        return InlineKeyboardMarkup(inline_keyboard=[])
    buttons = []
    for plan in service["plans"]:
        price_str = f"{plan.get('price_usdt', 0):.2f} USDT"
        buttons.append([pbtn(
            text=f"{plan['name']} — {price_str} ({plan['duration_days']}дн.)",
            callback_data=f"edplan_{service_id}:{plan['id']}",
            emoji_name="wallet",
            style="success",
        )])
    buttons.append([pbtn("Добавить план", f"addplan_{service_id}", "plus", "primary")])
    # Delete plan option (only if plans exist)
    if service["plans"]:
        buttons.append([pbtn("Удалить план", f"delplan_{service_id}", "cross", "danger")])
    buttons.append([pbtn("Назад", f"edit_{service_id}", "refresh")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_plan_detail_kb(service_id: str, plan_id: str) -> InlineKeyboardMarkup:
    """Admin: edit a specific plan — change name, price, duration, or delete."""
    key = f"{service_id}:{plan_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Название", f"epname_{key}", "pencil", "primary")],
        [pbtn("Цена (USDT)", f"epprice_{key}", "wallet", "success")],
        [pbtn("Длительность (дн.)", f"epdur_{key}", "calendar", "primary")],
        [pbtn("Удалить план", f"delplanask_{key}", "cross", "danger")],
        [pbtn("Назад", f"edprice_{service_id}", "refresh")],
    ])


def back_kb(callback_data: str = "back_menu", text: str = "Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=callback_data, icon_custom_emoji_id=ce_id("refresh") or None)]
    ])


def admin_promo_list_kb(promos: list, page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    """Admin: список промокодов (срез страницы) — каждый код открывает ЭКРАН
    РЕДАКТИРОВАНИЯ (UX №5) + строка пагинации (UX №6)."""
    buttons = []
    for p in promos:
        p_dict = dict(p) if not isinstance(p, dict) else p
        code = p_dict["code"]
        active = p_dict.get("active", 1)
        emoji_name = "check" if active else "cross"
        pct = p_dict.get("discount_pct") or 0
        fixed = p_dict.get("discount_fixed_usdt") or 0
        discount = f"{pct:.0f}%" if pct > 0 else f"{fixed:.2f}$"
        style = "success" if active else "danger"
        buttons.append([pbtn(
            f"{code} — {discount}",
            f"promodet_{code}",
            emoji_name,
            style,
        )])
    row = pagination_row("promolist_", page, total_pages)
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_promo_detail_kb(code: str, active: int | bool = 1) -> InlineKeyboardMarkup:
    """Admin: экран одного промокода — редактирование параметров (UX №5)."""
    toggle_text = "Выключить" if active else "Включить"
    toggle_emoji = "cross" if active else "check"
    toggle_style = "danger" if active else "success"
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Скидка", f"promodisc_{code}", "discount", "primary"),
         pbtn("Лимит", f"promolim_{code}", "chart", "primary")],
        [pbtn("Срок действия", f"promoexp_{code}", "calendar", "primary")],
        [pbtn(toggle_text, f"promo_toggle_{code}", toggle_emoji, toggle_style)],
        [pbtn("Удалить промокод", f"promodask_{code}", "trash", "danger")],
        [pbtn("Назад к списку", "promolist", "refresh")],
    ])


def admin_promo_delask_kb(code: str) -> InlineKeyboardMarkup:
    """Admin: ступень подтверждения удаления промокода — одно нажатие
    больше не стирает код безвозвратно (та же схема, что у планов)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [pbtn("Да, удалить", f"promo_del_{code}", "trash", "danger"),
         pbtn("Отмена", f"promodet_{code}", "refresh", "primary")],
    ])


def delete_plans_kb(service_id: str) -> InlineKeyboardMarkup:
    """Admin: select a plan to delete."""
    service = get_service_by_id(service_id)
    if not service:
        return InlineKeyboardMarkup(inline_keyboard=[])
    buttons = []
    for plan in service["plans"]:
        price_str = f"{plan.get('price_usdt', 0):.2f} USDT"
        buttons.append([pbtn(
            text=f"{plan['name']} — {price_str} ({plan['duration_days']}дн.)",
            callback_data=f"delplanask_{service_id}:{plan['id']}",
            emoji_name="cross",
            style="danger",
        )])
    buttons.append([pbtn("Назад", f"edprice_{service_id}", "refresh")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
