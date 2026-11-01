# Telegram NFT Offer Bot (Business)

## Render setup
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
- Add environment variables:
  - `BOT_TOKEN`: token from @BotFather
  - `WEBHOOK_SECRET`: random secret, 20-40 characters from A-Z, a-z, 0-9, _ and -
  - `DB_PATH`: `offers.sqlite3` (for testing only; use persistent storage/database for production)

## Important
This is a starter implementation. It stores recipients only after they send a message to your connected Telegram Business account. Then send `/buy https://t.me/nft/Example 500` to the bot's own private chat and choose a recipient.

Accepting an offer does NOT transfer Stars or the NFT. Payment and gift transfer must be implemented separately and confirmed through Telegram-supported flows.

For reliable production operation, use persistent storage (Render paid disk or managed database) and a service plan that stays online. Free Render web services can spin down and their local filesystem is ephemeral.
