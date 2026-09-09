# Кофейный Telegram-бот

Одна кодовая база на несколько кофеен. Всё, что меняется от заведения к заведению
(название, меню, цены, часы, адрес, chat_id владельца и группы баристы), лежит
в `shops/<SHOP>.json`. В коде хардкодов нет.

## Файлы
- `bot.py` — движок: хендлеры, меню, заказ, штампы, кнопки баристы
- `db.py` — SQLite (пользователи, заказы, позиции, штампы)
- `jobs.py` — фоновая задача: неявки
- `shops/demo.json` — конфиг конкретной кофейни (пример с меню)
- `requirements.txt`, `Procfile`

## Переменные окружения
| Переменная | Обязательна | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | да | токен бота от @BotFather |
| `SHOP` | нет | какой конфиг грузить, по умолчанию `demo` (файл `shops/demo.json`) |
| `DB_PATH` | нет | путь к базе, по умолчанию `coffee.db`. На Railway → `/data/coffee.db` |
| `OWNER_CHAT_ID` | нет | перекрывает `owner_chat_id` из json |
| `BARISTA_CHAT_ID` | нет | перекрывает `barista_chat_id` из json |

## Настройка заведения
1. В `shops/demo.json` впиши свои `name`, `greeting`, `address`, `hours`, меню и цены.
2. Укажи `owner_chat_id` — твой Telegram ID (узнать: @userinfobot).
3. Создай группу для баристы, добавь бота, узнай её `chat_id` (для супергрупп он
   отрицательный, вида `-100...`) и впиши в `barista_chat_id`.
4. Новую кофейню = новый файл `shops/coffeename.json` + переменная `SHOP=coffeename`.

## Деплой на Railway (через GitHub)
1. Залей эти файлы в репозиторий на GitHub.
2. Railway → New Project → Deploy from GitHub repo → выбери репозиторий.
3. Variables → добавь `TELEGRAM_BOT_TOKEN` (и при желании `OWNER_CHAT_ID`,
   `BARISTA_CHAT_ID`, `SHOP`).
4. **Volume (обязательно, иначе база стирается при редеплое):** добавь Volume,
   смонтируй на `/data`, добавь переменную `DB_PATH=/data/coffee.db`.
5. Дождись билда. В логах: «Кофейный бот ... запущен». Открой бота, `/start`.

## Метки источников
`t.me/ВАШ_БОТ?start=ig_bio` — при первом визите источник пишется в базу.
Пустой → `direct`, нераспознанный (нет в `known_sources`) → `unknown`.
Список распознаваемых меток — в `known_sources` конфига.
