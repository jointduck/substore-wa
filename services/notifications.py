import asyncio
import logging
from datetime import datetime

import aiosqlite
from models.database import db
from config import NOTIFY_BEFORE_DAYS, NOTIFY_CHECK_INTERVAL, SUBSCRIPTION_PLANS

logger = logging.getLogger(__name__)


class NotificationService:
    """Background service that checks for expiring subscriptions and sends notifications."""

    def __init__(self, bot):
        self.bot = bot
        self._task = None

    async def start(self):
        """Start the background notification task."""
        self._task = asyncio.create_task(self._run())
        logger.info("Notification service started")

    async def stop(self):
        """Stop the background notification task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("Notification service stopped")

    async def _run(self):
        """Main loop for the notification service."""
        while True:
            try:
                await self._check_and_notify()
            except Exception as e:
                logger.error(f"Error in notification service: {e}", exc_info=True)
            await asyncio.sleep(NOTIFY_CHECK_INTERVAL)

    async def _check_and_notify(self):
        """Check for expiring subscriptions and send notifications."""
        # First, deactivate expired subscriptions
        deactivated = await db.deactivate_expired()

        # Notify about subscriptions expiring soon
        expiring = await db.get_expiring_subscriptions(days=NOTIFY_BEFORE_DAYS)

        for sub in expiring:
            # Check if we already sent this type of notification
            notif_type = f"expiring_{NOTIFY_BEFORE_DAYS}d"
            already_notified = await db.was_notified(sub["sub_id"], notif_type)
            if already_notified:
                continue

            end_date = datetime.fromisoformat(sub["end_date"])
            days_left = (end_date - datetime.utcnow()).days
            plan_name = SUBSCRIPTION_PLANS.get(sub["plan"], {}).get("name", sub["plan"])

            try:
                await self.bot.send_message(
                    chat_id=sub["user_id"],
                    text=(
                        f"⚠️ <b>Подписка скоро истечёт!</b>\n\n"
                        f"📋 Тариф: {plan_name}\n"
                        f"📅 Окончание: {end_date.strftime('%d.%m.%Y')}\n"
                        f"⏳ Осталось дней: <b>{days_left}</b>\n\n"
                        f"🔑 Для продления получите новый ключ у администратора."
                    ),
                    parse_mode="HTML",
                )
                await db.log_notification(sub["user_id"], sub["sub_id"], notif_type)
                logger.info(f"Sent expiring notification to user {sub['user_id']} for sub {sub['sub_id']}")
            except Exception as e:
                logger.warning(f"Failed to send notification to {sub['user_id']}: {e}")

        # Notify about just-expired subscriptions
        if deactivated > 0:
            expired_subs = await self._get_recently_expired()
            for sub in expired_subs:
                notif_type = "expired"
                already_notified = await db.was_notified(sub["sub_id"], notif_type)
                if already_notified:
                    continue

                plan_name = SUBSCRIPTION_PLANS.get(sub["plan"], {}).get("name", sub["plan"])
                try:
                    await self.bot.send_message(
                        chat_id=sub["user_id"],
                        text=(
                            f"❌ <b>Подписка истекла</b>\n\n"
                            f"📋 Тариф: {plan_name}\n"
                            f"📅 Истёк: {sub['end_date'][:10]}\n\n"
                            f"🔑 Для возобновления доступа активируйте новый ключ."
                        ),
                        parse_mode="HTML",
                    )
                    await db.log_notification(sub["user_id"], sub["sub_id"], notif_type)
                    logger.info(f"Sent expired notification to user {sub['user_id']}")
                except Exception as e:
                    logger.warning(f"Failed to send expired notification to {sub['user_id']}: {e}")

    async def _get_recently_expired(self) -> list[dict]:
        """Get subscriptions that were deactivated in the last check cycle."""
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """SELECT s.*, u.user_id, u.username FROM subscriptions s
                   JOIN users u ON s.user_id = u.user_id
                   WHERE s.is_active = 0
                   AND NOT EXISTS (
                       SELECT 1 FROM notifications n
                       WHERE n.sub_id = s.sub_id AND n.notif_type = 'expired'
                   )
                   ORDER BY s.end_date DESC LIMIT 50""",
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
