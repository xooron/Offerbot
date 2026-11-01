import asyncio
import html
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PUBLIC_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_PATH = f"/telegram/{WEBHOOK_SECRET}"
DB_PATH = os.environ.get("DB_PATH", "offers.sqlite3")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""
CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    username TEXT,
    last_seen INTEGER NOT NULL,
    UNIQUE(connection_id, chat_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    connection_id TEXT NOT NULL,
    nft_url TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at INTEGER NOT NULL
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    owner_name TEXT NOT NULL,
    nft_url TEXT NOT NULL,
    amount INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
)
""")
db.commit()

router = Router()
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
dp.include_router(router)


def safe_name(user):
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(p for p in parts if p) or "Пользователь"
    return name[:80]


def offer_keyboard(offer_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{offer_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{offer_id}"),
    ]])


@router.business_message()
async def remember_business_chat(message: Message):
    """Remember private chats that have sent a message to the connected Business account."""
    if not message.business_connection_id or message.chat.type != "private":
        return
    try:
        connection = await bot.get_business_connection(message.business_connection_id)
    except Exception:
        return
    if not connection.is_enabled:
        return

    # Ignore messages authored by the business account owner, if present.
    if message.from_user and message.from_user.id == connection.user.id:
        return

    sender = message.from_user
    display = safe_name(sender) if sender else (message.chat.full_name or "Покупатель")
    username = sender.username if sender else None

    db.execute("""
        INSERT INTO recipients
        (connection_id, owner_id, chat_id, display_name, username, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(connection_id, chat_id) DO UPDATE SET
            owner_id=excluded.owner_id,
            display_name=excluded.display_name,
            username=excluded.username,
            last_seen=excluded.last_seen
    """, (
        message.business_connection_id, connection.user.id, message.chat.id,
        display, username, int(time.time())
    ))
    db.commit()


@router.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я помощник для создания NFT-офферов в Telegram Business.\n\n"
        "Команда:\n/buy https://t.me/nft/Example 500\n\n"
        "После команды выбери покупателя из списка бизнес-чатов."
    )


@router.message(Command("buy"))
async def create_draft(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return await message.answer("Отправь команду мне в личный чат с ботом.")

    parts = (message.text or "").split()
    if len(parts) != 3:
        return await message.answer(
            "Формат команды:\n/buy https://t.me/nft/Example 500"
        )

    _, nft_url, amount_text = parts
    if not (nft_url.startswith("https://t.me/") or nft_url.startswith("https://telegram.me/")):
        return await message.answer("Укажи ссылку Telegram на подарок, начинающуюся с https://t.me/")
    if not amount_text.isdigit() or not 1 <= int(amount_text) <= 1000000:
        return await message.answer("Сумма должна быть целым числом от 1 до 1000000.")

    rows = db.execute("""
        SELECT DISTINCT connection_id, owner_id
        FROM recipients
        WHERE owner_id=?
        ORDER BY last_seen DESC
    """, (message.from_user.id,)).fetchall()

    if not rows:
        return await message.answer(
            "Пока нет доступных бизнес-чатов.\n"
            "Сначала подключи этого бота в Telegram → Настройки → Telegram Business → Чат-боты, "
            "разреши нужные чаты и дождись хотя бы одного входящего сообщения от покупателя."
        )

    # One connected Business account is supported per Telegram account.
    connection_id = rows[0]["connection_id"]
    draft_id = uuid.uuid4().hex[:12]
    db.execute("""
        INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)
    """, (draft_id, message.from_user.id, connection_id, nft_url,
          int(amount_text), int(time.time())))
    db.commit()

    recipients = db.execute("""
        SELECT id, display_name, username
        FROM recipients
        WHERE connection_id=? AND owner_id=?
        ORDER BY last_seen DESC LIMIT 20
    """, (connection_id, message.from_user.id)).fetchall()

    buttons = []
    for recipient in recipients:
        label = recipient["display_name"]
        if recipient["username"]:
            label += " @" + recipient["username"]
        buttons.append([InlineKeyboardButton(
            text=label[:60],
            callback_data=f"target:{draft_id}:{recipient['id']}"
        )])

    await message.answer(
        f"Оффер: {int(amount_text)} ⭐ Stars\n"
        f"Подарок: {html.escape(nft_url)}\n\n"
        "Выбери чат, куда отправить предложение:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("target:"))
async def choose_target(call: CallbackQuery):
    _, draft_id, recipient_id = call.data.split(":")
    draft = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    recipient = db.execute("SELECT * FROM recipients WHERE id=?", (int(recipient_id),)).fetchone()

    if not draft or not recipient:
        return await call.answer("Черновик или чат не найден.", show_alert=True)
    if call.from_user.id != draft["owner_id"]:
        return await call.answer("Это не ваш оффер.", show_alert=True)
    if recipient["connection_id"] != draft["connection_id"]:
        return await call.answer("Неверный бизнес-чат.", show_alert=True)

    try:
        connection = await bot.get_business_connection(draft["connection_id"])
    except Exception:
        return await call.answer("Не удалось проверить подключение Business.", show_alert=True)

    if not connection.is_enabled or not connection.rights or not connection.rights.can_reply:
        return await call.answer(
            "Business-подключение выключено или у бота нет права отвечать в этом чате.",
            show_alert=True
        )

    owner_name = safe_name(connection.user)
    offer_id = uuid.uuid4().hex[:16]
    expires_at = int(time.time()) + 6 * 60 * 60
    amount = draft["amount"]
    nft_url = draft["nft_url"]
    text = (
        f"Пользователь <b>{html.escape(owner_name)}</b> предлагает вам "
        f"<b>{amount} ⭐ Stars</b> за подарок\n\n"
        f"🎁 {html.escape(nft_url)}\n\n"
        "⏳ Оффер действителен ещё 5 ч. 59 мин."
    )

    try:
        sent = await bot.send_message(
            chat_id=recipient["chat_id"],
            business_connection_id=draft["connection_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=offer_keyboard(offer_id)
        )
    except Exception as e:
        return await call.answer(
            "Не получилось отправить оффер. Проверь права Business-бота и доступ к чату.",
            show_alert=True
        )

    db.execute("""
        INSERT INTO offers
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
    """, (
        offer_id, draft["connection_id"], recipient["chat_id"], sent.message_id,
        draft["owner_id"], owner_name, nft_url, amount, expires_at
    ))
    db.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
    db.commit()

    await call.message.edit_text("✅ Оффер отправлен выбранному покупателю.")
    await call.answer("Отправлено!")


async def close_offer(offer, status, text):
    cur = db.execute(
        "UPDATE offers SET status=? WHERE id=? AND status='active'",
        (status, offer["id"])
    )
    db.commit()
    if cur.rowcount != 1:
        return False
    try:
        await bot.edit_message_text(
            chat_id=offer["chat_id"],
            message_id=offer["message_id"],
            business_connection_id=offer["connection_id"],
            text=text
        )
    except Exception:
        pass
    return True


@router.callback_query(F.data.startswith("accept:"))
async def accept_offer(call: CallbackQuery):
    offer_id = call.data.split(":", 1)[1]
    offer = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not offer or offer["status"] != "active":
        return await call.answer("Оффер уже закрыт.", show_alert=True)
    if int(time.time()) >= offer["expires_at"]:
        await close_offer(offer, "expired", "⌛ Срок действия оффера истёк.")
        return await call.answer("Срок действия оффера истёк.", show_alert=True)
    if call.from_user.id == offer["owner_id"]:
        return await call.answer("Нельзя принять собственный оффер.", show_alert=True)

    success = await close_offer(
        offer, "accepted",
        "✅ Оффер принят.\n\nПередача подарка и оплата Stars должны быть завершены отдельно."
    )
    if not success:
        return await call.answer("Оффер уже закрыт.", show_alert=True)

    try:
        await bot.send_message(
            offer["owner_id"],
            f"Покупатель принял ваш оффер на {offer['amount']} ⭐ Stars.\n"
            "Передача подарка и оплата ещё не выполнены автоматически."
        )
    except Exception:
        pass

    await call.answer(
        "Вы приняли оффер. Передача подарка и оплата Stars должны быть завершены отдельно.",
        show_alert=True
    )


@router.callback_query(F.data.startswith("reject:"))
async def reject_offer(call: CallbackQuery):
    offer_id = call.data.split(":", 1)[1]
    offer = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not offer or offer["status"] != "active":
        return await call.answer("Оффер уже закрыт.", show_alert=True)
    if int(time.time()) >= offer["expires_at"]:
        await close_offer(offer, "expired", "⌛ Срок действия оффера истёк.")
        return await call.answer("Срок действия оффера истёк.", show_alert=True)

    success = await close_offer(offer, "rejected", "❌ Оффер отклонён.")
    if not success:
        return await call.answer("Оффер уже закрыт.", show_alert=True)
    await call.answer("Вы отклонили оффер.", show_alert=True)


async def expiration_worker():
    while True:
        now = int(time.time())
        rows = db.execute(
            "SELECT * FROM offers WHERE status='active' AND expires_at<=?",
            (now,)
        ).fetchall()
        for offer in rows:
            await close_offer(offer, "expired", "⌛ Срок действия оффера истёк.")
        await asyncio.sleep(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(
        url=PUBLIC_URL + WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET
    )
    expiry_task = asyncio.create_task(expiration_worker())
    yield
    expiry_task.cancel()
    await bot.delete_webhook()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return PlainTextResponse("NFT Offer Bot is running")


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
