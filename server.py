"""
GitHub MCP Connector — OAuth-based, persistent token, works in every session.

Tools exposed:
  Auth:    github_authorize · github_status · github_logout
  User:    github_whoami
  Repos:   github_create_repo · github_list_repos · github_get_repo · github_search_repos
           github_get_file · github_list_directory · github_upsert_file · github_push_files
  Issues:  github_list_issues · github_get_issue · github_create_issue
           github_update_issue · github_add_comment
  PRs:     github_list_prs · github_get_pr · github_create_pr
  Search:  github_search_code · github_search_issues
  Commits: github_list_commits · github_get_commit
  Notifs:  github_list_notifications
"""

import asyncio
import base64
import json
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

import keyring
import keyring.errors
import requests
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ── paths ──────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
TOKEN_FILE   = BASE / "tokens.json"    # legacy — used only for one-time migration
PENDING_FILE = BASE / "auth_pending.json"  # transient state between auth Phase 1 & 2

GITHUB_API       = "https://api.github.com"
DEVICE_CODE_URL  = "https://github.com/login/device/code"
OAUTH_TOKEN_URL  = "https://github.com/login/oauth/access_token"
SCOPES = "repo read:org notifications user"   # space-delimited per GitHub docs

# ── Keychain ────────────────────────────────────────────────────────────────
KEYRING_SERVICE  = "github-mcp-connector"
KEYRING_USERNAME = "oauth_token"


# ── config & token helpers ─────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    raise FileNotFoundError(
        "config.json not found. Copy config.example.json to config.json "
        "and add your GitHub OAuth App Client ID.\n"
        "See README.md → Step 2 for instructions."
    )


def load_token() -> Optional[str]:
    """Read token from macOS Keychain. Migrates tokens.json on first call if found."""
    token = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if token:
        return token
    # One-time migration: move legacy tokens.json → Keychain then delete the file
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE) as f:
                old = json.load(f)
            old_token = old.get("access_token")
            if old_token:
                print("Migrating token from tokens.json → macOS Keychain…", file=sys.stderr)
                save_token(old_token)
                TOKEN_FILE.unlink()
                return old_token
        except Exception:
            pass
    return None


def save_token(access_token: str) -> None:
    """Store token in macOS Keychain (item: github-mcp-connector / oauth_token)."""
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, access_token)


def clear_token() -> None:
    """Remove token from Keychain (and legacy tokens.json if still present)."""
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


# ── GitHub API helpers ─────────────────────────────────────────────────────
def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get(path: str, token: str, params: dict = None) -> any:
    resp = requests.get(
        f"{GITHUB_API}{path}",
        headers=gh_headers(token),
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def gh_post(path: str, token: str, data: dict) -> any:
    resp = requests.post(
        f"{GITHUB_API}{path}",
        headers=gh_headers(token),
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def gh_put(path: str, token: str, data: dict) -> any:
    resp = requests.put(
        f"{GITHUB_API}{path}",
        headers=gh_headers(token),
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def gh_patch(path: str, token: str, data: dict) -> any:
    resp = requests.patch(
        f"{GITHUB_API}{path}",
        headers=gh_headers(token),
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def gh_graphql(query: str, token: str, variables: dict = None) -> any:
    resp = requests.post(
        "https://api.github.com/graphql",
        headers=gh_headers(token),
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def require_token() -> str:
    """Return stored token or raise a user-friendly error."""
    token = load_token()
    if not token:
        raise ValueError(
            "Not authenticated with GitHub.\n"
            "Call the `github_authorize` tool first — it opens your browser "
            "for a one-time login and saves the token for all future sessions."
        )
    return token


# ── Device Flow — Phase 1: request codes & open browser ───────────────────
def device_flow_start(client_id: str) -> dict:
    """
    Request a device_code + user_code from GitHub, open the browser,
    and return a dict saved to auth_pending.json for Phase 2.
    """
    resp = requests.post(
        DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": SCOPES},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(
            f"GitHub rejected the device code request: {data.get('error_description', data['error'])}\n"
            "Make sure 'Device Flow' is enabled on your GitHub OAuth App:\n"
            "  github.com/settings/developers → your app → ✓ Enable Device Flow"
        )

    user_code  = data["user_code"]                   # e.g. "ABCD-1234"
    expires_in = data.get("expires_in", 900)
    interval   = data.get("interval", 5)

    # verification_uri_complete pre-fills the code in the browser; fall back to base URL
    verify_url = data.get("verification_uri_complete") or data.get("verification_uri")
    webbrowser.open(verify_url)

    return {
        "client_id":   client_id,
        "device_code": data["device_code"],
        "user_code":   user_code,
        "verify_url":  verify_url,
        "expires_at":  time.time() + expires_in,
        "interval":    interval,
    }


# ── Device Flow — Phase 2: poll once for the access token ─────────────────
def device_flow_poll(pending: dict) -> Optional[str]:
    """
    Poll GitHub once for the access token.
    Returns the token string on success, None if still pending.
    Raises on hard errors (denied, expired, disabled, bad credentials).
    """
    interval = pending.get("interval", 5)
    time.sleep(interval)   # always respect the minimum interval

    poll = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id":   pending["client_id"],
            "device_code": pending["device_code"],
            "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    poll.raise_for_status()
    data = poll.json()

    error = data.get("error")
    if not error:
        return data.get("access_token")   # success (may still be None if malformed)
    if error == "authorization_pending":
        return None                       # user hasn't clicked yet
    if error == "slow_down":
        pending["interval"] = interval + 5   # mutate so next call backs off too
        return None
    if error == "expired_token":
        raise TimeoutError("Device code expired. Call `github_authorize` to start again.")
    if error == "access_denied":
        raise RuntimeError("Authorization was denied on GitHub.")
    if error == "device_flow_disabled":
        raise RuntimeError(
            "Device Flow is not enabled on this OAuth App.\n"
            "Fix: github.com/settings/developers → your app → ✓ Enable Device Flow"
        )
    if error == "incorrect_client_credentials":
        raise RuntimeError("Client ID rejected by GitHub. Check client_id in config.json.")
    raise RuntimeError(f"OAuth error: {data.get('error_description', error)}")


# ── MCP server ─────────────────────────────────────────────────────────────
server = Server("github-connector")

TOOLS: list[Tool] = [
    # ── Auth ──
    Tool(
        name="github_authorize",
        description=(
            "Authorize this connector with GitHub using Device Flow OAuth. "
            "Two-phase: first call opens your browser and shows the code to enter at "
            "github.com/login/device; second call (after you click Authorize) "
            "completes the flow and saves the token to macOS Keychain for all future sessions."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="github_status",
        description="Check GitHub authentication status and show the logged-in user.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="github_logout",
        description="Remove the stored GitHub token. You will need to re-authorize.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    # ── User ──
    Tool(
        name="github_whoami",
        description="Get your GitHub profile (or any user's profile by username).",
        inputSchema={
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "GitHub username (omit for your own profile)"},
            },
        },
    ),
    # ── Repos ──
    Tool(
        name="github_create_repo",
        description="Create a new GitHub repository for the authenticated user.",
        inputSchema={
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Repository name"},
                "description": {"type": "string", "description": "Short description"},
                "private":     {"type": "boolean", "default": False},
                "auto_init":   {"type": "boolean", "default": False, "description": "Initialise with an empty README"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="github_upsert_file",
        description=(
            "Create or update a single file in a repository. "
            "Content is plain text — base64 encoding is handled automatically. "
            "For updates, the current file SHA is fetched automatically if not supplied."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "owner":   {"type": "string"},
                "repo":    {"type": "string"},
                "path":    {"type": "string", "description": "File path in the repo, e.g. 'src/main.py'"},
                "content": {"type": "string", "description": "Plain-text file content"},
                "message": {"type": "string", "description": "Commit message"},
                "branch":  {"type": "string", "description": "Target branch (default: repo default branch)"},
                "sha":     {"type": "string", "description": "Blob SHA of the file being replaced (auto-fetched if omitted)"},
            },
            "required": ["owner", "repo", "path", "content", "message"],
        },
    ),
    Tool(
        name="github_push_files",
        description=(
            "Commit multiple files to a repository in a single commit using the Git Data API. "
            "All files land atomically in one commit — use this instead of calling "
            "github_upsert_file repeatedly when uploading or updating several files at once."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "owner":   {"type": "string"},
                "repo":    {"type": "string"},
                "message": {"type": "string", "description": "Commit message"},
                "files": {
                    "type": "array",
                    "description": "Files to include in the commit",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path":    {"type": "string", "description": "File path in the repo, e.g. 'src/main.py'"},
                            "content": {"type": "string", "description": "Plain-text file content"},
                        },
                        "required": ["path", "content"],
                    },
                },
                "branch": {"type": "string", "description": "Target branch (default: repo default branch)"},
            },
            "required": ["owner", "repo", "message", "files"],
        },
    ),
    Tool(
        name="github_list_repos",
        description="List your GitHub repositories.",
        inputSchema={
            "type": "object",
            "properties": {
                "visibility": {"type": "string", "enum": ["all", "public", "private"], "default": "all"},
                "sort": {"type": "string", "enum": ["updated", "created", "pushed", "full_name"], "default": "updated"},
                "per_page": {"type": "integer", "default": 25, "maximum": 100},
            },
        },
    ),
    Tool(
        name="github_get_repo",
        description="Get details about a specific repository.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "GitHub username or org"},
                "repo":  {"type": "string", "description": "Repository name"},
            },
            "required": ["owner", "repo"],
        },
    ),
    Tool(
        name="github_search_repos",
        description="Search GitHub repositories. Supports GitHub search syntax (e.g. 'language:python stars:>100').",
        inputSchema={
            "type": "object",
            "properties": {
                "query":    {"type": "string"},
                "sort":     {"type": "string", "enum": ["stars", "forks", "updated"], "default": "stars"},
                "per_page": {"type": "integer", "default": 10, "maximum": 30},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="github_get_file",
        description="Get the contents of a file from a repository (decoded from base64).",
        inputSchema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo":  {"type": "string"},
                "path":  {"type": "string", "description": "File path, e.g. 'src/main.py'"},
                "ref":   {"type": "string", "description": "Branch, tag, or commit SHA (default: default branch)"},
            },
            "required": ["owner", "repo", "path"],
        },
    ),
    Tool(
        name="github_list_directory",
        description="List the contents of a directory in a repository.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo":  {"type": "string"},
                "path":  {"type": "string", "description": "Directory path (empty string for repo root)", "default": ""},
                "ref":   {"type": "string", "description": "Branch, tag, or commit SHA"},
            },
            "required": ["owner", "repo"],
        },
    ),
    # ── Issues ──
    Tool(
        name="github_list_issues",
        description="List issues in a repository (pull requests are excluded).",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":    {"type": "string"},
                "repo":     {"type": "string"},
                "state":    {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                "per_page": {"type": "integer", "default": 20, "maximum": 100},
            },
            "required": ["owner", "repo"],
        },
    ),
    Tool(
        name="github_get_issue",
        description="Get a specific issue with its full description and all comments.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":  {"type": "string"},
                "repo":   {"type": "string"},
                "number": {"type": "integer", "description": "Issue number"},
            },
            "required": ["owner", "repo", "number"],
        },
    ),
    Tool(
        name="github_create_issue",
        description="Create a new issue in a repository.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":  {"type": "string"},
                "repo":   {"type": "string"},
                "title":  {"type": "string"},
                "body":   {"type": "string", "description": "Issue body (markdown supported)"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to apply"},
            },
            "required": ["owner", "repo", "title"],
        },
    ),
    Tool(
        name="github_update_issue",
        description="Update an issue: change title, body, or state (open/closed).",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":  {"type": "string"},
                "repo":   {"type": "string"},
                "number": {"type": "integer"},
                "title":  {"type": "string"},
                "body":   {"type": "string"},
                "state":  {"type": "string", "enum": ["open", "closed"]},
            },
            "required": ["owner", "repo", "number"],
        },
    ),
    Tool(
        name="github_add_comment",
        description="Add a comment to an issue or pull request.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":  {"type": "string"},
                "repo":   {"type": "string"},
                "number": {"type": "integer", "description": "Issue or PR number"},
                "body":   {"type": "string", "description": "Comment text (markdown supported)"},
            },
            "required": ["owner", "repo", "number", "body"],
        },
    ),
    # ── PRs ──
    Tool(
        name="github_list_prs",
        description="List pull requests in a repository.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":    {"type": "string"},
                "repo":     {"type": "string"},
                "state":    {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                "per_page": {"type": "integer", "default": 20},
            },
            "required": ["owner", "repo"],
        },
    ),
    Tool(
        name="github_get_pr",
        description="Get a pull request with description and list of changed files.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":  {"type": "string"},
                "repo":   {"type": "string"},
                "number": {"type": "integer", "description": "PR number"},
            },
            "required": ["owner", "repo", "number"],
        },
    ),
    Tool(
        name="github_create_pr",
        description="Create a pull request.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo":  {"type": "string"},
                "title": {"type": "string"},
                "head":  {"type": "string", "description": "Branch with your changes (e.g. 'feature-branch')"},
                "base":  {"type": "string", "description": "Target branch (e.g. 'main')"},
                "body":  {"type": "string", "description": "PR description (markdown supported)"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    ),
    # ── Search ──
    Tool(
        name="github_search_code",
        description=(
            "Search for code across GitHub. "
            "Supports qualifiers like 'language:python repo:owner/name in:file'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query":    {"type": "string"},
                "per_page": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="github_search_issues",
        description=(
            "Search issues and PRs across GitHub. "
            "Supports qualifiers like 'is:open label:bug repo:owner/name'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query":    {"type": "string"},
                "per_page": {"type": "integer", "default": 15},
            },
            "required": ["query"],
        },
    ),
    # ── Commits ──
    Tool(
        name="github_list_commits",
        description="List commits in a repository, optionally filtered by branch or file path.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner":    {"type": "string"},
                "repo":     {"type": "string"},
                "branch":   {"type": "string", "description": "Branch name (default: default branch)"},
                "path":     {"type": "string", "description": "Only show commits touching this file"},
                "per_page": {"type": "integer", "default": 20},
            },
            "required": ["owner", "repo"],
        },
    ),
    Tool(
        name="github_get_commit",
        description="Get details about a specific commit including changed files.",
        inputSchema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo":  {"type": "string"},
                "sha":   {"type": "string", "description": "Commit SHA (full or abbreviated)"},
            },
            "required": ["owner", "repo", "sha"],
        },
    ),
    # ── Notifications ──
    Tool(
        name="github_list_notifications",
        description="List your GitHub notifications (unread by default).",
        inputSchema={
            "type": "object",
            "properties": {
                "show_all": {"type": "boolean", "default": False, "description": "Include read notifications too"},
            },
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
    except Exception as e:
        result = f"❌ Error: {e}"
    return [TextContent(type="text", text=result)]


# ── tool dispatch ──────────────────────────────────────────────────────────
async def _dispatch(name: str, args: dict) -> str:
    loop = asyncio.get_event_loop()

    # Run blocking requests calls in a thread pool so we don't block the event loop
    def sync(fn, *a, **kw):
        return loop.run_in_executor(None, lambda: fn(*a, **kw))

    # Clean up any expired auth_pending.json on every non-authorize call
    if name != "github_authorize" and PENDING_FILE.exists():
        try:
            with open(PENDING_FILE) as f:
                _p = json.load(f)
            if time.time() > _p.get("expires_at", 0):
                PENDING_FILE.unlink(missing_ok=True)
        except Exception:
            PENDING_FILE.unlink(missing_ok=True)

    # ── Auth ──────────────────────────────────────────────────────────────
    if name == "github_authorize":
        # Already have a valid token?
        existing = load_token()
        if existing:
            try:
                user = await sync(gh_get, "/user", existing)
                return (
                    f"✅ Already authorized as **@{user['login']}** — no action needed.\n"
                    f"Use `github_logout` first if you want to switch accounts."
                )
            except Exception:
                pass  # token invalid — fall through to re-auth

        cfg = load_config()

        # ── Phase 2: pending auth exists → poll for token ──────────────
        if PENDING_FILE.exists():
            try:
                with open(PENDING_FILE) as f:
                    pending = json.load(f)

                if time.time() > pending.get("expires_at", 0):
                    PENDING_FILE.unlink(missing_ok=True)
                    return (
                        "⏱️ That device code expired (15 min limit).\n"
                        "Call `github_authorize` again to get a fresh one."
                    )

                def _poll():
                    return device_flow_poll(pending)

                token = await loop.run_in_executor(None, _poll)

                if token:
                    save_token(token)
                    PENDING_FILE.unlink(missing_ok=True)
                    user = await sync(gh_get, "/user", token)
                    return (
                        f"✅ Authorized as **@{user['login']}** ({user.get('name') or ''})!\n"
                        f"Token saved to macOS Keychain — reused automatically every session."
                    )
                else:
                    # Update interval in case slow_down mutated it
                    with open(PENDING_FILE, "w") as f:
                        json.dump(pending, f)
                    return (
                        f"⏳ Still waiting — GitHub hasn't seen your authorization yet.\n\n"
                        f"Make sure you've entered code **`{pending['user_code']}`** at "
                        f"<https://github.com/login/device> and clicked **Authorize**.\n\n"
                        f"Then call `github_authorize` once more to complete."
                    )

            except Exception as e:
                PENDING_FILE.unlink(missing_ok=True)
                return f"❌ Authorization failed: {e}\nCall `github_authorize` to start again."

        # ── Phase 1: start device flow → show code, open browser ───────
        def _start():
            return device_flow_start(cfg["client_id"])

        pending = await loop.run_in_executor(None, _start)
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f)
        PENDING_FILE.chmod(0o600)  # owner-read/write only

        return (
            f"🔐 **GitHub Device Authorization**\n\n"
            f"Your browser has opened to GitHub's device page.\n"
            f"**Enter this code if prompted:** `{pending['user_code']}`\n\n"
            f"1. Go to <https://github.com/login/device> (already open)\n"
            f"2. Enter **`{pending['user_code']}`** if it isn't pre-filled\n"
            f"3. Click **Authorize**\n\n"
            f"Then call `github_authorize` again and I'll complete the flow."
        )

    if name == "github_status":
        token = load_token()
        if not token:
            return "❌ Not authorized. Call `github_authorize` to connect your GitHub account."
        try:
            user = await sync(gh_get, "/user", token)
            plan = (user.get("plan") or {}).get("name", "N/A")
            return (
                f"✅ Authenticated as **@{user['login']}**\n"
                f"  Name: {user.get('name') or '—'}\n"
                f"  Plan: {plan}\n"
                f"  Public repos: {user.get('public_repos', 0)}\n"
                f"  Private repos: {user.get('total_private_repos', 0)}\n"
                f"  Followers: {user.get('followers', 0)}"
            )
        except Exception as e:
            return f"⚠️ Token found but GitHub rejected it: {e}\nTry `github_logout` then `github_authorize`."

    if name == "github_logout":
        clear_token()
        return "✅ Token removed. Use `github_authorize` to reconnect."

    # ── User ──────────────────────────────────────────────────────────────
    if name == "github_whoami":
        token = require_token()
        username = args.get("username")
        path = f"/users/{username}" if username else "/user"
        u = await sync(gh_get, path, token)
        return json.dumps({
            "login":        u["login"],
            "name":         u.get("name"),
            "bio":          u.get("bio"),
            "company":      u.get("company"),
            "location":     u.get("location"),
            "email":        u.get("email"),
            "public_repos": u.get("public_repos"),
            "followers":    u.get("followers"),
            "following":    u.get("following"),
            "url":          u.get("html_url"),
            "created_at":   u.get("created_at", "")[:10],
        }, indent=2)

    # ── Repos ─────────────────────────────────────────────────────────────
    if name == "github_create_repo":
        token = require_token()
        payload = {
            "name":      args["name"],
            "private":   args.get("private", False),
            "auto_init": args.get("auto_init", False),
        }
        if args.get("description"):
            payload["description"] = args["description"]
        r = await sync(gh_post, "/user/repos", token, payload)
        return (
            f"✅ Created **{r['full_name']}** ({'private' if r['private'] else 'public'})\n"
            f"  {r['html_url']}\n"
            f"  Default branch: `{r.get('default_branch', 'main')}`"
        )

    if name == "github_upsert_file":
        token = require_token()
        owner, repo  = args["owner"], args["repo"]
        path         = args["path"]
        content_b64  = base64.b64encode(args["content"].encode()).decode()
        payload: dict = {
            "message": args["message"],
            "content": content_b64,
        }
        if args.get("branch"):
            payload["branch"] = args["branch"]
        # Fetch existing SHA automatically so callers don't need to supply it
        sha = args.get("sha")
        if not sha:
            try:
                existing = await sync(gh_get, f"/repos/{owner}/{repo}/contents/{path}", token,
                                      {"ref": args["branch"]} if args.get("branch") else {})
                if isinstance(existing, dict):
                    sha = existing.get("sha")
            except Exception:
                pass   # file doesn't exist yet — create mode
        if sha:
            payload["sha"] = sha
        result = await sync(gh_put, f"/repos/{owner}/{repo}/contents/{path}", token, payload)
        action = "Updated" if sha else "Created"
        cf = result.get("content", {})
        return f"✅ {action} `{cf.get('path', path)}`  ({cf.get('size', '?')} bytes)\n  {cf.get('html_url', '')}"

    if name == "github_push_files":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        files   = args["files"]
        message = args["message"]
        branch  = args.get("branch")

        if not files:
            return "❌ No files provided."

        # 1. Resolve default branch if not supplied
        if not branch:
            repo_info = await sync(gh_get, f"/repos/{owner}/{repo}", token)
            branch = repo_info["default_branch"]

        # 2. Get HEAD commit SHA for the branch
        ref = await sync(gh_get, f"/repos/{owner}/{repo}/git/ref/heads/{branch}", token)
        head_sha = ref["object"]["sha"]

        # 3. Get base tree SHA from the HEAD commit
        head_commit = await sync(gh_get, f"/repos/{owner}/{repo}/git/commits/{head_sha}", token)
        base_tree_sha = head_commit["tree"]["sha"]

        # 4. Create a blob for each file
        tree_entries = []
        for f in files:
            blob = await sync(gh_post, f"/repos/{owner}/{repo}/git/blobs", token, {
                "content":  f["content"],
                "encoding": "utf-8",
            })
            tree_entries.append({
                "path": f["path"],
                "mode": "100644",   # regular file
                "type": "blob",
                "sha":  blob["sha"],
            })

        # 5. Create new tree on top of the base tree
        new_tree = await sync(gh_post, f"/repos/{owner}/{repo}/git/trees", token, {
            "base_tree": base_tree_sha,
            "tree":      tree_entries,
        })

        # 6. Create the commit
        new_commit = await sync(gh_post, f"/repos/{owner}/{repo}/git/commits", token, {
            "message": message,
            "tree":    new_tree["sha"],
            "parents": [head_sha],
        })

        # 7. Fast-forward the branch ref
        await sync(gh_patch, f"/repos/{owner}/{repo}/git/refs/heads/{branch}", token, {
            "sha": new_commit["sha"],
        })

        short_sha = new_commit["sha"][:7]
        paths = "\n".join(f"  • `{f['path']}`" for f in files)
        return (
            f"✅ Committed {len(files)} file(s) to `{branch}` — `{short_sha}`\n\n"
            f"{paths}\n\n"
            f"  https://github.com/{owner}/{repo}/commit/{new_commit['sha']}"
        )

    if name == "github_list_repos":
        token = require_token()
        repos = await sync(gh_get, "/user/repos", token, {
            "visibility": args.get("visibility", "all"),
            "sort":       args.get("sort", "updated"),
            "per_page":   args.get("per_page", 25),
        })
        if not repos:
            return "No repositories found."
        return "\n\n".join(_fmt_repo(r) for r in repos)

    if name == "github_get_repo":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        r = await sync(gh_get, f"/repos/{owner}/{repo}", token)
        return json.dumps({
            "full_name":      r["full_name"],
            "description":    r.get("description"),
            "url":            r["html_url"],
            "default_branch": r.get("default_branch"),
            "language":       r.get("language"),
            "stars":          r.get("stargazers_count"),
            "forks":          r.get("forks_count"),
            "open_issues":    r.get("open_issues_count"),
            "visibility":     r.get("visibility"),
            "topics":         r.get("topics", []),
            "created_at":     r.get("created_at", "")[:10],
            "updated_at":     r.get("updated_at", "")[:10],
            "size_kb":        r.get("size"),
            "license":        (r.get("license") or {}).get("name"),
        }, indent=2)

    if name == "github_search_repos":
        token = require_token()
        result = await sync(gh_get, "/search/repositories", token, {
            "q":        args["query"],
            "sort":     args.get("sort", "stars"),
            "per_page": args.get("per_page", 10),
        })
        items = result.get("items", [])
        if not items:
            return f"No repositories found for: {args['query']}"
        lines = [f"Found {result.get('total_count', 0):,} repos (showing {len(items)}):\n"]
        lines += [_fmt_repo(r) for r in items]
        return "\n\n".join(lines)

    if name == "github_get_file":
        token = require_token()
        owner, repo, path = args["owner"], args["repo"], args["path"]
        params = {}
        if args.get("ref"):
            params["ref"] = args["ref"]
        data = await sync(gh_get, f"/repos/{owner}/{repo}/contents/{path}", token, params)
        if isinstance(data, list):
            return f"'{path}' is a directory — use `github_list_directory` instead."
        content_b64 = data.get("content", "")
        if content_b64:
            decoded = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            size = data.get("size", len(decoded))
            return f"# `{data['path']}` ({size:,} bytes)\n\n```\n{decoded}\n```"
        return json.dumps(data, indent=2)

    if name == "github_list_directory":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        path = args.get("path", "")
        params = {}
        if args.get("ref"):
            params["ref"] = args["ref"]
        data = await sync(gh_get, f"/repos/{owner}/{repo}/contents/{path}", token, params)
        if not isinstance(data, list):
            return f"'{path}' is a file — use `github_get_file` instead."
        dirs  = sorted([i for i in data if i["type"] == "dir"],  key=lambda x: x["name"])
        files = sorted([i for i in data if i["type"] == "file"], key=lambda x: x["name"])
        header = f"📁 `{owner}/{repo}`" + (f"/{path}" if path else "")
        lines = [header, ""]
        for d in dirs:
            lines.append(f"  📂 {d['name']}/")
        for f in files:
            lines.append(f"  📄 {f['name']}  ({f.get('size', 0):,} B)")
        return "\n".join(lines)

    # ── Issues ────────────────────────────────────────────────────────────
    if name == "github_list_issues":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        issues = await sync(gh_get, f"/repos/{owner}/{repo}/issues", token, {
            "state":    args.get("state", "open"),
            "per_page": args.get("per_page", 20),
        })
        issues = [i for i in issues if "pull_request" not in i]  # exclude PRs
        if not issues:
            return f"No {args.get('state', 'open')} issues in {owner}/{repo}."
        return "\n\n".join(_fmt_issue(i) for i in issues)

    if name == "github_get_issue":
        token = require_token()
        owner, repo, num = args["owner"], args["repo"], args["number"]
        issue    = await sync(gh_get, f"/repos/{owner}/{repo}/issues/{num}", token)
        comments = await sync(gh_get, f"/repos/{owner}/{repo}/issues/{num}/comments", token)
        labels = ", ".join(l["name"] for l in issue.get("labels", []))
        out = (
            f"# Issue #{issue['number']}: {issue['title']}\n"
            f"**State:** {issue['state']}  |  **Author:** @{issue['user']['login']}\n"
            f"**Labels:** {labels or 'none'}  |  **Comments:** {issue.get('comments', 0)}\n"
            f"**Created:** {issue['created_at'][:10]}  |  **Updated:** {issue['updated_at'][:10]}\n"
            f"**URL:** {issue['html_url']}\n\n"
            f"## Description\n{issue.get('body') or '_No description_'}"
        )
        if comments:
            out += f"\n\n## Comments ({len(comments)})"
            for c in comments:
                out += (
                    f"\n\n---\n**@{c['user']['login']}**  ({c['created_at'][:10]})\n"
                    f"{c.get('body', '')}"
                )
        return out

    if name == "github_create_issue":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        payload: dict = {"title": args["title"], "body": args.get("body", "")}
        if args.get("labels"):
            payload["labels"] = args["labels"]
        issue = await sync(gh_post, f"/repos/{owner}/{repo}/issues", token, payload)
        return f"✅ Created issue **#{issue['number']}**: {issue['title']}\n{issue['html_url']}"

    if name == "github_update_issue":
        token = require_token()
        owner, repo, num = args["owner"], args["repo"], args["number"]
        payload = {k: args[k] for k in ("title", "body", "state") if k in args}
        issue = await sync(gh_patch, f"/repos/{owner}/{repo}/issues/{num}", token, payload)
        return f"✅ Updated issue **#{issue['number']}**: {issue['title']} [{issue['state']}]\n{issue['html_url']}"

    if name == "github_add_comment":
        token = require_token()
        owner, repo, num = args["owner"], args["repo"], args["number"]
        comment = await sync(gh_post, f"/repos/{owner}/{repo}/issues/{num}/comments", token, {"body": args["body"]})
        return f"✅ Comment added: {comment['html_url']}"

    # ── PRs ───────────────────────────────────────────────────────────────
    if name == "github_list_prs":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        prs = await sync(gh_get, f"/repos/{owner}/{repo}/pulls", token, {
            "state":    args.get("state", "open"),
            "per_page": args.get("per_page", 20),
        })
        if not prs:
            return f"No {args.get('state', 'open')} PRs in {owner}/{repo}."
        return "\n\n".join(_fmt_pr(p) for p in prs)

    if name == "github_get_pr":
        token = require_token()
        owner, repo, num = args["owner"], args["repo"], args["number"]
        pr    = await sync(gh_get, f"/repos/{owner}/{repo}/pulls/{num}", token)
        files = await sync(gh_get, f"/repos/{owner}/{repo}/pulls/{num}/files", token)
        out = (
            f"# PR #{pr['number']}: {pr['title']}\n"
            f"**State:** {pr['state']}  |  **Author:** @{pr['user']['login']}\n"
            f"**Branch:** `{pr['head']['label']}` → `{pr['base']['label']}`\n"
            f"**Commits:** {pr.get('commits', 0)}  |  "
            f"**+{pr.get('additions', 0)} −{pr.get('deletions', 0)}** across {pr.get('changed_files', 0)} files\n"
            f"**Mergeable:** {pr.get('mergeable_state', 'unknown')}\n"
            f"**URL:** {pr['html_url']}\n\n"
            f"## Description\n{pr.get('body') or '_No description_'}"
        )
        if files:
            out += f"\n\n## Changed Files ({len(files)})\n"
            for f in files:
                out += f"  `{f['filename']}` (+{f['additions']} −{f['deletions']})\n"
        return out

    if name == "github_create_pr":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        pr = await sync(gh_post, f"/repos/{owner}/{repo}/pulls", token, {
            "title": args["title"],
            "head":  args["head"],
            "base":  args["base"],
            "body":  args.get("body", ""),
        })
        return f"✅ Created PR **#{pr['number']}**: {pr['title']}\n{pr['html_url']}"

    # ── Search ────────────────────────────────────────────────────────────
    if name == "github_search_code":
        token = require_token()
        result = await sync(gh_get, "/search/code", token, {
            "q":        args["query"],
            "per_page": args.get("per_page", 10),
        })
        items = result.get("items", [])
        if not items:
            return f"No code found for: {args['query']}"
        lines = [f"Found {result.get('total_count', 0):,} results (showing {len(items)}):\n"]
        for item in items:
            lines.append(
                f"**{item['repository']['full_name']}** — `{item['path']}`\n"
                f"  {item.get('html_url', '')}"
            )
        return "\n\n".join(lines)

    if name == "github_search_issues":
        token = require_token()
        result = await sync(gh_get, "/search/issues", token, {
            "q":        args["query"],
            "per_page": args.get("per_page", 15),
        })
        items = result.get("items", [])
        if not items:
            return f"No issues/PRs found for: {args['query']}"
        lines = [f"Found {result.get('total_count', 0):,} results (showing {len(items)}):\n"]
        for i in items:
            kind = "PR" if "pull_request" in i else "Issue"
            lines.append(
                f"[{kind}] **{i['repository_url'].split('repos/')[-1]}** "
                f"#{i['number']} — {i['title']} [{i['state']}]\n"
                f"  {i.get('html_url', '')}"
            )
        return "\n\n".join(lines)

    # ── Commits ───────────────────────────────────────────────────────────
    if name == "github_list_commits":
        token = require_token()
        owner, repo = args["owner"], args["repo"]
        params: dict = {"per_page": args.get("per_page", 20)}
        if args.get("branch"):
            params["sha"] = args["branch"]
        if args.get("path"):
            params["path"] = args["path"]
        commits = await sync(gh_get, f"/repos/{owner}/{repo}/commits", token, params)
        if not commits:
            return f"No commits found in {owner}/{repo}."
        lines = []
        for c in commits:
            msg    = c["commit"]["message"].split("\n")[0][:80]
            author = c["commit"]["author"]["name"]
            date   = c["commit"]["author"]["date"][:10]
            sha    = c["sha"][:7]
            lines.append(f"`{sha}`  **{msg}**\n  {author} · {date}")
        return "\n\n".join(lines)

    if name == "github_get_commit":
        token = require_token()
        owner, repo, sha = args["owner"], args["repo"], args["sha"]
        c = await sync(gh_get, f"/repos/{owner}/{repo}/commits/{sha}", token)
        msg    = c["commit"]["message"]
        author = c["commit"]["author"]["name"]
        date   = c["commit"]["author"]["date"][:10]
        stats  = c.get("stats", {})
        files  = c.get("files", [])
        out = (
            f"# Commit `{c['sha'][:7]}`\n"
            f"**Message:** {msg}\n"
            f"**Author:** {author}  ·  {date}\n"
            f"**Stats:** +{stats.get('additions', 0)} −{stats.get('deletions', 0)} "
            f"in {len(files)} file(s)\n"
        )
        if files:
            out += "\n## Changed Files\n"
            for f in files[:30]:
                out += f"  `{f['filename']}` (+{f['additions']} −{f['deletions']})\n"
        return out

    # ── Notifications ─────────────────────────────────────────────────────
    if name == "github_list_notifications":
        token = require_token()
        show_all = args.get("show_all", False)
        notifs = await sync(gh_get, "/notifications", token, {"all": show_all, "per_page": 50})
        if not notifs:
            return "🎉 No unread notifications!" if not show_all else "No notifications."
        label = "total" if show_all else "unread"
        lines = [f"📬 {len(notifs)} {label} notification(s):\n"]
        for n in notifs:
            repo    = n.get("repository", {}).get("full_name", "")
            subject = n.get("subject", {})
            dot     = "●" if n.get("unread") else "○"
            lines.append(
                f"{dot} **{repo}** [{subject.get('type', '?')}]  {subject.get('title', '')}\n"
                f"  Reason: {n.get('reason', '')}  ·  {n.get('updated_at', '')[:10]}"
            )
        return "\n\n".join(lines)

    return f"Unknown tool: {name}"


# ── formatters ─────────────────────────────────────────────────────────────
def _fmt_repo(r: dict) -> str:
    return (
        f"**{r['full_name']}** ({r.get('visibility', '?')})\n"
        f"  {r.get('description') or '_No description_'}\n"
        f"  ⭐ {r.get('stargazers_count', 0):,}  🍴 {r.get('forks_count', 0):,}  "
        f"Language: {r.get('language') or 'N/A'}  "
        f"Updated: {r.get('updated_at', '')[:10]}"
    )


def _fmt_issue(i: dict) -> str:
    labels = ", ".join(l["name"] for l in i.get("labels", []))
    return (
        f"#{i['number']} **{i['title']}** [{i['state']}]\n"
        f"  @{i['user']['login']}  ·  💬 {i.get('comments', 0)}  "
        f"Labels: {labels or 'none'}\n"
        f"  {i.get('html_url', '')}"
    )


def _fmt_pr(p: dict) -> str:
    return (
        f"#{p['number']} **{p['title']}** [{p['state']}]\n"
        f"  @{p['user']['login']}  ·  "
        f"`{p.get('head', {}).get('label', '?')}` → `{p.get('base', {}).get('label', '?')}`\n"
        f"  {p.get('html_url', '')}"
    )


# ── entry point ────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
