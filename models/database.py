import aiosqlite
import asyncio
import json
import logging
from datetime import datetime, timedelta

from config import DB_PATH, CATALOG_PATH

logger = logging.getLogger(__name__)

# Lock for catalog read-modify-write operations.
# Prevents lost updates when two admins edit the catalog concurrently.
_catalog_lock = asyncio.Lock()


class _PragmaConnection:
    """Обёртка над aiosqlite.connect для использования как async CM.

    При входе выставляет per-connection PRAGMA (foreign_keys, busy_timeout)
    и возвращает реальное соединение. Совместим с любым
    `async with db._get_connection() as conn:` — наружу отдаётся тот же
    aiosqlite.Connection, что и раньше.
    """

    def __init__(self, db_path: str):
        self._cm = aiosqlite.connect(db_path, timeout=15.0)

    async def __aenter__(self):
        conn = await self._cm.__aenter__()
        try:
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=15000")
        except Exception as e:  # PRAGMA не критичны — не роняем запрос
            logger.debug(f"PRAGMA setup skipped: {e}")
        return conn

    async def __aexit__(self, exc_type, exc, tb):
        return await self._cm.__aexit__(exc_type, exc, tb)


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def _get_connection(self):
        """Get an aiosqlite connection (for use as async context manager).

        v17: каждое соединение получает свои PRAGMA (раньше
        foreign_keys=ON выставлялся ОДИН раз в init() на одном соединении,
        а каждый метод открывает новое — на практике FK были выключены).
        Дополнительно busy_timeout=15с: при параллельной записи нескольких
        корутин SQLite больше не отвалится с «database is locked» через
        дефолтные 5с.
        """
        return _PragmaConnection(self.db_path)

    async def get_pending_bonus_reserved(self, user_id: int) -> float:
        """Сумма бонуса за друзей, ЗАРЕЗЕРВИРОВАННОГО живыми заказами.

        Бонус применяется к цене при СОЗДАНИИ заказа, а списывается при
        оплате. Пока заказ не оплачен, его бонус фактически «заморожен»:
        без учёта резерва юзер мог оформить два заказа на весь баланс и
        получить двойную скидку (оплата обоих уводила баланс в 0 дважды).
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(bonus_applied), 0) FROM orders "
                "WHERE user_id = ? AND status = 'pending_payment'",
                (user_id,),
            )
            row = await cursor.fetchone()
            return float(row[0] or 0.0)

    async def get_order_by_digiseller_invoice(self, invoice_id) -> dict | None:
        """Найти заказ по invoice_id Digiseller (защита от двойного
        подтверждения: один платёж с email юзера не должен подтверждать
        два заказа и не должен использоваться повторно новым заказом)."""
        try:
            inv_int = int(invoice_id)
        except (TypeError, ValueError):
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM orders WHERE digiseller_invoice_id = ?",
                (inv_int,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_order_admin_comment(self, order_id: int, comment: str) -> None:
        """Записать ТОЛЬКО комментарий админа, НЕ трогая статус заказа.

        Раньше underpaid-репорт Digiseller-поллера писал
        update_order_status(order_id, "pending_payment", admin_comment=...)
        и мог откатить ТОЛЬКО ЧТО подтверждённый поллером заказ обратно в
        pending_payment (гонка подтверждение ↔ недоплата).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE orders SET admin_comment = ? WHERE order_id = ?",
                (comment, order_id),
            )
            await db.commit()

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            # ── Enable WAL mode for better concurrency ──
            try:
                await db.execute("PRAGMA journal_mode=WAL")
                logger.info("SQLite WAL mode enabled")
            except Exception as e:
                logger.warning(f"Could not enable WAL mode: {e}")

            # ── Enable foreign key enforcement (off by default in SQLite) ──
            try:
                await db.execute("PRAGMA foreign_keys=ON")
                logger.info("SQLite foreign keys enabled")
            except Exception as e:
                logger.warning(f"Could not enable foreign keys: {e}")

            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    service_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    price_usdt REAL NOT NULL DEFAULT 0,
                    price INTEGER NOT NULL DEFAULT 0,
                    currency TEXT DEFAULT 'USDT',
                    payment_method TEXT DEFAULT '',
                    ton_amount REAL DEFAULT 0,
                    ton_tx_hash TEXT,
                    ton_wallet_from TEXT,
                    payment_expires_at TIMESTAMP,
                    status TEXT DEFAULT 'pending_payment',
                    account_data TEXT,
                    admin_id INTEGER,
                    admin_comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    paid_at TIMESTAMP,
                    activated_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    notif_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    order_id INTEGER,
                    notif_type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # ── Support tickets ─────────────────────────────────────
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    order_id INTEGER,
                    topic TEXT NOT NULL DEFAULT 'Обращение',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS ticket_messages (
                    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender TEXT NOT NULL,
                    sender_id INTEGER,
                    msg_type TEXT DEFAULT 'text',
                    text TEXT,
                    file_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
                );

                CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_tmsgs_ticket ON ticket_messages(ticket_id);
            """)

            # Migrate: add new columns if they don't exist (for existing DBs)
            for col_sql in [
                "ALTER TABLE orders ADD COLUMN price_usdt REAL NOT NULL DEFAULT 0",
                "ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT ''",
                "ALTER TABLE orders ADD COLUMN ton_amount REAL DEFAULT 0",
                "ALTER TABLE orders ADD COLUMN ton_tx_hash TEXT",
                "ALTER TABLE orders ADD COLUMN ton_wallet_from TEXT",
                "ALTER TABLE orders ADD COLUMN payment_expires_at TIMESTAMP",
                "ALTER TABLE orders ADD COLUMN payment_memo TEXT",
                "ALTER TABLE orders ADD COLUMN robokassa_inv_id INTEGER",  # legacy — no longer used
                "ALTER TABLE orders ADD COLUMN webmoney_payment_no INTEGER",  # legacy — no longer used
                "ALTER TABLE orders ADD COLUMN digiseller_invoice_id INTEGER",
                "ALTER TABLE orders ADD COLUMN digiseller_email TEXT",    # email юзера, на который оформлена оплата
                "ALTER TABLE orders ADD COLUMN digiseller_amount REAL",   # фактическая сумма оплаты (юниты × цена)
                "ALTER TABLE orders ADD COLUMN digiseller_id_po TEXT",    # подписанный предзаказ Digiseller
                "ALTER TABLE orders ADD COLUMN digiseller_link_created_at TEXT",  # момент создания ссылки (UTC): платежи старше — чужие
                "ALTER TABLE orders ADD COLUMN tribute_order_uuid TEXT",  # UUID заказа в Tribute Shop API — точное подтверждение оплаты
            ]:
                try:
                    await db.execute(col_sql)
                except Exception:
                    pass  # Column already exists

            # ── Marketing: promo codes ──────────────────────────────
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    discount_pct REAL DEFAULT 0,
                    discount_fixed_usdt REAL DEFAULT 0,
                    description TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    max_uses INTEGER DEFAULT 0,
                    used_count INTEGER DEFAULT 0,
                    per_user_limit INTEGER DEFAULT 1,
                    first_order_only INTEGER DEFAULT 0,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS promo_uses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    promo_code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    order_id INTEGER,
                    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_promo_uses_user ON promo_uses(user_id, promo_code);

                -- Marketing: referrals
                CREATE TABLE IF NOT EXISTS referrals (
                    referrer_id INTEGER PRIMARY KEY,
                    referral_code TEXT UNIQUE NOT NULL,
                    referral_count INTEGER DEFAULT 0,
                    bonus_earned REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS referral_uses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER NOT NULL,
                    referred_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(referred_id)
                );

                -- Marketing: user metadata (welcome discount, etc.)
                CREATE TABLE IF NOT EXISTS user_meta (
                    user_id INTEGER PRIMARY KEY,
                    referred_by INTEGER,
                    welcome_discount_used INTEGER DEFAULT 0,
                    bonus_balance REAL DEFAULT 0
                );

                -- Marketing: drip follow-up tracking
                CREATE TABLE IF NOT EXISTS drip_tracking (
                    user_id INTEGER PRIMARY KEY,
                    last_drip_sent TIMESTAMP,
                    last_template TEXT DEFAULT '',
                    drip_count INTEGER DEFAULT 0
                );

                -- Marketing: loyalty points
                CREATE TABLE IF NOT EXISTS loyalty_points (
                    user_id INTEGER PRIMARY KEY,
                    points_balance INTEGER DEFAULT 0,
                    total_earned INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS loyalty_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    order_id INTEGER,
                    points INTEGER NOT NULL,
                    action TEXT NOT NULL DEFAULT 'earn',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_loyalty_user ON loyalty_history(user_id);
            """)

            # ── Chat dedup registry (UX №10) ─────────────────────────
            # Персистентный реестр отправленных сообщений для дедупликации:
            # переживает рестарт бота, столбик «Каталог» не растёт заново
            # после перезапуска. sent_at — unix-время (wall clock).
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS chat_dedup_registry (
                    chat_id INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    sent_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, signature)
                );
                CREATE INDEX IF NOT EXISTS idx_dedup_sent ON chat_dedup_registry(sent_at);
            """)

            # ── Order columns for marketing features ────────────────
            for col, col_type in [
                ("promo_code", "TEXT DEFAULT ''"),
                ("discount_pct", "REAL DEFAULT 0"),
                ("original_price_usdt", "REAL DEFAULT 0"),
                ("renewal_reminder_sent", "INTEGER DEFAULT 0"),       # напоминание за 72ч отправлено
                ("renewal_reminder_24h_sent", "INTEGER DEFAULT 0"),   # напоминание за 24ч отправлено
                ("account_prompt_sent", "INTEGER DEFAULT 0"),
                ("bonus_applied", "REAL DEFAULT 0"),                  # бонус за друзей, применённый к заказу
                ("bonus_debited", "INTEGER DEFAULT 0"),               # бонус уже списан с баланса при оплате
            ]:
                try:
                    await db.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

            # ── Referral bonus balance (тратимый счёт за друзей) ──────
            for col_sql in [
                "ALTER TABLE user_meta ADD COLUMN bonus_balance REAL DEFAULT 0",
                # v17: флаг «бонус за приглашение уже выплачен» — выплата
                # ровно ОДИН раз, иначе парой сговоренных аккаунтов бонус
                # фармился на каждой оплате (реферал платит → рефerrer
                # получает тратимый бонус снова и снова)
                "ALTER TABLE user_meta ADD COLUMN referral_bonus_paid INTEGER DEFAULT 0",
            ]:
                try:
                    await db.execute(col_sql)
                except Exception:
                    pass  # Column already exists

            # Create indexes for fast lookups
            await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_payment_method ON orders(payment_method)")
            # v17: индексы под частые запросы
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_tribute_uuid ON orders(tribute_order_uuid)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_prompt "
                "ON orders(status, account_prompt_sent)"
            )
            await db.commit()
            logger.info("Database initialized successfully (with marketing tables)")

    # ─── Users ─────────────────────────────────────────────────────

    async def get_or_create_user(self, user_id: int, username: str = None,
                                  first_name: str = None, last_name: str = None) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
                    (user_id, username, first_name, last_name),
                )
                await db.commit()
                return {"user_id": user_id, "username": username, "first_name": first_name, "last_name": last_name}
            user = dict(row)
            # v17: обновляем устаревшие username/имя — иначе в списке
            # обращений и у админов висят старые @username навсегда
            if (user.get("username") or None) != (username or None) or \
                    (user.get("first_name") or None) != (first_name or None) or \
                    (user.get("last_name") or None) != (last_name or None):
                try:
                    await db.execute(
                        "UPDATE users SET username = ?, first_name = ?, last_name = ? "
                        "WHERE user_id = ?",
                        (username, first_name, last_name, user_id),
                    )
                    await db.commit()
                    user.update({"username": username, "first_name": first_name,
                                 "last_name": last_name})
                except Exception as e:
                    logger.warning(f"get_or_create_user: profile update failed: {e}")
            return user

    async def get_user(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_total_users(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            return (await cursor.fetchone())[0]

    async def get_all_user_ids(self) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT user_id FROM users")
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    # ─── Orders ────────────────────────────────────────────────────

    async def create_order(self, user_id: int, service_id: str, service_name: str,
                           plan_id: str, plan_name: str, duration_days: int,
                           price_usdt: float, price: int = 0, currency: str = "USDT",
                           payment_method: str = "", ton_amount: float = 0,
                           payment_expires_at: str = None,
                           bonus_applied: float = 0) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO orders
                   (user_id, service_id, service_name, plan_id, plan_name, duration_days,
                    price_usdt, price, currency, payment_method, ton_amount, payment_expires_at,
                    bonus_applied)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, service_id, service_name, plan_id, plan_name, duration_days,
                 price_usdt, price, currency, payment_method, ton_amount, payment_expires_at,
                 bonus_applied),
            )
            await db.commit()
            return cursor.lastrowid

    async def _debit_order_bonus(self, db, order_id: int) -> None:
        """Списать бонус за друзей при ПОДТВЕРЖДЕНИИ оплаты заказа.

        Вызывается при переходе заказа в pending_account — единственный
        момент, когда оплата считается состоявшейся (включая revival
        отменённых заказов, оплаченных в офлайне). Идемпотентно: флаг
        bonus_debited гарантирует ровно одно списание на заказ; баланс
        не может уйти в минус.
        """
        cursor = await db.execute(
            """SELECT user_id, COALESCE(bonus_applied, 0)
               FROM orders
               WHERE order_id = ? AND COALESCE(bonus_debited, 0) = 0""",
            (order_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return
        user_id, bonus_applied = row[0], row[1] or 0
        # Флаг ставим ДО списания — защита от повторного перехода в pending_account
        await db.execute(
            "UPDATE orders SET bonus_debited = 1 WHERE order_id = ?",
            (order_id,),
        )
        if bonus_applied <= 0 or not user_id:
            return
        await db.execute(
            "UPDATE user_meta SET bonus_balance = MAX(0, COALESCE(bonus_balance, 0) - ?) "
            "WHERE user_id = ?",
            (bonus_applied, user_id),
        )

    async def get_order(self, order_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            order = dict(row)
            # Decrypt account_data if encrypted
            if order.get("account_data"):
                order["account_data"] = self._decrypt_account_data(order["account_data"])
            return order

    async def update_order_status(self, order_id: int, status: str, **kwargs):
        """Update order status and optional fields."""
        async with aiosqlite.connect(self.db_path) as db:
            sets = ["status = ?"]
            vals = [status]

            if "account_data" in kwargs:
                # Encrypt account_data before storing
                raw = kwargs["account_data"]
                if isinstance(raw, dict):
                    raw = json.dumps(raw, ensure_ascii=False)
                encrypted = self._encrypt_account_data(raw)
                sets.append("account_data = ?")
                vals.append(encrypted)
            if "paid_at" in kwargs:
                sets.append("paid_at = ?")
                vals.append(kwargs["paid_at"])
            if "admin_id" in kwargs:
                sets.append("admin_id = ?")
                vals.append(kwargs["admin_id"])
            if "admin_comment" in kwargs:
                sets.append("admin_comment = ?")
                vals.append(kwargs["admin_comment"])
            if "activated_at" in kwargs:
                sets.append("activated_at = ?")
                vals.append(kwargs["activated_at"])
            if "ton_tx_hash" in kwargs:
                sets.append("ton_tx_hash = ?")
                vals.append(kwargs["ton_tx_hash"])
            if "ton_wallet_from" in kwargs:
                sets.append("ton_wallet_from = ?")
                vals.append(kwargs["ton_wallet_from"])
            if "payment_method" in kwargs:
                sets.append("payment_method = ?")
                vals.append(kwargs["payment_method"])
            if "ton_amount" in kwargs:
                sets.append("ton_amount = ?")
                vals.append(kwargs["ton_amount"])
            if "payment_memo" in kwargs:
                sets.append("payment_memo = ?")
                vals.append(kwargs["payment_memo"])
            if "payment_expires_at" in kwargs:
                sets.append("payment_expires_at = ?")
                vals.append(kwargs["payment_expires_at"])
            if "promo_code" in kwargs:
                sets.append("promo_code = ?")
                vals.append(kwargs["promo_code"])
            if "discount_pct" in kwargs:
                sets.append("discount_pct = ?")
                vals.append(kwargs["discount_pct"])
            if "original_price_usdt" in kwargs:
                sets.append("original_price_usdt = ?")
                vals.append(kwargs["original_price_usdt"])
            if "renewal_reminder_sent" in kwargs:
                sets.append("renewal_reminder_sent = ?")
                vals.append(kwargs["renewal_reminder_sent"])
            if "renewal_reminder_24h_sent" in kwargs:
                sets.append("renewal_reminder_24h_sent = ?")
                vals.append(kwargs["renewal_reminder_24h_sent"])
            if "price_usdt" in kwargs:
                sets.append("price_usdt = ?")
                vals.append(kwargs["price_usdt"])

            vals.append(order_id)
            await db.execute(
                f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ?",
                vals,
            )
            if status == "pending_account":
                # Оплата подтверждена — списываем зарезервированный бонус за друзей
                await self._debit_order_bonus(db, order_id)
            await db.commit()

    async def set_order_tribute_info(self, order_id: int, order_uuid: str) -> None:
        """Сохранить UUID заказа в Tribute (вызывается после создания заказа).

        UUID — единственный ключ подтверждения оплаты: poller сверяет
        статус ТОЛЬКО этого заказа в Tribute (никаких эвристик по email).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE orders SET tribute_order_uuid = ? WHERE order_id = ?",
                (str(order_uuid), order_id),
            )
            await db.commit()

    async def get_order_by_tribute_uuid(self, order_uuid: str) -> dict | None:
        """Найти заказ по UUID платежа Tribute (для сверки/поддержки)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM orders WHERE tribute_order_uuid = ?",
                (str(order_uuid),),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

    async def set_order_digiseller_info(
        self,
        order_id: int,
        email: str | None = None,
        amount: float | None = None,
        id_po: str | None = None,
    ) -> None:
        """Сохранить платёжные данные Digiseller у заказа.

        Вызывается сразу после создания ссылки на оплату. Данные нужны,
        чтобы потом НАЙТИ оплату на площадке:
          — email попадает в ссылку (&email=...) и записывается на продажу;
          — amount — фактическая сумма (unit_cnt × цена юнита), по ней
            сверяется оплата (устраняет дрейф курса USDT/RUB);
          — id_po — подписанный предзаказ (для диагностики/поддержки).

        Всегда проставляет digiseller_link_created_at (UTC) — момент
        создания ссылки. find_payment_for_order не считает оплатой
        продажи, оплаченные раньше этого момента: это старые платежи
        с тем же email (прошлые покупки), а не оплата этого заказа.
        """
        async with aiosqlite.connect(self.db_path) as db:
            sets, vals = [], []
            if email is not None:
                sets.append("digiseller_email = ?")
                vals.append(email.strip())
            if amount is not None:
                sets.append("digiseller_amount = ?")
                vals.append(float(amount))
            if id_po is not None:
                sets.append("digiseller_id_po = ?")
                vals.append(str(id_po))
            # Штамп времени — ВСЕГДА, даже если остальные поля не переданы:
            # без него остаётся риск ложного подтверждения по старой продаже.
            sets.append("digiseller_link_created_at = ?")
            vals.append(datetime.utcnow().isoformat())
            vals.append(order_id)
            await db.execute(
                f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ?",
                vals,
            )
            await db.commit()

    async def try_transition_order_status(
        self, order_id: int, from_status: str, to_status: str, **kwargs
    ) -> bool:
        """Atomically transition order status (CAS — Compare-And-Swap).

        Only updates if the current status matches from_status.
        Returns True if the transition succeeded, False if status didn't match
        (i.e. another process already changed it).

        This prevents race conditions when two concurrent webhook requests
        try to mark the same order as paid.
        """
        async with aiosqlite.connect(self.db_path) as db:
            sets = ["status = ?"]
            vals = [to_status]

            # Same kwargs handling as update_order_status
            if "account_data" in kwargs:
                raw = kwargs["account_data"]
                if isinstance(raw, dict):
                    raw = json.dumps(raw, ensure_ascii=False)
                encrypted = self._encrypt_account_data(raw)
                sets.append("account_data = ?")
                vals.append(encrypted)
            if "paid_at" in kwargs:
                sets.append("paid_at = ?")
                vals.append(kwargs["paid_at"])
            if "admin_id" in kwargs:
                sets.append("admin_id = ?")
                vals.append(kwargs["admin_id"])
            if "admin_comment" in kwargs:
                sets.append("admin_comment = ?")
                vals.append(kwargs["admin_comment"])
            if "activated_at" in kwargs:
                sets.append("activated_at = ?")
                vals.append(kwargs["activated_at"])
            if "ton_tx_hash" in kwargs:
                sets.append("ton_tx_hash = ?")
                vals.append(kwargs["ton_tx_hash"])
            if "ton_wallet_from" in kwargs:
                sets.append("ton_wallet_from = ?")
                vals.append(kwargs["ton_wallet_from"])
            if "payment_method" in kwargs:
                sets.append("payment_method = ?")
                vals.append(kwargs["payment_method"])
            if "ton_amount" in kwargs:
                sets.append("ton_amount = ?")
                vals.append(kwargs["ton_amount"])
            if "payment_memo" in kwargs:
                sets.append("payment_memo = ?")
                vals.append(kwargs["payment_memo"])
            if "payment_expires_at" in kwargs:
                sets.append("payment_expires_at = ?")
                vals.append(kwargs["payment_expires_at"])
            if "promo_code" in kwargs:
                sets.append("promo_code = ?")
                vals.append(kwargs["promo_code"])
            if "discount_pct" in kwargs:
                sets.append("discount_pct = ?")
                vals.append(kwargs["discount_pct"])
            if "original_price_usdt" in kwargs:
                sets.append("original_price_usdt = ?")
                vals.append(kwargs["original_price_usdt"])
            if "renewal_reminder_sent" in kwargs:
                sets.append("renewal_reminder_sent = ?")
                vals.append(kwargs["renewal_reminder_sent"])
            if "renewal_reminder_24h_sent" in kwargs:
                sets.append("renewal_reminder_24h_sent = ?")
                vals.append(kwargs["renewal_reminder_24h_sent"])
            if "price_usdt" in kwargs:
                sets.append("price_usdt = ?")
                vals.append(kwargs["price_usdt"])
            if "digiseller_invoice_id" in kwargs:
                sets.append("digiseller_invoice_id = ?")
                vals.append(kwargs["digiseller_invoice_id"])
            if "digiseller_email" in kwargs:
                sets.append("digiseller_email = ?")
                vals.append(kwargs["digiseller_email"])
            if "digiseller_amount" in kwargs:
                sets.append("digiseller_amount = ?")
                vals.append(kwargs["digiseller_amount"])
            if "digiseller_id_po" in kwargs:
                sets.append("digiseller_id_po = ?")
                vals.append(kwargs["digiseller_id_po"])

            # CAS: only update WHERE current status = from_status
            vals.append(order_id)
            vals.append(from_status)
            cursor = await db.execute(
                f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ? AND status = ?",
                vals,
            )
            if to_status == "pending_account":
                # Оплата подтверждена (в т.ч. revival после офлайна) — списываем бонус
                await self._debit_order_bonus(db, order_id)
            await db.commit()
            return cursor.rowcount > 0

    async def get_user_orders(self, user_id: int, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_orders_by_status(self, status: str, limit: int = 50) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY created_at ASC LIMIT ?",
                (status, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_unnotified_account_orders(self, limit: int = 50) -> list[dict]:
        """Orders that are PAID (pending_account) but the user was never
        prompted to enter account data (e.g. the prompt send failed).

        Used by the recovery task in main.py — guarantees that EVERY paid
        order eventually gets its account-data prompt, even if the payment
        poller crashed right after confirming the payment.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM orders
                   WHERE status = 'pending_account'
                     AND COALESCE(account_prompt_sent, 0) = 0
                   ORDER BY created_at ASC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def mark_account_prompt_sent(self, order_id: int) -> None:
        """Flag the order as 'user was prompted to enter account data'."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE orders SET account_prompt_sent = 1 WHERE order_id = ?",
                    (order_id,),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"mark_account_prompt_sent({order_id}) failed: {e}")

    async def get_expired_pending_orders(self) -> list[dict]:
        """Get orders that have expired payment timeout."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM orders WHERE status = 'pending_payment' AND payment_expires_at IS NOT NULL AND payment_expires_at < ?",
                (now,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_orders_stats(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            stats = {}
            for status in ["pending_payment", "pending_account", "pending_activation", "active", "cancelled", "failed"]:
                cursor = await db.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,))
                stats[status] = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT COUNT(*) FROM orders")
            stats["total"] = (await cursor.fetchone())[0]
            # v17: «Активные» с учётом срока подписки. DB-статус 'active'
            # намеренно НЕ переводится в 'expired' (см. utils/order_status.py —
            # drip-рассылка и бонусы выбирают подписки строго по 'active'),
            # поэтому сырой счётчик завышал «Активные» истёкшими заказами.
            # Делим по эффективному статусу — тот же код, что и на экранах.
            cursor = await db.execute(
                "SELECT activated_at, duration_days FROM orders WHERE status = 'active'"
            )
            from utils.order_status import effective_status
            active_now, expired_now = 0, 0
            for activated_at, duration_days in await cursor.fetchall():
                eff = effective_status(
                    {"status": "active", "activated_at": activated_at,
                     "duration_days": duration_days}
                )
                if eff == "active":
                    active_now += 1
                else:
                    expired_now += 1
            stats["active"] = active_now
            stats["expired"] = expired_now
            # Revenue in USDT
            cursor = await db.execute("SELECT COALESCE(SUM(price_usdt), 0) FROM orders WHERE status IN ('active')")
            stats["revenue_usdt"] = (await cursor.fetchone())[0]
            # v17: деньги уже получены, но заказ ещё в очереди (ждёт данные
            # аккаунта / активацию). Раньше «Выручка» их не показывала —
            # владелец видел меньше, чем реально пришло.
            cursor = await db.execute(
                "SELECT COALESCE(SUM(price_usdt), 0) FROM orders "
                "WHERE status IN ('pending_account', 'pending_activation')"
            )
            stats["paid_queue_usdt"] = (await cursor.fetchone())[0]
            # Revenue by payment method
            for method in ["ton", "usdt", "digiseller", "tribute", "stars"]:
                cursor = await db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(price_usdt), 0) FROM orders WHERE payment_method = ? AND status = 'active'",
                    (method,),
                )
                row = await cursor.fetchone()
                stats[f"{method}_count"] = row[0]
                stats[f"{method}_revenue"] = row[1]
            return stats

    # ─── Account Data Security ─────────────────────────────────────

    @staticmethod
    def _encrypt_account_data(data: str) -> str:
        """Encrypt account_data before storing in DB."""
        try:
            from utils.crypto import encrypt
            return encrypt(data)
        except Exception as e:
            logger.warning(f"Could not encrypt account_data: {e}")
            return data

    @staticmethod
    def _decrypt_account_data(data: str) -> str:
        """Decrypt account_data after reading from DB. Backwards-compatible with plain text."""
        try:
            from utils.crypto import decrypt
            return decrypt(data)
        except Exception:
            return data

    async def cleanup_old_account_data(self):
        """Delete account_data for orders activated more than ACCOUNT_DATA_TTL_DAYS ago.
        Called periodically from background task.
        """
        from config import ACCOUNT_DATA_TTL_DAYS
        if ACCOUNT_DATA_TTL_DAYS <= 0:
            return  # Feature disabled

        cutoff = (datetime.utcnow() - timedelta(days=ACCOUNT_DATA_TTL_DAYS)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    """UPDATE orders SET account_data = NULL
                       WHERE status = 'active'
                         AND activated_at IS NOT NULL
                         AND activated_at < ?
                         AND account_data IS NOT NULL""",
                    (cutoff,),
                )
                await db.commit()
                if cursor.rowcount > 0:
                    logger.info(f"Cleaned up account_data for {cursor.rowcount} old orders (TTL={ACCOUNT_DATA_TTL_DAYS}d)")
        except Exception as e:
            logger.error(f"Failed to cleanup account_data: {e}")

    # ─── Notifications ─────────────────────────────────────────────

    async def log_notification(self, user_id: int, order_id: int | None, notif_type: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO notifications (user_id, order_id, notif_type) VALUES (?, ?, ?)",
                (user_id, order_id, notif_type),
            )
            await db.commit()

    # ─── Support tickets ───────────────────────────────────────────

    async def create_ticket(
        self,
        user_id: int,
        topic: str,
        order_id: int | None,
        first_text: str,
        msg_type: str = "text",
        file_id: str | None = None,
    ) -> int:
        """Create a ticket together with its first user message (atomic).

        Returns the new ticket_id.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO tickets (user_id, order_id, topic) VALUES (?, ?, ?)",
                (user_id, order_id, topic),
            )
            ticket_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO ticket_messages "
                "(ticket_id, sender, sender_id, msg_type, text, file_id) "
                "VALUES (?, 'user', ?, ?, ?, ?)",
                (ticket_id, user_id, msg_type, first_text, file_id),
            )
            await db.commit()
            return ticket_id

    async def add_ticket_message(
        self,
        ticket_id: int,
        sender: str,
        sender_id: int | None,
        text: str | None,
        msg_type: str = "text",
        file_id: str | None = None,
    ) -> None:
        """Append a message to the ticket thread and bump updated_at."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO ticket_messages "
                "(ticket_id, sender, sender_id, msg_type, text, file_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ticket_id, sender, sender_id, msg_type, text, file_id),
            )
            await db.execute(
                "UPDATE tickets SET updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?",
                (ticket_id,),
            )
            await db.commit()

    async def get_ticket(self, ticket_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def get_user_tickets(self, user_id: int, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tickets WHERE user_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def count_open_tickets(self, user_id: int) -> int:
        """Number of NOT closed tickets of the user (anti-spam limit)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE user_id = ? AND status != 'closed'",
                (user_id,),
            )
            return (await cursor.fetchone())[0]

    async def get_last_ticket_created(self, user_id: int) -> str | None:
        """ISO timestamp of the user's most recent ticket (cooldown check)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT MAX(created_at) FROM tickets WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None

    async def get_ticket_messages(self, ticket_id: int, limit: int = 30) -> list[dict]:
        """Ticket thread in chronological order (oldest → newest, last `limit`)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM ("
                "  SELECT * FROM ticket_messages WHERE ticket_id = ? "
                "  ORDER BY msg_id DESC LIMIT ?"
                ") ORDER BY msg_id ASC",
                (ticket_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def set_ticket_status(self, ticket_id: int, status: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE tickets SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE ticket_id = ?",
                (status, ticket_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_admin_tickets(
        self, status: str | None, offset: int = 0, limit: int = 10
    ) -> list[dict]:
        """Tickets for the admin browser («Обращения» в админ-меню).

        status=None → все; 'open' / 'answered' / 'closed' → точный фильтр.
        Свежая активность сверху; JOIN users — @username без доп. запросов.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            where, params = "", []
            if status is not None:
                where = "WHERE t.status = ?"
                params.append(status)
            params.extend([limit, offset])
            cursor = await db.execute(
                "SELECT t.*, u.username, u.first_name FROM tickets t "
                f"LEFT JOIN users u ON u.user_id = t.user_id "
                f"{where} "
                "ORDER BY t.updated_at DESC, t.ticket_id DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def count_admin_tickets(self, status: str | None = None) -> int:
        """Число обращений для пагинации браузера (с тем же фильтром)."""
        async with aiosqlite.connect(self.db_path) as db:
            where, params = "", []
            if status is not None:
                where = "WHERE status = ?"
                params.append(status)
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM tickets {where}", params
            )
            return (await cursor.fetchone())[0]

    async def get_open_tickets(self, limit: int = 15) -> list[dict]:
        """All not-closed tickets (admin /tickets), newest activity first.

        JOINs users to show @username without extra queries.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT t.*, u.username, u.first_name FROM tickets t "
                "LEFT JOIN users u ON u.user_id = t.user_id "
                "WHERE t.status != 'closed' "
                "ORDER BY t.updated_at DESC, t.ticket_id DESC "
                "LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# Singleton
db = Database()


async def try_transition_order_status(
    order_id: int, from_status: str, to_status: str, **kwargs
) -> bool:
    """Module-level wrapper around db.try_transition_order_status (CAS).

    Allows handlers to `from models.database import try_transition_order_status`
    instead of reaching into the db singleton.
    """
    return await db.try_transition_order_status(order_id, from_status, to_status, **kwargs)


# ─── Catalog Loader ────────────────────────────────────────────────

def load_catalog() -> dict:
    """Load services catalog from JSON file.
    
    If the file is missing or corrupted, returns a minimal valid catalog
    structure instead of crashing the entire bot.
    """
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            catalog = json.load(f)
            # Validate minimal structure
            if "services" not in catalog or not isinstance(catalog["services"], list):
                logger.error("catalog.json: invalid structure — 'services' key missing or not a list. Resetting.")
                return {"services": []}
            return catalog
    except FileNotFoundError:
        logger.warning(f"catalog.json not found at {CATALOG_PATH}. Creating empty catalog.")
        empty = {"services": []}
        save_catalog(empty)
        return empty
    except json.JSONDecodeError as e:
        logger.error(f"catalog.json is corrupted (JSON error: {e}). Backing up and resetting.")
        # Try to back up the corrupted file for manual recovery
        try:
            import shutil
            backup_path = CATALOG_PATH + ".corrupted.bak"
            shutil.copy2(CATALOG_PATH, backup_path)
            logger.info(f"Corrupted catalog backed up to {backup_path}")
        except Exception:
            pass
        empty = {"services": []}
        save_catalog(empty)
        return empty


def save_catalog(catalog: dict):
    """Save services catalog to JSON file — ATOMIC write.
    
    Writes to a temp file first, then atomically renames.
    This prevents corruption if the bot crashes during write
    (which was a likely cause of the 'plan not found' bug).
    """
    import tempfile
    import os as _os
    
    catalog_dir = _os.path.dirname(CATALOG_PATH) or "."
    
    # Write to temp file in the same directory (same filesystem = atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        prefix="catalog_",
        dir=catalog_dir,
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
            f.flush()
            _os.fsync(f.fileno())  # Force write to disk
        # Atomic rename (POSIX) — on Windows this may fail if dest exists
        _os.replace(tmp_path, CATALOG_PATH)
    except Exception:
        # Clean up temp file on error
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass
        raise


async def async_save_catalog(catalog: dict):
    """Async wrapper for save_catalog with asyncio.Lock protection.
    
    Must be used instead of save_catalog() in all async handlers to:
    1. Prevent lost updates when two admins edit catalog concurrently
    2. Avoid blocking the event loop with synchronous file I/O
    
    Usage in handlers:
        catalog = load_catalog()
        catalog["services"][0]["name"] = "New Name"
        await async_save_catalog(catalog)
    """
    async with _catalog_lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, save_catalog, catalog)


async def async_load_and_lock_catalog():
    """Acquire catalog lock and load catalog — for read-modify-write pattern.
    
    Usage:
        async with _catalog_lock:
            catalog = load_catalog()
            # ... modify catalog ...
            save_catalog(catalog)  # safe — lock is held
    """
    # This is a convenience — callers should use async_save_catalog instead.
    # Kept for backwards compat with existing load-modify-save patterns.
    async with _catalog_lock:
        return load_catalog()


def get_active_services() -> list[dict]:
    """Get only active services from catalog."""
    catalog = load_catalog()
    return [s for s in catalog["services"] if s.get("active", True)]


def get_service_by_id(service_id: str) -> dict | None:
    """Find service by ID in catalog."""
    catalog = load_catalog()
    for s in catalog["services"]:
        if s["id"] == service_id:
            return s
    return None


def get_plan_from_service(service: dict, plan_id: str) -> dict | None:
    """Find plan within a service."""
    for p in service.get("plans", []):
        if p["id"] == plan_id:
            return p
    return None
