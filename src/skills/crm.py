"""CRM skill — manage leads/contacts through a sales pipeline with follow-ups
and lead scrubbing, for DJ's projects (band bookings, app users, brand deals)."""
import os, json, uuid, datetime, re
from collections import Counter, defaultdict

NAME = "crm"
DESCRIPTION = (
    "Lightweight CRM for DJ's projects: track leads/contacts through a sales pipeline, "
    "log touches, schedule follow-ups, and scrub lead data. "
    "Pipeline stages: lead | contacted | qualified | proposal | negotiation | won | lost. "
    "Use whenever a person, lead, prospect, booking inquiry, customer, or deal is mentioned, "
    "or when asked about follow-ups, the pipeline, or cleaning up contact data."
)
STAGES = ["lead", "contacted", "qualified", "proposal", "negotiation", "won", "lost"]

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "list", "update", "stage", "note", "followup",
                     "due", "search", "summary", "scrub", "delete"],
            "description": "What to do",
        },
        "name":    {"type": "string", "description": "Contact name (add/search)"},
        "company": {"type": "string", "description": "Company / band / org"},
        "email":   {"type": "string"},
        "phone":   {"type": "string"},
        "project": {"type": "string", "description": "Which of DJ's projects this lead is for"},
        "stage":   {"type": "string", "enum": STAGES, "description": "Pipeline stage"},
        "source":  {"type": "string", "description": "Where the lead came from (referral, IG, web, gig)"},
        "value":   {"type": "number", "description": "Estimated deal value in dollars"},
        "tags":    {"type": "array", "items": {"type": "string"}},
        "notes":   {"type": "string", "description": "A note / touch to log, or initial context"},
        "next_action": {"type": "string", "description": "The next follow-up step"},
        "next_date":   {"type": "string", "description": "Follow-up date YYYY-MM-DD or e.g. +3d, +1w"},
        "id":      {"type": "string", "description": "Contact id (c_xxxx) for update/stage/note/delete"},
        "field":   {"type": "string", "description": "Field to update"},
        "value_str": {"type": "string", "description": "New value for the field (update)"},
        "query":   {"type": "string", "description": "Search string"},
        "limit":   {"type": "integer"},
    },
    "required": ["action"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

_CRM_DIR  = os.path.expanduser("~/.runai/crm")
_FILE     = os.path.join(_CRM_DIR, "contacts.jsonl")


# ----------------------------------------------------------------- storage
def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")

def _today():
    return datetime.date.today()

def _load():
    out = []
    if not os.path.exists(_FILE):
        return out
    with open(_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out

def _save(rows):
    os.makedirs(_CRM_DIR, exist_ok=True)
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, _FILE)

def _id():
    return "c_" + uuid.uuid4().hex[:8]


# ----------------------------------------------------------------- helpers
def _parse_date(s):
    """Accept YYYY-MM-DD, +Nd / +Nw / +Nm (relative), or 'today'/'tomorrow'."""
    if not s:
        return None
    s = s.strip().lower()
    if s in ("today", "now"):
        return _today().isoformat()
    if s in ("tomorrow", "tmrw"):
        return (_today() + datetime.timedelta(days=1)).isoformat()
    m = re.match(r"\+(\d+)\s*([dwm])", s)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        days = n * {"d": 1, "w": 7, "m": 30}[unit]
        return (_today() + datetime.timedelta(days=days)).isoformat()
    try:
        return datetime.date.fromisoformat(s).isoformat()
    except ValueError:
        return None

def _days_until(iso):
    try:
        return (datetime.date.fromisoformat(iso) - _today()).days
    except Exception:
        return None

def _find(rows, tid):
    return next((r for r in rows if r["id"] == tid), None)

def _norm_email(e):
    return (e or "").strip().lower()

def _norm_phone(p):
    digits = re.sub(r"\D", "", p or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return (p or "").strip()

def _fmt(c):
    val = f" · ${c['value']:,.0f}" if c.get("value") else ""
    tags = (" [" + ", ".join(c.get("tags", [])) + "]") if c.get("tags") else ""
    who = c.get("name") or "(no name)"
    org = f" @ {c['company']}" if c.get("company") else ""
    head = f"[{c['id']}] {who}{org}{tags}"
    line2 = f"    {c.get('project','—')} · {c.get('stage','lead')}{val} · {c.get('source','—')}"
    contact = " · ".join(x for x in [c.get("email"), c.get("phone")] if x)
    line3 = f"    {contact}" if contact else ""
    fu = ""
    if c.get("next_date"):
        d = _days_until(c["next_date"])
        when = "overdue" if d is not None and d < 0 else (f"in {d}d" if d else "today")
        fu = f"\n    -> next: {c.get('next_action','follow up')} ({c['next_date']}, {when})"
    return "\n".join(x for x in [head, line2, line3] if x) + fu


# ----------------------------------------------------------------- dispatch
def run(args):
    action = (args.get("action") or "list").lower()
    return {
        "add": _add, "list": _list, "update": _update, "stage": _stage,
        "note": _note, "followup": _followup, "due": _due, "search": _search,
        "summary": _summary, "scrub": _scrub, "delete": _delete,
    }.get(action, lambda a: f"Unknown action: {action}")(args)


def _add(args):
    name = (args.get("name") or "").strip()
    company = (args.get("company") or "").strip()
    if not name and not company:
        return "Error: a name or company is required to add a contact."
    rows = _load()
    # duplicate guard on email or name+company
    email = _norm_email(args.get("email"))
    for r in rows:
        if email and _norm_email(r.get("email")) == email:
            return f"A contact with that email already exists: {_fmt(r)}"
    c = {
        "id": _id(),
        "name": name,
        "company": company,
        "email": email,
        "phone": _norm_phone(args.get("phone")),
        "project": (args.get("project") or "general").strip().lower(),
        "stage": (args.get("stage") or "lead").lower(),
        "source": (args.get("source") or "").strip().lower(),
        "value": float(args["value"]) if args.get("value") not in (None, "") else 0,
        "tags": [t.strip() for t in (args.get("tags") or []) if t.strip()],
        "log": [],
        "next_action": (args.get("next_action") or "").strip(),
        "next_date": _parse_date(args.get("next_date")),
        "created": _now(),
        "updated": _now(),
        "last_touch": _now(),
    }
    if c["stage"] not in STAGES:
        c["stage"] = "lead"
    if args.get("notes"):
        c["log"].append({"t": _now(), "note": args["notes"].strip()})
    # auto follow-up: a brand-new lead with no follow-up set gets a default nudge
    if not c["next_date"]:
        c["next_action"] = c["next_action"] or "Initial outreach"
        c["next_date"] = _parse_date("+2d")
    rows.append(c)
    _save(rows)
    return f"Added contact:\n{_fmt(c)}"


def _list(args):
    rows = _load()
    project = (args.get("project") or "").lower()
    stage   = (args.get("stage") or "").lower()
    limit   = int(args.get("limit") or 30)
    if project: rows = [r for r in rows if r.get("project", "").lower() == project]
    if stage:   rows = [r for r in rows if r.get("stage", "").lower() == stage]
    order = {s: i for i, s in enumerate(STAGES)}
    rows.sort(key=lambda r: (order.get(r.get("stage", "lead"), 99), -(r.get("value") or 0)))
    rows = rows[:limit]
    if not rows:
        return "No contacts found."
    return f"{len(rows)} contact(s):\n\n" + "\n\n".join(_fmt(r) for r in rows)


def _update(args):
    tid = (args.get("id") or "").strip()
    field = (args.get("field") or "").strip()
    value = args.get("value_str")
    if value is None:
        value = args.get("value")
    if not tid or not field:
        return "Error: id and field are required."
    rows = _load()
    c = _find(rows, tid)
    if not c:
        return f"Contact {tid} not found."
    allowed = {"name", "company", "email", "phone", "project", "stage",
               "source", "value", "tags", "next_action", "next_date"}
    if field not in allowed:
        return f"Field '{field}' not updatable. Allowed: {', '.join(sorted(allowed))}"
    if field == "tags" and isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    elif field == "value":
        value = float(value or 0)
    elif field == "email":
        value = _norm_email(value)
    elif field == "phone":
        value = _norm_phone(value)
    elif field == "next_date":
        value = _parse_date(value)
    elif field == "stage" and value not in STAGES:
        return f"Invalid stage. Use one of: {', '.join(STAGES)}"
    c[field] = value
    c["updated"] = _now()
    _save(rows)
    return f"Updated {field} on {tid}:\n{_fmt(c)}"


def _stage(args):
    tid = (args.get("id") or "").strip()
    new = (args.get("stage") or "").lower()
    if not tid or new not in STAGES:
        return f"Error: id and a valid stage required ({', '.join(STAGES)})."
    rows = _load()
    c = _find(rows, tid)
    if not c:
        return f"Contact {tid} not found."
    old = c.get("stage", "lead")
    c["stage"] = new
    c["updated"] = c["last_touch"] = _now()
    c["log"].append({"t": _now(), "note": f"Stage {old} → {new}"})
    # auto follow-up cadence per stage; won/lost clears the follow-up
    cadence = {"contacted": "+3d", "qualified": "+5d", "proposal": "+2d", "negotiation": "+2d"}
    if new in ("won", "lost"):
        c["next_action"], c["next_date"] = "", None
    elif new in cadence:
        c["next_action"] = c.get("next_action") or f"Follow up after moving to {new}"
        c["next_date"] = _parse_date(cadence[new])
    _save(rows)
    extra = "" if new in ("won", "lost") else f"  (next follow-up {c.get('next_date')})"
    return f"Moved {tid} {old} → {new}.{extra}\n{_fmt(c)}"


def _note(args):
    tid = (args.get("id") or "").strip()
    note = (args.get("notes") or "").strip()
    if not tid or not note:
        return "Error: id and notes required to log a touch."
    rows = _load()
    c = _find(rows, tid)
    if not c:
        return f"Contact {tid} not found."
    c["log"].append({"t": _now(), "note": note})
    c["last_touch"] = c["updated"] = _now()
    _save(rows)
    return f"Logged touch on {tid}. ({len(c['log'])} total)\n{_fmt(c)}"


def _followup(args):
    tid = (args.get("id") or "").strip()
    if not tid:
        return _due(args)  # no id -> show what's due
    rows = _load()
    c = _find(rows, tid)
    if not c:
        return f"Contact {tid} not found."
    c["next_action"] = (args.get("next_action") or c.get("next_action") or "Follow up").strip()
    c["next_date"] = _parse_date(args.get("next_date")) or c.get("next_date")
    c["updated"] = _now()
    _save(rows)
    return f"Follow-up set on {tid}: {c['next_action']} @ {c.get('next_date')}\n{_fmt(c)}"


def _due(args):
    rows = _load()
    project = (args.get("project") or "").lower()
    window = int(args.get("limit") or 7)  # days ahead to include
    items = []
    for r in rows:
        if r.get("stage") in ("won", "lost"):
            continue
        if project and r.get("project", "").lower() != project:
            continue
        nd = r.get("next_date")
        if not nd:
            continue
        d = _days_until(nd)
        if d is not None and d <= window:
            items.append((d, r))
    if not items:
        return "Nothing due in the follow-up window. Pipeline's clean."
    items.sort(key=lambda x: x[0])
    lines = [f"{len(items)} follow-up(s) due (next {window}d):"]
    for d, r in items:
        when = "OVERDUE" if d < 0 else ("TODAY" if d == 0 else f"in {d}d")
        lines.append(f"  [{when}] {r['id']} {r.get('name') or r.get('company')} — "
                     f"{r.get('next_action','follow up')} ({r['next_date']}) · {r.get('stage')}")
    return "\n".join(lines)


def _search(args):
    q = (args.get("query") or "").lower().strip()
    if not q:
        return "Error: query is required."
    rows = _load()
    hits = [r for r in rows if
            q in (r.get("name", "") + r.get("company", "") + r.get("email", "") +
                  r.get("project", "") + " ".join(r.get("tags", []))).lower() or
            any(q in (e.get("note", "").lower()) for e in r.get("log", []))]
    if not hits:
        return f"No contacts matching '{q}'."
    return f"{len(hits)} match(es):\n\n" + "\n\n".join(_fmt(r) for r in hits)


def _summary(args):
    rows = _load()
    if not rows:
        return "CRM is empty. Add a contact with action=add."
    project = (args.get("project") or "").lower()
    if project:
        rows = [r for r in rows if r.get("project", "").lower() == project]
    open_rows = [r for r in rows if r.get("stage") not in ("won", "lost")]
    by_stage = Counter(r.get("stage", "lead") for r in rows)
    pipe_val = sum(r.get("value") or 0 for r in open_rows)
    won_val  = sum(r.get("value") or 0 for r in rows if r.get("stage") == "won")
    by_proj  = Counter(r.get("project", "general") for r in open_rows)
    overdue  = [r for r in open_rows if (_days_until(r.get("next_date") or "") or 99) < 0]

    lines = [
        f"=== CRM Pipeline ({len(rows)} contacts, {len(open_rows)} open) ===",
        "",
        "Funnel:  " + "  ".join(f"{s}:{by_stage.get(s,0)}" for s in STAGES),
        f"Open pipeline value: ${pipe_val:,.0f}   |   Won: ${won_val:,.0f}",
        "",
        "By project: " + "  ".join(f"{k}({v})" for k, v in by_proj.most_common(6)),
    ]
    if overdue:
        lines += ["", f"OVERDUE FOLLOW-UPS ({len(overdue)}):"]
        for r in overdue[:6]:
            lines.append(f"  {r['id']} {r.get('name') or r.get('company')} — "
                         f"{r.get('next_action','follow up')} ({r.get('next_date')})")
    return "\n".join(lines)


def _scrub(args):
    """Lead hygiene: normalize email/phone, flag missing fields, find duplicates,
    flag stale (no touch in 30d). Returns a report; applies safe normalizations."""
    rows = _load()
    if not rows:
        return "Nothing to scrub — CRM is empty."
    changed = 0
    issues = defaultdict(list)
    seen_email, seen_nameco = {}, {}
    for r in rows:
        # normalize in place
        ne, np_ = _norm_email(r.get("email")), _norm_phone(r.get("phone"))
        if ne != (r.get("email") or ""):
            r["email"] = ne; changed += 1
        if np_ != (r.get("phone") or ""):
            r["phone"] = np_; changed += 1
        # completeness
        if not r.get("email") and not r.get("phone"):
            issues["no contact method"].append(r["id"])
        if not r.get("name"):
            issues["missing name"].append(r["id"])
        if r.get("stage") not in ("won", "lost") and not r.get("next_date"):
            issues["no next follow-up"].append(r["id"])
        # email format
        if r.get("email") and not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", r["email"]):
            issues["malformed email"].append(r["id"])
        # duplicates
        if r.get("email"):
            seen_email.setdefault(r["email"], []).append(r["id"])
        key = (r.get("name", "").lower(), r.get("company", "").lower())
        if any(key):
            seen_nameco.setdefault(key, []).append(r["id"])
        # stale
        try:
            last = datetime.datetime.fromisoformat(r.get("last_touch", r.get("created", "")))
            if r.get("stage") not in ("won", "lost") and (datetime.datetime.now() - last).days > 30:
                issues["stale (>30d no touch)"].append(r["id"])
        except Exception:
            pass
    dupes = {k: v for k, v in {**seen_email, **{f"{n}/{c}": ids for (n, c), ids in seen_nameco.items()}}.items() if len(v) > 1}
    if changed:
        _save(rows)
    lines = [f"=== Lead scrub: {len(rows)} contacts, {changed} field(s) normalized ==="]
    if dupes:
        lines.append(f"\nPossible duplicates ({len(dupes)} groups):")
        for k, ids in list(dupes.items())[:8]:
            lines.append(f"  {k} → {', '.join(ids)}")
    if issues:
        lines.append("\nData issues:")
        for label, ids in sorted(issues.items(), key=lambda x: -len(x[1])):
            lines.append(f"  {label}: {len(ids)}  ({', '.join(ids[:6])}{'…' if len(ids) > 6 else ''})")
    if not dupes and not issues:
        lines.append("\nClean — no duplicates or missing data.")
    return "\n".join(lines)


def _delete(args):
    tid = (args.get("id") or "").strip()
    if not tid:
        return "Error: id is required."
    rows = _load()
    kept = [r for r in rows if r["id"] != tid]
    if len(kept) == len(rows):
        return f"Contact {tid} not found."
    _save(kept)
    return f"Deleted contact {tid}."
