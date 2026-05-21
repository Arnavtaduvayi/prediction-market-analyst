#!/usr/bin/env bash
# Prediction Market Analyst — VPS deploy
# Tested on Ubuntu 22.04 / 24.04 (Hetzner CX22, DigitalOcean basic, etc.)
#
# Usage (as root or with sudo):
#   curl -fsSL https://raw.githubusercontent.com/Arnavtaduvayi/prediction-market-analyst/main/scripts/deploy.sh | bash
# Or after manual git clone:
#   sudo bash ./scripts/deploy.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Arnavtaduvayi/prediction-market-analyst.git}"
APP_USER="predmkt"
APP_DIR="/opt/predmkt"
PY_VERSION="3.12"

require_root() {
  if [[ "$(id -u)" != "0" ]]; then
    echo "This script needs root. Re-run with: sudo bash $0" >&2
    exit 1
  fi
}

install_packages() {
  echo "==> Installing system packages..."
  apt-get update -qq
  apt-get install -y -qq \
    python${PY_VERSION} python${PY_VERSION}-venv python${PY_VERSION}-dev \
    git curl ca-certificates build-essential libssl-dev libffi-dev
}

create_user() {
  if ! id -u "$APP_USER" >/dev/null 2>&1; then
    echo "==> Creating user $APP_USER..."
    useradd --system --create-home --shell /bin/bash "$APP_USER"
  fi
}

clone_or_update_repo() {
  if [[ -d "$APP_DIR/.git" ]]; then
    echo "==> Updating existing repo at $APP_DIR..."
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard origin/main
  else
    echo "==> Cloning $REPO_URL into $APP_DIR..."
    rm -rf "$APP_DIR"
    sudo -u "$APP_USER" git clone --quiet "$REPO_URL" "$APP_DIR"
  fi
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
}

setup_venv() {
  echo "==> Creating Python venv..."
  sudo -u "$APP_USER" python${PY_VERSION} -m venv "$APP_DIR/.venv"
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip --quiet
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
}

prep_data_dir() {
  echo "==> Preparing data + keys directories..."
  mkdir -p "$APP_DIR/data" "$APP_DIR/keys"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR/data" "$APP_DIR/keys"
  chmod 700 "$APP_DIR/keys"
  chmod +x "$APP_DIR/scripts/commit_journal.sh"
}

install_systemd_units() {
  echo "==> Installing systemd units..."
  cp "$APP_DIR/systemd/"*.service /etc/systemd/system/
  cp "$APP_DIR/systemd/"*.timer /etc/systemd/system/
  systemctl daemon-reload

  echo "==> Enabling and starting services..."
  systemctl enable --now predmkt-scanner.service
  systemctl enable --now predmkt-brain.service
  systemctl enable --now predmkt-executor.service
  systemctl enable --now predmkt-exit.service
  systemctl enable --now predmkt-targets.timer
  systemctl enable --now predmkt-commit.timer
}

bootstrap_targets() {
  echo "==> Bootstrap whale target list (first run)..."
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/targets.py" --candidates 100 || true
}

print_summary() {
  cat <<EOF

╔════════════════════════════════════════════════════════════════════╗
║  DEPLOY COMPLETE                                                   ║
╠════════════════════════════════════════════════════════════════════╣

Services running:
  systemctl status predmkt-scanner predmkt-brain predmkt-executor predmkt-exit

Watch logs (live):
  journalctl -u predmkt-scanner -f
  journalctl -u predmkt-exit -f

Check status:
  sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/paper_cross.py status

REMAINING SETUP (for live Kalshi trading):
  1. SCP your Kalshi private key from your laptop:
       scp keys/kalshi_private.pem root@<VPS_IP>:$APP_DIR/keys/
  2. Create .env with your key ID:
       echo "KALSHI_API_KEY_ID=<your-uuid>" > $APP_DIR/.env
       chown $APP_USER:$APP_USER $APP_DIR/.env $APP_DIR/keys/kalshi_private.pem
       chmod 600 $APP_DIR/.env $APP_DIR/keys/kalshi_private.pem
  3. Restart services to pick up creds:
       systemctl restart predmkt-executor predmkt-exit

Paper trading runs WITHOUT these credentials. Live trading needs them.

EOF
}

main() {
  require_root
  install_packages
  create_user
  clone_or_update_repo
  setup_venv
  prep_data_dir
  install_systemd_units
  bootstrap_targets
  print_summary
}

main "$@"
