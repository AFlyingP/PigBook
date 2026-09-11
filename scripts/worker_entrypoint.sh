#!/bin/bash
set -e

cleanup() {
    echo "Supervisor received termination signal, forwarding to children..."
    if [ -n "$WORKER_PID" ]; then
        kill -TERM "$WORKER_PID" 2>/dev/null || true
    fi
    if [ -n "$ALLOY_PID" ]; then
        kill -TERM "$ALLOY_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
    exit 0
}

trap cleanup TERM INT

# Start background python worker
python -m app.worker &
WORKER_PID=$!

# Start Alloy sidecar process
ALLOY_CONFIG=""
if [ -f "/app/infra/alloy.alloy" ]; then
    ALLOY_CONFIG="/app/infra/alloy.alloy"
elif [ -f "infra/alloy.alloy" ]; then
    ALLOY_CONFIG="infra/alloy.alloy"
fi

if [ -n "$ALLOY_CONFIG" ] && command -v alloy >/dev/null 2>&1; then
    alloy run "$ALLOY_CONFIG" --storage.path=/tmp/alloy-data &
    ALLOY_PID=$!
else
    alloy run --storage.path=/tmp/alloy-data &
    ALLOY_PID=$!
fi

# Supervise both children: fail container if either exits
set +e
wait -n "$WORKER_PID" "$ALLOY_PID"
EXIT_CODE=$?

kill -TERM "$WORKER_PID" 2>/dev/null || true
kill -TERM "$ALLOY_PID" 2>/dev/null || true
wait 2>/dev/null || true

exit ${EXIT_CODE:-1}
