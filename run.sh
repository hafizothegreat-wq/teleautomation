#!/bin/bash

# Start Xvfb (virtual display) in the background on display :99
echo "[SCRIPT] Starting virtual display (Xvfb)..."
Xvfb :99 -screen 0 1024x768x24 > /dev/null 2>&1 &
XVFB_PID=$!
sleep 2

# Set the DISPLAY environment variable to use the virtual display
export DISPLAY=:99

# Render captures stdout/stderr; unbuffered output keeps the log stream live.
# LOG_LEVEL (DEBUG|INFO|WARNING|ERROR) controls logging verbosity.
export PYTHONUNBUFFERED=1
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

echo "[SCRIPT] Virtual display started on $DISPLAY"
echo "[SCRIPT] Starting Python application (LOG_LEVEL=$LOG_LEVEL)..."

# Run the Python script (unbuffered so Render shows logs in real time)
python -u telegram_headless.py

# Cleanup: Kill Xvfb when done
echo "[SCRIPT] Killing virtual display..."
kill $XVFB_PID 2>/dev/null

exit 0
