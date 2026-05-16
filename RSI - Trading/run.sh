#!/bin/bash
# Start the RSI-2 trading bot inside a persistent screen session
# Usage: bash run.sh

SESSION="rsi_bot"
DIR="$HOME/RSI - Trading"

cd "$DIR" || { echo "Directory not found: $DIR"; exit 1; }

# Kill any existing session
screen -S "$SESSION" -X quit 2>/dev/null

# Start new detached session
screen -dmS "$SESSION" bash -c "python3 bot.py 2>&1 | tee -a bot.log"

echo ""
echo "Bot started in screen session: $SESSION"
echo ""
echo "Useful commands:"
echo "  screen -r $SESSION       # attach to live output"
echo "  Ctrl+A then D            # detach (leave bot running)"
echo "  screen -S $SESSION -X quit  # stop the bot"
echo "  tail -f bot.log          # follow logs without attaching"
echo ""
