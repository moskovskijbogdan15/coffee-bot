# -*- coding: utf-8 -*-
"""jobs.py — фоновые задачи (JobQueue)."""
import logging
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

# Через сколько минут после времени готовности незабранный заказ считаем неявкой.
NO_SHOW_AFTER_MIN = 10


async def check_no_shows(context):
    """Заказы в статусе new, не забранные через 10 мин после готовности,
    переводим в no_show, шлём гостю сообщение и растим счётчик неявок."""
    now = datetime.now(timezone.utc)
    for row in db.new_orders():
        ready = datetime.fromisoformat(row["ready_at"])  # хранится в UTC
        if now - ready < timedelta(minutes=NO_SHOW_AFTER_MIN):
            continue
        oid, uid = row["id"], row["user_id"]
        db.set_order_status(oid, "no_show")
        n = db.inc_no_shows(uid)
        if n >= 2:
            # TODO: предоплата пока не реализована — только ставим флаг в базе.
            db.set_prepay(uid, True)
        try:
            await context.bot.send_message(
                uid,
                "Заказ отменён — его не забрали вовремя. Оформите заново, если ещё актуально ☕",
            )
        except Exception as e:
            logger.warning("Неявка: не смог уведомить пользователя %s: %s", uid, e)
