"""GitHub skill: list repos, create issues, trigger repository_dispatch."""
from _lib import http, secret

NAME = "github"
DESCRIPTION = (
    "GitHub actions. 'list_repos' = your most recently pushed repos. "
    "'create_issue' needs repo + title (+ optional body). "
    "'dispatch' fires a repository_dispatch with event_type (to trigger a GitHub Action). "
    "repo may be 'owner/name' or just 'name' (your own account is assumed)."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action":     {"type": "string", "enum": ["list_repos", "create_issue", "dispatch"]},
        "repo":       {"type": "string"},
        "title":      {"type": "string"},
        "body":       {"type": "string"},
        "event_type": {"type": "string"},
    },
    "required": ["action"],
}

API = "https://api.github.com"


def _headers(tok):
    return {"Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "runai"}


def _full_repo(repo, headers):
    if "/" in repo:
        return repo
    me = http("GET", f"{API}/user", headers=headers)
    login = me.get("login") if isinstance(me, dict) else None
    return f"{login}/{repo}" if login else repo


def run(args):
    tok = secret("github_token")
    if not tok:
        return "No GitHub token. Add \"github_token\" (a PAT with repo scope) to ~/.runai/secrets.json."
    H = _headers(tok)
    action = (args.get("action") or "").lower()

    if action == "list_repos":
        repos = http("GET", f"{API}/user/repos?per_page=20&sort=pushed&affiliation=owner", headers=H)
        if not isinstance(repos, list):
            return f"GitHub error: {repos}"
        return "\n".join(f"{r['full_name']}  (pushed {str(r.get('pushed_at', '?'))[:10]})" for r in repos) or "(no repos)"

    if action == "create_issue":
        repo, title = args.get("repo"), args.get("title")
        if not (repo and title):
            return "create_issue needs 'repo' and 'title'."
        full = _full_repo(repo, H)
        r = http("POST", f"{API}/repos/{full}/issues", headers=H,
                 body={"title": title, "body": args.get("body", "")})
        return f"Opened {r['html_url']}" if isinstance(r, dict) and r.get("html_url") else f"GitHub error: {r}"

    if action == "dispatch":
        repo = args.get("repo")
        if not repo:
            return "dispatch needs 'repo'."
        ev = args.get("event_type", "runai")
        full = _full_repo(repo, H)
        r = http("POST", f"{API}/repos/{full}/dispatches", headers=H, body={"event_type": ev})
        if r in ("", None) or (isinstance(r, dict) and not r.get("_error")):
            return f"Dispatched '{ev}' to {full}."
        return f"GitHub error: {r}"

    return f"Unknown action: {action}"
