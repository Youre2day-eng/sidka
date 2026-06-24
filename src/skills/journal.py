"""Work journal skill — read and write daily work notes to ~/.runai/journal.md"""
import os
import time

NAME = "journal"
DESCRIPTION = (
    "Read or write to the work journal at ~/.runai/journal.md. "
    "Use 'write' to append a dated entry about what you worked on, decisions made, or blockers. "
    "Use 'read' to retrieve recent journal entries (last N days or all). "
    "Use 'today' to read just today's entries."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action":  {"type": "string", "enum": ["read", "write", "today"],
                    "description": "read: return recent entries; write: append new entry; today: read today only"},
        "entry":   {"type": "string", "description": "text to append (for write action)"},
        "days":    {"type": "integer", "description": "how many days of history to return (read action, default 7)"},
        "project": {"type": "string", "description": "optional project tag for the entry (e.g. 'nova-daw')"},
    },
    "required": ["action"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

JOURNAL_FILE = os.path.expanduser("~/.runai/journal.md")


def run(args):
    action = (args.get("action") or "").lower()

    if action == "write":
        return _write(args.get("entry", ""), args.get("project", ""))

    if action in ("read", "today"):
        days = 1 if action == "today" else int(args.get("days", 7))
        return _read(days)

    return f"Unknown action: {action}"


def _write(entry, project):
    entry = (entry or "").strip()
    if not entry:
        return "Error: entry text is required."
    os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    header = f"## {today}" + (f" [{project}]" if project else "")
    timestamp = time.strftime("%H:%M")

    existing = ""
    if os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            existing = f.read()

    # If today's header already exists, append under it; otherwise prepend a new section.
    if f"## {today}" in existing:
        lines = existing.splitlines(keepends=True)
        out = []
        inserted = False
        for line in lines:
            out.append(line)
            if not inserted and line.strip() == f"## {today}" or (
                project and line.strip().startswith(f"## {today}")):
                out.append(f"\n**{timestamp}** — {entry}\n")
                inserted = True
        if not inserted:
            out.append(f"\n**{timestamp}** — {entry}\n")
        new_content = "".join(out)
    else:
        new_section = f"{header}\n\n**{timestamp}** — {entry}\n\n"
        new_content = new_section + existing

    with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)
    return f"Journal entry saved ({today} {timestamp})."


def _read(days):
    if not os.path.exists(JOURNAL_FILE):
        return "(journal is empty — use action='write' to add your first entry)"
    with open(JOURNAL_FILE, encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        return "(journal is empty)"

    # Filter to sections whose date header falls within the last `days` days.
    cutoff = time.strftime(
        "%Y-%m-%d",
        time.localtime(time.time() - days * 86400)
    )
    sections = []
    current = []
    in_range = False
    for line in content.splitlines():
        if line.startswith("## "):
            # Extract date from header like "## 2026-06-20" or "## 2026-06-20 [project]"
            date_part = line[3:13]
            if len(date_part) == 10 and date_part >= cutoff:
                in_range = True
                if current:
                    sections.append("\n".join(current))
                current = [line]
            else:
                in_range = False
                if current:
                    sections.append("\n".join(current))
                current = []
        elif in_range:
            current.append(line)
    if current:
        sections.append("\n".join(current))

    result = "\n\n".join(sections).strip()
    return result[:6000] if result else f"(no journal entries in the last {days} days)"
