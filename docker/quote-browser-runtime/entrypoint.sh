#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/profile /data/artifacts

if [[ -z "${CHROME_BIN:-}" ]]; then
  CHROME_BIN="$(find /ms-playwright -path '*/chrome-linux/chrome' -type f | head -n 1 || true)"
fi

if [[ -z "${CHROME_BIN:-}" || ! -x "${CHROME_BIN}" ]]; then
  echo "Chromium executable not found under /ms-playwright" >&2
  exit 1
fi

exec "${CHROME_BIN}" \
  --headless=new \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-background-networking \
  --no-first-run \
  --no-default-browser-check \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port="${REMOTE_DEBUGGING_PORT:-9222}" \
  --user-data-dir=/data/profile \
  --window-size=1440,960 \
  about:blank

