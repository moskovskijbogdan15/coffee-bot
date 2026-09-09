# -*- coding: utf-8 -*-
"""bot.py — движок кофейного бота.
Одна кодовая база на несколько кофеен: всё специфичное лежит в shops/<SHOP>.json,
в коде нет ни названий, ни цен, ни часов работы."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from jobs import check_no_shows

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# КОНФИГ ЗАВЕДЕНИЯ
# ---------------------------------------------------------------------------
SHOP = os.environ.get("SHOP", "demo")  # какой json грузить: shops/<SHOP>.json
CFG_PATH = os.path.join(os.path.dirname(__file__), "shops", f"{SHOP}.json")
with open(CFG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)

# Токен — только из окружения, в коде и в json его нет.
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# chat_id владельца и группы баристы берём из конфига заведения, но разрешаем
# переопределить переменными окружения (удобно держать секреты в Railway).
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", CFG.get("owner_chat_id", 0)) or 0)
BARISTA_CHAT_ID = int(os.environ.get("BARISTA_CHAT_ID", CFG.get("barista_chat_id", 0)) or 0)

TZ = ZoneInfo(CFG.get("timezone", "Europe/Kyiv"))  # показываем время гостю в киевском
STAMPS_NEEDED = int(CFG.get("stamps_needed", 6))
KNOWN_SOURCES = CFG.get("known_sources", [])
CUR = CFG.get("currency", "грн")
MILK = CFG.get("milk_options", [])  # [[название, доплата], ...]


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ
# ---------------------------------------------------------------------------
def hhmm(dt_utc):
    """UTC -> строка ЧЧ:ММ в часовом поясе заведения."""
    return dt_utc.astimezone(TZ).strftime("%H:%M")


def dots(count):
    """Штампы визуально: ●●●●○○"""
    count = max(0, min(count, STAMPS_NEEDED))
    return "●" * count + "○" * (STAMPS_NEEDED - count)


def get_category(cid):
    for cat in CFG["menu"]:
        if cat["id"] == cid:
            return cat
    return None


def get_item(cid, iid):
    cat = get_category(cid)
    if cat:
        for it in cat["items"]:
            if it["id"] == iid:
                return it
    return None


def line_label(it):
    """Читаемая строка позиции: 'Латте 0,3, овсяное'."""
    s = it["item"]
    size = it.get("size")
    if size and size not in ("стандарт", "1 шт", ""):
        s += f" {size}"
    if it.get("milk"):
        s += f", {it['milk']}"
    return s


def cart_total(cart):
    return sum(x["price"] for x in cart)


async def notify_owner(context, text):
    if not OWNER_CHAT_ID:
        return
    try:
        await context.bot.send_message(OWNER_CHAT_ID, text)
    except Exception as e:  # уведомление не должно ронять основную логику
        logger.warning("Не смог уведомить владельца: %s", e)


# ---------------------------------------------------------------------------
# ГЛАВНЫЙ ЭКРАН
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # deep-link метка источника: t.me/bot?start=ig_bio -> context.args == ["ig_bio"]
    args = context.args or []
    src = args[0] if args else ""
    u = update.effective_user
    is_new, source = db.ensure_user(u.id, u.username or "", src, KNOWN_SOURCES)
    if is_new:
        who = f" · @{u.username}" if u.username else ""
        await notify_owner(context, f"🆕 Новый гость. Источник: {source}{who}")
    context.user_data.pop("cart", None)
    await show_main(update, context)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    uid = update.effective_user.id
    rows = []
    last = db.last_order_items(uid)
    if last:  # кнопку повтора показываем только если есть история
        summary = " · ".join(line_label(x) for x in last)
        if len(summary) > 38:
            summary = summary[:38] + "…"
        rows.append([InlineKeyboardButton(f"☕ Повторить: {summary}", callback_data="rp")])
    rows.append([InlineKeyboardButton("📋 Меню", callback_data="menu")])
    rows.append([InlineKeyboardButton(f"⭐ Мои штампы {dots(db.get_stamps(uid))}",
                                      callback_data="stamps")])
    rows.append([InlineKeyboardButton("📍 Где мы · часы работы", callback_data="where")])
    kb = InlineKeyboardMarkup(rows)
    text = CFG.get("greeting", CFG.get("name", "Кофейня"))
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=kb)


async def cb_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await show_main(update, context, edit=True)


async def cb_stamps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cnt = db.get_stamps(update.effective_user.id)
    left = STAMPS_NEEDED - cnt
    text = f"⭐ Ваши штампы: {dots(cnt)}\nЕщё {left} — и напиток за счёт заведения."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main")]])
    await q.edit_message_text(text, reply_markup=kb)


async def cb_where(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = f"📍 {CFG.get('address', '')}\n🕒 {CFG.get('hours', '')}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main")]])
    await q.edit_message_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# МЕНЮ И ЗАКАЗ
# ---------------------------------------------------------------------------
async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rows = [[InlineKeyboardButton(cat["title"], callback_data=f"c:{cat['id']}")]
            for cat in CFG["menu"]]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="main")])
    await q.edit_message_text("Что будем пить?", reply_markup=InlineKeyboardMarkup(rows))


async def cb_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # «Ещё что-то» — снова список категорий
    await cb_menu(update, context)


async def cb_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.data.split(":", 1)[1]
    cat = get_category(cid)
    rows = []
    for it in cat["items"]:
        if "price" in it:
            label = f"{it['name']} · {it['price']} {CUR}"
        else:
            pmin = min(p for _, p in it["sizes"])
            pref = "от " if len(it["sizes"]) > 1 else ""
            label = f"{it['name']} · {pref}{pmin} {CUR}"
        rows.append([InlineKeyboardButton(label, callback_data=f"i:{cid}:{it['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Меню", callback_data="menu")])
    await q.edit_message_text(cat["title"], reply_markup=InlineKeyboardMarkup(rows))


async def cb_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, cid, iid = q.data.split(":")
    it = get_item(cid, iid)
    # плоская позиция (выпечка) — без размера и молока, сразу в корзину
    if "price" in it:
        add_to_cart(context, {"item": it["name"], "size": None, "milk": None,
                              "price": it["price"]})
        await show_cart(update, context)
        return
    # есть размеры — показываем выбор размера
    rows = [[InlineKeyboardButton(f"{label} · {price} {CUR}", callback_data=f"s:{cid}:{iid}:{i}")]
            for i, (label, price) in enumerate(it["sizes"])]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"c:{cid}")])
    await q.edit_message_text(f"{it['name']} — размер:", reply_markup=InlineKeyboardMarkup(rows))


async def cb_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, cid, iid, si = q.data.split(":")
    it = get_item(cid, iid)
    label, price = it["sizes"][int(si)]
    if not it.get("milk"):  # молоко не нужно — сразу в корзину
        add_to_cart(context, {"item": it["name"], "size": label, "milk": None, "price": price})
        await show_cart(update, context)
        return
    rows = []
    for i, (mname, extra) in enumerate(MILK):
        tag = f" (+{extra})" if extra else ""
        rows.append([InlineKeyboardButton(f"{mname}{tag}", callback_data=f"m:{cid}:{iid}:{si}:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"i:{cid}:{iid}")])
    await q.edit_message_text(f"{it['name']} {label} — молоко:",
                              reply_markup=InlineKeyboardMarkup(rows))


async def cb_milk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, cid, iid, si, mi = q.data.split(":")
    it = get_item(cid, iid)
    label, price = it["sizes"][int(si)]
    mname, extra = MILK[int(mi)]
    add_to_cart(context, {"item": it["name"], "size": label, "milk": mname,
                          "price": price + extra})
    await show_cart(update, context)


def add_to_cart(context, line):
    context.user_data.setdefault("cart", []).append(line)


async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cart = context.user_data.get("cart", [])
    total = cart_total(cart)
    lines = "\n".join(f"• {line_label(x)} — {x['price']} {CUR}" for x in cart)
    text = f"🛒 В заказе:\n{lines}\n\nИтого: {total} {CUR}"
    rows = [
        [InlineKeyboardButton("➕ Ещё что-то", callback_data="more")],
        [InlineKeyboardButton(f"✅ Оформить · {total} {CUR}", callback_data="co")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


def time_keyboard():
    # Время только относительное: человек идёт пешком, дата не нужна.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("через 5 мин", callback_data="t:5"),
         InlineKeyboardButton("через 10 мин", callback_data="t:10")],
        [InlineKeyboardButton("через 15 мин", callback_data="t:15"),
         InlineKeyboardButton("через 20 мин", callback_data="t:20")],
    ])


async def cb_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not context.user_data.get("cart"):
        await q.answer("Корзина пуста", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text("Когда заберёте?", reply_markup=time_keyboard())


async def cb_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    last = db.last_order_items(update.effective_user.id)
    if not last:
        await q.answer("Истории пока нет", show_alert=True)
        return
    await q.answer()
    context.user_data["cart"] = [dict(x) for x in last]  # копия позиций последнего заказа
    total = cart_total(context.user_data["cart"])
    lines = "\n".join(f"• {line_label(x)} — {x['price']} {CUR}" for x in context.user_data["cart"])
    await q.edit_message_text(
        f"Повторяем заказ:\n{lines}\n\nИтого {total} {CUR}\nКогда заберёте?",
        reply_markup=time_keyboard(),
    )


async def cb_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not context.user_data.get("cart"):
        await q.edit_message_text("Корзина пуста. Начните заново: /start")
        return
    mins = int(q.data.split(":")[1])
    ready = datetime.now(timezone.utc) + timedelta(minutes=mins)
    context.user_data["ready_at"] = ready.isoformat()
    cart = context.user_data["cart"]
    total = cart_total(cart)
    lines = "\n".join(f"• {line_label(x)} — {x['price']} {CUR}" for x in cart)
    text = (f"{lines}\n\nИтого: {total} {CUR}\n"
            f"🕗 К {hhmm(ready)} · оплата на кассе при получении")
    rows = [
        [InlineKeyboardButton("✅ Подтвердить", callback_data="ok")],
        [InlineKeyboardButton("⬅️ Другое время", callback_data="co")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cart = context.user_data.get("cart")
    ready_iso = context.user_data.get("ready_at")
    if not cart or not ready_iso:
        await q.edit_message_text("Что-то пошло не так. Начните заново: /start")
        return
    total = cart_total(cart)
    user = update.effective_user
    oid = db.create_order(user.id, ready_iso, cart, total)
    ready = datetime.fromisoformat(ready_iso)
    await send_to_barista(context, oid, ready, cart, total, user)
    # чистим корзину
    context.user_data.pop("cart", None)
    context.user_data.pop("ready_at", None)
    await q.edit_message_text(
        f"Готово! Заказ №{oid}, к {hhmm(ready)} 🕗\n"
        f"Подходите к кассе — оплата при получении. До встречи ☕"
    )


async def send_to_barista(context, oid, ready, cart, total, user):
    """Заказ в группу баристы. Акцент на времени: готовить за 1–2 мин до срока."""
    lines = "\n".join(f"{line_label(x)} — {x['price']} {CUR}" for x in cart)
    who = f"@{user.username}" if user.username else user.full_name
    text = (
        f"<b>Заказ #{oid} · 🕗 к {hhmm(ready)}</b>\n"
        f"<i>не готовь сразу — начинай за 1–2 мин до времени</i>\n\n"
        f"{lines}\n"
        f"Итого {total} {CUR} · {who}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выдал", callback_data=f"pk:{oid}"),
        InlineKeyboardButton("🚫 Не забрал", callback_data=f"ns:{oid}"),
    ]])
    try:
        await context.bot.send_message(BARISTA_CHAT_ID, text, reply_markup=kb,
                                        parse_mode=ParseMode.HTML)
    except Exception as e:  # проблема с уведомлением не должна ронять оформление
        logger.warning("Не удалось отправить заказ баристе: %s", e)


# ---------------------------------------------------------------------------
# КНОПКИ БАРИСТЫ
# ---------------------------------------------------------------------------
async def cb_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    oid = int(q.data.split(":")[1])
    o, _ = db.get_order(oid)
    if not o:
        await q.answer("Заказ не найден", show_alert=True)
        return
    if o["status"] != "new":
        await q.answer(f"Уже обработан: {o['status']}", show_alert=True)
        return
    db.set_order_status(oid, "picked")
    db.reset_no_shows(o["user_id"])  # серия неявок прервана
    count, free = db.add_stamp(o["user_id"], STAMPS_NEEDED)
    await q.answer("Выдан ✅")
    try:
        await q.edit_message_text(q.message.text_html + "\n\n✅ <b>выдан</b>",
                                  parse_mode=ParseMode.HTML)
    except Exception:
        pass
    # уведомляем гостя о штампе
    try:
        if free:
            await context.bot.send_message(
                o["user_id"],
                f"🎉 Штампов собрано! Следующий напиток — за счёт заведения ☕ {dots(0)}")
        else:
            await context.bot.send_message(
                o["user_id"],
                f"Спасибо! Штамп +1 {dots(count)} — ещё {STAMPS_NEEDED - count} до бесплатного")
    except Exception as e:
        logger.warning("Не смог уведомить гостя о штампе: %s", e)


async def cb_noshow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    oid = int(q.data.split(":")[1])
    o, _ = db.get_order(oid)
    if not o:
        await q.answer("Заказ не найден", show_alert=True)
        return
    if o["status"] != "new":
        await q.answer(f"Уже обработан: {o['status']}", show_alert=True)
        return
    db.set_order_status(oid, "no_show")
    n = db.inc_no_shows(o["user_id"])
    if n >= 2:
        db.set_prepay(o["user_id"], True)  # TODO: предоплату пока не реализуем, только флаг
    await q.answer("Отмечено: не забрал")
    try:
        await q.edit_message_text(q.message.text_html + "\n\n🚫 <b>не забрал</b>",
                                  parse_mode=ParseMode.HTML)
    except Exception:
        pass
    try:
        await context.bot.send_message(
            o["user_id"],
            "Заказ отменён — вы не забрали его вовремя. Оформите заново, если актуально ☕")
    except Exception as e:
        logger.warning("Не смог уведомить о неявке: %s", e)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # любой текст возвращает на главный экран — не заставляем искать кнопки
    await show_main(update, context)


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------
def main():
    if not TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    db.init_db()  # ВАЖНО: реально создаём таблицы при старте (иначе первый /start падает)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb_repeat, pattern="^rp$"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(cb_more, pattern="^more$"))
    app.add_handler(CallbackQueryHandler(cb_stamps, pattern="^stamps$"))
    app.add_handler(CallbackQueryHandler(cb_where, pattern="^where$"))
    app.add_handler(CallbackQueryHandler(cb_main, pattern="^main$"))
    app.add_handler(CallbackQueryHandler(cb_category, pattern="^c:"))
    app.add_handler(CallbackQueryHandler(cb_item, pattern="^i:"))
    app.add_handler(CallbackQueryHandler(cb_size, pattern="^s:"))
    app.add_handler(CallbackQueryHandler(cb_milk, pattern="^m:"))
    app.add_handler(CallbackQueryHandler(cb_checkout, pattern="^co$"))
    app.add_handler(CallbackQueryHandler(cb_time, pattern="^t:"))
    app.add_handler(CallbackQueryHandler(cb_confirm, pattern="^ok$"))
    app.add_handler(CallbackQueryHandler(cb_picked, pattern="^pk:"))
    app.add_handler(CallbackQueryHandler(cb_noshow, pattern="^ns:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # фоновая проверка неявок раз в минуту
    app.job_queue.run_repeating(check_no_shows, interval=60, first=15)

    logger.info("Кофейный бот «%s» запущен", CFG.get("name"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
