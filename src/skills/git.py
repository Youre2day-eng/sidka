"""Local git skill — run git operations on any project on the user's machine."""
import os
import subprocess

NAME = "git"
DESCRIPTION = (
    "Run local git operations on any project directory. "
    "Safe actions (status, log, diff, branch) run immediately. "
    "Destructive actions (add, commit, push, reset, checkout) require cockpit approval. "
    "'path' is the project directory (supports ~). "
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["status", "log", "diff", "branch", "add", "commit", "push", "reset", "checkout"],
            "description": "git subcommand to run",
        },
        "path":    {"type": "string", "description": "path to the git repo root (e.g. ~/Desktop/Cld/nova-daw)"},
        "message": {"type": "string", "description": "commit message (for commit action)"},
        "files":   {"type": "string", "description": "files to add, space-separated (for add; '.' for all)"},
        "args":    {"type": "string", "description": "extra flags passed verbatim (e.g. '--oneline -20' for log)"},
    },
    "required": ["action", "path"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

_SAFE = {"status", "log", "diff", "branch"}


def run(args):
    action = (args.get("action") or "").lower()
    cwd = os.path.expanduser(args.get("path", "~"))
    extra = str(args.get("args", "") or "").strip()

    if not os.path.isdir(cwd):
        return f"Error: directory not found: {cwd}"

    if action == "status":
        return _git(cwd, ["git", "status", "--short", "--branch"])

    if action == "log":
        flags = extra or "--oneline -15"
        return _git(cwd, ["git", "log"] + flags.split())

    if action == "diff":
        cmd = ["git", "diff"] + (extra.split() if extra else [])
        out = _git(cwd, cmd)
        return out[:5000] + "\n(truncated)" if len(out) > 5000 else (out or "(no diff)")

    if action == "branch":
        return _git(cwd, ["git", "branch", "-a"])

    if action == "add":
        files = str(args.get("files", ".") or ".").strip()
        return _git(cwd, ["git", "add"] + files.split())

    if action == "commit":
        message = str(args.get("message", "") or "").strip()
        if not message:
            return "Error: commit needs a 'message'."
        return _git(cwd, ["git", "commit", "-m", message])

    if action == "push":
        cmd = ["git", "push"] + (extra.split() if extra else [])
        return _git(cwd, cmd)

    if action == "reset":
        cmd = ["git", "reset"] + (extra.split() if extra else ["HEAD"])
        return _git(cwd, cmd)

    if action == "checkout":
        branch = extra or args.get("args", "")
        if not branch:
            return "Error: checkout needs a branch name in 'args'."
        return _git(cwd, ["git", "checkout"] + branch.split())

    return f"Unknown action: {action}"


def _git(cwd, cmd):
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=30
        )
        out = (result.stdout + result.stderr).strip()
        return out[:4000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: git command timed out."
    except FileNotFoundError:
        return "Error: git not found. Is git installed?"
    except Exception as e:
        return f"Error: {e}"
