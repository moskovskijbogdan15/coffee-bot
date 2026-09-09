# -*- coding: utf-8 -*-
"""bot.py — движок кофейного бота.
Одна кодовая база на несколько кофеен: всё специфичное лежит в shops/<SHOP>.json,
в коде нет ни названий, ни цен, ни часов работы."""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ai
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


# --- разбор времени, написанного словами ------------------------------------
# Гость не обязан попадать в кнопки: «через 7 минут», «минут через 20», «в 14:30»,
# «уже иду», «10» — всё это должно превращаться в число минут.
MIN_WAIT_MIN = 1
MAX_WAIT_MIN = 180  # дальше трёх часов заказ смысла не имеет — это кофе навынос

_NUM_WORDS = {
    "один": 1, "одну": 1, "одна": 1, "полторы": 1,
    "два": 2, "две": 2, "дві": 2, "пара": 2, "пару": 2,
    "три": 3, "четыре": 4, "чотири": 4,
    "пять": 5, "п'ять": 5, "шесть": 6, "шість": 6,
    "семь": 7, "сім": 7, "восемь": 8, "вісім": 8,
    "девять": 9, "дев'ять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "п'ятнадцять": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "двадцять": 20, "тридцать": 30, "тридцять": 30,
    "сорок": 40, "пятьдесят": 50,
}
# \b важен: без него «иду» находится внутри «приду завтра»
_RE_SOON = re.compile(r"\b(?:сейчас|щас|зараз|сразу|иду|бегу|рядом|подхожу)\b")
_RE_CLOCK = re.compile(r"\b([01]?\d|2[0-3])[:.\-]([0-5]\d)\b")
_RE_HOURS = re.compile(r"\b(\d{1,2})\s*(?:час|годин)")
_RE_MINS = re.compile(r"\b(\d{1,3})\s*(?:мин|min|хв)")
_RE_MINS_REV = re.compile(r"(?:мин|хв)\w*\s+(\d{1,3})\b")  # «минут через 10», «через хвилин 10»
_RE_AFTER = re.compile(r"через\s+(\d{1,3})\b")
_RE_HOUR_WORD = re.compile(r"\bчас(?:а|ов|ик)?\b|\bгодин")


def parse_minutes(text, now_local):
    """Текст гостя -> сколько минут ждать. None, если это вообще не про время."""
    t = (text or "").lower().replace("ё", "е").replace("’", "'").strip()
    if not t or len(t) > 140:
        return None

    def ok(n):
        return int(n) if MIN_WAIT_MIN <= n <= MAX_WAIT_MIN else None

    # «10» одним числом — самый частый быстрый ответ
    if re.fullmatch(r"\d{1,3}", t):
        return ok(int(t))

    # абсолютное время: «в 14:30», «к 9.05», «буду 18-40»
    m = _RE_CLOCK.search(t)
    if m:
        target = now_local.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                   second=0, microsecond=0)
        delta = (target - now_local).total_seconds()
        return ok(max(1, round(delta / 60))) if delta > 0 else None

    if "завтра" in t or "вечером" in t:
        return None  # заказы на потом не поддерживаем — пусть ответит консультант
    if _RE_SOON.search(t):
        return 5  # «уже иду» — всё равно нужно время на приготовление
    if "полчаса" in t or "півгодини" in t:
        return 30
    if "полтора часа" in t or "півтори години" in t:
        return 90

    # цифрами
    m = _RE_HOURS.search(t)
    if m:
        return ok(int(m.group(1)) * 60)
    m = _RE_MINS.search(t) or _RE_MINS_REV.search(t) or _RE_AFTER.search(t)
    if m:
        return ok(int(m.group(1)))

    # словами: «через три минуты», «минут через пятнадцать», «через час»
    words = re.findall(r"[а-яa-zіїєґ']+", t)
    n = None
    for i, w in enumerate(words):
        if w in _NUM_WORDS:
            n = _NUM_WORDS[w]
            # «двадцать пять» — десятки плюс единицы
            if n >= 20 and i + 1 < len(words) and _NUM_WORDS.get(words[i + 1], 99) < 10:
                n += _NUM_WORDS[words[i + 1]]
            break
    has_hour = bool(_RE_HOUR_WORD.search(t))
    has_min = any(w.startswith(("мин", "хв")) for w in words)
    if n is None:
        return 60 if has_hour else None  # «через час» без числительного
    if has_hour:
        return ok(n * 60)
    # голое числительное принимаем только в коротком ответе — «десять», «минут пятнадцать»
    if not has_min and "через" not in t and len(words) > 3:
        return None
    return ok(n)


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
    context.user_data.pop("awaiting_time", None)
    # баннер-приветствие: шлём один раз при /start, если файл лежит в репозитории
    banner = CFG.get("banner")
    if banner and os.path.exists(banner):
        try:
            with open(banner, "rb") as _b:
                await context.bot.send_photo(update.effective_chat.id, _b)
        except Exception as e:
            logger.warning("Баннер не отправился: %s", e)
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
    if ai.ENABLED:  # подсказываем, что можно просто писать в чат
        text += "\n\n💬 Есть вопрос — напишите прямо сюда, отвечу."
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=kb)


async def cb_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data.pop("awaiting_time", None)
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
    # вышли из «когда заберёте» — числа в тексте больше не считаем временем
    context.user_data.pop("awaiting_time", None)
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
         InlineKeyboardButton("через 30 мин", callback_data="t:30")],
    ])


# Подсказка про свободный ввод: кнопки покрывают 90% случаев, остальные — текстом.
TIME_HINT = "Когда заберёте?\n\nИли напишите своими словами: «через 7 минут», «в 14:30»"


async def cb_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not context.user_data.get("cart"):
        await q.answer("Корзина пуста", show_alert=True)
        return
    await q.answer()
    context.user_data["awaiting_time"] = True
    await q.edit_message_text(TIME_HINT, reply_markup=time_keyboard())


async def cb_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    last = db.last_order_items(update.effective_user.id)
    if not last:
        await q.answer("Истории пока нет", show_alert=True)
        return
    await q.answer()
    context.user_data["cart"] = [dict(x) for x in last]  # копия позиций последнего заказа
    context.user_data["awaiting_time"] = True
    total = cart_total(context.user_data["cart"])
    lines = "\n".join(f"• {line_label(x)} — {x['price']} {CUR}" for x in context.user_data["cart"])
    await q.edit_message_text(
        f"Повторяем заказ:\n{lines}\n\nИтого {total} {CUR}\n{TIME_HINT}",
        reply_markup=time_keyboard(),
    )


def confirm_screen(context, mins):
    """Экран подтверждения. Общий для кнопок и для времени, написанного текстом."""
    ready = datetime.now(timezone.utc) + timedelta(minutes=mins)
    context.user_data["ready_at"] = ready.isoformat()
    cart = context.user_data["cart"]
    total = cart_total(cart)
    lines = "\n".join(f"• {line_label(x)} — {x['price']} {CUR}" for x in cart)
    text = (f"{lines}\n\nИтого: {total} {CUR}\n"
            f"🕗 К {hhmm(ready)} · оплата на кассе при получении")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="ok")],
        [InlineKeyboardButton("⬅️ Другое время", callback_data="co")],
    ])
    return text, kb


async def cb_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not context.user_data.get("cart"):
        await q.edit_message_text("Корзина пуста. Начните заново: /start")
        return
    text, kb = confirm_screen(context, int(q.data.split(":")[1]))
    await q.edit_message_text(text, reply_markup=kb)


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
    context.user_data.pop("awaiting_time", None)
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
    """Свободный текст: сначала пробуем прочитать в нём время, потом отвечает ИИ."""
    text = (update.message.text or "").strip()
    uid = update.effective_user.id
    waiting = bool(context.user_data.get("awaiting_time") and context.user_data.get("cart"))

    # 1) гость называет время своими словами вместо кнопки
    if waiting:
        mins = parse_minutes(text, datetime.now(TZ))
        if mins:
            body, kb = confirm_screen(context, mins)
            await update.message.reply_text(body, reply_markup=kb)
            return

    # 2) всё остальное — вопрос консультанту
    if ai.ENABLED:
        if not ai.limit_ok(uid):
            await update.message.reply_text(
                "На сегодня вопросов достаточно 🙂 Загляните завтра, "
                "а заказ можно оформить прямо сейчас — «📋 Меню».")
            return
        answer = ""
        try:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            answer = await ai.ask(text, CFG)
            ai.record(uid)
        except Exception as e:  # ИИ упал — гость не должен этого заметить
            logger.warning("Консультант не ответил: %s", e)
        if answer:
            if waiting:  # не теряем незаконченный заказ
                answer += f"\n\n{TIME_HINT}"
                kb = time_keyboard()
            else:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Меню", callback_data="menu")],
                    [InlineKeyboardButton("⬅️ Главный экран", callback_data="main")],
                ])
            await update.message.reply_text(answer, reply_markup=kb)
            return

    # 3) ИИ выключен или не ответил — показываем кнопки, чтобы не оставлять в тупике
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

    logger.info("Кофейный бот «%s» запущен. Консультант: %s",
                CFG.get("name"), "включён" if ai.ENABLED else "выключен (нет AI_API_KEY)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
