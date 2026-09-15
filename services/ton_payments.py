"""
Gram (formerly TON) blockchain payment service.

Supports:
  - Gram (native coin, formerly TON/Toncoin)
  - USDT (Jetton on Gram blockchain)

Uses Toncenter API v2 for:
  - Checking transaction status
  - Getting Gram/USDT exchange rates

Uses CoinGecko as fallback for rates.
Optionally uses TonAPI (tonapi.io) for decoded jetton transfers.

Setup:
  1. Create a wallet for the bot (e.g., Tonkeeper)
  2. Get Toncenter API key at https://toncenter.com/api/v2/
  3. Set TONCENTER_API_KEY and TON_WALLET_ADDRESS in .env
  4. (Optional) Get TonAPI key at https://tonapi.io for reliable USDT verification
"""

import aiohttp
import aiosqlite
import base64
import io
import logging
import random
import json
import re
import string
import time
import qrcode

from config import TONCENTER_API_KEY, TON_WALLET_ADDRESS, TONAPI_KEY, ADMIN_IDS

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────

TONCENTER_BASE = "https://toncenter.com/api/v2"

# v21: настоящий jetton-мастер Tether USD (USD₮) в сети Gram/TON.
# Раньше здесь лежал БИТЫЙ адрес (опечатка) — TonAPI отвечал «can't decode
# address», кошелёк юзера открывался с неверной суммой, платежи не
# находились. Проверено через TonAPI: "Tether USD", USD₮, decimals=6,
# verification=whitelist, holders 3.4M+.
USDT_JETTON_ROOT = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
# Тот же мастер в raw-форме (0:hex) — для сравнения с адресами из API.
USDT_JETTON_HEX = "0:b113a994b5024a16719f69139328eb759596c38a25f59028b146fecdc3621dfe"

# Nanotons per Gram
GRAM_DECIMALS = 1_000_000_000
USDT_DECIMALS = 1_000_000

# Keep TON_DECIMALS as alias for backward compat
TON_DECIMALS = GRAM_DECIMALS

# Payment expiration (seconds)
PAYMENT_TTL = 1800  # 30 minutes

# Transaction check limits
TX_CHECK_LIMIT = 50  # Check up to 50 transactions (was 20)
TX_PAGINATION_MAX = 3  # Max pages to paginate through

# Memo storage: memo -> {"order_id": int, "created_at": float}
_memos: dict[str, dict] = {}
_MEMO_TTL = 3600  # 1 hour — memos older than this are cleaned up


# ─── API Client ─────────────────────────────────────────────────────

async def _toncenter_get(method: str, params: dict = None) -> dict:
    """Make GET request to Toncenter API v2."""
    if not TONCENTER_API_KEY:
        logger.warning("TONCENTER_API_KEY not set, Gram payments unavailable")
        return {"ok": False, "error": "API key not configured"}

    headers = {"X-API-Key": TONCENTER_API_KEY}
    url = f"{TONCENTER_BASE}/{method}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                return data
        except Exception as e:
            logger.error(f"Toncenter API error: {e}")
            return {"ok": False, "error": str(e)}


async def _toncenter_post(method: str, body: dict = None) -> dict:
    """Make POST request to Toncenter API v2."""
    if not TONCENTER_API_KEY:
        return {"ok": False, "error": "API key not configured"}

    headers = {"X-API-Key": TONCENTER_API_KEY, "Content-Type": "application/json"}
    url = f"{TONCENTER_BASE}/{method}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                return data
        except Exception as e:
            logger.error(f"Toncenter API error: {e}")
            return {"ok": False, "error": str(e)}


async def _tonapi_get(path: str, params: dict = None) -> dict | None:
    """Make GET request to TonAPI (tonapi.io). Returns None on error.

    v21: работает и БЕЗ ключа (публичные лимиты) — ключ только повышает их.
    """
    headers = {"Accept": "application/json"}
    if TONAPI_KEY:
        headers["Authorization"] = f"Bearer {TONAPI_KEY}"
    url = f"https://tonapi.io/v2{path}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.debug(f"TonAPI returned status {resp.status}")
                    return None
        except Exception as e:
            logger.debug(f"TonAPI request failed: {e}")
            return None


async def _toncenter_v3_get(path: str, params: dict = None) -> dict | None:
    """GET к Toncenter API v3. Работает БЕЗ ключа (публичные лимиты)."""
    headers = {"Accept": "application/json"}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    url = f"{TONCENTER_V3_BASE}{path}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.debug(f"Toncenter v3 returned status {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Toncenter v3 API error: {e}")
            return None


# ─── Exchange Rates ────────────────────────────────────────────────

_rates_cache = {"gram_usd": 0, "gram_usd_updated": 0, "usd_rub": 0, "usd_rub_updated": 0}
_RATES_CACHE_TTL = 60  # 1 minute — near real-time rates

# Track whether we're using a fallback rate (for admin alerting)
_using_fallback_rate = {"gram_usd": False, "usd_rub": False}


async def get_ton_usd_rate() -> float:
    """Get current Gram/USD rate. Alias kept for backward compat."""
    return await get_gram_usd_rate()


async def get_gram_usd_rate() -> float:
    """Get current Gram/USD rate from CoinGecko or fallback."""
    now = time.time()
    if _rates_cache["gram_usd"] and now - _rates_cache["gram_usd_updated"] < _RATES_CACHE_TTL:
        return _rates_cache["gram_usd"]

    # Primary: CoinGecko
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                rate = data["the-open-network"]["usd"]
                _rates_cache["gram_usd"] = rate
                _rates_cache["gram_usd_updated"] = now
                _using_fallback_rate["gram_usd"] = False
                return rate
    except Exception as e:
        logger.warning(f"Failed to get Gram/USD rate from CoinGecko: {e}")

    # Fallback: Toncenter
    try:
        result = await _toncenter_get("getExchangeRate", {"currency": "TON"})
        if result.get("ok"):
            rate = float(result["result"])
            _rates_cache["gram_usd"] = rate
            _rates_cache["gram_usd_updated"] = now
            _using_fallback_rate["gram_usd"] = False
            return rate
    except Exception as e:
        logger.warning(f"Failed to get Gram/USD rate from Toncenter: {e}")

    # Last known or hardcoded fallback
    # v17: было _rates_cache.get("gram_usd", 3.5) — ключ ВСЕГДА существует
    # со стартовым значением 0, дефолт .get() был мёртвым кодом. При сбое
    # CoinGecko+Toncenter возвращался 0 → usdt_to_ton() = 0, а для карточных
    # сумм — «0 ₽». Теперь: последнее живое значение, иначе константа.
    fallback = _rates_cache.get("gram_usd") or 3.5
    if not _using_fallback_rate.get("gram_usd"):
        logger.warning(
            f"⚠️ USED STALE/FALLBACK Gram/USD rate: {fallback} — live rate unavailable! "
            f"Check CoinGecko and Toncenter API connectivity."
        )
        _using_fallback_rate["gram_usd"] = True
    return fallback


async def get_usd_rub_rate() -> float:
    """Get current USD/RUB rate from CoinGecko (USDT/RUB)."""
    now = time.time()
    if _rates_cache["usd_rub"] and now - _rates_cache["usd_rub_updated"] < _RATES_CACHE_TTL:
        return _rates_cache["usd_rub"]

    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                rate = data["tether"]["rub"]
                _rates_cache["usd_rub"] = rate
                _rates_cache["usd_rub_updated"] = now
                _using_fallback_rate["usd_rub"] = False
                return rate
    except Exception as e:
        logger.warning(f"Failed to get USD/RUB rate: {e}")

    # v17: аналогично gram_usd — дефолт .get() был мёртв (ключ существовал
    # со значением 0). Ноль приводил к rub_amount=0 на карточной оплате:
    # Digiseller-ссылка создавалась на МИНИМАЛЬНУЮ цену товара, а
    # SKIP_AMOUNT_CHECK=true пропускал такой платёж → заказ почти даром.
    fallback = _rates_cache.get("usd_rub") or 90.0
    if not _using_fallback_rate.get("usd_rub"):
        logger.warning(
            f"⚠️ USED STALE/FALLBACK USD/RUB rate: {fallback} — live rate unavailable! "
            f"Check CoinGecko API connectivity."
        )
        _using_fallback_rate["usd_rub"] = True
    return fallback


async def usdt_to_ton(usdt_amount: float) -> float:
    """Convert USDT amount to Gram amount."""
    gram_usd = await get_gram_usd_rate()
    if gram_usd <= 0:
        return 0
    return usdt_amount / gram_usd


# Keep alias
usdt_to_gram = usdt_to_ton


async def ton_to_usdt(ton_amount: float) -> float:
    """Convert Gram amount to USDT."""
    gram_usd = await get_gram_usd_rate()
    return ton_amount * gram_usd


async def usdt_to_rub(usdt_amount: float) -> float:
    """Convert USDT amount to RUB."""
    usd_rub = await get_usd_rub_rate()
    return usdt_amount * usd_rub


async def format_price_rub(usdt_price: float) -> str:
    """Format price: primary RUB, secondary USDT and Gram equivalents."""
    rub_amount = await usdt_to_rub(usdt_price)
    gram_amount = await usdt_to_gram(usdt_price)
    parts = [f"{rub_amount:.0f} ₽"]
    parts.append(f"{usdt_price:.2f} USDT")
    if gram_amount > 0:
        parts.append(f"{gram_amount:.2f} Gram")
    return " / ".join(parts)


async def format_price_usdt(usdt_price: float) -> str:
    """Format USDT price with RUB and Gram equivalents. RUB primary."""
    return await format_price_rub(usdt_price)


# ─── Payment Address Generation ────────────────────────────────────

def get_deposit_address() -> str:
    """Get the wallet address for receiving payments."""
    if not TON_WALLET_ADDRESS:
        return ""
    return TON_WALLET_ADDRESS


def generate_memo(order_id: int) -> str:
    """Generate a random memo for the payment transaction.
    
    Random memo is more secure — users can't guess other orders' memos.
    The mapping memo->order_id is stored in _memos dict (in-memory) with TTL.
    Memo is also saved to DB (payment_memo column) for persistence across restarts.
    """
    # Clean up expired memos first
    _cleanup_expired_memos()

    # Check if memo already exists for this order (avoid duplicates on re-check)
    for m, info in _memos.items():
        if info["order_id"] == order_id:
            return m  # Return existing memo
    
    # Generate 8-char random alphanumeric memo
    memo = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    _memos[memo] = {"order_id": order_id, "created_at": time.time()}
    return memo


def get_order_id_by_memo(memo: str) -> int | None:
    """Look up order_id by its memo."""
    info = _memos.get(memo)
    return info["order_id"] if info else None


def restore_memo_from_db(order_id: int, memo: str):
    """Restore memo from DB into in-memory dict after bot restart."""
    if memo and memo not in _memos:
        _memos[memo] = {"order_id": order_id, "created_at": time.time()}


async def restore_memos_at_startup():
    """Restore ALL pending payment memos from DB into in-memory dict.
    
    Called once at bot startup to prevent memo mismatches after restart.
    Without this, a user who generated a payment link before the restart
    would have their memo missing from _memos when they click "check payment".
    """
    from models.database import db
    try:
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT order_id, payment_memo FROM orders "
                "WHERE status = 'pending_payment' AND payment_memo IS NOT NULL AND payment_memo != ''"
            )
            rows = await cursor.fetchall()
            count = 0
            for row in rows:
                memo = row["payment_memo"]
                order_id = row["order_id"]
                if memo and memo not in _memos:
                    _memos[memo] = {"order_id": order_id, "created_at": time.time()}
                    count += 1
            if count > 0:
                logger.info(f"Restored {count} pending payment memo(s) from DB at startup")
    except Exception as e:
        logger.warning(f"Failed to restore memos at startup: {e}")


def _cleanup_expired_memos():
    """Remove memos older than _MEMO_TTL (1 hour). Called automatically by generate_memo."""
    now = time.time()
    expired = [m for m, info in _memos.items() if now - info["created_at"] > _MEMO_TTL]
    for m in expired:
        del _memos[m]
    if expired:
        logger.debug(f"Cleaned up {len(expired)} expired memos")


# ─── QR Code Generation ────────────────────────────────────────────

def generate_qr_code(payment_url: str) -> io.BytesIO | None:
    """Generate QR code image for a payment URL.
    
    Returns BytesIO buffer containing PNG image, or None on error.
    """
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(payment_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Failed to generate QR code: {e}")
        return None


# ─── Transaction Checking ──────────────────────────────────────────

async def _get_transactions_paginated(address: str, limit: int = TX_CHECK_LIMIT) -> list[dict]:
    """Get transactions with pagination to check more than the default 20.
    
    Paginates through up to TX_PAGINATION_MAX pages of transactions.
    """
    all_transactions = []
    lt = 0
    hash_val = ""

    for page in range(TX_PAGINATION_MAX):
        params = {
            "address": address,
            "limit": min(limit, 50),  # Toncenter max per request is ~50
        }
        if lt:
            params["lt"] = lt
            params["hash"] = hash_val

        result = await _toncenter_get("getTransactions", params)
        if not result.get("ok"):
            break

        transactions = result.get("result", [])
        if not transactions:
            break

        all_transactions.extend(transactions)

        # Get the last transaction's lt and hash for pagination
        last_tx = transactions[-1]
        tx_id = last_tx.get("transaction_id", {})
        lt = tx_id.get("lt", 0)
        hash_val = tx_id.get("hash", "")

        if not lt or len(transactions) < limit:
            break

    return all_transactions


async def check_ton_payment(order_id: int, expected_amount_ton: float, tolerance: float = 0.05) -> dict:
    """Check if a Gram payment for the given order has been received."""
    if not TON_WALLET_ADDRESS:
        return {"found": False, "error": "Wallet not configured"}

    # Clean up expired memos before checking
    _cleanup_expired_memos()

    transactions = await _get_transactions_paginated(TON_WALLET_ADDRESS)

    mismatch = None
    for tx in transactions:
        msg = tx.get("in_msg", {})
        message = msg.get("message", "") or ""

        # Check for exact memo match or legacy sub_ format
        # Use word-boundary matching to avoid substring collisions
        matched = False
        message_words = message.split()
        for m, info in _memos.items():
            if m in message_words:  # exact word match, not substring
                if info["order_id"] == order_id:
                    matched = True
                    break
                # Don't break — keep looking for our order's memo
        
        if not matched and f"sub_{order_id}" in message_words:
            matched = True

        if matched:
            value_nanoton = int(msg.get("value", 0))
            value_ton = value_nanoton / GRAM_DECIMALS

            if abs(value_ton - expected_amount_ton) <= expected_amount_ton * tolerance:
                return {
                    "found": True,
                    "amount": value_ton,
                    "tx_hash": tx.get("transaction_id", {}).get("hash", ""),
                    "confirmed": True,
                }

            # Memo совпал, но сумма вне допуска — недоплата/переплата.
            # НЕ возвращаем сразу: у юзера может быть несколько транзакций
            # с одним memo (сначала ошибочная, потом верная) — ищем верную.
            if mismatch is None:
                mismatch = {
                    "found": False,
                    "error": "amount_mismatch",
                    "amount": value_ton,
                    "expected": expected_amount_ton,
                    "tx_hash": tx.get("transaction_id", {}).get("hash", ""),
                }

    if mismatch:
        logger.warning(
            f"TON payment amount MISMATCH for order {order_id}: "
            f"received {mismatch['amount']:.4f} Gram, "
            f"expected {mismatch['expected']:.4f} Gram — REJECTING"
        )
        return mismatch

    return {"found": False}


# ─── USDT helpers (v21) ────────────────────────────────────────────

def _nano_to_usdt(nano) -> float | None:
    """Нано-единицы USDT (6 знаков) → float; пусто/мусор → None."""
    try:
        n = int(str(nano).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n / USDT_DECIMALS


def _to_raw_hex(addr: str) -> str:
    """EQ/UQ/0:-адрес → '0:hex' (для сравнения адресов из API).

    user-friendly base64url (48 симв.) = 36 байт:
    tag(1) + workchain(1) + hash(32) + crc(2).
    workchain 0x00 → basechain '0:', 0xFF → masterchain '-1:'.
    Не смог декодировать → возвращает нижний регистр как есть.
    """
    addr = (addr or "").strip()
    if addr.lower().startswith(("0:", "-1:")):
        return addr.lower()
    try:
        raw = base64.urlsafe_b64decode(addr + "=" * (-len(addr) % 4))
        if len(raw) == 36:
            prefix = "-1:" if raw[1] == 0xFF else "0:"
            return prefix + raw[2:34].hex()
    except Exception:
        pass
    return addr.lower()


def _memo_in_blob(memo: str, blob: str) -> bool:
    """Memo ищем как ОТДЕЛЬНОЕ слово (не подстроку) в JSON-дампе записи.

    v17: наивная подстрока опасна — «ab12cd34» матчится внутри
    «ab12cd34xyz», а legacy-мемо «sub_12» — внутри «sub_123».
    Регэксп с границами по символам [a-z0-9_] отсекает такие
    наложения, но находит memo внутри любой формы, в которой
    провайдер отдаёт комментарий (кавычки/пунктуация JSON не мешают).
    Регистр не важен: юзер может набрать мемо заглавными.
    """
    if not memo or not blob:
        return False
    pattern = r"(?<![a-z0-9_])" + re.escape(memo) + r"(?![a-z0-9_])"
    return re.search(pattern, blob, re.IGNORECASE) is not None


async def check_usdt_payment(order_id: int, expected_amount_usdt: float, tolerance: float = 0.01) -> dict:
    """Проверить поступление USDT-платежа по заказу.

    v21 — полная переделка. Прежняя цепочка не работала вообще:
      - Toncenter v2 /getJettonWalletAddress → 404 (эндпоинта нет);
      - TonAPI /blockchain/accounts/{id}/jettons/history → 404;
      - USDT_JETTON_ROOT содержал битый адрес (TonAPI: can't decode),
        из-за него кошелёк юзера открывался с неверной суммой.

    Теперь два независимых источника (оба отвечают и без API-ключа):
      1) Toncenter v3  GET /jetton/transfers?owner_address=&direction=in
         — amount в нано-единицах + decoded_forward_payload (комментарий);
      2) TonAPI v2     GET /accounts/{owner}/jettons/history
         — amount в нано-единицах + payload.

    Фильтр по каждому переводу: jetton-мастер == Tether USD
    (отсекает спам-токены), комментарий содержит memo заказа,
    сумма в допуске tolerance от ожидаемой.
    """
    if not TON_WALLET_ADDRESS:
        return {"found": False, "error": "Wallet not configured"}

    # Clean up expired memos before checking
    _cleanup_expired_memos()

    # Get the memo for this order
    order_memo = None
    for m, info in _memos.items():
        if info["order_id"] == order_id:
            order_memo = m
            break
    if not order_memo:
        order_memo = f"sub_{order_id}"  # Legacy format

    owner_hex = _to_raw_hex(TON_WALLET_ADDRESS)

    # ── Источник 1: Toncenter v3 jetton/transfers ──
    v3 = await _toncenter_v3_get("/jetton/transfers", {
        "owner_address": owner_hex,
        "direction": "in",
        "limit": 30,
    })
    for t in (v3 or {}).get("jetton_transfers", []):
        if str(t.get("jetton_master") or "").lower() != USDT_JETTON_HEX:
            continue  # чужой jetton (спам-токены, эйрдропы и т.п.)
        blob = json.dumps(t, ensure_ascii=False)
        if not _memo_in_blob(order_memo, blob):
            continue
        amount_usdt = _nano_to_usdt(t.get("amount"))
        if amount_usdt is None:
            continue
        if abs(amount_usdt - expected_amount_usdt) <= expected_amount_usdt * tolerance:
            logger.info(
                f"USDT payment VERIFIED (Toncenter v3) for order {order_id}: "
                f"{amount_usdt:.2f} USDT (expected {expected_amount_usdt:.2f})"
            )
            return {
                "found": True,
                "amount": amount_usdt,
                "tx_hash": t.get("transaction_hash", ""),
                "confirmed": True,
            }
        logger.warning(
            f"⚠️ USDT payment amount MISMATCH for order {order_id}: "
            f"received {amount_usdt:.2f} USDT, expected {expected_amount_usdt:.2f} USDT. "
            f"REJECTING — possible underpayment attack!"
        )
        return {"found": False, "error": "amount_mismatch",
                "amount": amount_usdt, "expected": expected_amount_usdt}

    # ── Источник 2: TonAPI accounts/{owner}/jettons/history ──
    data = await _tonapi_get(
        f"/accounts/{owner_hex}/jettons/history", {"limit": 30})
    for op in (data or {}).get("operations", []):
        if str((op.get("jetton") or {}).get("address") or "").lower() != USDT_JETTON_HEX:
            continue
        dest = str((op.get("destination") or {}).get("address") or "").lower()
        if dest != owner_hex:
            continue  # только входящие переводы
        blob = json.dumps(op, ensure_ascii=False)
        if not _memo_in_blob(order_memo, blob):
            continue
        amount_usdt = _nano_to_usdt(op.get("amount"))
        if amount_usdt is None:
            continue
        if abs(amount_usdt - expected_amount_usdt) <= expected_amount_usdt * tolerance:
            logger.info(
                f"USDT payment VERIFIED (TonAPI) for order {order_id}: "
                f"{amount_usdt:.2f} USDT (expected {expected_amount_usdt:.2f})"
            )
            return {
                "found": True,
                "amount": amount_usdt,
                "tx_hash": op.get("transaction_hash", ""),
                "confirmed": True,
            }
        logger.warning(
            f"⚠️ USDT amount MISMATCH (TonAPI) for order {order_id}: "
            f"received {amount_usdt:.2f}, expected {expected_amount_usdt:.2f}"
        )
        return {"found": False, "error": "amount_mismatch",
                "amount": amount_usdt, "expected": expected_amount_usdt}

    return {"found": False}


# ─── Payment Link Generation ───────────────────────────────────────

def generate_ton_payment_link(
    amount_ton: float,
    order_id: int,
    wallet_address: str = None,
) -> str:
    """Generate a ton:// deep link for Gram payment."""
    addr = wallet_address or TON_WALLET_ADDRESS
    if not addr:
        return ""

    memo = generate_memo(order_id)
    amount_nanoton = int(amount_ton * GRAM_DECIMALS)
    return f"ton://transfer/{addr}?amount={amount_nanoton}&text={memo}"


def generate_tonkeeper_link(
    amount_ton: float,
    order_id: int,
    wallet_address: str = None,
) -> str:
    """Generate a Tonkeeper redirect link for Gram payment."""
    addr = wallet_address or TON_WALLET_ADDRESS
    if not addr:
        return ""

    memo = generate_memo(order_id)
    amount_nanoton = int(amount_ton * GRAM_DECIMALS)
    return f"https://app.tonkeeper.com/transfer/{addr}?amount={amount_nanoton}&text={memo}"


def generate_usdt_payment_link(
    amount_usdt: float,
    order_id: int,
    wallet_address: str = None,
) -> str:
    """Generate a Tonkeeper link for USDT Jetton payment."""
    addr = wallet_address or TON_WALLET_ADDRESS
    if not addr:
        return ""

    memo = generate_memo(order_id)
    amount_units = int(amount_usdt * USDT_DECIMALS)
    return (
        f"https://app.tonkeeper.com/transfer/{addr}?"
        f"jetton={USDT_JETTON_ROOT}&amount={amount_units}&text={memo}"
    )


# ─── Payment Status Polling ────────────────────────────────────────

# Idempotency lock: prevents double-processing from rapid button clicks
_payment_check_locks: set[int] = set()


async def verify_payment(
    order_id: int,
    payment_method: str,  # "ton" or "usdt"
    expected_amount: float,
    db_memo: str = None,
) -> dict:
    """Verify if payment has been received for an order.
    
    Includes idempotency guard: if the same order is being checked
    simultaneously (double-click), the second check returns the cached result.
    
    db_memo: memo from DB, used to restore in-memory mapping after bot restart.
    """
    # Restore memo from DB if available and not in memory
    if db_memo:
        restore_memo_from_db(order_id, db_memo)
    
    # Idempotency: prevent concurrent checks for the same order
    if order_id in _payment_check_locks:
        return {"paid": False, "error": "check_in_progress"}
    
    _payment_check_locks.add(order_id)
    try:
        if payment_method == "ton":
            result = await check_ton_payment(order_id, expected_amount)
        elif payment_method == "usdt":
            result = await check_usdt_payment(order_id, expected_amount)
        else:
            return {"paid": False, "error": f"Unknown payment method: {payment_method}"}

        return {
            "paid": result.get("found", False),
            "amount": result.get("amount", 0),
            "expected": expected_amount,
            "tx_hash": result.get("tx_hash", ""),
            "error": result.get("error"),
        }
    finally:
        _payment_check_locks.discard(order_id)
