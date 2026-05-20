# GitHub MCP Connector

OAuth-based GitHub connector for Claude. Authorize once — works in every session, across Cowork, Code, and regular Claude chats.

## Quick Start

```bash
cd /path/to/github-mcp-connector
bash setup.sh
```

The setup script will:
1. Create a Python venv and install dependencies
2. Walk you through creating a GitHub OAuth App (takes ~2 minutes)
3. Write `config.json` with your credentials
4. Add the connector to `claude_desktop_config.json`

Then **restart Claude** and call `github_authorize` in any chat — your browser opens, you enter the shown code on GitHub, click **Authorize**, and call `github_authorize` once more to complete. The token is stored in macOS Keychain and reused automatically forever.

---

## Manual Setup (if you prefer)

### Step 1 — Create the GitHub OAuth App

1. Go to **https://github.com/settings/applications/new**
2. Fill in:
   - **Application name**: Claude GitHub Connector (or anything)
   - **Homepage URL**: `http://localhost`
   - **Authorization callback URL**: `http://localhost` (unused — Device Flow doesn't redirect)
3. Click **Register application**
4. Copy the **Client ID**
5. On the app settings page, scroll to **Device Flow** → check **✓ Enable Device Flow**

> **No client secret needed.** Device Flow is designed for native/CLI apps that cannot keep secrets — GitHub only requires your `client_id`.

### Step 2 — Create `config.json`

```json
{
  "client_id": "YOUR_CLIENT_ID"
}
```

> **Security:** `config.json` is in `.gitignore`. The token never touches the filesystem — it lives in macOS Keychain.

### Step 3 — Python venv

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### Step 4 — Add to Claude

In `~/Library/Application Support/Claude/claude_desktop_config.json`, add under `mcpServers`:

```json
"github-connector": {
  "command": "/path/to/github-mcp-connector/venv/bin/python",
  "args": ["/path/to/github-mcp-connector/server.py"]
}
```

Restart Claude, then call `github_authorize`.

---

## Available Tools (24)

| Tool | Description |
|------|-------------|
| `github_authorize` | Device Flow OAuth — two-phase, browser + code |
| `github_status` | Show currently logged-in user |
| `github_logout` | Remove stored token |
| `github_whoami` | Get your (or any user's) profile |
| `github_create_repo` | Create a new repository |
| `github_upsert_file` | Create or update a file in a repo |
| `github_list_repos` | List your repos |
| `github_get_repo` | Repo details |
| `github_search_repos` | Search GitHub repos |
| `github_get_file` | Read a file from any repo |
| `github_list_directory` | Browse a repo directory |
| `github_list_issues` | List issues |
| `github_get_issue` | Issue + all comments |
| `github_create_issue` | Create an issue |
| `github_update_issue` | Edit / close an issue |
| `github_add_comment` | Comment on issue or PR |
| `github_list_prs` | List pull requests |
| `github_get_pr` | PR details + changed files |
| `github_create_pr` | Open a pull request |
| `github_search_code` | Code search across GitHub |
| `github_search_issues` | Issue/PR search |
| `github_list_commits` | Commit history |
| `github_get_commit` | Commit details + files |
| `github_list_notifications` | Unread notifications |

## OAuth Scopes

The connector requests: `repo read:org notifications user gist`

Full access to your repos (public + private), org membership, notifications, profile, and gists. Narrow this in `server.py` → `SCOPES` if needed.

## Re-authorizing

To switch GitHub accounts or if your token expires:
```
github_logout
github_authorize
```

## How Device Flow works

1. `github_authorize` (call 1) — requests a `device_code` + `user_code` from GitHub, opens your browser to `github.com/login/device`, returns the code to enter
2. You enter the code and click **Authorize** on GitHub
3. `github_authorize` (call 2) — polls GitHub, receives the token, saves it to macOS Keychain

No callback server, no open port, no `client_secret` to protect.

## Token Storage

The access token is stored in the **macOS Keychain** (`keyring.backends.macOS`) under the item `github-mcp-connector / oauth_token`. It never touches the filesystem. macOS may prompt "python3 wants to use your keychain" on first use — click **Always Allow** to avoid future prompts.
