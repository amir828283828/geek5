import logging
import re
import sqlite3
import uuid
from html import escape
from urllib.parse import urlparse
from datetime import datetime, timedelta

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

CHANNEL_USERNAME = "@sosaworking"
TON_WALLET = "UQBsdCC3QfN8OUim2ym5mKu9lx21F_X7DKA_xnVUWDymW0VR"
PRICE_NANOTON = 5_000_000_000
TONAPI_KEY = "AG3OQIBPC7OZYLAAAAAFHTLPFKSTBTOKP6LLJ2E7NETOTH6JL7GQWQIDEVIAQ7MD6HYCXQY"
BOT_TOKEN = "9078325056:AAHmwt_XpTLrvcu9nd6Kc15huT38xAQDxus"
OWNER_ID = 9041049277
DB_NAME = "offers.sqlite3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def connect_db():
    return sqlite3.connect(DB_NAME)


def register_user(user):
    if not user:
        return
    with connect_db() as con:
        con.execute("""
            INSERT INTO users (user_id, username, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name
        """, (user.id, user.username, user.full_name))


def init_db():
    with connect_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                username TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                display_name TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS twins (
                main_user_id INTEGER PRIMARY KEY,
                twin_user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS access (
                user_id INTEGER PRIMARY KEY,
                paid_until TIMESTAMP,
                tx_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                price INTEGER NOT NULL,
                gift_url TEXT NOT NULL,
                status TEXT NOT NULL,
                buyer_username TEXT,
                currency TEXT DEFAULT 'stars'
            )
        """)
        columns = [row[1] for row in con.execute("PRAGMA table_info(offers)").fetchall()]
        if "buyer_username" not in columns:
            con.execute("ALTER TABLE offers ADD COLUMN buyer_username TEXT")
        if "currency" not in columns:
            con.execute("ALTER TABLE offers ADD COLUMN currency TEXT DEFAULT 'stars'")


def valid_tme_link(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc.lower() in {"t.me", "www.t.me"}


def save_contact(username, chat_id, user_id, connection_id, display_name):
    with connect_db() as con:
        con.execute("""
            INSERT OR REPLACE INTO contacts
            (username, chat_id, user_id, connection_id, display_name)
            VALUES (?, ?, ?, ?, ?)
        """, (username.lower(), chat_id, user_id, connection_id, display_name))


def find_contact(username):
    with connect_db() as con:
        return con.execute("""
            SELECT chat_id, user_id, connection_id, display_name
            FROM contacts WHERE username = ?
        """, (username.lower(),)).fetchone()


async def has_access(user_id: int) -> bool:
    with connect_db() as con:
        # Проверяем самого пользователя
        row = con.execute("SELECT paid_until FROM access WHERE user_id = ?", (user_id,)).fetchone()
        if row and row[0]:
            if datetime.utcnow() < datetime.fromisoformat(row[0]):
                return True
        # Проверяем, не является ли он твинком
        twin_row = con.execute("SELECT main_user_id FROM twins WHERE twin_user_id = ?", (user_id,)).fetchone()
        if twin_row:
            main_id = twin_row[0]
            main_row = con.execute("SELECT paid_until FROM access WHERE user_id = ?", (main_id,)).fetchone()
            if main_row and main_row[0]:
                return datetime.utcnow() < datetime.fromisoformat(main_row[0])
    return False


async def get_main_keyboard(user_id: int):
    with connect_db() as con:
        twin = con.execute("SELECT twin_user_id FROM twins WHERE main_user_id = ?", (user_id,)).fetchone()
    buttons = [
        [InlineKeyboardButton("Подписка", callback_data="subscribe")],
        [InlineKeyboardButton("Инструкция", callback_data="instruction")],
    ]
    if twin:
        buttons.append([InlineKeyboardButton("🔓 Отвязать твинк", callback_data="unlink_twin")])
    else:
        buttons.append([InlineKeyboardButton("🔗 Привязать твинк", callback_data="link_twin")])
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)

    is_subscribed = await check_subscription(context, user.id)
    if not is_subscribed:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подписаться на канал", url="https://t.me/sosaworking")],
            [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")]
        ])
        await update.effective_message.reply_text(
            "👋 Для использования бота необходимо быть подписанным на канал!",
            reply_markup=keyboard
        )
        return

    keyboard = await get_main_keyboard(user.id)
    await update.effective_message.reply_photo(
        photo=open("welcome.png", "rb"),
        caption="Добро пожаловать! Ты успешно зарегистрирован.",
        reply_markup=keyboard,
    )


async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False


async def check_sub_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    is_subscribed = await check_subscription(context, user_id)

    if is_subscribed:
        await query.edit_message_text(
            "✅ Отлично! Ты подписан на канал.\n\nТеперь можешь пользоваться ботом.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Продолжить", callback_data="back_to_start")]])
        )
    else:
        await query.edit_message_text(
            "❌ Ты всё ещё не подписан на канал\n\nПодпишись и нажми кнопку ниже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Подписаться", url="https://t.me/sosaworking")],
                [InlineKeyboardButton("✅ Проверить ещё раз", callback_data="check_sub")]
            ])
        )


async def instruction_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = (
        "<b>Как отправить оффер:</b>\n\n"
        "В чате с пользователем напиши:\n"
        "<code>жз ссылка сумма зв</code>\n"
        "или\n"
        "<code>жз ссылка сумма грам</code>\n\n"
        "Примеры:\n"
        "<code>жз https://t.me/nft/... 790 зв</code>\n"
        "<code>жз https://t.me/nft/... 5 грам</code>\n\n"
        "После отправки бот удалит твоё сообщение и отправит оффер."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back_to_start")]])
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)


async def link_twin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    with connect_db() as con:
        existing = con.execute("SELECT twin_user_id FROM twins WHERE main_user_id = ?", (user_id,)).fetchone()
    if existing:
        await query.answer("У тебя уже есть привязанный твинк", show_alert=True)
        return

    try:
        await query.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Отправь user_id твинка, которого хочешь привязать.\n\n"
             "Чтобы узнать user_id — пусть твинк напишет боту /start",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Отмена", callback_data="back_to_start")
        ]])
    )
    context.user_data["waiting_twin_id"] = True


async def unlink_twin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    with connect_db() as con:
        con.execute("DELETE FROM twins WHERE main_user_id = ?", (user_id,))

    try:
        await query.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="✅ Твинк успешно отвязан."
    )

    # Возвращаем в главное меню
    keyboard = await get_main_keyboard(user_id)
    await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=open("welcome.png", "rb"),
        caption="Добро пожаловать! Ты успешно зарегистрирован.",
        reply_markup=keyboard
    )

async def handle_twin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_twin_id"):
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    try:
        twin_id = int(text)
    except ValueError:
        await update.message.reply_text("Некорректный user_id. Отправь число.")
        return

    if twin_id == user_id:
        await update.message.reply_text("Нельзя привязать самого себя.")
        return

    with connect_db() as con:
        existing = con.execute("SELECT main_user_id FROM twins WHERE twin_user_id = ?", (twin_id,)).fetchone()
        if existing:
            await update.message.reply_text("Этот аккаунт уже привязан к другому пользователю.")
            return
        con.execute("INSERT INTO twins (main_user_id, twin_user_id) VALUES (?, ?)", (user_id, twin_id))

    context.user_data["waiting_twin_id"] = False
    await update.message.reply_text(f"✅ Твинк {twin_id} успешно привязан.")


async def business_offer_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message or not message.from_user or message.chat.type != "private" or not message.business_connection_id:
        return

    if not await has_access(message.from_user.id):
        try:
            await context.bot.send_message(
                chat_id=message.chat.id,
                text="❌ У тебя нет активной подписки. Оформи её в боте, чтобы кидать офферы.",
                business_connection_id=message.business_connection_id
            )
        except Exception:
            pass
        return

    command_text = (message.text or "").strip()
    parts = command_text.split()

    if not parts or parts[0].lower() != "жз":
        return

    async def notify_owner(text):
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=text)
        except Exception:
            log.exception("Не удалось уведомить владельца")

    if len(parts) != 4:
        await notify_owner("Формат: жз ссылка сумма зв/грам\nПример: жз https://t.me/nft/... 790 зв")
        return

    gift_url = parts[1]
    price_text = parts[2]
    currency = parts[3].lower()

    if currency not in ("зв", "грам", "звёзды", "звезды"):
        await notify_owner("Валюта должна быть 'зв' или 'грам'")
        return

    is_grams = currency in ("грам",)

    if not price_text.isdigit() or not (1 <= int(price_text) <= 1_000_000):
        await notify_owner("Некорректная цена.")
        return
    if not valid_tme_link(gift_url):
        await notify_owner("Некорректная ссылка.")
        return

    chat_id = message.chat.id
    user_id = message.chat.id
    connection_id = message.business_connection_id
    price = int(price_text)
    buyer_username = message.from_user.username or ""

    try:
        business = await context.bot.get_business_connection(connection_id)
        if not business.is_enabled or not business.rights or not business.rights.can_reply:
            raise ValueError("Нет прав или подключение отключено.")
    except Exception as exc:
        await notify_owner(f"Ошибка подключения: {exc}")
        return

    offer_id = uuid.uuid4().hex
    with connect_db() as con:
        con.execute("""
            INSERT INTO offers
            (id, username, chat_id, user_id, connection_id, price, gift_url, status, buyer_username, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (offer_id, "", chat_id, user_id, connection_id, price, gift_url, buyer_username, "grams" if is_grams else "stars"))

    sender_name = escape(message.from_user.full_name)
    safe_url = escape(gift_url, quote=True)
    gift_name = escape(gift_url.rstrip("/").split("/")[-1])

    emoji = '<tg-emoji emoji-id="5474161364664629753">💎</tg-emoji>' if is_grams else '<tg-emoji emoji-id="5321485469249198987">⭐</tg-emoji>'

    text = (
        f"<b>{sender_name}</b> хочет приобрести Ваш подарок "
        f'<a href="{safe_url}">{gift_name}</a> '
        f'за <b>{price}</b> {emoji}\n\n'
        'Сделка активна в течение 23ч 59м.'
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Отклонить", callback_data=f"decline:{offer_id}"),
        InlineKeyboardButton("Принять", callback_data=f"accept:{offer_id}"),
    ]])

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            business_connection_id=connection_id,
            link_preview_options=LinkPreviewOptions(
                url=gift_url, is_disabled=False, prefer_large_media=True, show_above_text=True,
            ),
        )
        try:
            await message.delete()
        except Exception:
            pass
        await notify_owner(f"Оффер отправлен. Цена: {price} {'Gram' if is_grams else '⭐'}")
    except Exception as exc:
        with connect_db() as con:
            con.execute("DELETE FROM offers WHERE id = ?", (offer_id,))
        await notify_owner(f"Ошибка отправки: {exc}")


async def admin_give_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Использование: /give_sub <user_id> <часы>")
        return
    try:
        target_id = int(context.args[0])
        hours = int(context.args[1])
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Некорректные данные.")
        return

    paid_until = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
    with connect_db() as con:
        con.execute("""
            INSERT INTO access (user_id, paid_until, tx_hash)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET paid_until = excluded.paid_until, tx_hash = 'admin_grant'
        """, (target_id, paid_until, "admin_grant"))
    await update.message.reply_text(f"✅ Подписка выдана пользователю {target_id} на {hours} ч.")


async def admin_revoke_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Использование: /revoke_sub <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный user_id")
        return
    with connect_db() as con:
        con.execute("DELETE FROM access WHERE user_id = ?", (target_id,))
    await update.message.reply_text(f"❌ Подписка у пользователя {target_id} забрана.")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text = (
        "Админ-панель\n\n"
        "/give_sub <user_id> <часы> — выдать подписку\n"
        "/revoke_sub <user_id> — забрать подписку\n\n"
        "Пример: /give_sub 123456789 24"
    )
    await update.message.reply_text(text)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.answer()
        return

    action, offer_id = parts
    with connect_db() as con:
        row = con.execute("""
            SELECT username, chat_id, user_id, connection_id, status, gift_url, price, buyer_username, currency
            FROM offers WHERE id = ?
        """, (offer_id,)).fetchone()

    if not row:
        await query.answer("Предложение не найдено.", show_alert=True)
        return

    username, chat_id, user_id, connection_id, status, gift_url, price, buyer_username, currency = row

    if query.from_user.id != user_id:
        await query.answer("Это предложение адресовано другому человеку.", show_alert=True)
        return
    if status != "pending":
        await query.answer("На это предложение уже ответили.", show_alert=True)
        return
    if action not in ("accept", "decline"):
        await query.answer()
        return

    new_status = "accepted" if action == "accept" else "declined"
    with connect_db() as con:
        result = con.execute("UPDATE offers SET status = ? WHERE id = ? AND status = 'pending'", (new_status, offer_id))
        if result.rowcount != 1:
            await query.answer("Уже ответили.", show_alert=True)
            return

    await query.answer("Оффер принят." if action == "accept" else "Оффер отклонён.")

    if action == "decline":
        response = "Предложение отклонено."
        keyboard = None
    else:
        gift_name = escape(gift_url.rstrip("/").split("/")[-1])
        safe_url = escape(gift_url, quote=True)
        emoji = '<tg-emoji emoji-id="5474161364664629753">💎</tg-emoji>' if currency == "grams" else '<tg-emoji emoji-id="5321485469249198987">⭐</tg-emoji>'
        response = (
            f'<tg-emoji emoji-id="6032937473162614352">🎉</tg-emoji> '
            f'<b>Ваш подарок</b> <a href="{safe_url}">{gift_name}</a> <b>успешно продан!</b>\n\n'
            f'• Передайте данный подарок покупателю для зачисления <b>{price}</b> {emoji} на ваш баланс.'
        )
        if buyer_username:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Передать", url=f"tg://send_gift?to={buyer_username}", icon_custom_emoji_id="5256143829672672750")
            ]])
        else:
            keyboard = None
            response += "\n\nНе удалось определить получателя."

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=response,
            parse_mode="HTML",
            reply_markup=keyboard,
            business_connection_id=connection_id,
            link_preview_options=LinkPreviewOptions(url=gift_url, is_disabled=False, prefer_large_media=True, show_above_text=True),
        )
    except Exception as e:
        log.exception("Ошибка отправки ответа")
        await context.bot.send_message(chat_id=OWNER_ID, text=f"Ошибка после ответа на оффер:\n{e}")


async def check_and_grant_access(user_id: int) -> bool:
    url = f"https://tonapi.io/v2/blockchain/accounts/{TON_WALLET}/transactions"
    headers = {"Authorization": f"Bearer {TONAPI_KEY}"}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, headers=headers, params={"limit": 30})
            data = r.json()
        except Exception:
            return False

    for tx in data.get("transactions", []):
        in_msg = tx.get("in_msg", {})
        if not in_msg:
            continue
        value = int(in_msg.get("value", 0))
        comment = in_msg.get("decoded_body", {}).get("text", "") or ""
        if value >= PRICE_NANOTON and str(user_id) in comment:
            paid_until = (datetime.utcnow() + timedelta(hours=24)).isoformat()
            with connect_db() as con:
                con.execute("""
                    INSERT INTO access (user_id, paid_until, tx_hash)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET paid_until = excluded.paid_until, tx_hash = excluded.tx_hash
                """, (user_id, paid_until, tx.get("hash", "")))
            return True
    return False


async def subscribe_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    try:
        await query.message.delete()
    except Exception:
        pass

    if await has_access(user.id):
        with connect_db() as con:
            # Сначала ищем свою подписку
            row = con.execute("SELECT paid_until FROM access WHERE user_id = ?", (user.id,)).fetchone()
            if not row or not row[0]:
                # Если своей нет — ищем через твинка
                twin_row = con.execute("SELECT main_user_id FROM twins WHERE twin_user_id = ?", (user.id,)).fetchone()
                if twin_row:
                    row = con.execute("SELECT paid_until FROM access WHERE user_id = ?", (twin_row[0],)).fetchone()

        if row and row[0]:
            paid_until = datetime.fromisoformat(row[0])
            remaining = paid_until - datetime.utcnow()
            hours = int(remaining.total_seconds() // 3600)
            minutes = int((remaining.total_seconds() % 3600) // 60)
            text = f"✅ У тебя есть активная подписка.\nОсталось: <b>{hours} ч. {minutes} мин.</b>"
        else:
            text = "✅ У тебя есть активная подписка (через твинк)."
    else:
        text = "❌ У тебя нет активной подписки."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Купить", callback_data="buy_subscription")],
        [InlineKeyboardButton("Назад", callback_data="back_to_start")]
    ])

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def buy_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    try:
        await query.message.delete()
    except Exception:
        pass

    text = (
        f"<b>Подписка на бота (24 часа)</b>\n\n"
        f"Стоимость: <b>5 Gram</b>\n"
        f"Адрес: <code>{TON_WALLET}</code>\n"
        f"Комментарий: <code>{user.id}</code>\n\n"
        f"Переведи указанную сумму с комментарием и нажми кнопку ниже."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Я оплатил", callback_data="check_payment")],
        [InlineKeyboardButton("Назад", callback_data="back_to_start")]
    ])
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)


async def check_payment_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await has_access(user_id):
        await query.edit_message_text("Доступ уже активирован.")
        return

    await query.edit_message_text("Проверяю оплату...")
    success = await check_and_grant_access(user_id)
    if success:
        await query.edit_message_text("✅ Оплата найдена! Доступ открыт на 24 часа.")
    else:
        await query.edit_message_text(
            "Оплата пока не найдена. Подожди 1-2 минуты и нажми ещё раз.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Проверить ещё раз", callback_data="check_payment")]])
        )


async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass

    keyboard = await get_main_keyboard(query.from_user.id)
    await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=open("welcome.png", "rb"),
        caption="Добро пожаловать! Ты успешно зарегистрирован.",
        reply_markup=keyboard
    )

async def capture_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if (
        not message
        or not message.business_connection_id
        or message.chat.type != "private"
        or not message.from_user
        or message.from_user.is_bot
        or not message.from_user.username
    ):
        return

    user = message.from_user
    save_contact(
        user.username,
        message.chat_id,
        user.id,
        message.business_connection_id,
        user.full_name,
    )

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_twin_input))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, business_offer_text))

    app.add_handler(CallbackQueryHandler(subscribe_button, pattern="^subscribe$"))
    app.add_handler(CallbackQueryHandler(check_payment_button, pattern="^check_payment$"))
    app.add_handler(CallbackQueryHandler(back_to_start, pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(buy_subscription, pattern="^buy_subscription$"))
    app.add_handler(CallbackQueryHandler(instruction_button, pattern="^instruction$"))
    app.add_handler(CallbackQueryHandler(check_sub_button, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(link_twin_button, pattern="^link_twin$"))
    app.add_handler(CallbackQueryHandler(unlink_twin_button, pattern="^unlink_twin$"))

    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("give_sub", admin_give_sub))
    app.add_handler(CommandHandler("revoke_sub", admin_revoke_sub))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, capture_business_message), group=1)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()