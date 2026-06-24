"""Task engine skill — create, track and query tasks across all projects."""
import os, json, uuid, datetime

NAME = "tasks"
DESCRIPTION = (
    "Full task management for DJ's projects. Create, update, list, search and close tasks. "
    "Categories: dev | brand | campaign | content | crm | release | research. "
    "Statuses: todo | doing | done | blocked | cancelled. Priorities: high | medium | low. "
    "Call this whenever a user mentions a bug, feature, TODO, campaign task, or any trackable work item."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create","list","update","close","block","cancel","delete","search","summary"],
            "description": "Action to perform"
        },
        "title":    {"type": "string",  "description": "Task title (for create/search)"},
        "project":  {"type": "string",  "description": "Project name e.g. nova-daw, fsbacktrack"},
        "category": {"type": "string",  "enum": ["dev","brand","campaign","content","crm","release","research"]},
        "status":   {"type": "string",  "enum": ["todo","doing","done","blocked","cancelled"]},
        "priority": {"type": "string",  "enum": ["high","medium","low"]},
        "tags":     {"type": "array",   "items": {"type": "string"}},
        "notes":    {"type": "string",  "description": "Extra context or description"},
        "id":       {"type": "string",  "description": "Task ID for update/close/block/delete"},
        "field":    {"type": "string",  "description": "Field name to update"},
        "value":    {"type": "string",  "description": "New value for the field"},
        "query":    {"type": "string",  "description": "Search string"},
        "limit":    {"type": "integer", "description": "Max results for list (default 20)"},
    },
    "required": ["action"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

_TASKS_FILE = os.path.expanduser("~/.runai/tasks.jsonl")
_ARCHIVE_FILE = os.path.expanduser("~/.runai/tasks_archive.jsonl")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")

def _load():
    tasks = []
    if not os.path.exists(_TASKS_FILE):
        return tasks
    with open(_TASKS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    tasks.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return tasks

def _save(tasks):
    tmp = _TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")
    os.replace(tmp, _TASKS_FILE)

def _make_id():
    return "t_" + uuid.uuid4().hex[:8]

def _fmt(t):
    pri_sym = {"high": "!!!", "medium": "!!", "low": "!"}
    tags = (" [" + ", ".join(t.get("tags", [])) + "]") if t.get("tags") else ""
    notes = (f"\n    notes: {t['notes'][:120]}") if t.get("notes") else ""
    age = ""
    try:
        created = datetime.datetime.fromisoformat(t["created"])
        delta = datetime.datetime.now() - created
        if delta.days > 0:
            age = f" · {delta.days}d ago"
        elif delta.seconds > 3600:
            age = f" · {delta.seconds//3600}h ago"
    except Exception:
        pass
    return (f"[{t['id']}] {pri_sym.get(t.get('priority','medium'), '!')} "
            f"{t['title']}{tags}\n"
            f"    {t.get('project','—')} · {t.get('category','dev')} · "
            f"{t.get('status','todo')}{age}{notes}")


def run(args):
    action = (args.get("action") or "list").lower()

    if action == "create":
        return _create(args)
    if action == "list":
        return _list(args)
    if action == "update":
        return _update(args)
    if action in ("close", "done"):
        return _set_status(args.get("id"), "done")
    if action == "block":
        return _set_status(args.get("id"), "blocked", args.get("notes"))
    if action in ("cancel", "cancelled"):
        return _set_status(args.get("id"), "cancelled")
    if action == "delete":
        return _delete(args.get("id"))
    if action == "search":
        return _search(args.get("query", ""))
    if action == "summary":
        return _summary(args)
    return f"Unknown action: {action}"


def _create(args):
    title = (args.get("title") or "").strip()
    if not title:
        return "Error: title is required."
    task = {
        "id":       _make_id(),
        "title":    title,
        "project":  (args.get("project") or "general").strip().lower(),
        "category": (args.get("category") or "dev").lower(),
        "status":   (args.get("status") or "todo").lower(),
        "priority": (args.get("priority") or "medium").lower(),
        "tags":     [t.strip() for t in (args.get("tags") or []) if t.strip()],
        "notes":    (args.get("notes") or "").strip(),
        "created":  _now(),
        "updated":  _now(),
    }
    tasks = _load()
    tasks.append(task)
    _save(tasks)
    return f"Created: {_fmt(task)}"


def _list(args):
    tasks = _load()
    project  = (args.get("project") or "").lower()
    category = (args.get("category") or "").lower()
    status   = (args.get("status") or "").lower()
    limit    = int(args.get("limit") or 30)

    if project:  tasks = [t for t in tasks if t.get("project","").lower() == project]
    if category: tasks = [t for t in tasks if t.get("category","").lower() == category]
    if status:   tasks = [t for t in tasks if t.get("status","").lower() == status]

    # Sort: active first, then by priority, then by created desc
    prio_order = {"high": 0, "medium": 1, "low": 2}
    stat_order = {"doing": 0, "todo": 1, "blocked": 2, "done": 3, "cancelled": 4}
    tasks.sort(key=lambda t: (
        stat_order.get(t.get("status","todo"), 5),
        prio_order.get(t.get("priority","medium"), 3),
        t.get("created",""),
    ))
    tasks = tasks[:limit]

    if not tasks:
        return "No tasks found."
    lines = [f"{len(tasks)} task(s):"]
    for t in tasks:
        lines.append(_fmt(t))
    return "\n\n".join(lines)


def _update(args):
    tid   = (args.get("id") or "").strip()
    field = (args.get("field") or "").strip()
    value = args.get("value")
    if not tid or not field:
        return "Error: id and field are required for update."
    tasks = _load()
    for t in tasks:
        if t["id"] == tid:
            allowed = {"title","project","category","status","priority","notes","tags"}
            if field not in allowed:
                return f"Field '{field}' not updatable. Allowed: {', '.join(sorted(allowed))}"
            if field == "tags" and isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            t[field]   = value
            t["updated"] = _now()
            _save(tasks)
            return f"Updated {field} on {tid}:\n{_fmt(t)}"
    return f"Task {tid} not found."


def _set_status(tid, status, notes=None):
    if not tid:
        return f"Error: id is required to mark as {status}."
    tasks = _load()
    for t in tasks:
        if t["id"] == tid:
            t["status"]  = status
            t["updated"] = _now()
            if notes:
                t["notes"] = ((t.get("notes") or "") + f"\n[{_now()}] {notes}").strip()
            _save(tasks)
            return f"Marked {status}: {_fmt(t)}"
    return f"Task {tid} not found."


def _delete(tid):
    if not tid:
        return "Error: id is required."
    tasks = _load()
    kept  = [t for t in tasks if t["id"] != tid]
    if len(kept) == len(tasks):
        return f"Task {tid} not found."
    _save(kept)
    return f"Deleted task {tid}."


def _search(query):
    if not query:
        return "Error: query is required."
    q = query.lower()
    tasks = _load()
    matches = [t for t in tasks if
               q in t.get("title","").lower() or
               q in t.get("notes","").lower() or
               q in t.get("project","").lower() or
               any(q in tag.lower() for tag in t.get("tags",[]))]
    if not matches:
        return f"No tasks matching '{query}'."
    lines = [f"{len(matches)} match(es) for '{query}':"]
    for t in matches:
        lines.append(_fmt(t))
    return "\n\n".join(lines)


def _summary(args):
    tasks = _load()
    if not tasks:
        return "No tasks yet."

    from collections import Counter
    by_status   = Counter(t.get("status","todo") for t in tasks)
    by_category = Counter(t.get("category","dev") for t in tasks)
    by_project  = Counter(t.get("project","general") for t in tasks)
    by_priority = Counter(t.get("priority","medium") for t in tasks if t.get("status") not in ("done","cancelled"))

    active = [t for t in tasks if t.get("status") not in ("done","cancelled")]
    blocked = [t for t in tasks if t.get("status") == "blocked"]

    lines = [
        f"=== Task Summary ({len(tasks)} total, {len(active)} active) ===",
        "",
        "By status:   " + "  ".join(f"{k}:{v}" for k,v in sorted(by_status.items())),
        "By priority: " + "  ".join(f"{k}:{v}" for k,v in sorted(by_priority.items())),
        "",
        "By category: " + "  ".join(f"{k}:{v}" for k,v in sorted(by_category.items())),
        "",
        "Top projects: " + "  ".join(f"{k}({v})" for k,v in by_project.most_common(6)),
    ]
    if blocked:
        lines += ["", f"BLOCKED ({len(blocked)}):"]
        for t in blocked:
            lines.append(f"  {t['id']}  {t['title']}  [{t.get('project','—')}]")
    high = [t for t in active if t.get("priority") == "high"]
    if high:
        lines += ["", f"HIGH PRIORITY ({len(high)}):"]
        for t in high[:5]:
            lines.append(f"  {t['id']}  {t['title']}  [{t.get('project','—')}]")
    return "\n".join(lines)
