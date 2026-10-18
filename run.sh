#!/bin/bash
# NOTE: the systemd service (chatbots.service) already runs this app on :8123
# and restarts it on boot/crash. Only use this script if you stopped it:
#   systemctl --user stop chatbots.service
set -e
cd "$(dirname "$0")"
if systemctl --user is-active -q chatbots.service; then
  echo "chatbots.service is already running on :8123 — nothing to do."
  echo "Open http://localhost:8123 (or stop the service first to run manually)."
  exit 0
fi
python3 -m venv .venv 2>/dev/null || true
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -c "import db; db.init()"
exec .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8123
