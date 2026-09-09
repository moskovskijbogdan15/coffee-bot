# -*- coding: utf-8 -*-
"""db.py — работа с SQLite. Никакой бизнес-логики о заведении здесь нет."""
import os
import sqlite3
from datetime import datetime, timezone

# Путь к базе. На Railway файловая система ЭФЕМЕРНАЯ: без volume база (заказы,
# штампы, неявки) сотрётся при каждом редеплое. На Railway создай Volume,
# смонтируй его на /data и задай переменную DB_PATH=/data/coffee.db
DB_PATH = os.environ.get("DB_PATH", "coffee.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    """Создаёт таблицы, если их ещё нет. Обязательно вызывать при старте."""
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id  INTEGER PRIMARY KEY,
                username TEXT,
                source   TEXT,
                no_shows INTEGER NOT NULL DEFAULT 0,
                prepay   INTEGER NOT NULL DEFAULT 0,
                created  TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                ready_at TEXT NOT NULL,
                status   TEXT NOT NULL DEFAULT 'new',
                total    INTEGER NOT NULL,
                ts       TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER NOT NULL,
                item     TEXT NOT NULL,
                size     TEXT,
                milk     TEXT,
                price    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stamps (
                user_id INTEGER PRIMARY KEY,
                count   INTEGER NOT NULL DEFAULT 0,
                updated TEXT
            );
            """
        )


# ---------- пользователи ----------
def normalize_source(raw, known_sources):
    """Пустой источник -> 'direct'; известный -> как есть; иначе -> 'unknown'."""
    raw = (raw or "").strip()
    if not raw:
        return "direct"
    if raw in known_sources:
        return raw
    return "unknown"


def ensure_user(user_id, username, source_raw, known_sources):
    """Регистрирует пользователя при первом визите.
    Возвращает (is_new: bool, source: str|None)."""
    with _conn() as c:
        row = c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            return False, None
        source = normalize_source(source_raw, known_sources)
        c.execute(
            "INSERT INTO users(user_id, username, source, created) VALUES(?,?,?,?)",
            (user_id, username, source, _utc_now()),
        )
        c.execute(
            "INSERT OR IGNORE INTO stamps(user_id, count, updated) VALUES(?,0,?)",
            (user_id, _utc_now()),
        )
        return True, source


def set_prepay(user_id, value):
    with _conn() as c:
        c.execute("UPDATE users SET prepay=? WHERE user_id=?", (1 if value else 0, user_id))


def inc_no_shows(user_id):
    """+1 к счётчику неявок, возвращает новое значение."""
    with _conn() as c:
        c.execute("UPDATE users SET no_shows = no_shows + 1 WHERE user_id=?", (user_id,))
        return c.execute("SELECT no_shows FROM users WHERE user_id=?", (user_id,)).fetchone()[0]


def reset_no_shows(user_id):
    """Серия неявок прерывается при удачном получении заказа."""
    with _conn() as c:
        c.execute("UPDATE users SET no_shows = 0 WHERE user_id=?", (user_id,))


# ---------- заказы ----------
def create_order(user_id, ready_at_utc, items, total):
    """items: список dict(item,size,milk,price). Возвращает id нового заказа."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO orders(user_id, ready_at, status, total, ts) VALUES(?,?,'new',?,?)",
            (user_id, ready_at_utc, total, _utc_now()),
        )
        oid = cur.lastrowid
        c.executemany(
            "INSERT INTO order_items(order_id, item, size, milk, price) VALUES(?,?,?,?,?)",
            [(oid, it["item"], it.get("size"), it.get("milk"), it["price"]) for it in items],
        )
        return oid


def get_order(order_id):
    """Возвращает (order_row|None, [item_rows])."""
    with _conn() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            return None, []
        items = c.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
        return o, items


def set_order_status(order_id, status):
    with _conn() as c:
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))


def last_order_items(user_id):
    """Позиции последнего заказа пользователя (для кнопки «Повторить»)."""
    with _conn() as c:
        o = c.execute(
            "SELECT id FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if not o:
            return []
        rows = c.execute(
            "SELECT item, size, milk, price FROM order_items WHERE order_id=?", (o["id"],)
        ).fetchall()
        return [dict(r) for r in rows]


def new_orders():
    """Все заказы в статусе new (для фоновой проверки неявок)."""
    with _conn() as c:
        return c.execute(
            "SELECT id, user_id, ready_at FROM orders WHERE status='new'"
        ).fetchall()


# ---------- штампы ----------
def add_stamp(user_id, needed):
    """+1 штамп. Если достигли needed — сбрасываем счётчик и возвращаем free=True.
    Возвращает (count_после, free: bool)."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO stamps(user_id, count, updated) VALUES(?,0,?)",
            (user_id, _utc_now()),
        )
        count = c.execute("SELECT count FROM stamps WHERE user_id=?", (user_id,)).fetchone()[0]
        count += 1
        free = False
        if count >= needed:
            free = True
            count = 0
        c.execute(
            "UPDATE stamps SET count=?, updated=? WHERE user_id=?",
            (count, _utc_now(), user_id),
        )
        return count, free


def get_stamps(user_id):
    with _conn() as c:
        r = c.execute("SELECT count FROM stamps WHERE user_id=?", (user_id,)).fetchone()
        return r["count"] if r else 0
