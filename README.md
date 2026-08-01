# MyCash 💰

Простой Telegram-бот для учёта личных финансов.

Пишешь операции обычными сообщениями — бот считает баланс автоматически:

```
-50000 такси
+100000 скинул папа
```

## Возможности

- При первом запуске бот спрашивает стартовый баланс.
- Расходы (`-`) и доходы (`+`) с описанием.
- Кнопка **💰 Изменить баланс** — задать баланс заново.
- Кнопка **📜 История операций** — последние операции и текущий баланс.

## Стек

- Python + [python-telegram-bot](https://python-telegram-bot.org/)
- PostgreSQL (asyncpg)
- Docker → деплой на [Railway](https://railway.app/)

## Переменные окружения

| Переменная     | Описание                              |
| -------------- | ------------------------------------- |
| `BOT_TOKEN`    | Токен бота от [@BotFather](https://t.me/BotFather) |
| `DATABASE_URL` | Строка подключения к PostgreSQL       |

## Локальный запуск

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
export DATABASE_URL=postgresql://user:pass@host:5432/db
python bot.py
```
