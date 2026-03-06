# Telegram Credentials Setup Guide

How to find every Telegram-related value for your `.env` file.

## Telegram User API

These credentials let the bot connect to Telegram as your user account (via Telethon).

### `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`

1. Go to [https://my.telegram.org](https://my.telegram.org) and log in with your phone number.
2. Click **API development tools**.
3. Fill out the form — app title and short name can be anything (e.g., "MyBot").
4. Click **Create application**.
5. Copy the **App api_id** (a number like `12345678`) and **App api_hash** (a hex string like `0123456789abcdef0123456789abcdef`).


### `NOTIFY_CHAT`

Your Telegram username with the `@` prefix (e.g., `@joe`). This is where the bot sends trade notifications.

### `ADMIN_CHAT`

Set to `ME` to use your own Saved Messages as the admin console. This is the recommended default.

### `ADMIN_USERNAMES`

Comma-separated list of Telegram usernames (with `@`) authorized to use admin commands. At minimum, set this to your own username.

## Telegram Bot API (Optional)

These are only needed if you want the dual-channel notification system (Telethon + Bot API).

### `TELEGRAM_BOT_TOKEN`

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to pick a name and username.
3. BotFather replies with a token like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz` — copy this.
4. **Important:** Open a conversation with your new bot and press **Start**, otherwise it can't message you.

### `ADMIN_CHAT_ID`

Your numeric Telegram user ID (not your username). To find it, message [@userinfobot](https://t.me/userinfobot) — it replies with your ID (a number like `123456789`). [@RawDataBot](https://t.me/RawDataBot) also works.

## Example `.env` (Telegram section)

```env
# Telegram User API
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION=tgsession
TELEGRAM_CHANNELS=example_channel
NOTIFY_CHAT=@sheldon
ADMIN_CHAT=ME
ADMIN_USERNAMES=@sheldon

# Telegram Bot API (optional)
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_CHAT_ID=123456789
```
