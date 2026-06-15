#!/bin/bash
set -e

echo "════════════════════════════════════════"
echo "  DEAD MONEY bot — server setup"
echo "════════════════════════════════════════"

# 1. System update + Python
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git > /dev/null

# 2. Create app directory
mkdir -p /opt/deadmoney
cd /opt/deadmoney

# 3. Clone the bot code from GitHub (already uploaded)
if [ -d ".git" ]; then
  git pull
else
  git clone https://github.com/surenohan/deadmoney-trading.git .
fi

# 4. Virtual environment + dependencies
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

# 5. Create .env file for secrets (if not exists)
if [ ! -f /opt/deadmoney/.env ]; then
  echo ""
  echo "════════════════════════════════════════"
  echo "  Enter your credentials"
  echo "════════════════════════════════════════"
  read -p "BINANCE_API_KEY: " BINANCE_API_KEY
  read -p "BINANCE_API_SECRET: " BINANCE_API_SECRET
  read -p "TELEGRAM_BOT_TOKEN: " TELEGRAM_BOT_TOKEN
  read -p "TELEGRAM_CHAT_ID: " TELEGRAM_CHAT_ID

  cat > /opt/deadmoney/.env <<EOF
BINANCE_API_KEY=$BINANCE_API_KEY
BINANCE_API_SECRET=$BINANCE_API_SECRET
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
LEVERAGE=20
RISK_PCT=2
MAX_POSITIONS=5
MIN_SCORE=70
MIN_WITNESSES=2
SCAN_INTERVAL_MIN=30
MAX_DAILY_LOSS_PCT=10
DIRECTION=both
EOF
  chmod 600 /opt/deadmoney/.env
  echo "✓ Credentials saved to /opt/deadmoney/.env (permissions locked to root)"
else
  echo "✓ .env already exists, skipping credential prompt"
fi

# 6. Create systemd service for 24/7 operation + auto-restart
cat > /etc/systemd/system/deadmoney.service <<'EOF'
[Unit]
Description=Dead Money Trading Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/deadmoney
EnvironmentFile=/opt/deadmoney/.env
ExecStart=/opt/deadmoney/venv/bin/python /opt/deadmoney/bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 7. Enable and start
systemctl daemon-reload
systemctl enable deadmoney
systemctl restart deadmoney

echo ""
echo "════════════════════════════════════════"
echo "  ✓ Bot installed and started!"
echo "════════════════════════════════════════"
echo ""
echo "Useful commands:"
echo "  systemctl status deadmoney     — check if running"
echo "  journalctl -u deadmoney -f     — live logs"
echo "  systemctl restart deadmoney    — restart bot"
echo "  nano /opt/deadmoney/.env       — edit settings, then restart"
echo ""
echo "Check Telegram for the startup message."
