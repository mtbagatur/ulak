#!/usr/bin/env sh
# One-command setup: venv, dependencies, VAPID keys, optional systemd user unit.
# Usage: ULAK_ALLOWED_LOGIN=you@github ULAK_VAPID_SUBJECT=mailto:you@example.com ./install.sh
set -eu
cd "$(dirname "$0")"
: "${ULAK_ALLOWED_LOGIN:?Set ULAK_ALLOWED_LOGIN to your exact Tailscale login}"
: "${ULAK_VAPID_SUBJECT:?Set ULAK_VAPID_SUBJECT to a mailto: contact}"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .secrets/vapid_private.pem ] || .venv/bin/python tools/generate_vapid.py

if command -v systemctl >/dev/null 2>&1; then
  unit="$HOME/.config/systemd/user/ulak.service"
  mkdir -p "$(dirname "$unit")"
  sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$PWD|" \
      -e "s|^ExecStart=.*|ExecStart=$PWD/.venv/bin/python app.py|" \
      -e "s|^Environment=ULAK_ALLOWED_LOGIN=.*|Environment=ULAK_ALLOWED_LOGIN=$ULAK_ALLOWED_LOGIN|" \
      -e "s|^Environment=ULAK_VAPID_SUBJECT=.*|Environment=ULAK_VAPID_SUBJECT=$ULAK_VAPID_SUBJECT|" \
      deploy/ulak.service.example > "$unit"
  systemctl --user daemon-reload
  systemctl --user enable --now ulak.service
  echo "ulak.service started. Next: tailscale serve --bg 8765"
else
  echo "No systemd. Start with: .venv/bin/python app.py"
fi
