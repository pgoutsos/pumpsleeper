#!/bin/sh
# Starts both the PumpSleeper server and dashboard as background processes,
# then tails both logs so Docker sees output from both services.

set -e

mkdir -p /data

echo "[entrypoint] Starting PumpSleeper server on :8081..."
python3 /app/server.py &
SERVER_PID=$!

echo "[entrypoint] Starting PumpSleeper dashboard on :8080..."
python3 /app/dashboard.py &
DASH_PID=$!

# If either process dies, exit so Docker can restart the container
wait_for_exit() {
    wait -n $SERVER_PID $DASH_PID 2>/dev/null || true
    echo "[entrypoint] A service exited — restarting container"
    kill $SERVER_PID $DASH_PID 2>/dev/null || true
    exit 1
}

trap wait_for_exit TERM INT

echo "[entrypoint] Both services running. Tailing logs..."
tail -F /data/server.log /data/dashboard.log 2>/dev/null &
TAIL_PID=$!

wait $SERVER_PID $DASH_PID
