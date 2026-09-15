import os
import pathlib
import json
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Resolve script directory for absolute paths (prevents "plan not found" bug
# when bot is started from a different working directory)
_SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()

# .env: сначала ищем в текущей рабочей папке, затем явно в папке бота —
# так он находится даже при запуске «python /path/to/bot/main.py»
# из другого каталога. Повторный вызов безвреден: существующие
# переменные окружения не перезаписываются.
load_dotenv()
load_dotenv(_SCRIPT_DIR / ".env")

# ─── Telegram Bot ──────────────────────────────────────────────────

# Telegram Bot Token (get from @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Admin Telegram IDs (comma-separated, with error handling)
_raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = []
for _x in _raw_admin_ids.split(","):
    _x = _x.strip()
    if _x:
        try:
            ADMIN_IDS.append(int(_x))
        except ValueError:
            logger.warning(f"Invalid ADMIN_IDS entry ignored: {_x!r}")

# ─── Database ──────────────────────────────────────────────────────

DB_PATH = os.getenv("DB_PATH", str(_SCRIPT_DIR / "subscriptions.db"))

# Catalog file path (JSON with all services/prices) — ABSOLUTE path
CATALOG_PATH = os.getenv("CATALOG_PATH", str(_SCRIPT_DIR / "catalog.json"))

# ─── Currency & Pricing ────────────────────────────────────────────

# Base currency for catalog prices (USDT recommended for crypto-friendly setup)
BASE_CURRENCY = os.getenv("BASE_CURRENCY", "USDT")

# Display currency symbol for UI
CURRENCY = os.getenv("CURRENCY", "USD")
CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "$")

# Show RUB equivalent alongside crypto prices
SHOW_RUB_EQUIVALENT = os.getenv("SHOW_RUB_EQUIVALENT", "true").lower() == "true"

# ─── Gram (formerly TON) Blockchain Payments ──────────────────────

# Toncenter API key (get at https://toncenter.com/api/v2/)
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")

# Wallet address for receiving Gram/USDT payments
TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "")

# Payment confirmation check interval (seconds)
TON_CHECK_INTERVAL = int(os.getenv("TON_CHECK_INTERVAL", "30"))

# Payment expiration timeout (seconds) — after this, unpaid order is cancelled
TON_PAYMENT_TTL = int(os.getenv("TON_PAYMENT_TTL", "1800"))  # 30 min

# ─── Card Payments (Digiseller) ──────────────────────────────────────

# Digiseller seller ID (my.digiseller.com → My Account)
DIGISELLER_SELLER_ID = int(os.getenv("DIGISELLER_SELLER_ID", "0") or 0)

# Digiseller API key (my.digiseller.com → API → API keys)
# The key must have permission: [Statistics] → Sales statistics
DIGISELLER_API_KEY = os.getenv("DIGISELLER_API_KEY", "").strip()

# Digiseller product ID (id_d) — the product buyers pay for.
# Recommended: product with "произвольная цена" (free price) so users can
# pay the exact RUB amount shown by the bot.
DIGISELLER_PRODUCT_ID = int(os.getenv("DIGISELLER_PRODUCT_ID", "0") or 0)

# Payment currency for the payment page (RUB | USD | EUR)
DIGISELLER_CURRENCY = os.getenv("DIGISELLER_CURRENCY", "RUB")

# Минимальная цена одной единицы (юнита) товара на Digiseller.
# Задаётся в панели Digiseller при создании товара с «произвольной ценой»:
# my.digiseller.com → Товары → товар → «Минимальная цена».
#
# Итоговая сумма оплаты = DIGISELLER_UNIT_PRICE × unit_cnt (целое число юнитов).
#
# Рекомендация: установи минимальную цену товара = 1 ₽ (или 1 USDT),
# тогда юзер заплатит точную сумму. Если минимальная цена 10 ₽ —
# сумма округлится до кратной 10 (139.80 ₽ → 140 ₽).
DIGISELLER_UNIT_PRICE = float(os.getenv("DIGISELLER_UNIT_PRICE", "1.0"))

# ВАЛЮТА ТОВАРА на Digiseller — в какой валюте указана «Минимальная цена»
# товара в панели Digiseller. Бот должен это знать, чтобы правильно считать
# unit_cnt для желаемой суммы в RUB.
#
# Как посмотреть: my.digiseller.com → Товары → твой товар → поле «Валюта».
# Возможные значения: "RUB", "WMT", "WMZ", "USD", "EUR".
#
# Пример:
#   DIGISELLER_PRODUCT_CURRENCY=WMT, DIGISELLER_UNIT_PRICE=0.01
#   → юнит стоит 0.01 WMT (~1 ₽). Для заказа 9 ₽ бот сделает
#     unit_cnt = round(9 / 95 / 0.01) = 9 → юзер заплатит 9 × 0.01 = 0.09 WMT.
#
# Если не задано — бот предполагает DIGISELLER_CURRENCY (обычно RUB).
DIGISELLER_PRODUCT_CURRENCY = os.getenv("DIGISELLER_PRODUCT_CURRENCY", "").strip().upper() or DIGISELLER_CURRENCY

# Domain used to build per-order buyer emails for payment matching:
# email = "{order_id}@{DIGISELLER_EMAIL_DOMAIN}"
# Use your own domain so receipts/notifications make sense.
DIGISELLER_EMAIL_DOMAIN = os.getenv("DIGISELLER_EMAIL_DOMAIN", "orders.digibot.pay")

# Optional: numeric ID of a required TEXT parameter on the product
# (product editor → Параметры товара). When set, the order id is bound
# via /api/purchases/options → id_po (cleaner matching, no fake emails).
DIGISELLER_PARAM_ID = int(os.getenv("DIGISELLER_PARAM_ID", "0") or 0)

# Background payment check interval (seconds)
DIGISELLER_CHECK_INTERVAL = int(os.getenv("DIGISELLER_CHECK_INTERVAL", "60"))

# МЯГКАЯ ПРОВЕРКА СУММЫ (для товаров в RUB с зачислением в WMT).
#
# Если true — бот засчитывает платёж КАК ОПЛАЧЕННЫЙ при совпадении:
#   1) email покупки == email заказа (order.digiseller_email);
#   2) invoice_state == 3 (платёж подтверждён площадкой);
#   3) returned == 0 (не возврат).
#
# Сумма НЕ проверяется. Это нужно, потому что Digiseller конвертирует
# карточные платежи (RUB) в WMT (USD-эквивалент) через банк+PayMaster с
# плавающим курсом и комиссией, и точно сравнить "ожидаемые 8 RUB" с
# "полученными 0.04 WMT" невозможно. Совпадение email уже гарантирует,
# что платёж относится именно к этому заказу.
#
# Если false — бот проверяет сумму с допуском max(1.0, 5% суммы).
DIGISELLER_SKIP_AMOUNT_CHECK = os.getenv("DIGISELLER_SKIP_AMOUNT_CHECK", "true").lower() == "true"

# If card currency is not RUB, prices are converted at this rate
# Example: 1 USDT = 95 RUB → USDT_RUB_RATE=95
USDT_RUB_RATE = float(os.getenv("USDT_RUB_RATE", "95"))

# ─── Card Payments (Tribute) ─────────────────────────────────────────
#
# Альтернатива Digiseller для приёма карточных платежей через
# Tribute Shop API (https://wiki.tribute.tg/ru/for-shops/api-magazina).
#
# Преимущества: заказ имеет собственный UUID и подтверждается ТОЧНО
# (никаких подбо́ров платежей по email), оплата происходит внутри
# Telegram (Mini App), коммиссия 10%.
#
# Как подключить:
#   1. В веб-приложении @tribute создай МАГАЗИН (раздел «Магазин»)
#      — без активного магазина API вернёт «shop not found»
#   2. Настройки (⋯) → «Управление API-ключами» → «Сгенерировать API-ключ»
#   3. Впиши ключ в .env: TRIBUTE_API_KEY=...
#   4. TRIBUTE_SHOP_ID задавать НЕ нужно (опция для нескольких магазинов)
#
# Если TRIBUTE_API_KEY пуст — кнопка «Банковская карта» работает
# через Digiseller (как раньше). Заполнен — через Tribute.
TRIBUTE_API_KEY = os.getenv("TRIBUTE_API_KEY", "").strip()

# ID магазина — ОПЦИЯ только для владельцев НЕСКОЛЬКИХ магазинов.
# Без него заказ создаётся для первого (самого раннего) магазина аккаунта
# (так задокументировано в Tribute Shop API). Список магазинов ключа
# бот печатает при старте (preflight).
TRIBUTE_SHOP_ID = int(os.getenv("TRIBUTE_SHOP_ID", "0") or 0)

# Интервал фоновой проверки оплат Tribute (секунды).
# Бот опрашивает GET /shop/orders/{uuid}/status для каждого
# неоплаченного заказа — точный матчинг по UUID, домен не нужен.
TRIBUTE_CHECK_INTERVAL = int(os.getenv("TRIBUTE_CHECK_INTERVAL", "15"))

# ─── Telegram Stars Payments ─────────────────────────────────────

# Exchange rate: how many Telegram Stars per 1 USDT
# 100 stars = 1 USDT (default)
STARS_PER_USDT = int(os.getenv("STARS_PER_USDT", "100"))

# ─── Available Payment Methods ─────────────────────────────────────

# List of enabled payment methods: "ton", "usdt", "card", "stars"
_VALID_PAYMENT_METHODS = {"ton", "usdt", "card", "stars"}
_raw_methods = [m.strip() for m in os.getenv("ENABLED_PAYMENT_METHODS", "ton,usdt,card,stars").split(",") if m.strip()]
ENABLED_PAYMENT_METHODS = []
for _m in _raw_methods:
    if _m in _VALID_PAYMENT_METHODS:
        ENABLED_PAYMENT_METHODS.append(_m)
    else:
        logger.warning(f"Unknown payment method ignored: {_m!r}. Valid: {_VALID_PAYMENT_METHODS}")

# ─── Proxy ─────────────────────────────────────────────────────────

# Proxy for Telegram API (if blocked by ISP)
# Format: http://user:pass@host:port or socks5://user:pass@host:port
PROXY_URL = os.getenv("PROXY_URL", "")

# ─── Notifications ─────────────────────────────────────────────────

NOTIFY_CHECK_INTERVAL = int(os.getenv("NOTIFY_CHECK_INTERVAL", "30"))

# ─── Chat hygiene ──────────────────────────────────────────────────

# Дедупликация сообщений: стираются ТОЛЬКО дубли — если бот отправляет
# в чат сообщение с тем же содержимым, что и ранее (юзер снова открыл
# каталог, повторил команду, заново открыл оплату того же заказа),
# предыдущая копия удаляется. Всё остальное (карточки заказов, оплаты,
# уведомления) остаётся в чате — см. также EPHEMERAL_* ниже.
# false — полностью отключить стирание дублей.
CHAT_DEDUP_ENABLED = os.getenv("CHAT_DEDUP_ENABLED", "true").lower() == "true"

# Task 35 (решение владельца «во всех чатах»): дедуп применять и в
# админских чатах. false — прежняя политика «админский лог не трогается».
DEDUP_ADMINS = os.getenv("DEDUP_ADMINS", "true").lower() == "true"

# Ephemeral-растворение переходных сообщений (Task 35): FSM-подсказки
# и ошибки стираются после завершения ввода, команды и нажатия кнопок
# меню юзера — через ~5 с. Критические экраны (деньги, заказы, каталог,
# уведомления) не растворяются никогда — растворение только для
# переходных групп, размеченных в хендлерах через utils/ephemeral.py.
# false — полностью выключить растворение (останется только дедуп).
EPHEMERAL_ENABLED = os.getenv("EPHEMERAL_ENABLED", "true").lower() == "true"

# Нативные Ephemeral Messages (Bot API 10.3): transient-сообщения
# отправляются с ephemeral_message_parameters, жизнью управляет Telegram,
# стирание — deleteEphemeralMessage. При отказе API — тихий откат на
# классику. По умолчанию false: классика («отправили + стёрли сами»)
# даёт гарантированную доставку переходного сообщения.
EPHEMERAL_NATIVE = os.getenv("EPHEMERAL_NATIVE", "false").lower() == "true"

# ─── Referral & Marketing ─────────────────────────────────────────

# Username бота без @ — для реферальных и рекламных ссылок.
# Если пусто, бот возьмёт username автоматически через Telegram API.
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

REFERRAL_BONUS_USDT = float(os.getenv("REFERRAL_BONUS_USDT", "0.50"))
REFERRAL_REQUIRED_ORDERS = int(os.getenv("REFERRAL_REQUIRED_ORDERS", "1"))
PROMO_CODE_ENABLED = os.getenv("PROMO_CODE_ENABLED", "true").lower() == "true"
AUTO_RENEWAL_REMINDER_HOURS = int(os.getenv("AUTO_RENEWAL_REMINDER_HOURS", "72"))

# ─── Mini App (веб-магазин) ────────────────────────────────────────

# Базовый публичный URL health-сервера бота (без /app/). Пример:
#   WEBAPP_URL=https://mybot.onrender.com
# Тогда Mini App доступен на {WEBAPP_URL}/app/ и в меню чата появится
# кнопка «Магазин». Если пусто — кнопка не ставится, бот работает как
# раньше (магазин доступен только по прямой ссылке).
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")

# Название магазина — показывается в шапке Mini App и в ответе /api/session.
STORE_NAME = os.getenv("STORE_NAME", "SUBSTORE").strip() or "SUBSTORE"

# Версия релиза — отдаётся в /health и /api/catalog. Быстрая проверка,
# что на хостинге крутится именно свежий код: откройте /health в браузере.
APP_VERSION = os.getenv("APP_VERSION", "27.2").strip() or "27.2"

# Drip-напоминания (1ч/24ч/72ч для непокупавших) приходят только юзерам,
# зарегистрированным НЕ старее этого срока. Старая база «мёртвых» юзеров
# не получает залп из 3 сообщений при первом запуске бота.
# 0 — отключить возрастной фильтр (не рекомендую).
DRIP_MAX_USER_AGE_HOURS = int(os.getenv("DRIP_MAX_USER_AGE_HOURS", "96"))

# Минимальная пауза между касаниями drip (часы). Каждая стадия и так
# уходит ровно один раз (строгая лестница), а пауза не даёт «догнать»
# юзера несколькими сообщениями подряд после офлайна бота.
DRIP_MIN_GAP_HOURS = int(os.getenv("DRIP_MIN_GAP_HOURS", "24"))

# ─── Security ──────────────────────────────────────────────────────

# Fernet encryption key for account_data (logins/passwords).
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# If not set, a key will be auto-generated on first run (fine for dev, BAD for production).
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

# Auto-delete account_data N days after order activation (0 = never delete)
ACCOUNT_DATA_TTL_DAYS = int(os.getenv("ACCOUNT_DATA_TTL_DAYS", "30"))

# ─── Broadcast ─────────────────────────────────────────────────────

# Delay between broadcast messages in seconds (to avoid Telegram rate limits)
BROADCAST_DELAY = float(os.getenv("BROADCAST_DELAY", "0.05"))

# ─── TonAPI (optional, for decoded jetton transfers) ──────────────

# TonAPI.io key for reliable USDT amount verification (optional but recommended)
TONAPI_KEY = os.getenv("TONAPI_KEY", "")
