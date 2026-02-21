import os
import sys
import asyncio
from telethon import TelegramClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION = os.getenv("TELEGRAM_SESSION", "tgsession")

# Your channel
CHANNEL_ID = int(os.getenv("TEST_CHANNEL_ID", "-1001363986630"))

if not API_ID or not API_HASH:
    print("Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in env/.env", file=sys.stderr)
    raise SystemExit(1)

client = TelegramClient(SESSION, int(API_ID), API_HASH)

async def main():
    # Resolve entity (proves the session can see the channel)
    entity = await client.get_entity(CHANNEL_ID)
    title = getattr(entity, "title", None) or str(entity)
    print(f"Resolved channel: {title} ({CHANNEL_ID})")

    # Fetch last message
    msgs = await client.get_messages(entity, limit=5)

    if not msgs:
        print("No messages returned. (Channel empty, or access issue.)")
        return

    latest = msgs[0]
    text = latest.message or ""
    print("\n=== Latest message ===")
    print(f"ID: {latest.id}")
    print(f"Date: {latest.date}")
    print(f"Text: {text[:2000] if text else '[no text]'}")

    print("\n=== Last 5 message IDs ===")
    print([m.id for m in msgs])

async def run():
    await client.start()  # uses existing session; will prompt only if needed
    await main()

if __name__ == "__main__":
    asyncio.run(run())