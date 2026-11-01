# Telegram NFT Offer Bot — MTProto test build

This version listens for your own outgoing `/buy URL AMOUNT` command in a private Telegram chat using Telethon, then asks the connected Telegram Business bot to send the offer in that same chat.

## Important limitations and safety
- This uses a Telegram **user account session** through MTProto. Treat `TELETHON_SESSION` like a password: never commit it, share it, or paste it into chat.
- Use this only on an account you own. Telegram may restrict automated behavior; comply with Telegram's terms and rate limits.
- The outgoing `/buy` command is sent to the other person before the client can delete it. Deletion is best-effort and the recipient may see it.
- The Business bot must already have received a `business_message` from that private chat, be connected to Telegram Business, and have permission to reply. Telegram's Business restrictions still apply.
- Accepting an offer only records acceptance. It does **not** transfer Stars or the NFT.
- Render Free may sleep after inactivity; this listener is not reliable 24/7. SQLite on a free instance is not durable across all redeploys/restarts.

## Render settings
Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn bot:app --host 0.0.0.0 --port $PORT`

Environment variables:
- `BOT_TOKEN` — BotFather token (existing)
- `WEBHOOK_SECRET` — existing random secret
- `DB_PATH` — `offers.sqlite3` (or `/tmp/offers.sqlite3` for temporary test)
- `API_ID` — from my.telegram.org
- `API_HASH` — from my.telegram.org; secret
- `TELETHON_SESSION` — generated StringSession; secret

Render automatically provides `RENDER_EXTERNAL_URL`. Keep all secrets out of GitHub.

## Create TELETHON_SESSION
Do this on a trusted computer/terminal with Python and Telethon installed. Do not use a third-party session generator website. Create a local script using Telethon's `StringSession`, sign in interactively with your own phone number, Telegram login code, and 2FA password if requested. The script prints a session string; copy it directly into Render's secret environment variable and then securely delete the local script/output. Never send the code, password, or session string to anyone.

Example local script:

```python
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 123456
API_HASH = "your_api_hash"

async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        print("TELETHON_SESSION=" + client.session.save())

asyncio.run(main())
```

Install dependency first with `pip install telethon`. Run the script. Telegram sends a login code through its official app/service; enter it only in your own local terminal. If 2-step verification is enabled, enter that password locally.

## Command
In a private chat with a buyer, send:
`/buy https://example.com/nft 25`

The command is best-effort deleted, then the Business bot attempts to send an offer with Accept/Reject buttons.
