# -*- coding: utf-8 -*-
"""ai.py — ИИ-консультант кофейни.

Отвечает на свободный текст гостя: что есть в меню, из чего напиток, сколько
стоит, где мы, как работают штампы. Модель — бесплатная Groq (OpenAI-совместимый
API), поэтому весь модуль — один POST-запрос.

Ключ живёт только в переменной окружения AI_API_KEY. Нет ключа — модуль тихо
выключается (ENABLED = False), бот продолжает работать на кнопках как раньше.
Ни названий, ни цен здесь нет: промпт целиком собирается из shops/<SHOP>.json,
поэтому для новой кофейни менять код не нужно.
"""
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

API_KEY = os.environ.get("AI_API_KEY", "").strip()
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
MODEL = os.environ.get("AI_MODEL", "openai/gpt-oss-20b").strip()
PER_DAY = int(os.environ.get("AI_DAILY_LIMIT", "30") or 30)
ENABLED = bool(API_KEY)

_usage: dict = {}  # {user_id: [datetime, ...]} — в памяти, обнуляется при рестарте


# ---------------------------------------------------------------------------
# ЛИМИТ НА ГОСТЯ
# ---------------------------------------------------------------------------
def limit_ok(uid: int) -> bool:
    """Не больше PER_DAY вопросов в сутки — чтобы один человек не сжёг весь лимит."""
    now = datetime.now()
    hist = [t for t in _usage.get(uid, []) if now - t < timedelta(days=1)]
    _usage[uid] = hist
    return len(hist) < PER_DAY


def record(uid: int) -> None:
    _usage.setdefault(uid, []).append(datetime.now())


# ---------------------------------------------------------------------------
# ПРОМПТ ИЗ КОНФИГА ЗАВЕДЕНИЯ
# ---------------------------------------------------------------------------
def _menu_text(cfg: dict) -> str:
    cur = cfg.get("currency", "грн")
    out = []
    for cat in cfg.get("menu", []):
        out.append(cat.get("title", ""))
        for it in cat.get("items", []):
            if "price" in it:
                out.append(f"- {it['name']}: {it['price']} {cur}")
            else:
                sizes = ", ".join(f"{lbl} — {p} {cur}" for lbl, p in it.get("sizes", []))
                milk = " (можно выбрать молоко)" if it.get("milk") else ""
                out.append(f"- {it['name']}: {sizes}{milk}")
    return "\n".join(out)


def _milk_text(cfg: dict) -> str:
    cur = cfg.get("currency", "грн")
    parts = [f"{name}" + (f" (+{extra} {cur})" if extra else " (без доплаты)")
             for name, extra in cfg.get("milk_options", [])]
    return ", ".join(parts) if parts else "—"


def system_prompt(cfg: dict) -> str:
    tz = ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
    now = datetime.now(tz).strftime("%H:%M, %d.%m")
    stamps = int(cfg.get("stamps_needed", 6))
    return (
        f"Ты — вежливый консультант кофейни «{cfg.get('name', 'Кофейня')}» в Telegram-боте.\n"
        "Отвечай по-русски, тепло и коротко: 2–4 предложения. Обычный текст, "
        "без markdown, без списков и без звёздочек.\n"
        "Говори ТОЛЬКО про эту кофейню: напитки, состав, размеры, цены, молоко, "
        "выпечка, адрес, часы работы, самовывоз и штампы. На посторонние темы "
        "мягко возвращай разговор к кофе.\n"
        "НИКОГДА не выдумывай позиции, цены и добавки, которых нет в меню ниже. "
        "Если чего-то нет — честно скажи и предложи ближайшую замену из меню.\n"
        "Работаем только на самовывоз, доставки нет. Оплата на кассе при получении.\n"
        "Заказ оформляется кнопками — подскажи нажать «📋 Меню». Время получения "
        "можно написать словами: «через 7 минут» или «в 14:30».\n\n"
        f"АДРЕС: {cfg.get('address', '')}\n"
        f"ЧАСЫ РАБОТЫ: {cfg.get('hours', '')}\n"
        f"СЕЙЧАС: {now} (местное время кофейни)\n"
        f"МОЛОКО НА ВЫБОР: {_milk_text(cfg)}\n"
        f"ШТАМПЫ: за каждый выданный заказ +1 штамп, "
        f"{stamps} штампов — следующий напиток бесплатно.\n\n"
        f"МЕНЮ И ЦЕНЫ:\n{_menu_text(cfg)}"
    )


# ---------------------------------------------------------------------------
# ЗАПРОС К МОДЕЛИ
# ---------------------------------------------------------------------------
async def ask(question: str, cfg: dict) -> str:
    """Один вопрос — один ответ. Исключения ловит вызывающий код."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt(cfg)},
                    {"role": "user", "content": (question or "")[:600]},
                ],
                "max_tokens": 350,
                "temperature": 0.4,
            },
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
