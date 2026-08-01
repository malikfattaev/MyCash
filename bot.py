import logging
import os
import re
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation

import asyncpg
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

# Uzbekistan time (UTC+5) for showing history dates.
TASHKENT = timezone(timedelta(hours=5))

BTN_BALANCE = "Изменить баланс"
BTN_HISTORY = "История операций"
BTN_CLEAR = "Очистить историю"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_BALANCE)],
        [KeyboardButton(BTN_HISTORY)],
        [KeyboardButton(BTN_CLEAR)],
    ],
    resize_keyboard=True,
)

db_pool: asyncpg.Pool | None = None


# Database
async def init_db() -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                balance     NUMERIC NOT NULL DEFAULT 0,
                initialized BOOLEAN NOT NULL DEFAULT FALSE,
                state       TEXT
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id          BIGSERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                amount      NUMERIC NOT NULL,
                description TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions (user_id, id DESC);"
        )


# Helpers
def fmt_amount(amount: Decimal) -> str:
    """1000000 -> '1 000 000', 1234.5 -> '1 234.50'."""
    if amount == amount.to_integral_value():
        s = f"{int(amount):,}"
    else:
        s = f"{amount:,.2f}"
    return s.replace(",", " ")


def fmt_signed(amount: Decimal) -> str:
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{fmt_amount(abs(amount))} сум"


def parse_amount(text: str) -> Decimal | None:
    """Parse a bare number like '500000', '500 000' or '500000.50'."""
    cleaned = text.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_transaction(text: str):
    """'-50000 сум такси' -> (Decimal('-50000'), 'такси')."""
    m = re.match(r"^([+-]\s*[\d\s.,]+)\s*(.*)$", text.strip(), re.DOTALL)
    if not m:
        return None
    amount = parse_amount(m.group(1))
    if amount is None:
        return None
    desc = m.group(2).strip()
    # Drop a leading currency word if the user typed one.
    desc = re.sub(
        r"^(сум|so'?m|sum|uzs)\b[\s,:.]*",
        "",
        desc,
        flags=re.IGNORECASE,
    ).strip()
    return amount, desc


async def get_user(conn, user_id: int):
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    if user is None:
        await conn.execute(
            "INSERT INTO users (user_id, state) VALUES ($1, 'awaiting_initial_balance')",
            user_id,
        )
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    return user


# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with db_pool.acquire() as conn:
        user = await get_user(conn, user_id)
        if user["initialized"]:
            await update.message.reply_text(
                f"Твой баланс: {fmt_amount(user['balance'])} сум\n\n"
                "Пиши операции, например:\n"
                "-50000 такси\n"
                "+100000 скинул папа",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        await conn.execute(
            "UPDATE users SET state = 'awaiting_initial_balance' WHERE user_id = $1",
            user_id,
        )

    await update.message.reply_text(
        "Привет. Это MyCash, простой учёт денег.\n\n"
        "Напиши свой текущий баланс, чтобы начать. Например: 500000"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    async with db_pool.acquire() as conn:
        user = await get_user(conn, user_id)

        # First run: whatever the user types is treated as the starting balance.
        if not user["initialized"]:
            amount = parse_amount(text)
            if amount is None:
                await update.message.reply_text(
                    "Напиши число, свой текущий баланс. Например: 500000"
                )
                return
            await conn.execute(
                "UPDATE users SET balance = $1, initialized = TRUE, state = NULL "
                "WHERE user_id = $2",
                amount,
                user_id,
            )
            await update.message.reply_text(
                f"Стартовый баланс: {fmt_amount(amount)} сум\n\n"
                "Теперь пиши операции:\n"
                "-50000 такси\n"
                "+100000 скинул папа",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # Buttons.
        if text == BTN_BALANCE:
            await conn.execute(
                "UPDATE users SET state = 'awaiting_new_balance' WHERE user_id = $1",
                user_id,
            )
            await update.message.reply_text(
                "Напиши новый баланс числом. Например: 500000"
            )
            return

        if text == BTN_HISTORY:
            await send_history(update, conn, user_id)
            return

        if text == BTN_CLEAR:
            await ask_clear(update)
            return

        # Setting a new balance.
        if user["state"] == "awaiting_new_balance":
            amount = parse_amount(text)
            if amount is None:
                await update.message.reply_text(
                    "Не понял число. Напиши баланс, например: 500000"
                )
                return
            await conn.execute(
                "UPDATE users SET balance = $1, state = NULL WHERE user_id = $2",
                amount,
                user_id,
            )
            await update.message.reply_text(
                f"Баланс обновлён: {fmt_amount(amount)} сум",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        # Otherwise try to read it as an operation.
        parsed = parse_transaction(text)
        if parsed is None:
            await update.message.reply_text(
                "Не понял. Пиши операции так:\n"
                "-50000 такси\n"
                "+100000 скинул папа\n\n"
                "Или выбери кнопку ниже.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        amount, desc = parsed
        new_balance = user["balance"] + amount
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, description) "
            "VALUES ($1, $2, $3)",
            user_id,
            amount,
            desc or None,
        )
        await conn.execute(
            "UPDATE users SET balance = $1 WHERE user_id = $2",
            new_balance,
            user_id,
        )

    desc_part = f" {desc}" if desc else ""
    await update.message.reply_text(
        f"{fmt_signed(amount)}{desc_part}\n\n"
        f"Баланс: {fmt_amount(new_balance)} сум",
        reply_markup=MAIN_KEYBOARD,
    )


async def send_history(update: Update, conn, user_id: int) -> None:
    rows = await conn.fetch(
        "SELECT amount, description, created_at FROM transactions "
        "WHERE user_id = $1 ORDER BY id DESC LIMIT 50",
        user_id,
    )
    user = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)

    if not rows:
        await update.message.reply_text(
            "История пока пуста.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    blocks = []
    for r in rows:
        dt = r["created_at"].astimezone(TASHKENT).strftime("%d.%m %H:%M")
        line = fmt_signed(r["amount"])
        if r["description"]:
            line += f" {r['description']}"
        blocks.append(f"{line}\n{dt}")

    text = "История операций\n\n" + "\n\n".join(blocks)
    text += f"\n\nБаланс: {fmt_amount(user['balance'])} сум"

    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def ask_clear(update: Update) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, очистить", callback_data="clear_yes"),
                InlineKeyboardButton("Отмена", callback_data="clear_no"),
            ]
        ]
    )
    await update.message.reply_text(
        "Очистить всю историю операций? Баланс останется прежним.",
        reply_markup=keyboard,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "clear_yes":
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM transactions WHERE user_id = $1", query.from_user.id
            )
        await query.edit_message_text("История очищена.")
    else:
        await query.edit_message_text("Отменено.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused an error", exc_info=context.error)


# Lifecycle
async def on_startup(app: Application) -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    await init_db()
    logger.info("Database ready, bot started.")


async def on_shutdown(app: Application) -> None:
    if db_pool is not None:
        await db_pool.close()


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
