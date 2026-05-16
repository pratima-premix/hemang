#!/usr/bin/env bash
set -euo pipefail

echo "======================================================"
echo "  RSI2 Option Bot — VPS Setup"
echo "======================================================"

cd ~/rsi-option

# System packages
echo "[1/5] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv

# Virtual environment
echo "[2/5] Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Python dependencies
echo "[3/5] Installing Python packages..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

# Init data files
echo "[4/5] Initialising data files..."
[ -f open_positions.json ] || echo "[]" > open_positions.json
[ -f iv_history.json ]     || echo "{}" > iv_history.json
touch trades.log bot.log

# systemd service
echo "[5/5] Installing systemd service..."
sudo cp bot.service /etc/systemd/system/rsi-option-bot.service
sudo systemctl daemon-reload
sudo systemctl enable rsi-option-bot.service

echo ""
echo "======================================================"
echo "  Setup complete!"
echo ""
echo "  Start now:   sudo systemctl start rsi-option-bot"
echo "  View logs:   sudo journalctl -fu rsi-option-bot"
echo "  Manual run:  source venv/bin/activate && python main.py"
echo "  Health:      curl http://localhost:5000/health"
echo "  Status:      curl http://localhost:5000/status"
echo ""
echo "  TradingView Webhook URL:"
echo "  http://$(curl -s ifconfig.me):5000/webhook"
echo "  Header: X-Webhook-Secret: rsi_bot_secure_2024"
echo "======================================================"
