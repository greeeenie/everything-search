#!/bin/bash
set -euo pipefail

rm -f /profile/SingletonLock /profile/SingletonSocket /profile/SingletonCookie

export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1366x768x24 -nolisten tcp &
xvfb_pid=$!
chrome_pid=

cleanup() {
    if [[ -n "$chrome_pid" ]]; then
        kill "$chrome_pid" 2>/dev/null || true
        wait "$chrome_pid" 2>/dev/null || true
    fi
    kill "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT

for attempt in {1..50}; do
    if xdpyinfo >/dev/null 2>&1; then
        break
    fi
    if [[ "$attempt" == 50 ]] || ! kill -0 "$xvfb_pid" 2>/dev/null; then
        echo "Xvfb did not start on $DISPLAY" >&2
        exit 1
    fi
    sleep 0.1
done

chromium \
    --no-sandbox \
    --remote-debugging-port=9222 \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir=/profile \
    --no-first-run \
    --no-default-browser-check \
    --disable-dev-shm-usage \
    --disable-gpu \
    --lang=ru-RU \
    about:blank &
chrome_pid=$!

wait -n "$xvfb_pid" "$chrome_pid"
