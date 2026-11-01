import asyncio
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Update, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.exceptions import TelegramBadRequest
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nft-offer-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TELETHON_SESSION = os.environ["TELETHON_SESSION"]
DB_PATH = os.getenv("DB_PATH", "/tmp/offers.sqlite3")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "/telegram-webhook"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}" if RENDER_URL else ""

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
user_client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
db_lock = asyncio.Lock()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS business_chats (
        chat_id INTEGER PRIMARY KEY, business_connection_id TEXT NOT NULL,
        can_reply INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
        business_connection_id TEXT NOT NULL, nft_url TEXT NOT NULL,
        amount TEXT NOT NULL, status TEXT NOT NULL, expires_at INTEGER NOT NULL,
        message_id INTEGER
    )""")
    conn.commit()
    return conn

def valid_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def offer_keyboard(offer_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Принять", callback_data=f"accept:{offer_id}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"reject:{offer_id}")
    ]])

@dp.business_message()
async def remember_business_chat(message: Message):
    bc = message.business_connection_id
    if not bc or not message.chat or message.chat.type != "private":
        return
    # A received business message is evidence that this chat is managed.
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO business_chats(chat_id,business_connection_id,can_reply,updated_at) VALUES(?,?,1,?)",
        (message.chat.id, bc, int(time.time()))
    )
    conn.commit()
    conn.close()

@dp.callback_query(F.data.startswith(("accept:", "reject:")))
async def handle_offer_choice(query: CallbackQuery):
    try:
        action, raw_id = query.data.split(":", 1)
        offer_id = int(raw_id)
    except Exception:
        await query.answer("Некорректная кнопка", show_alert=True)
        return

    conn = db()
    row = conn.execute(
        "SELECT chat_id,business_connection_id,status,expires_at FROM offers WHERE id=?",
        (offer_id,)
    ).fetchone()
    if not row:
        conn.close()
        await query.answer("Оффер не найден", show_alert=True)
        return
    chat_id, bc_id, status, expires_at = row
    if status != "pending":
        conn.close()
        await query.answer("Этот оффер уже завершён", show_alert=True)
        return
    if int(time.time()) >= expires_at:
        conn.execute("UPDATE offers SET status='expired' WHERE id=?", (offer_id,))
        conn.commit()
        conn.close()
        await query.answer("Срок действия оффера истёк", show_alert=True)
        try:
            await query.message.edit_text("⏰ Срок действия оффера истёк.")
        except Exception:
            pass
        return

    new_status = "accepted" if action == "accept" else "rejected"
    conn.execute("UPDATE offers SET status=? WHERE id=?", (new_status, offer_id))
    conn.commit()
    conn.close()
    await query.answer("Вы приняли оффер" if action == "accept" else "Вы отклонили оффер")
    try:
        await query.message.edit_text(
            ("✅ Покупатель принял оффер." if action == "accept"
             else "❌ Покупатель отклонил оффер.")
        )
    except Exception:
        pass
    # This is only an acceptance/rejection record. It does NOT transfer Stars or NFT.
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=("Покупатель принял оффер. Передача NFT и Stars автоматически не выполнена."
                  if action == "accept" else "Покупатель отклонил оффер.")
        )
    except Exception:
        log.exception("Could not notify account owner")

async def expire_offers():
    while True:
        try:
            now = int(time.time())
            conn = db()
            rows = conn.execute(
                "SELECT id,chat_id,business_connection_id,message_id FROM offers WHERE status='pending' AND expires_at<=?",
                (now,)
            ).fetchall()
            conn.execute("UPDATE offers SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
            conn.commit()
            conn.close()
            for offer_id, chat_id, bc_id, message_id in rows:
                if message_id:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id, message_id=message_id,
                            text="⏰ Срок действия оффера истёк.",
                            business_connection_id=bc_id
                        )
                    except Exception:
                        pass
        except Exception:
            log.exception("Expiry loop error")
        await asyncio.sleep(60)

async def process_buy_command(event):
    if not event.is_private:
        return
    raw = (event.raw_text or "").strip()
    # Exact format: /buy https://... 25
    match = re.match(r"^/buy(?:@\w+)?\s+(\S+)\s+([0-9]+(?:[.,][0-9]{1,2})?)$", raw)
    if not match:
        return
    nft_url, amount = match.groups()
    amount = amount.replace(",", ".")
    if not valid_url(nft_url):
        await event.respond("Не удалось распознать ссылку. Формат: /buy https://ссылка-на-NFT 25")
        return

    # Remove the command from the buyer chat where possible.
    try:
        await event.delete()
    except Exception:
        pass

    chat_id = int(event.chat_id)
    conn = db()
    row = conn.execute(
        "SELECT business_connection_id,can_reply FROM business_chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    conn.close()
    if not row or not row[0] or not row[1]:
        await event.respond(
            "Не могу отправить оффер: этот чат ещё не зарегистрирован Business-ботом. "
            "Убедись, что покупатель недавно написал тебе, чат разрешён в Telegram Business "
            "и у бота есть право отвечать."
        )
        return

    bc_id = row[0]
    expires = int(time.time()) + 6 * 60 * 60
    conn = db()
    cur = conn.execute(
        "INSERT INTO offers(chat_id,business_connection_id,nft_url,amount,status,expires_at) VALUES(?,?,?,?,?,?)",
        (chat_id, bc_id, nft_url, amount, "pending", expires)
    )
    offer_id = cur.lastrowid
    conn.commit()
    conn.close()

    text = (
        "💎 Предложение NFT\n\n"
        f"NFT: {nft_url}\n"
        f"Сумма: {amount} Stars\n\n"
        "Предложение действительно 6 часов."
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=offer_keyboard(offer_id),
            business_connection_id=bc_id
        )
        conn = db()
        conn.execute("UPDATE offers SET message_id=? WHERE id=?", (sent.message_id, offer_id))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.exception("Business message send failed")
        await event.respond(
            "Не удалось отправить оффер через Telegram Business. Проверь подключение бота, "
            "право can_reply и ограничения Telegram для этого чата."
        )

@user_client.on(events.NewMessage(outgoing=True))
async def outgoing_message_handler(event):
    try:
        if (event.raw_text or "").strip().startswith("/buy"):
            await process_buy_command(event)
    except Exception:
        log.exception("Could not process outgoing /buy command")

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_conn = db()
    db_conn.close()
    if WEBHOOK_URL:
        await bot.set_webhook(
            WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["business_message", "callback_query"]
        )
    await user_client.start()
    expiry_task = asyncio.create_task(expire_offers())
    log.info("Bot API and Telegram user client started")
    try:
        yield
    finally:
        expiry_task.cancel()
        await user_client.disconnect()
        await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    return {"status": "ok"}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
