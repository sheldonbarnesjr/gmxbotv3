#!/usr/bin/env bash
# ============================================================================
# GMX Trading Bot — REST API Setup Script
#
# Run once on your VPS to install deps, configure the API, and start it.
#   chmod +x setup_api.sh && ./setup_api.sh
#
# Safe to re-run — it won't overwrite existing API keys or venv.
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; }

# ── 1. Ensure json/ directory exists ──
info "Ensuring json/ directory exists..."
mkdir -p json

# ── 2. Setup Python virtual environment ──
if [ -d "venv" ]; then
    info "Virtual environment already exists, activating..."
else
    info "Creating Python virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# ── 3. Install all dependencies ──
info "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo ""
info "Dependencies installed."

# ── 4. Generate API key ──
if [ -f "json/api_keys.json" ] && [ "$(cat json/api_keys.json 2>/dev/null)" != "[]" ] && [ -s "json/api_keys.json" ]; then
    warn "API keys already exist. Skipping key generation."
    echo "  Existing keys in json/api_keys.json"
else
    info "Generating API key for the iOS app..."
    python rest_api.py genkey
fi

# ── 5. Open firewall port ──
if command -v ufw &> /dev/null; then
    if ufw status | grep -q "8000"; then
        info "Port 8000 already open in UFW."
    else
        info "Opening port 8000 in UFW..."
        sudo ufw allow 8000/tcp
    fi
fi

# ── 6. Create systemd service for auto-restart ──
SERVICE_FILE="/etc/systemd/system/gmxbot-api.service"
if [ -f "$SERVICE_FILE" ]; then
    warn "Systemd service already exists. Restarting..."
    sudo systemctl restart gmxbot-api
else
    info "Creating systemd service for auto-restart..."
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=GMX Trading Bot REST API
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/rest_api.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable gmxbot-api
    sudo systemctl start gmxbot-api
    info "Systemd service created and started."
fi

# ── 7. Wait and verify ──
sleep 2
if systemctl is-active --quiet gmxbot-api; then
    info "API server is running!"
else
    error "API server failed to start. Check logs:"
    echo "  sudo journalctl -u gmxbot-api -n 50 --no-pager"
    exit 1
fi

# ── 8. Get server IP ──
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

# ── 9. Print summary ──
echo ""
echo "============================================================"
echo -e "${GREEN}  REST API is live!${NC}"
echo "============================================================"
echo ""
echo "  Server:    http://$SERVER_IP:8000"
echo "  Docs:      http://$SERVER_IP:8000/docs"
echo "  Health:    http://$SERVER_IP:8000/api/v1/health"
echo ""
echo "  iOS App Settings:"
echo "    Base URL:  http://$SERVER_IP:8000"
echo "    API Key:   (check json/api_keys.json)"
echo ""
echo "  Useful commands:"
echo "    View logs:     sudo journalctl -u gmxbot-api -f"
echo "    Restart:       sudo systemctl restart gmxbot-api"
echo "    Stop:          sudo systemctl stop gmxbot-api"
echo "    New API key:   source venv/bin/activate && python rest_api.py genkey"
echo ""
echo "  Test it:"
echo "    curl -H 'Authorization: Bearer YOUR_KEY' http://$SERVER_IP:8000/api/v1/health"
echo "============================================================"
