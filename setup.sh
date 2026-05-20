#!/usr/bin/env bash
# GitHub MCP Connector — one-time setup script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "🔧 GitHub MCP Connector — Setup"
echo "================================"
echo ""

# ── 1. Create virtualenv ─────────────────────────────────────────────────
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

if [ ! -f "$PYTHON" ]; then
    # fallback to system python3
    PYTHON="$(which python3)"
fi

echo "Using Python: $PYTHON ($($PYTHON --version))"

if [ ! -d "venv" ]; then
    echo "→ Creating virtual environment…"
    "$PYTHON" -m venv venv
fi

echo "→ Installing dependencies…"
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt

echo "✅ Python environment ready."
echo ""

# ── 2. config.json ──────────────────────────────────────────────────────
if [ ! -f "config.json" ]; then
    echo "⚠️  config.json not found."
    echo ""
    echo "You need a GitHub OAuth App. Here's how:"
    echo ""
    echo "  1. Go to: https://github.com/settings/applications/new"
    echo "  2. Fill in:"
    echo "       Application name:  Claude GitHub Connector"
    echo "       Homepage URL:      http://localhost"
    echo "       Callback URL:      http://localhost  (anything; unused by Device Flow)"
    echo "  3. Click 'Register application'"
    echo "  4. Copy the Client ID"
    echo "  5. On the next page, scroll to 'Device Flow' → check ✓ Enable Device Flow"
    echo "     (No client secret needed — Device Flow is secret-free)"
    echo ""
    read -r -p "Paste your Client ID: " CLIENT_ID
    echo ""

    cat > config.json <<EOF
{
  "client_id": "${CLIENT_ID}"
}
EOF
    chmod 600 config.json
    echo "✅ config.json created."
else
    echo "✅ config.json already exists — skipping."
fi

echo ""

# ── 3. Update claude_desktop_config.json ────────────────────────────────
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
SERVER_PY="$SCRIPT_DIR/server.py"

if [ -f "$DESKTOP_CONFIG" ]; then
    if grep -q "github-connector" "$DESKTOP_CONFIG"; then
        echo "✅ Claude desktop config already has the GitHub connector."
    else
        echo "→ Adding GitHub connector to Claude desktop config…"
        # Back up the config before touching it
        BACKUP="${DESKTOP_CONFIG}.backup-$(date +%Y%m%d-%H%M%S)"
        cp "$DESKTOP_CONFIG" "$BACKUP"
        echo "  Backup saved: $(basename "$BACKUP")"
        # Use Python to safely merge JSON
        "$VENV_PYTHON" - "$DESKTOP_CONFIG" "$VENV_PYTHON" "$SERVER_PY" <<'PYEOF'
import json, sys

config_path = sys.argv[1]
venv_python  = sys.argv[2]
server_py    = sys.argv[3]

with open(config_path) as f:
    config = json.load(f)

config.setdefault("mcpServers", {})
config["mcpServers"]["github-connector"] = {
    "command": venv_python,
    "args": [server_py]
}

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)
print("✅ Claude desktop config updated.")
PYEOF
    fi
else
    echo "⚠️  Claude desktop config not found at expected path."
    echo "Add this to your claude_desktop_config.json manually:"
    echo ""
    echo '  "github-connector": {'
    echo "    \"command\": \"$VENV_PYTHON\","
    echo "    \"args\": [\"$SERVER_PY\"]"
    echo '  }'
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Restart the Claude desktop app (or reload MCP servers)"
echo "  2. In any Claude chat, call:  github_authorize"
echo "     → Your browser opens and you get a code to enter on GitHub"
echo "     → After authorizing, call github_authorize again to complete"
echo "     → Token saved to macOS Keychain — reused every session automatically"
echo ""
echo "That's it! All github_* tools are now available."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
