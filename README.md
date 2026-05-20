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
4. Add the connector to `claude_desktop_config.json` (backs up the original first)

Then **restart Claude** and call `github_authorize` in any chat — your browser opens, you enter the shown code on GitHub, click **Authorize**, and call `github_authorize` once more to complete. The token is stored in macOS Keychain and reused automatically forever.

---

## ⚠️ Access & permissions

**This connector has broad access and includes write tools. Read this before installing.**

### OAuth scopes requested

| Scope | What it allows |
|---|---|
| `repo` | Full access to public **and private** repositories — read and write |
| `read:org` | Read org membership and teams |
| `notifications` | Read and mark notifications |
| `user` | Read your profile |

If you only need public-repo read access (no private repos), edit `SCOPES` in `server.py` to `"public_repo read:org notifications user"` before authorizing. Note: `public_repo` does not grant access to private repositories.

### Write tools

The connector includes tools that **make real changes on GitHub**:

| Tool | What it does |
|---|---|
| `github_create_repo` | Creates a new repository |
| `github_upsert_file` | Creates or overwrites a file (makes a commit) |
| `github_create_issue` | Opens a new issue |
| `github_update_issue` | Edits or closes an issue |
| `github_add_comment` | Posts a comment |
| `github_create_pr` | Opens a pull request |

Claude Desktop will prompt you for approval before each tool call — review the arguments carefully before allowing write operations.

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

> `config.json` is in `.gitignore`. The OAuth token never touches the filesystem — it lives in macOS Keychain.

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

### Read-only

| Tool | Description |
|------|-------------|
| `github_authorize` | Device Flow OAuth — two-phase, browser + code |
| `github_status` | Show currently logged-in user |
| `github_logout` | Remove stored token |
| `github_whoami` | Get your (or any user's) profile |
| `github_list_repos` | List your repos |
| `github_get_repo` | Repo details |
| `github_search_repos` | Search GitHub repos |
| `github_get_file` | Read a file from any repo |
| `github_list_directory` | Browse a repo directory |
| `github_list_issues` | List issues |
| `github_get_issue` | Issue + all comments |
| `github_list_prs` | List pull requests |
| `github_get_pr` | PR details + changed files |
| `github_search_code` | Code search across GitHub |
| `github_search_issues` | Issue/PR search |
| `github_list_commits` | Commit history |
| `github_get_commit` | Commit details + files |
| `github_list_notifications` | Unread notifications |

### Write (makes real changes — review before approving)

| Tool | Description |
|------|-------------|
| `github_create_repo` | Create a new repository |
| `github_upsert_file` | Create or update a file (commits to repo) |
| `github_create_issue` | Open a new issue |
| `github_update_issue` | Edit or close an issue |
| `github_add_comment` | Post a comment on an issue or PR |
| `github_create_pr` | Open a pull request |

---

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

## Token & local file security

| File | Contains | Permissions | Notes |
|---|---|---|---|
| `config.json` | OAuth App client_id | `0600` | In `.gitignore` |
| macOS Keychain | OAuth access token | System-managed | Never written to disk |
| `auth_pending.json` | Temporary device code (not the token) | `0600` | Auto-deleted on completion or expiry |

macOS may prompt "python3 wants to use your keychain" on first use — click **Always Allow**.
