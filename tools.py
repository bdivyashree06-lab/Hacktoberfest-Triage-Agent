"""Tool registry: GitHub API + a local clone of the demo repo.

Design rules:
  * Tools RETURN data or an error string — they never raise at the agent.
  * All GitHub calls go through _github_get(), which degrades gracefully on
    rate limits / network loss (failure path #5).
  * Write actions are only PROPOSED here; a human clicks Approve in the UI
    before anything touches GitHub (responsible-use requirement).
  * Credentials are never printed: output is scrubbed through _scrub().
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import uuid

import requests

GITHUB_API = "https://api.github.com"
DEMO_REPO = os.environ.get("DEMO_REPO", "")  # e.g. "youruser/triage-demo"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_DIR = os.environ.get("REPO_DIR", "/tmp/triage-repo")
TOOL_TIMEOUT = float(os.environ.get("TOOL_TIMEOUT", "15"))
TOOL_RETRIES = 2
MAX_FILE_LINES = 80

ALLOWED_LABELS = ("bug", "enhancement", "documentation", "question", "duplicate")

# action_id -> {"type": "label"|"comment", "issue": int, "description": str, ...}
PENDING: dict[str, dict] = {}


def _scrub(text: str) -> str:
    """Protect credentials (explicit participant-guideline requirement)."""
    if GITHUB_TOKEN:
        text = text.replace(GITHUB_TOKEN, "***")
    return text


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _github_get(path: str) -> tuple[dict | list | None, str | None]:
    """Failure path #5 — returns (data, error). Never raises."""
    try:
        r = requests.get(f"{GITHUB_API}{path}", headers=_headers(), timeout=10)
    except requests.RequestException as exc:
        return None, f"network error ({type(exc).__name__}) — continue with local repo data"
    if r.status_code in (403, 429):
        reset = r.headers.get("X-RateLimit-Reset", "?")
        return None, f"GitHub rate limited (resets at epoch {reset}) — continue with local data"
    if r.status_code == 404:
        return None, f"not found: {path} (is DEMO_REPO set to 'owner/repo'?)"
    if not r.ok:
        return None, f"GitHub HTTP {r.status_code}"
    return r.json(), None


def setup() -> dict:
    """Shallow-clone the demo repo once so search/read tools work offline."""
    if os.path.isdir(os.path.join(REPO_DIR, ".git")):
        return {"clone": REPO_DIR}
    if not DEMO_REPO:
        return {"error": "DEMO_REPO env var is not set"}
    if GITHUB_TOKEN:
        url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{DEMO_REPO}.git"
    else:
        url = f"https://github.com/{DEMO_REPO}.git"
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, REPO_DIR],
            capture_output=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"error": _scrub(f"clone failed: {type(exc).__name__}: {exc}")}
    if proc.returncode != 0:
        return {"error": _scrub("clone failed: " + proc.stderr.decode(errors="replace")[:300])}
    return {"clone": REPO_DIR}


# ---------------------------------------------------------------- read-only tools

def get_issue(number: int) -> dict:
    """Fetch one issue: number, title, state, author, labels, body."""
    data, err = _github_get(f"/repos/{DEMO_REPO}/issues/{int(number)}")
    if err:
        return {"error": err}
    return {
        "number": data["number"],
        "title": data["title"],
        "state": data["state"],
        "author": (data.get("user") or {}).get("login"),
        "labels": [l["name"] for l in data.get("labels", [])],
        "created_at": data.get("created_at"),
        "comments": data.get("comments", 0),
        "body": (data.get("body") or "")[:3000],
    }


def list_issues(limit: int = 15, state: str = "all") -> dict:
    """List recent issues (PRs excluded) — used for duplicate detection.
    state: "open", "closed", or "all"."""
    limit = min(int(limit or 15), 50)
    if state not in ("open", "closed", "all"):
        return {"error": f"state must be open|closed|all, got {state!r}"}
    data, err = _github_get(f"/repos/{DEMO_REPO}/issues?state={state}&per_page={limit}")
    if err:
        return {"error": err}
    return {
        "issues": [
            {"number": i["number"], "title": i["title"], "state": i["state"]}
            for i in data if "pull_request" not in i
        ]
    }


def search_repo(query: str, limit: int = 10) -> dict:
    """Substring search over the local clone (no embeddings, no network)."""
    if not os.path.isdir(REPO_DIR):
        return {"error": "local clone unavailable (setup failed) — use list_issues instead"}
    q = str(query).lower()
    limit = min(int(limit or 10), 20)
    hits: list[str] = []
    for root, dirs, files in os.walk(REPO_DIR):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > 1_000_000:
                    continue
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if q in line.lower():
                            rel = os.path.relpath(path, REPO_DIR)
                            hits.append(f"{rel}:{lineno}: {line.strip()[:160]}")
                            if len(hits) >= limit:
                                return {"query": query, "hits": hits}
            except OSError:
                continue
    return {"query": query, "hits": hits}


def read_file(path: str, limit: int = MAX_FILE_LINES) -> dict:
    """Read a file from the clone, capped at MAX_FILE_LINES (protects the context window)."""
    limit = min(int(limit or MAX_FILE_LINES), MAX_FILE_LINES)
    root = os.path.realpath(REPO_DIR)
    target = os.path.realpath(os.path.join(REPO_DIR, str(path)))
    if not (target == root or target.startswith(root + os.sep)):
        return {"error": "path escapes the repository"}
    try:
        with open(target, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[:limit]
    except OSError as exc:
        return {"error": f"cannot read {path}: {exc}"}
    return {"path": path, "lines": len(lines), "content": "".join(lines)}


# --------------------------------------------- write proposals (human approval gate)

def propose_label(number: int, label: str) -> dict:
    """PROPOSE a label; it is applied only after a human clicks Approve."""
    if label not in ALLOWED_LABELS:
        return {"error": f"label '{label}' not allowed; choose from {list(ALLOWED_LABELS)}"}
    action_id = uuid.uuid4().hex[:8]
    PENDING[action_id] = {
        "type": "label", "issue": int(number), "label": label,
        "description": f"Label #{number} as '{label}'",
    }
    return {
        "pending_approval": action_id,
        "message": f"Proposed: label #{number} as '{label}'. Awaiting human approval — nothing was sent yet.",
    }


def propose_comment(number: int, body: str) -> dict:
    """PROPOSE an issue comment; it is posted only after a human clicks Approve."""
    if not str(body).strip():
        return {"error": "comment body is empty"}
    action_id = uuid.uuid4().hex[:8]
    PENDING[action_id] = {
        "type": "comment", "issue": int(number), "body": str(body),
        "description": f"Comment on #{number}: {str(body)[:80]!r}",
    }
    return {
        "pending_approval": action_id,
        "message": f"Proposed a comment on #{number}. Awaiting human approval — nothing was sent yet.",
    }


def pending_snapshot() -> list[dict]:
    return [{"id": k, "description": v["description"]} for k, v in PENDING.items()]


def approve(action_id: str) -> dict:
    """Called ONLY from the UI's Approve button — never callable by the model.
    The proposal stays pending until the write actually succeeds, so a failed
    approval can be retried instead of silently vanishing."""
    action = PENDING.get(action_id)
    if not action:
        return {"error": "unknown or already-resolved action"}
    if action["type"] == "label":
        path, body = f"/repos/{DEMO_REPO}/issues/{action['issue']}/labels", {"labels": [action["label"]]}
    else:
        path, body = f"/repos/{DEMO_REPO}/issues/{action['issue']}/comments", {"body": action["body"]}
    try:
        r = requests.post(f"{GITHUB_API}{path}", headers=_headers(), json=body, timeout=10)
    except requests.RequestException as exc:
        return {"error": f"network error: {type(exc).__name__} — proposal kept, retry Approve"}
    if not r.ok:
        return {"error": f"GitHub HTTP {r.status_code} — proposal kept, retry Approve"}
    PENDING.pop(action_id, None)  # only drop the proposal after GitHub accepted it
    try:
        data = r.json()
    except ValueError:
        data = None
    # Label endpoint returns a LIST of label objects; comment endpoint an object.
    if isinstance(data, dict):
        url = data.get("html_url")
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        url = data[0].get("html_url")
    else:
        url = None
    return {"applied": True, "url": url}


def deny(action_id: str) -> dict:
    action = PENDING.pop(action_id, None)
    if not action:
        return {"error": "unknown or already-resolved action"}
    return {"denied": True, "description": action["description"]}


# ------------------------------------------------------------------- execution

TOOLS = {
    "get_issue": get_issue,
    "list_issues": list_issues,
    "search_repo": search_repo,
    "read_file": read_file,
    "propose_label": propose_label,
    "propose_comment": propose_comment,
    # NOTE: approve/deny are intentionally NOT in TOOLS — the model can never
    # execute a write itself. Only the human UI can.
}


def execute_tool(name: str, args: dict) -> dict | str:
    """Failure paths #2-4: unknown tool, bad args, crashing tool, hanging tool.

    Never raises: errors are returned as observations the agent can react to.
    """
    if name not in TOOLS:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(sorted(TOOLS))}"
    if not isinstance(args, dict):
        args = {}
    last = ""
    for _ in range(TOOL_RETRIES):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(TOOLS[name], **args)
                return future.result(timeout=TOOL_TIMEOUT)
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}. Check the tool's docstring for the signature."
        except concurrent.futures.TimeoutError:
            last = f"ERROR: {name} timed out after {TOOL_TIMEOUT:.0f}s"
        except Exception as exc:  # noqa: BLE001 — failure handling is a feature here
            last = f"ERROR: {name} failed: {type(exc).__name__}: {exc}"
    return _scrub(last) + " — adapt your approach or try a different tool."
