# GMXBot Setup Guide

## Quick Start

1. Get a Linux VPS (Ubuntu 22.04 recommended)
2. SSH into your server and clone the bot
3. Run `python3 gmx.py` — the setup wizard handles everything else

That's it. The wizard installs dependencies, walks you through configuration, writes your `.env`, and offers to run the bot as a background service. (recomended fior 24/7 trading)

---

# Hostinger VPS Setup Guide

How to get a VPS on Hostinger and deploy the bot.

## 1. Create a Hostinger Account

1. Go to [https://www.hostinger.com/vps-hosting](https://www.hostinger.com/vps-hosting).
2. Pick a plan — **KVM 1** (1 vCPU, 4 GB RAM) is enough for this bot.
3. Click **Add to cart**, create an account (or log in), and complete payment.
4. In your Hostinger dashboard, go to **VPS** and click **Setup** on your new server.
5. Choose **Ubuntu 22.04** as the operating system.
6. Set a **root password** — save this somewhere safe, you'll need it to connect.
7. Wait a minute or two for the server to provision. Once ready, you'll see your server's **IP address** on the VPS dashboard.

## 2. Connect to Your VPS via SSH

Open a terminal on your local machine (Terminal on Mac/Linux, PowerShell or Windows Terminal on Windows).

```bash
ssh root@YOUR_SERVER_IP
```

Replace `YOUR_SERVER_IP` with the IP from your Hostinger dashboard. Type `yes` when asked about the fingerprint, then enter your root password.

## 3. Create a Dedicated User

Running the bot as root is not recommended. Create a `gmxbot` user:

```bash
adduser gmxbot
usermod -aG sudo gmxbot
su - gmxbot
```

## 4. Clone the Repo and Run Setup

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/sheldonbarnesjr/gmxbotv3.git
cd gmxbotv3
python3 gmx.py
```

The setup wizard starts automatically. It will:
- Install pip and venv if needed
- Create a virtual environment and install all dependencies
- Prompt you for your configuration values (see below)
- Write your `.env` file
- Offer to install the bot as a systemd service that auto-starts on reboot

---

# Setup Wizard — What You'll Need

Before running the wizard, have these ready. The wizard prompts for each one.

## Telegram API ID and API Hash

These let the bot connect to Telegram as your user account.

1. Go to [https://my.telegram.org](https://my.telegram.org) and log in with your phone number.
2. Click **API development tools**.
3. Fill out the form — app title and short name can be anything (e.g., "MyBot").
4. Click **Create application**.
5. Copy the **App api_id** (a number like `12345678`) and **App api_hash** (a hex string like `0123456789abcdef0123456789abcdef`).

## Telegram Bot Token

This creates a bot account that sends you notifications and accepts commands.

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to pick a name and username.
3. BotFather replies with a token like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz` — copy this.
4. **Important:** Open a conversation with your new bot and press **Start**, otherwise it can't message you.

## Admin Chat ID

Your numeric Telegram user ID (not your username). The bot uses this to know where to send admin messages.

- Message [@userinfobot](https://t.me/userinfobot) on Telegram — it replies with your ID (a number like `123456789`).
- [@RawDataBot](https://t.me/RawDataBot) also works.

## Admin Username

Your Telegram username without the `@` symbol. This authorizes you to use admin commands like `/status`, `/pause`, `/resume`, etc.

## Wallet Private Key

The private key for the wallet the bot will trade with on Arbitrum. You can add up to 4 wallets — only the first is required.

**How to export from MetaMask:**
1. Open MetaMask and click the three dots next to your account name.
2. Click **Account details** then **Show private key**.
3. Enter your MetaMask password and copy the key.

**Security:**
- Use a dedicated trading wallet, not your main wallet.
- Fund it with USDC on Arbitrum and a small amount of ETH for gas.
- The private key is stored in your `.env` file with restricted permissions (owner-only read/write).
- Never share your private key with anyone.

## Trading Settings

| Setting | What it means | Default |
|---------|--------------|---------|
| **Portfolio %** | % of your wallet balance used per trade. `0.25` means 25% of your available USDC goes into each trade. | `0.25` |
| **Max Leverage** | Maximum leverage multiplier for any trade. | `100` |
| **Max Position USD** | Largest single position size in USD. | `10000` |
| **Min Position USD** | Smallest position size — trades below this are skipped. | `20` |

Press Enter on any of these to accept the default value shown in brackets.

---

# After Setup

## If you installed the systemd service

The bot is already running. Useful commands:

```bash
sudo systemctl status gmxbot      # check if bot is running
sudo journalctl -u gmxbot -f      # view live logs
sudo systemctl restart gmxbot     # restart the bot
sudo systemctl stop gmxbot        # stop the bot
```

The bot auto-starts on reboot. If it crashes, systemd restarts it automatically after 10 seconds.

## If you skipped the systemd service

Run the bot manually:

```bash
cd ~/apps/gmxbotv3
.venv/bin/python3 gmx.py
```

## Reconfiguring

To change your settings, run the setup wizard again:

```bash
python3 setup.py
```

It will detect your existing `.env` and ask if you want to reconfigure.

You can also edit `.env` directly — it's a plain text file.

## Updating the Bot

```bash
sudo systemctl stop gmxbot
cd ~/apps/gmxbotv3
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl start gmxbot
```

## Quick Reference

| Task | Command |
|------|---------|
| SSH into server | `ssh root@YOUR_SERVER_IP` |
| Switch to bot user | `su - gmxbot` |
| Start bot | `sudo systemctl start gmxbot` |
| Stop bot | `sudo systemctl stop gmxbot` |
| Restart bot | `sudo systemctl restart gmxbot` |
| Check status | `systemctl status gmxbot` |
| View logs | `journalctl -u gmxbot -f` |
| View last 100 log lines | `journalctl -u gmxbot -n 100` |
