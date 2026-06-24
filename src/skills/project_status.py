"""Project status skill — git status across all projects in ~/Desktop/Cld."""
import os
import subprocess

NAME = "project_status"
DESCRIPTION = (
    "Get a git status snapshot across all DJ's projects in ~/Desktop/Cld. "
    "Returns which repos are dirty, ahead of remote, have uncommitted changes, or recent commits. "
    "Use compact=true (default) for a summary view, or compact=false to see every changed file. "
    "Optional 'project' arg narrows to a single project."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string",
                    "description": "optional: single project dir to check (e.g. 'nova-daw')"},
        "folder":  {"type": "string",
                    "description": "root folder to scan (default ~/Desktop/Cld)"},
        "compact": {"type": "boolean",
                    "description": "true = show file counts only (default); false = show all changed files"},
    },
    "required": [],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

_ROOT = os.path.expanduser("~/Desktop/Cld")


def run(args):
    folder = os.path.expanduser(args.get("folder") or _ROOT)
    single = (args.get("project") or "").strip()
    compact = args.get("compact", True)

    if single:
        rows = [_check(single, os.path.join(folder, single))]
    else:
        try:
            names = sorted(os.listdir(folder))
        except OSError as e:
            return f"Error listing {folder}: {e}"
        rows = []
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isdir(path) and os.path.isdir(os.path.join(path, ".git")):
                rows.append(_check(name, path))

    if not rows:
        return "No git repos found in " + folder

    dirty  = [r for r in rows if r["dirty"]]
    clean  = [r for r in rows if not r["dirty"] and not r["error"]]
    errors = [r for r in rows if r["error"]]
    ahead  = [r for r in rows if r["ahead"]]
    behind = [r for r in rows if r["behind"]]

    lines = [
        f"Checked {len(rows)} repos — {len(dirty)} dirty · {len(clean)} clean · {len(ahead)} ahead · {len(behind)} behind",
    ]

    if ahead:
        lines.append("")
        lines.append("PUSH NEEDED:")
        for r in ahead:
            ahead_n = r.get("ahead_n", "")
            lines.append(f"  {r['name']}  ({ahead_n} commits ahead)  —  {r['last'][:60]}")

    if behind:
        lines.append("")
        lines.append("BEHIND REMOTE:")
        for r in behind:
            lines.append(f"  {r['name']}  ({r.get('behind_n','')} behind)  —  {r['last'][:60]}")

    if dirty:
        lines.append("")
        lines.append("DIRTY:")
        for r in dirty:
            file_lines = [l for l in r["status"].splitlines()[1:] if l.strip()]
            n_mod  = sum(1 for l in file_lines if l.startswith(" M") or l.startswith("M "))
            n_del  = sum(1 for l in file_lines if l.startswith(" D") or l.startswith("D "))
            n_add  = sum(1 for l in file_lines if l.startswith("A "))
            n_unk  = sum(1 for l in file_lines if l.startswith("??"))
            parts  = []
            if n_mod: parts.append(f"{n_mod} modified")
            if n_del: parts.append(f"{n_del} deleted")
            if n_add: parts.append(f"{n_add} added")
            if n_unk: parts.append(f"{n_unk} untracked")
            summary = "  " + ", ".join(parts) if parts else "  changes"
            lines.append(f"  {r['name']:30s} {summary}")
            lines.append(f"  {'':30s}   last: {r['last'][:55]}")
            if not compact:
                for fl in file_lines[:12]:
                    lines.append(f"  {'':30s}   {fl}")
                if len(file_lines) > 12:
                    lines.append(f"  {'':30s}   ... +{len(file_lines)-12} more")

    if clean:
        lines.append("")
        lines.append("CLEAN: " + "  ".join(r["name"] for r in clean))

    if errors:
        lines.append("")
        lines.append("ERRORS: " + ", ".join(r["name"] for r in errors))

    return "\n".join(lines)


def _check(name, path):
    if not os.path.isdir(path):
        return {"name": name, "dirty": False, "ahead": False, "behind": False, "error": True,
                "status": "", "branch_line": "", "last": ""}
    try:
        st = subprocess.run(["git", "status", "--short", "--branch"],
                            cwd=path, capture_output=True, text=True, timeout=6)
        lg = subprocess.run(["git", "log", "--oneline", "-1"],
                            cwd=path, capture_output=True, text=True, timeout=6)
        status = st.stdout.strip()
        lines = status.splitlines()
        branch_line = lines[0] if lines else ""
        file_lines = [l for l in lines[1:] if l.strip()]
        ahead  = "ahead" in branch_line
        behind = "behind" in branch_line
        dirty  = bool(file_lines)

        import re
        ahead_n, behind_n = "", ""
        m = re.search(r"ahead (\d+)", branch_line)
        if m: ahead_n = m.group(1)
        m = re.search(r"behind (\d+)", branch_line)
        if m: behind_n = m.group(1)

        return {
            "name": name, "dirty": dirty, "ahead": ahead, "behind": behind,
            "error": False, "status": "\n".join(lines),
            "branch_line": branch_line, "last": lg.stdout.strip(),
            "ahead_n": ahead_n, "behind_n": behind_n,
        }
    except Exception as e:
        return {"name": name, "dirty": False, "ahead": False, "behind": False,
                "error": True, "status": str(e), "branch_line": "", "last": ""}
