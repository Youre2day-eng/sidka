#!/usr/bin/env python3
"""
RunAI Web v5 - local browser AI cockpit dashboard over the manager_v6 engine.

New in v5 (on top of v4):
  1. Fast operator routes       — /api/cmd/status|journal|today|index bypass the LLM entirely (2-3s)
  2. Fast-model standup         — /standup uses qwen2.5:1.5b not 7b for synthesis
  3. Live skill reload          — POST /api/reload broadcasts SSE event to update UI without refresh
  4. Skill load error report    — failed skills surface their import error in the Skills tab
  5. Cockpit model selector     — UI lets you pick fast/technical/agent per cockpit run

Everything from v4 is preserved unchanged.
"""

import difflib
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Empty, Queue

import ollama
from flask import Flask, Response, jsonify, request, send_from_directory

# Signal to _lib.confirm() and tool_run_shell() that approval is handled by the cockpit UI.
os.environ["RUNAI_WEB_MODE"] = "1"

# Cross-platform shell adapter — sets SHELL_CMD, projects_root(), etc.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
from platform_shell import PLATFORM_INFO, projects_root, normalize_path  # noqa: E402

# Reuse the terminal engine (models, router, skills, sessions, RAG).
sys.path.insert(0, os.path.expanduser("~"))
import manager as eng  # noqa: E402

RUNAI_DIR        = eng.RUNAI_DIR
AUTOMATIONS_FILE = os.path.join(RUNAI_DIR, "automations.json")
RUNS_FILE        = os.path.join(RUNAI_DIR, "runs.json")
LOG_DIR          = os.path.join(RUNAI_DIR, "logs")
QUICKCMDS_FILE   = os.path.join(RUNAI_DIR, "quickcmds.json")
PORT = 8765

app = Flask(__name__)
SKILLS = {}
SKILL_ERRORS = {}  # name -> error string for failed imports

def _reload_skills():
    """Load skills and capture per-skill import errors instead of silently dropping them."""
    global SKILLS, SKILL_ERRORS
    new_skills, new_errors = {}, {}
    skills_dir = os.path.join(eng.RUNAI_DIR, "skills")
    if not os.path.isdir(skills_dir):
        SKILLS, SKILL_ERRORS = new_skills, new_errors
        return
    import importlib.util as _ilu
    import sys as _sys
    # Skills use `import _lib` — the skills dir must be on sys.path.
    if skills_dir not in _sys.path:
        _sys.path.insert(0, skills_dir)
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(skills_dir, fn)
        name = fn[:-3]
        try:
            spec = _ilu.spec_from_file_location(name, path)
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            skill_name = getattr(mod, "NAME", name)
            new_skills[skill_name] = mod
        except Exception as e:
            new_errors[name] = str(e)
    SKILLS, SKILL_ERRORS = new_skills, new_errors

_reload_skills()

def _get_skill_mod(name):
    """Load a skill module for direct (no-LLM) calls."""
    import importlib.util as _ilu, sys as _sys
    skills_dir = os.path.join(eng.RUNAI_DIR, "skills")
    if skills_dir not in _sys.path:
        _sys.path.insert(0, skills_dir)
    path = os.path.join(skills_dir, name + ".py")
    if not os.path.exists(path):
        return None
    try:
        spec = _ilu.spec_from_file_location(name, path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# ----------------------------------------------------------------- logging
def _log_event(session_name, event_type, data):
    """Append one JSONL line to ~/.runai/logs/YYYY-MM-DD.jsonl. Never raises."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session_name, "type": event_type, **data}
        with open(os.path.join(LOG_DIR, time.strftime("%Y-%m-%d") + ".jsonl"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------- pending actions
_pending = {}           # action_id -> {id, fn, args, diff, event, approved}
_pending_lock = threading.Lock()


def _requires_approval(fn, args):
    if fn == "files":
        return args.get("action") in ("write", "move", "delete")
    if fn == "run_shell":
        return True
    if fn == "git":
        return args.get("action") in ("add", "commit", "push", "reset", "checkout")
    return False


def _make_diff(fn, args):
    """Generate unified diff for file writes; return None otherwise."""
    if fn != "files" or args.get("action") != "write":
        return None
    path = os.path.expanduser(args.get("path", ""))
    content = args.get("content", "")
    before = ""
    if os.path.exists(path):
        try:
            with open(path) as f:
                before = f.read()
        except Exception:
            pass
    diff = list(difflib.unified_diff(
        before.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile="before/" + os.path.basename(path),
        tofile="after/" + os.path.basename(path),
        lineterm="",
    ))
    if not diff:
        return "(no changes)" if before else "(new file — no diff to show)"
    return "".join(diff)


# ----------------------------------------------------------------- storage
def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default

def _save(path, data):
    os.makedirs(RUNAI_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_automations():
    return _load(AUTOMATIONS_FILE, [])

def save_automations(a):
    _save(AUTOMATIONS_FILE, a)

def load_runs():
    return _load(RUNS_FILE, [])

def log_run(source, name, steps, status):
    runs = load_runs()
    runs.insert(0, {
        "id": "run_" + uuid.uuid4().hex[:8],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source, "name": name, "status": status, "steps": steps,
    })
    _save(RUNS_FILE, runs[:200])

def load_quickcmds():
    return _load(QUICKCMDS_FILE, [])

def save_quickcmds(c):
    _save(QUICKCMDS_FILE, c)


# ----------------------------------------------------------------- automations
def interpolate(obj, data):
    """Replace {{key}} in any string with values from a webhook payload."""
    if isinstance(obj, str):
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(data.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: interpolate(v, data) for k, v in obj.items()}
    if isinstance(obj, list):
        return [interpolate(v, data) for v in obj]
    return obj

def run_automation(auto, source="manual", payload=None):
    steps, status = [], "ok"
    for action in auto.get("actions", []):
        skill = action.get("skill", "")
        args = interpolate(action.get("args", {}) or {}, payload or {})
        out = eng.dispatch(skill, args, SKILLS)
        steps.append({"skill": skill, "args": args, "output": str(out)[:2000]})
        if isinstance(out, str) and (out.lower().startswith("error") or "failed" in out.lower()):
            status = "error"
    log_run(source, auto.get("name", "?"), steps, status)
    return {"status": status, "steps": steps}


# ----------------------------------------------------------------- SSE helper
def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


# ----------------------------------------------------------------- chat stream (plain mode)
def chat_stream(session_name, text, model_choice):
    session = eng.load_session(session_name)
    if model_choice and model_choice in eng.MODELS:
        category = model_choice
    else:
        try:
            category = eng.route(text)
        except eng.OllamaDown:
            def err():
                yield _sse({"type": "error", "text": "Ollama isn't running."})
            return err()
    model = eng.MODELS[category]
    session["messages"].append({"role": "user", "content": text})

    extras = []
    hits = eng.retrieve(text)
    if hits:
        note = "\n\n---\n".join(f"From {os.path.basename(h['file'])}:\n{h['chunk']}" for h in hits)
        extras.append("Relevant context from the user's indexed notes:\n" + note)
    msgs = eng.build_messages(session, category, extras)

    def gen():
        yield _sse({"type": "meta", "model": model, "category": category, "notes": len(hits)})
        parts = []
        try:
            for chunk in ollama.chat(model=model, messages=msgs, stream=True):
                tok = eng._chunk_text(chunk)
                if tok:
                    parts.append(tok)
                    yield _sse({"type": "token", "text": tok})
        except Exception as e:
            yield _sse({"type": "error", "text": str(e)})
            return
        reply = "".join(parts)
        session["messages"].append({"role": "assistant", "content": reply})
        eng.save_session(session_name, session)
        yield _sse({"type": "done"})
    return gen()


# ----------------------------------------------------------------- agent (legacy, backward compat)
def run_agent(session_name, text):
    session = eng.load_session(session_name)
    session["messages"].append({"role": "user", "content": text})
    msgs = eng.build_messages(session, "agent")
    tools = eng.all_tools(SKILLS)
    model = eng._agent_model()
    used = []
    for _ in range(6):
        resp = ollama.chat(model=model, messages=msgs, tools=tools)
        msg = eng._attr(resp, "message")
        calls = eng._safe(msg, "tool_calls")
        content = eng._safe(msg, "content") or ""
        msgs.append(msg)
        if not calls:
            session["messages"].append({"role": "assistant", "content": content})
            eng.save_session(session_name, session)
            return {"reply": content, "tools": used, "model": model}
        for tc in calls:
            fn = eng._attr(eng._attr(tc, "function"), "name")
            raw = eng._attr(eng._attr(tc, "function"), "arguments")
            args = raw if isinstance(raw, dict) else json.loads(raw)
            out = eng.dispatch(fn, args, SKILLS)
            used.append({"skill": fn, "args": args, "output": str(out)[:1000]})
            msgs.append({"role": "tool", "content": str(out)})
    return {"reply": "(stopped after several tool rounds)", "tools": used, "model": model}


# ----------------------------------------------------------------- cockpit agent (SSE + approval + logging + parallel)
def run_agent_with_cockpit(session_name, text, q):
    """Run the agent loop in a thread, pushing SSE events into queue q.
    Destructive actions (file write/move/delete, run_shell) pause and wait for
    browser approval before executing. Safe parallel tool calls run concurrently."""
    try:
        t_start = time.time()
        session = eng.load_session(session_name)
        session["messages"].append({"role": "user", "content": text})
        msgs = eng.build_messages(session, "agent")
        tools = eng.all_tools(SKILLS)
        model = eng._agent_model()
        used = []

        q.put({"type": "meta", "model": model})

        def cockpit_dispatch(fn, args):
            """Like eng.dispatch but intercepts destructive actions for approval."""
            _log_event(session_name, "tool_call", {"fn": fn, "args": args})
            if _requires_approval(fn, args):
                action_id = "act_" + uuid.uuid4().hex[:10]
                diff = _make_diff(fn, args)

                # Determine badge type
                if fn == "run_shell":
                    badge = "shell"
                    label = args.get("command", "")
                elif fn == "files":
                    badge = args.get("action", "write")
                    label = args.get("path", "")
                else:
                    badge = fn
                    label = str(args)

                event = threading.Event()
                action_data = {
                    "id": action_id,
                    "fn": fn,
                    "args": args,
                    "diff": diff,
                    "badge": badge,
                    "label": label,
                    "approved": None,
                    "event": event,
                }
                with _pending_lock:
                    _pending[action_id] = action_data

                # Push pending notification to browser (without threading.Event)
                serializable = {k: v for k, v in action_data.items() if k != "event"}
                q.put({"type": "action_pending", "action": serializable})

                # Block until browser approves or denies (or 5-minute timeout)
                event.wait(timeout=300)

                with _pending_lock:
                    approved = _pending.get(action_id, {}).get("approved", False)
                    _pending.pop(action_id, None)

                q.put({"type": "action_resolved", "id": action_id, "approved": bool(approved)})

                if not approved:
                    _log_event(session_name, "tool_result", {"fn": fn, "output": "Action denied by user."})
                    return "Action denied by user."

            out = eng.dispatch(fn, args, SKILLS)
            _log_event(session_name, "tool_result", {"fn": fn, "output": str(out)[:500]})
            return out

        final_content = "(stopped after several tool rounds)"

        for _ in range(8):
            resp = ollama.chat(model=model, messages=msgs, tools=tools)
            msg = eng._attr(resp, "message")
            calls = eng._safe(msg, "tool_calls")
            content = eng._safe(msg, "content") or ""
            msgs.append(msg)

            # Emit any thinking text between tool calls
            if content and content.strip():
                q.put({"type": "thinking", "text": content})

            if not calls:
                session["messages"].append({"role": "assistant", "content": content})
                eng.save_session(session_name, session)
                final_content = content
                break

            # Determine which calls are safe (no approval needed)
            safe_indices = [
                i for i, tc in enumerate(calls)
                if not _requires_approval(
                    eng._attr(eng._attr(tc, "function"), "name"),
                    (lambda raw: raw if isinstance(raw, dict) else json.loads(raw or "{}"))(
                        eng._attr(eng._attr(tc, "function"), "arguments")
                    )
                )
            ]
            all_safe = len(safe_indices) == len(calls) and len(calls) > 1

            if all_safe:
                # Parse all calls first
                parsed = []
                for tc in calls:
                    fn = eng._attr(eng._attr(tc, "function"), "name")
                    raw = eng._attr(eng._attr(tc, "function"), "arguments")
                    args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                    parsed.append((fn, args))

                # Run in parallel
                q.put({"type": "tool_call", "fn": f"[parallel: {len(parsed)}]", "args": {}})
                with ThreadPoolExecutor(max_workers=min(len(parsed), 4)) as ex:
                    future_to_idx = {
                        ex.submit(eng.dispatch, fn, args, SKILLS): (i, fn, args)
                        for i, (fn, args) in enumerate(parsed)
                    }
                    results = {}
                    for future in as_completed(future_to_idx):
                        i, fn, args = future_to_idx[future]
                        try:
                            results[i] = future.result()
                        except Exception as e:
                            results[i] = f"Error: {e}"

                for i, (fn, args) in enumerate(parsed):
                    out = results.get(i, "no result")
                    used.append({"skill": fn, "args": args, "output": str(out)[:1000]})
                    msgs.append({"role": "tool", "content": str(out)})
                    q.put({"type": "tool_result", "fn": fn, "output": str(out)[:500]})
                    _log_event(session_name, "tool_result", {"fn": fn, "output": str(out)[:500]})
            else:
                # Sequential loop (including cockpit_dispatch for approvals)
                for tc in calls:
                    fn = eng._attr(eng._attr(tc, "function"), "name")
                    raw = eng._attr(eng._attr(tc, "function"), "arguments")
                    args = raw if isinstance(raw, dict) else json.loads(raw or "{}")

                    q.put({"type": "tool_call", "fn": fn, "args": args})

                    out = cockpit_dispatch(fn, args)
                    used.append({"skill": fn, "args": args, "output": str(out)[:1000]})
                    msgs.append({"role": "tool", "content": str(out)})

                    q.put({"type": "tool_result", "fn": fn, "output": str(out)[:500]})

        # Final save in case we hit the loop limit
        if not session["messages"] or session["messages"][-1].get("role") != "assistant":
            session["messages"].append({"role": "assistant", "content": final_content})
            eng.save_session(session_name, session)

        tools_used = [u["skill"] for u in used]
        _log_event(session_name, "agent_run", {
            "model": model,
            "tools": tools_used,
            "duration_s": round(time.time() - t_start, 1),
            "reply_len": len(final_content),
        })

        # Auto-journal: if any tools were used, append a dated entry so "what was I doing?" works.
        if tools_used:
            try:
                import importlib.util as _ilu, os as _os
                _jpath = _os.path.expanduser("~/.runai/skills/journal.py")
                _spec = _ilu.spec_from_file_location("journal", _jpath)
                _jmod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_jmod)
                entry = (
                    f"[{session_name}] cockpit run — tools: {', '.join(tools_used)}. "
                    f'Task: "{text[:120]}"'
                )
                _jmod.run({"action": "write", "entry": entry, "project": session_name})
            except Exception:
                pass

        q.put({"type": "done", "reply": final_content, "tools": used})

    except Exception as e:
        q.put({"type": "error", "text": str(e)})


# ----------------------------------------------------------------- routes
@app.route("/api/ping")
def ping():
    return jsonify(ok=True)

@app.route("/api/status-all")
def status_all():
    rows = eng.cmd_status_all()
    return jsonify(rows)

# ---- fast operator commands (no LLM, 2-3s) ----
@app.route("/api/cmd/<name>", methods=["GET", "POST"])
def fast_cmd(name):
    """Run a skill directly — no LLM, instant result. Used for /status, /journal, /today."""
    args = (request.get_json(silent=True) or {}) if request.method == "POST" else {}

    if name == "status":
        mod = _get_skill_mod("project_status")
        if not mod:
            return jsonify(error="project_status skill not found"), 404
        result = mod.run(args)
        return jsonify(result=result)

    if name in ("journal", "today"):
        mod = _get_skill_mod("journal")
        if not mod:
            return jsonify(error="journal skill not found"), 404
        if name == "today":
            args.setdefault("action", "today")
        else:
            args.setdefault("action", "read")
            args.setdefault("days", 7)
        result = mod.run(args)
        return jsonify(result=result)

    if name == "index":
        def do_index():
            eng.cmd_index(projects_root())
        threading.Thread(target=do_index, daemon=True).start()
        return jsonify(result="Indexing started in background. Will update ~/.runai/index.json when complete.")

    if name == "standup":
        # standup still needs LLM synthesis but uses the fast model
        import subprocess as _sp
        root = projects_root()
        lines = []
        try:
            for d in sorted(os.listdir(root))[:12]:
                p = os.path.join(root, d)
                if not os.path.isdir(os.path.join(p, ".git")):
                    continue
                r = _sp.run(["git", "log", "--oneline", "--since=36 hours ago"],
                            cwd=p, capture_output=True, text=True, timeout=5)
                if r.stdout.strip():
                    lines.append(f"[{d}]\n" + r.stdout.strip())
        except Exception:
            pass
        activity = "\n\n".join(lines) or "No recent commits found."
        # Use fast model (qwen2.5:1.5b) for synthesis
        import ollama as _ol
        prompt = (
            "You are a local AI assistant for DJ, an indie developer.\n"
            "Based on the git activity below, write a concise daily standup:\n"
            "**Yesterday:** what was worked on\n**Today:** logical next steps\n**Blocked:** anything stalled\n\n"
            f"Git activity (last 36h):\n{activity[:3000]}"
        )
        try:
            resp = _ol.generate(model="qwen2.5:1.5b", prompt=prompt)
            result = resp.get("response", "").strip()
        except Exception as e:
            result = f"Could not synthesize standup: {e}\n\nRaw activity:\n{activity}"
        return jsonify(result=result)

    if name == "tasks":
        mod = _tasks_skill()
        result = mod.run({"action": "summary"})
        return jsonify(result=result)

    return jsonify(error=f"Unknown command: {name}"), 404

@app.route("/api/workon/<project>", methods=["POST"])
def workon(project):
    """Switch context to a project session. Returns the session name."""
    # Just confirm the session exists (or create it).
    session = eng.load_session(project)
    if not session.get("system_prompt"):
        # Generic fallback system prompt for unknown projects.
        session["system_prompt"] = (
            f"You are a local AI assistant for DJ, working on the {project} project "
            f"at ~/Desktop/Cld/{project}. You have access to git, file, run_shell and other skills."
        )
        eng.save_session(project, session)
    return jsonify(ok=True, session=project)

@app.route("/api/state")
def state():
    return jsonify(
        models=["auto"] + list(eng.MODELS.keys()),
        agent_model=eng._agent_model(),
        agent_model_preferred=eng._AGENT_MODEL_PREFERRED,
        skills=[{"name": n, "description": getattr(m, "DESCRIPTION", "")} for n, m in SKILLS.items()],
        skill_errors=SKILL_ERRORS,
        sessions=eng.list_sessions() or ["default"],
        automations=load_automations(),
        runs=load_runs()[:40],
        quickcmds=load_quickcmds(),
    )

@app.route("/api/reload", methods=["POST"])
def reload_skills_route():
    _reload_skills()
    return jsonify(ok=True, skills=list(SKILLS), errors=SKILL_ERRORS)

@app.route("/api/session/<name>")
def get_session(name):
    return jsonify(eng.load_session(name))

@app.route("/api/session/<name>/clear", methods=["POST"])
def clear_session(name):
    eng.save_session(name, {"summary": "", "messages": []})
    return jsonify(ok=True)

@app.route("/api/session/<name>/export")
def export_session(name):
    session = eng.load_session(name)
    lines = [f"# Session: {name}\n\n*Exported {time.strftime('%Y-%m-%d %H:%M')}*\n\n"]
    for msg in session.get("messages", []):
        role = msg.get("role", "?")
        content = msg.get("content", "") or ""
        if role == "tool":
            continue  # skip raw tool results
        label = "**You**" if role == "user" else "**AI**"
        lines.append(f"{label}\n\n{content}\n\n---\n\n")
    md = "".join(lines)
    return Response(
        md,
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={name}-session.md"},
    )

@app.route("/api/chat", methods=["POST"])
def chat():
    d = request.get_json(force=True)
    return Response(
        chat_stream(d.get("session", "default"), d.get("message", ""), d.get("model", "auto")),
        mimetype="text/event-stream",
    )

@app.route("/api/agent", methods=["POST"])
def agent():
    """Legacy synchronous agent endpoint (backward compat)."""
    d = request.get_json(force=True)
    try:
        return jsonify(run_agent(d.get("session", "default"), d.get("message", "")))
    except Exception as e:
        return jsonify(reply=f"Error: {e}", tools=[], model="")

@app.route("/api/agent/stream", methods=["POST"])
def agent_stream_route():
    """Cockpit streaming agent with action approval SSE."""
    d = request.get_json(force=True)
    q = Queue()
    threading.Thread(
        target=run_agent_with_cockpit,
        args=(d.get("session", "default"), d.get("message", ""), q),
        daemon=True,
    ).start()

    def gen():
        while True:
            try:
                item = q.get(timeout=600)
                yield _sse(item)
                if item.get("type") in ("done", "error"):
                    break
            except Empty:
                break

    return Response(gen(), mimetype="text/event-stream")

@app.route("/api/action/<action_id>/approve", methods=["POST"])
def approve_action(action_id):
    with _pending_lock:
        if action_id in _pending:
            _pending[action_id]["approved"] = True
            _pending[action_id]["event"].set()
            return jsonify(ok=True)
    return jsonify(error="not found"), 404

@app.route("/api/action/<action_id>/deny", methods=["POST"])
def deny_action(action_id):
    with _pending_lock:
        if action_id in _pending:
            _pending[action_id]["approved"] = False
            _pending[action_id]["event"].set()
            return jsonify(ok=True)
    return jsonify(error="not found"), 404

@app.route("/api/automations", methods=["GET", "POST"])
def automations():
    autos = load_automations()
    if request.method == "GET":
        return jsonify(autos)
    d = request.get_json(force=True)
    auto = {
        "id": "auto_" + uuid.uuid4().hex[:8],
        "name": d.get("name", "Untitled"),
        "trigger": d.get("trigger", "manual"),
        "webhook_id": "wh_" + uuid.uuid4().hex[:10],
        "actions": d.get("actions", []),
    }
    autos.append(auto)
    save_automations(autos)
    return jsonify(auto)

@app.route("/api/automations/<aid>", methods=["DELETE"])
def delete_automation(aid):
    save_automations([a for a in load_automations() if a["id"] != aid])
    return jsonify(ok=True)

@app.route("/api/automations/<aid>/run", methods=["POST"])
def run_now(aid):
    auto = next((a for a in load_automations() if a["id"] == aid), None)
    if not auto:
        return jsonify(error="not found"), 404
    return jsonify(run_automation(auto, source="manual"))

@app.route("/hook/<wid>", methods=["GET", "POST"])
def hook(wid):
    auto = next((a for a in load_automations() if a.get("webhook_id") == wid), None)
    if not auto:
        return jsonify(error="no automation for this webhook"), 404
    payload = request.get_json(silent=True) or dict(request.args) or {}
    result = run_automation(auto, source="webhook", payload=payload)
    return jsonify(ok=True, **result)

@app.route("/api/runs")
def runs():
    return jsonify(load_runs()[:40])

# ---- quick commands ----
@app.route("/api/quickcmds", methods=["GET", "POST"])
def quickcmds_route():
    if request.method == "GET":
        return jsonify(load_quickcmds())
    d = request.get_json(force=True)
    cmds = load_quickcmds()
    cmd = {
        "id": "qc_" + uuid.uuid4().hex[:8],
        "label": d.get("label", "Untitled"),
        "prompt": d.get("prompt", ""),
        "agent": bool(d.get("agent", False)),
    }
    cmds.append(cmd)
    save_quickcmds(cmds)
    return jsonify(cmd)

@app.route("/api/quickcmds/<qid>", methods=["DELETE"])
def delete_quickcmd(qid):
    save_quickcmds([c for c in load_quickcmds() if c["id"] != qid])
    return jsonify(ok=True)

# ---- logs ----
@app.route("/api/logs")
def list_logs():
    """Return list of log files available."""
    if not os.path.exists(LOG_DIR):
        return jsonify([])
    files = sorted([f for f in os.listdir(LOG_DIR) if f.endswith(".jsonl")], reverse=True)
    return jsonify(files[:30])

@app.route("/api/logs/<date>")
def get_log(date):
    """Return parsed log entries for a given date (YYYY-MM-DD)."""
    path = os.path.join(LOG_DIR, date + ".jsonl")
    if not os.path.exists(path):
        return jsonify([])
    entries = []
    with open(path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    return jsonify(entries[-200:])

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


# ---- file tree / preview IDE routes --------------------------------

_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage",
              "__pycache__", "venv", ".venv", ".cache", ".turbo", "out", ".svelte-kit"}
_MAX_DEPTH = 5
_MAX_FILE_BYTES = 150_000

def _build_tree(path, depth=0):
    import os
    name = os.path.basename(path)
    if depth > _MAX_DEPTH:
        return None
    if os.path.isdir(path):
        if name.startswith(".") and depth > 0 and name not in {".env", ".env.local"}:
            return None
        children = []
        try:
            entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        except PermissionError:
            return None
        for entry in entries:
            if entry in _SKIP_DIRS:
                continue
            child = _build_tree(os.path.join(path, entry), depth + 1)
            if child:
                children.append(child)
        return {"name": name, "type": "dir", "path": path, "children": children}
    else:
        size = 0
        try:
            size = os.path.getsize(path)
        except OSError:
            pass
        ext = os.path.splitext(name)[1].lstrip(".")
        return {"name": name, "type": "file", "path": path, "ext": ext, "size": size}

@app.route("/api/filetree")
def api_filetree():
    import os
    root = request.args.get("path", "").strip()
    if not root:
        session_name = request.args.get("session", "default")
        root = os.path.join(projects_root(), session_name)
    root = os.path.realpath(os.path.expanduser(root))
    cld_root = os.path.realpath(projects_root())
    home_root = os.path.realpath(os.path.expanduser("~"))
    if not (root.startswith(cld_root) or root.startswith(os.path.join(home_root, ".runai"))):
        return jsonify({"error": "Path outside allowed roots"}), 403
    if not os.path.isdir(root):
        return jsonify({"error": f"Not a directory: {root}"}), 404
    tree = _build_tree(root)
    return jsonify(tree or {"error": "Could not read directory"})

@app.route("/api/file")
def api_file():
    import os, mimetypes
    path = request.args.get("path", "").strip()
    path = os.path.realpath(os.path.expanduser(path))
    cld_root = os.path.realpath(projects_root())
    home_root = os.path.realpath(os.path.expanduser("~"))
    if not (path.startswith(cld_root) or path.startswith(os.path.join(home_root, ".runai"))):
        return jsonify({"error": "Path outside allowed roots"}), 403
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404
    size = os.path.getsize(path)
    truncated = size > _MAX_FILE_BYTES
    ext = os.path.splitext(path)[1].lstrip(".")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(_MAX_FILE_BYTES)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"content": content, "ext": ext, "size": size, "truncated": truncated, "path": path})

@app.route("/api/probe")
def api_probe():
    import urllib.request, os
    session_name = request.args.get("session", "")
    ports = [3000, 3001, 4000, 4321, 5000, 5173, 5174, 8000, 8080, 8888]
    for port in ports:
        if port == 8765:
            continue
        try:
            req = urllib.request.Request(f"http://localhost:{port}/", method="HEAD")
            with urllib.request.urlopen(req, timeout=0.4):
                return jsonify({"port": port, "url": f"http://localhost:{port}/"})
        except Exception:
            pass
    return jsonify({"port": None})


@app.route("/api/platform")
def api_platform():
    """Return OS / shell / path info so the UI can surface platform context."""
    return jsonify(PLATFORM_INFO)


# ---- live preview: run generated code as a real served page --------
PREVIEW_DIR = os.path.join(RUNAI_DIR, "preview")

# Injected into previewed pages so a JS error shows a visible overlay instead of a black void.
_PREVIEW_HARNESS = """<script>
(function(){
  function report(msg){
    var d=document.getElementById('__sidka_err__');
    if(!d){d=document.createElement('div');d.id='__sidka_err__';
      d.style.cssText='position:fixed;left:0;right:0;bottom:0;z-index:2147483647;background:#2a0d0d;color:#ffb4b4;font:12px/1.5 ui-monospace,Menlo,monospace;padding:10px 14px;border-top:2px solid #e05050;white-space:pre-wrap;max-height:45%;overflow:auto';
      (document.body||document.documentElement).appendChild(d);}
    d.textContent='\\u26a0 '+msg;
    try { window.parent.postMessage({type:'sidka-preview-error',msg:msg},'*'); } catch(x){}
  }
  window.addEventListener('error',function(e){
    var loc=e.filename?' ('+String(e.filename).split('/').pop()+':'+e.lineno+':'+e.colno+')':'';
    report((e.message||'Script error')+loc);
  },true);
  window.addEventListener('unhandledrejection',function(e){
    report('Unhandled promise rejection: '+((e.reason&&e.reason.message)||e.reason));
  });
})();
</script>"""


def _inject_harness(html):
    """Insert the error-overlay harness as the first thing in <head> (or at the top)."""
    if "__sidka_err__" in html:
        return html
    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        i = m.end()
        return html[:i] + "\n" + _PREVIEW_HARNESS + html[i:]
    m = re.search(r"<html[^>]*>", html, re.I)
    if m:
        i = m.end()
        return html[:i] + "\n<head>" + _PREVIEW_HARNESS + "</head>" + html[i:]
    return _PREVIEW_HARNESS + html


def _safe_slug(s, default="default"):
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", (s or "").strip())
    return s or default


@app.route("/api/preview/save", methods=["POST"])
def api_preview_save():
    """Save a generated HTML artifact and return a live URL the preview pane can load."""
    d = request.get_json(force=True) or {}
    html = d.get("html", "")
    if not html.strip():
        return jsonify(error="no html provided"), 400
    session = _safe_slug(d.get("session"), "scratch")
    name    = _safe_slug(d.get("name"), "artifact")
    target  = os.path.join(PREVIEW_DIR, session)
    os.makedirs(target, exist_ok=True)
    fname = name if name.endswith(".html") else name + ".html"
    for path in (os.path.join(target, fname), os.path.join(target, "index.html")):
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    return jsonify(url=f"/preview/{session}/{fname}", index=f"/preview/{session}/")


@app.route("/preview/<session>/")
@app.route("/preview/<session>/<path:subpath>")
def serve_preview(session, subpath="index.html"):
    """Serve saved preview files. Same-origin so the user's own code runs unrestricted."""
    session = _safe_slug(session, "scratch")
    base = os.path.realpath(os.path.join(PREVIEW_DIR, session))
    full = os.path.realpath(os.path.join(base, subpath))
    if not (full == base or full.startswith(base + os.sep)):
        return "Forbidden", 403
    if os.path.isdir(full):
        subpath = os.path.join(subpath, "index.html")
        full = os.path.join(full, "index.html")
    if not os.path.exists(full):
        return "Not found", 404
    if full.endswith(".html"):
        try:
            html = _inject_harness(open(full, encoding="utf-8").read())
            return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})
        except Exception:
            pass
    resp = send_from_directory(base, subpath)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- project preview: detect + run real Cld projects ---------------
import glob as _glob
import subprocess as _subp
import signal as _signal
import atexit as _atexit

_PROJECTS_ROOT = projects_root()
_DEV_PROCS = {}            # name -> {"proc":Popen, "url":str, "type":str}
_DEV_LOCK = threading.Lock()


def _node_path_dirs():
    dirs = []
    nvm = sorted(_glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")))
    if nvm:
        dirs.append(nvm[-1])           # newest nvm node
    dirs += ["/usr/local/bin", "/opt/homebrew/bin"]
    return os.pathsep.join(d for d in dirs if os.path.isdir(d))


def _read_pkg(path):
    f = os.path.join(path, "package.json")
    if os.path.exists(f):
        try:
            return json.load(open(f))
        except Exception:
            return {}
    return {}


def _detect_project(name):
    path = os.path.join(_PROJECTS_ROOT, name)
    base = {"name": name, "type": "unknown", "previewable": False, "mode": "none", "reason": ""}
    if not os.path.isdir(path):
        return {**base, "type": "missing", "reason": "not found"}
    pkg     = _read_pkg(path)
    deps    = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    scripts = pkg.get("scripts", {})

    def has(f):
        return os.path.exists(os.path.join(path, f))

    is_electron = "electron" in deps or "electron" in str(pkg.get("main", ""))
    is_cep      = has("CSXS") or bool(_glob.glob(os.path.join(path, "**/CSXS/manifest.xml"), recursive=True))
    is_vite     = "vite" in deps
    is_next     = "next" in deps
    is_cra      = "react-scripts" in deps
    has_modules = has("node_modules")

    if is_cep:
        return {**base, "type": "cep",
                "reason": "Adobe CEP extension — runs inside Premiere Pro, not a browser"}

    dev_script = next((s for s in ("dev", "start", "preview") if s in scripts), None)
    if dev_script and (is_vite or is_next or is_cra or is_electron):
        if not has_modules:
            return {**base, "type": "node",
                    "reason": f"node_modules missing — run `npm install` in {name} first"}
        typ  = ("electron-web" if is_electron else
                "vite" if is_vite else "next" if is_next else "cra" if is_cra else "node")
        note = ("renderer preview only — Electron/native (IPC, fs) calls will error in the console"
                if is_electron else "")
        return {**base, "type": typ, "previewable": True, "mode": "dev-server",
                "dev_script": dev_script, "reason": note}

    static_idx = next((loc for loc in
                       ("index.html", "public/index.html", "dist/index.html", "build/index.html")
                       if has(loc)), None)
    if static_idx:
        return {**base, "type": "static", "previewable": True, "mode": "static",
                "static_index": static_idx, "reason": ""}

    if has("requirements.txt") or _glob.glob(os.path.join(path, "*.py")):
        return {**base, "type": "python", "reason": "Python project — no browser UI to preview"}

    return {**base, "reason": "no index.html or dev script detected"}


def _stop_dev(name=None):
    """Stop one dev server (by name) or all — escalate SIGTERM → SIGKILL on the process group."""
    with _DEV_LOCK:
        names = [name] if name else list(_DEV_PROCS.keys())
        entries = [(n, _DEV_PROCS.pop(n, None)) for n in names]
    for n, entry in entries:
        if not entry:
            continue
        proc = entry["proc"]
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
        for sig in (_signal.SIGTERM, _signal.SIGKILL):
            if pgid is None or proc.poll() is not None:
                break
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            except Exception:
                pass
            for _ in range(15):           # wait up to 1.5s before escalating
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def _start_dev(name, info):
    """Spawn the project's dev server under a PTY (so it flushes its URL line) and parse it."""
    import pty, select, struct, fcntl, termios
    path = os.path.join(_PROJECTS_ROOT, name)
    env  = os.environ.copy()
    env["PATH"]    = _node_path_dirs() + os.pathsep + env.get("PATH", "")
    env["BROWSER"] = "none"   # CRA: don't auto-open a browser
    env.pop("CI", None)        # never imply CI mode — it suppresses vite's dev URL

    master, slave = pty.openpty()
    # Give the PTY a real window size, else TUI tools wrap/truncate the URL line.
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    except Exception:
        pass
    try:
        proc = _subp.Popen(
            ["npm", "run", info["dev_script"]],
            cwd=path, env=env,
            stdout=slave, stderr=slave, stdin=slave,
            start_new_session=True, close_fds=True,
        )
    except Exception as e:
        os.close(master); os.close(slave)
        return {"ok": False, "reason": f"could not launch npm: {e}"}
    os.close(slave)

    url, buf = None, b""
    deadline = time.time() + 50
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 1.0)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # Vite color-codes the port digits separately, so strip ANSI before matching.
            clean = re.sub(rb"\x1b\[[0-9;]*m", b"", buf)
            m = re.search(rb"https?://(?:localhost|127\.0\.0\.1):(\d+)", clean)
            if m:
                url = "http://localhost:%s/" % m.group(1).decode()
                break
        elif proc.poll() is not None:
            break

    if not url:
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
        except Exception:
            pass
        os.close(master)
        tail = re.sub(r"\x1b\[[0-9;]*m", "", buf.decode("utf-8", "replace"))[-700:]
        return {"ok": False, "reason": "dev server didn't report a URL in 50s.\n" + tail}

    # Drain the PTY in the background so the child never blocks on a full buffer.
    def drain():
        try:
            while True:
                if not os.read(master, 4096):
                    break
        except OSError:
            pass
        finally:
            try:
                os.close(master)
            except Exception:
                pass
    threading.Thread(target=drain, daemon=True).start()

    with _DEV_LOCK:
        _DEV_PROCS[name] = {"proc": proc, "url": url, "type": info["type"], "master": master}
    return {"ok": True, "url": url}


@app.route("/api/projects")
def api_projects():
    """List Cld projects with detected stack + previewability."""
    names = sorted(d for d in os.listdir(_PROJECTS_ROOT)
                   if os.path.isdir(os.path.join(_PROJECTS_ROOT, d)) and not d.startswith("."))
    out = []
    for n in names:
        info = _detect_project(n)
        info["running"] = n in _DEV_PROCS
        out.append(info)
    return jsonify(out)


@app.route("/api/preview/project", methods=["POST"])
def api_preview_project():
    d    = request.get_json(force=True) or {}
    name = _safe_slug(d.get("name"), "")
    if not name:
        return jsonify(ok=False, reason="no project name"), 400
    info = _detect_project(name)
    if not info["previewable"]:
        return jsonify(ok=False, reason=info["reason"] or "not previewable", info=info)

    if info["mode"] == "static":
        idx = info["static_index"]
        return jsonify(ok=True, mode="static", type=info["type"],
                       url=f"/project-static/{name}/{idx}", note=info["reason"])

    # dev-server: only one at a time — stop the others first
    if name in _DEV_PROCS:
        return jsonify(ok=True, mode="dev-server", type=info["type"],
                       url=_DEV_PROCS[name]["url"], note=info["reason"], reused=True)
    _stop_dev()  # stop any other running preview
    res = _start_dev(name, info)
    if not res["ok"]:
        return jsonify(ok=False, reason=res["reason"], info=info)
    return jsonify(ok=True, mode="dev-server", type=info["type"], url=res["url"], note=info["reason"])


@app.route("/api/preview/project/stop", methods=["POST"])
def api_preview_project_stop():
    d = request.get_json(silent=True) or {}
    _stop_dev(_safe_slug(d.get("name")) if d.get("name") else None)
    return jsonify(ok=True, running=list(_DEV_PROCS.keys()))


@app.route("/project-static/<name>/")
@app.route("/project-static/<name>/<path:subpath>")
def serve_project_static(name, subpath="index.html"):
    name = _safe_slug(name, "")
    base = os.path.realpath(os.path.join(_PROJECTS_ROOT, name))
    full = os.path.realpath(os.path.join(base, subpath))
    if not (full == base or full.startswith(base + os.sep)):
        return "Forbidden", 403
    if os.path.isdir(full):
        subpath = os.path.join(subpath, "index.html")
        full = os.path.join(full, "index.html")
    if not os.path.exists(full):
        return "Not found", 404
    # Inject a <base> tag into the entry HTML so relative asset paths resolve.
    if full.endswith(".html"):
        try:
            html = open(full, encoding="utf-8").read()
            href = "/project-static/" + name + "/" + os.path.dirname(subpath)
            if not href.endswith("/"):
                href += "/"
            if "<base " not in html.lower():
                html = re.sub(r"(<head[^>]*>)", r"\1\n<base href='" + href + "'>",
                              html, count=1, flags=re.I)
            return Response(html, mimetype="text/html",
                            headers={"Cache-Control": "no-store"})
        except Exception:
            pass
    resp = send_from_directory(base, subpath)
    resp.headers["Cache-Control"] = "no-store"
    return resp


_atexit.register(lambda: _stop_dev())


# ---- task engine routes -------------------------------------------

def _tasks_skill():
    import importlib.util, sys
    skills_dir = os.path.expanduser("~/.runai/skills")
    if skills_dir not in sys.path:
        sys.path.insert(0, skills_dir)
    spec = importlib.util.spec_from_file_location("tasks", os.path.join(skills_dir, "tasks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@app.route("/api/tasks", methods=["GET"])
def api_tasks_list():
    mod = _tasks_skill()
    args = {k: v for k, v in request.args.items()}
    args["action"] = "list"
    args["limit"]  = int(args.get("limit", 200))
    # Return raw task objects for the UI
    tasks = mod._load()
    project  = args.get("project","").lower()
    category = args.get("category","").lower()
    status   = args.get("status","").lower()
    if project:  tasks = [t for t in tasks if t.get("project","").lower() == project]
    if category: tasks = [t for t in tasks if t.get("category","").lower() == category]
    if status:   tasks = [t for t in tasks if t.get("status","").lower() == status]
    prio_order = {"high":0,"medium":1,"low":2}
    stat_order = {"doing":0,"todo":1,"blocked":2,"done":3,"cancelled":4}
    tasks.sort(key=lambda t: (stat_order.get(t.get("status","todo"),5),
                              prio_order.get(t.get("priority","medium"),3),
                              t.get("created","")))
    return jsonify(tasks)

@app.route("/api/tasks", methods=["POST"])
def api_tasks_create():
    mod  = _tasks_skill()
    data = request.json or {}
    data["action"] = "create"
    result = mod.run(data)
    tasks = mod._load()
    last  = tasks[-1] if tasks else {}
    return jsonify({"message": result, "task": last})

@app.route("/api/tasks/<tid>", methods=["PATCH"])
def api_tasks_update(tid):
    mod  = _tasks_skill()
    data = request.json or {}
    # Allow bulk field updates: {field: value, field2: value2}
    results = []
    for field, value in data.items():
        r = mod.run({"action":"update","id":tid,"field":field,"value":value})
        results.append(r)
    return jsonify({"message": "\n".join(results)})

@app.route("/api/tasks/<tid>", methods=["DELETE"])
def api_tasks_delete(tid):
    mod = _tasks_skill()
    result = mod.run({"action":"delete","id":tid})
    return jsonify({"message": result})

@app.route("/api/tasks/summary", methods=["GET"])
def api_tasks_summary():
    mod = _tasks_skill()
    result = mod.run({"action":"summary"})
    return jsonify({"summary": result})


# ----------------------------------------------------------------- the UI
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sidka</title>
<style>
:root {
  --bg:     #0d0d0d;
  --bg2:    #111;
  --bg3:    #181818;
  --bg4:    #1e1e1e;
  --border: #252525;
  --border2:#2e2e2e;
  --text:   #e8e8e8;
  --dim:    #666;
  --mid:    #aaa;
  --acc:    #C9A84C;
  --ok:     #4ade80;
  --err:    #f87171;
  --warn:   #fb923c;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font: 13.5px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  display: flex;
  overflow: hidden;
}

/* ---- sidebar ---- */
.side {
  width: 196px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 18px 12px 14px;
  flex-shrink: 0;
}
.brand {
  font-size: 20px;
  font-weight: 800;
  letter-spacing: 0.5px;
  margin-bottom: 18px;
  padding-left: 4px;
}
.brand .ai { color: var(--acc); }
.nav {
  padding: 8px 12px;
  border-radius: 7px;
  cursor: pointer;
  color: var(--dim);
  font-size: 13px;
  transition: background 0.15s, color 0.15s;
  user-select: none;
}
.nav:hover { background: var(--bg3); color: var(--text); }
.nav.on    { background: var(--bg3); color: var(--text); font-weight: 600; }
.side-foot { margin-top: auto; font-size: 11px; color: var(--dim); padding-left: 4px; line-height: 1.7; }

/* ---- main area ---- */
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.tab  { display: none; flex: 1; flex-direction: column; min-height: 0; }
.tab.on { display: flex; }

/* ---- chat tab layout ---- */
#tab-chat { flex-direction: row; overflow: hidden; }
.chat-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  border-right: 1px solid var(--border);
  overflow: hidden;
}
.cockpit-col {
  width: 380px;
  flex-shrink: 0;
  display: none;
  flex-direction: column;
  background: var(--bg2);
  overflow: hidden;
}
.cockpit-col.visible { display: flex; }

/* ---- file tree panel ---- */
.filetree-col {
  width: 240px;
  flex-shrink: 0;
  display: none;
  flex-direction: column;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  overflow: hidden;
}
.filetree-col.visible { display: flex; }
.ft-header {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: var(--dim);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ft-header .ft-project { color: var(--acc); font-size: 12px; font-weight: 700; text-transform: none; }
.ft-body { flex: 1; overflow-y: auto; padding: 4px 0; }
.ft-node { cursor: pointer; user-select: none; }
.ft-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  font-size: 12px;
  color: var(--dim);
  border-radius: 4px;
  margin: 0 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ft-row:hover { background: var(--bg3); color: var(--text); }
.ft-row.ft-file:hover { color: var(--acc); }
.ft-row.ft-open { color: var(--text); }
.ft-icon { font-size: 11px; flex-shrink: 0; opacity: 0.7; }
.ft-name { overflow: hidden; text-overflow: ellipsis; }
.ft-children { display: none; }
.ft-children.open { display: block; }

/* ---- preview panel ---- */
.preview-col {
  width: 460px;
  flex-shrink: 0;
  display: none;
  flex-direction: column;
  background: var(--bg2);
  border-left: 1px solid var(--border);
  overflow: hidden;
}
.preview-col.visible { display: flex; }
.preview-header {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.preview-header .label { font-size: 10px; font-weight: 600; letter-spacing: 0.8px; color: var(--dim); text-transform: uppercase; }
.preview-header .port-badge { font-size: 11px; background: var(--bg4); border: 1px solid var(--border2); border-radius: 10px; padding: 1px 8px; color: var(--acc); }
.preview-body { flex: 1; overflow: hidden; position: relative; }
.preview-body iframe { width: 100%; height: 100%; border: none; display: block; background: #111; }
.preview-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; gap: 10px; color: var(--dim); font-size: 13px; }
.preview-port-input { display: flex; gap: 6px; align-items: center; }
.preview-port-input input { width: 70px; font-size: 12px; padding: 4px 8px; background: var(--bg3); border: 1px solid var(--border2); border-radius: 6px; color: var(--text); }

/* ---- file viewer overlay ---- */
#fileOverlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.7);
  z-index: 100;
  align-items: center;
  justify-content: center;
}
#fileOverlay.open { display: flex; }
.file-modal {
  background: var(--bg2);
  border: 1px solid var(--border2);
  border-radius: 10px;
  width: 820px;
  max-width: 95vw;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.file-modal-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.file-modal-head .fpath { font-family: monospace; font-size: 11px; color: var(--dim); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-modal-body { flex: 1; overflow: auto; }
.file-modal-body pre { margin: 0; padding: 14px 16px; font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; color: var(--text); }

/* ---- tasks tab ---- */
#tab-tasks { flex-direction: column; overflow: hidden; }
.tasks-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 16px; border-bottom: 1px solid var(--border);
  flex-shrink: 0; flex-wrap: wrap;
}
.tasks-toolbar h2 { font-size: 15px; font-weight: 700; margin-right: 4px; }
.filter-pill {
  font-size: 11px; padding: 3px 12px; border-radius: 9999px; cursor: pointer;
  border: 1px solid var(--border2); background: transparent; color: var(--dim);
  transition: background 0.12s, color 0.12s;
}
.filter-pill:hover, .filter-pill.on { background: var(--bg3); color: var(--text); }
.filter-pill.on { border-color: var(--acc); color: var(--acc); }
.tasks-board {
  flex: 1; display: flex; gap: 0; overflow: hidden;
}
.kb-col {
  flex: 1; display: flex; flex-direction: column; border-right: 1px solid var(--border);
  min-width: 0; overflow: hidden;
}
.kb-col:last-child { border-right: none; }
.kb-col-head {
  padding: 10px 12px 8px; font-size: 10px; font-weight: 700;
  letter-spacing: 1px; text-transform: uppercase; color: var(--dim);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
  display: flex; align-items: center; justify-content: space-between;
}
.kb-col-head .kb-count {
  font-size: 11px; font-weight: 500; background: var(--bg3);
  border-radius: 10px; padding: 1px 7px; color: var(--mid);
}
.kb-cards { flex: 1; overflow-y: auto; padding: 6px; display: flex; flex-direction: column; gap: 5px; }
.kb-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 8px; padding: 9px 11px; cursor: pointer;
  transition: border-color 0.12s; font-size: 13px;
}
.kb-card:hover { border-color: var(--border2); }
.kb-card.expanded { border-color: var(--acc); }
.kb-card-title { font-size: 13px; color: var(--text); line-height: 1.4; margin-bottom: 6px; }
.kb-card-meta { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
.cat-badge {
  font-size: 9px; font-weight: 700; letter-spacing: 0.5px;
  padding: 1px 6px; border-radius: 8px; text-transform: uppercase;
}
.pri-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.kb-card-age { font-size: 10px; color: var(--dim); margin-left: auto; }
.kb-card-detail {
  display: none; margin-top: 8px; padding-top: 8px;
  border-top: 1px solid var(--border); font-size: 12px; color: var(--dim);
}
.kb-card.expanded .kb-card-detail { display: block; }
.kb-card-notes { color: var(--mid); font-size: 12px; margin-bottom: 8px; line-height: 1.5; }
.kb-card-actions { display: flex; gap: 5px; flex-wrap: wrap; }
.kb-card-actions button {
  font-size: 10px; padding: 2px 10px; border-radius: 9999px;
  border: 1px solid var(--border2); background: transparent; color: var(--dim);
  cursor: pointer; transition: background 0.1s;
}
.kb-card-actions button:hover { background: var(--bg3); color: var(--text); }
.kb-card-actions .btn-danger:hover { background: #3a1010; color: #f07070; border-color: #5a2020; }
.new-task-form {
  margin: 6px; padding: 10px 12px; background: var(--bg2);
  border: 1px solid var(--border2); border-radius: 8px; display: none;
}
.new-task-form.open { display: block; }
.new-task-form input, .new-task-form select, .new-task-form textarea {
  width: 100%; margin-bottom: 6px; font-size: 12px;
}
.new-task-form textarea { height: 50px; resize: vertical; }

/* ---- chat header ---- */
.chat-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.chat-header h2 { font-size: 15px; font-weight: 700; }
.spacer { flex: 1; }
label.ctrl { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--mid); cursor: pointer; white-space: nowrap; }
label.ctrl input[type=checkbox] { accent-color: var(--acc); width: 14px; height: 14px; }

select, input[type=text] {
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: 6px;
  color: var(--text);
  font: inherit;
  font-size: 12px;
  padding: 5px 8px;
  outline: none;
  cursor: pointer;
}
select:focus, input[type=text]:focus { border-color: var(--acc); }

button {
  background: var(--acc);
  color: #1a1200;
  border: none;
  border-radius: 9999px;
  font: inherit;
  font-size: 12px;
  font-weight: 700;
  padding: 6px 14px;
  cursor: pointer;
  transition: filter 0.12s;
  white-space: nowrap;
}
button:hover { filter: brightness(1.1); }
button.ghost {
  background: var(--bg3);
  color: var(--mid);
  border: 1px solid var(--border2);
  font-weight: 500;
}
button.ghost:hover { color: var(--text); }
button.ok-btn  { background: var(--ok);  color: #021a0a; }
button.err-btn { background: var(--err); color: #1a0202; }

/* ---- messages ---- */
.msgs {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
}
.msg .who {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.8px;
  text-transform: uppercase;
  color: var(--dim);
  margin-bottom: 4px;
}
.bubble {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 13px;
  word-break: break-word;
  font-size: 13.5px;
  line-height: 1.6;
  max-width: 720px;
}
.bubble.pre { white-space: pre-wrap; }
.msg.me .bubble { background: var(--bg3); }
.bubble code { background: #0a0a0a; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.bubble pre  { background: #0a0a0a; padding: 10px 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0; font-size: 12px; white-space: pre; }
.svg-preview { background: #111; border: 1px solid #2a2a2a; border-radius: 8px; padding: 14px; margin: 8px 0; overflow: auto; display: flex; align-items: flex-start; justify-content: center; }
.svg-preview svg { max-width: 100%; height: auto; display: block; }
.html-preview { margin: 8px 0; border-radius: 8px; overflow: hidden; border: 1px solid #2a2a2a; background: #111; }
.html-preview iframe { width: 100%; border: none; display: block; min-height: 320px; background: #111; }
.code-header { font-size: 10px; letter-spacing: 0.8px; color: #555; padding: 6px 12px 0; text-transform: uppercase; font-weight: 600; }
.tool-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.tool-tag {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.4px;
  padding: 2px 7px;
  border-radius: 20px;
  background: var(--bg4);
  border: 1px solid var(--border2);
  color: var(--acc);
}

/* ---- quick command chips ---- */
#qcChips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  padding: 6px 16px 0;
  flex-shrink: 0;
}

/* ---- composer ---- */
.composer {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.composer textarea {
  flex: 1;
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: 8px;
  color: var(--text);
  font: 13px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  padding: 9px 11px;
  resize: none;
  min-height: 42px;
  max-height: 140px;
  outline: none;
}
.composer textarea:focus { border-color: var(--acc); }

/* ---- cockpit panel ---- */
.cockpit-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 11px 14px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.cockpit-header .label {
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 1.5px;
  color: var(--acc);
}
.cockpit-header .model-badge {
  font-size: 10px;
  color: var(--dim);
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}
.dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  display: inline-block;
}
.dot.green  { background: var(--ok); }
.dot.amber  { background: var(--warn); }

.cockpit-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ---- pending section ---- */
.pending-section {
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  max-height: 55%;
  overflow-y: auto;
}
.pending-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--dim);
  padding: 9px 14px 6px;
  text-transform: uppercase;
}
.action-card {
  margin: 0 10px 10px;
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: 9px;
  padding: 10px 12px;
}
.action-card-top {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 6px;
}
.badge {
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.7px;
  text-transform: uppercase;
  padding: 2px 7px;
  border-radius: 20px;
}
.badge.write  { background: rgba(201,168,76,0.15);  color: var(--acc);  border: 1px solid rgba(201,168,76,0.3); }
.badge.shell  { background: rgba(251,146,60,0.12);  color: var(--warn); border: 1px solid rgba(251,146,60,0.3); }
.badge.delete { background: rgba(248,113,113,0.12); color: var(--err);  border: 1px solid rgba(248,113,113,0.3); }
.badge.move   { background: rgba(125,211,252,0.12); color: #7dd3fc;     border: 1px solid rgba(125,211,252,0.3); }
.action-label {
  font-size: 11.5px;
  color: var(--mid);
  word-break: break-all;
  flex: 1;
}
.diff-wrap {
  background: #080808;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  font: 11px/1.5 "JetBrains Mono", "Fira Code", ui-monospace, monospace;
  overflow: auto;
  max-height: 180px;
  margin-bottom: 8px;
  white-space: pre;
}
.diff-add  { background: rgba(74,222,128,0.08); color: #86efac; display: block; }
.diff-del  { background: rgba(248,113,113,0.08); color: #fca5a5; display: block; }
.diff-hunk { color: #7dd3fc; display: block; }
.diff-ctx  { color: #444; display: block; }
.action-btns { display: flex; gap: 7px; }
.action-btns button { flex: 1; font-size: 11px; padding: 5px 0; }
.action-resolved {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 0;
  text-align: center;
}
.action-resolved.ok  { color: var(--ok); }
.action-resolved.err { color: var(--err); }

/* ---- activity log ---- */
.activity-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.activity-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--dim);
  padding: 9px 14px 6px;
  text-transform: uppercase;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
.activity-log {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.log-item {
  font-size: 11px;
  color: var(--mid);
  padding: 4px 8px;
  border-radius: 5px;
  background: var(--bg3);
  border: 1px solid var(--border);
  font-family: ui-monospace, monospace;
  word-break: break-all;
  line-height: 1.4;
}
.log-item .log-fn  { color: var(--acc); font-weight: 600; }
.log-item .log-out { color: var(--dim); }

/* ---- other tabs ---- */
.tab-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 18px 22px;
}
h2 { font-size: 15px; font-weight: 700; margin-bottom: 14px; }
.row { display: flex; gap: 8px; align-items: center; }
.card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
  margin-bottom: 12px;
}
.card h3 { margin-bottom: 4px; font-size: 13.5px; }
.muted { color: var(--dim); font-size: 12px; }
.pill {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 20px;
  background: var(--bg3);
  color: var(--dim);
  border: 1px solid var(--border2);
}
.pill.ok  { color: var(--ok);  border-color: rgba(74,222,128,0.3); }
.pill.err { color: var(--err); border-color: rgba(248,113,113,0.3); }
.action-row { display: flex; gap: 8px; margin-top: 6px; align-items: flex-start; }
.action-row select { flex: 0 0 130px; }
.action-row textarea {
  flex: 1;
  min-height: 36px;
  resize: vertical;
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: 6px;
  color: var(--text);
  font: 12px/1.4 ui-monospace, monospace;
  padding: 6px 9px;
  outline: none;
}
.steps { margin-top: 8px; font: 11.5px/1.5 ui-monospace, monospace; }
.step { padding: 5px 9px; background: var(--bg3); border-radius: 6px; margin-top: 5px; border: 1px solid var(--border); }
.kv   { color: var(--acc); }
.url-box {
  font: 11.5px/1.5 ui-monospace, monospace;
  background: var(--bg3);
  border: 1px solid var(--border2);
  padding: 6px 9px;
  border-radius: 6px;
  word-break: break-all;
  color: var(--mid);
}
.flex { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.sep  { height: 1px; background: var(--border); margin: 10px 0; }
input[type=text].full { width: 100%; }

/* ===== Sidka UI v2 — Editorial Dark (design panel winner + brass-stripe graft) ===== */
:root{
  --bg:#0b0b0c;--bg2:#101012;--bg3:#161618;--bg4:#1d1d20;
  --border:#242427;--border2:#303035;--hair:rgba(255,255,255,.06);
  --text:#ededee;--mid:#a6a6ab;--dim:#6a6a70;
  --acc:#cdac55;--acc-soft:rgba(205,172,85,.14);--acc-line:rgba(205,172,85,.55);
  --ok:#5cd693;--err:#f3837e;--warn:#f0a24e;
  --s1:6px;--s2:10px;--s3:14px;--s4:20px;--s5:28px;
  --rad:12px;--rad-sm:9px;
  --mono:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,monospace;
  --sans:-apple-system,"SF Pro Text","Segoe UI",system-ui,sans-serif;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 18px -8px rgba(0,0,0,.55);
}
body,.main,.msgs{background:var(--bg);color:var(--text);font-family:var(--sans);letter-spacing:.1px;}

/* ---- Sidebar ---- */
.sidebar{
  background:linear-gradient(180deg,var(--bg2),var(--bg));
  border-right:1px solid var(--border);
  padding:var(--s4) var(--s3);width:212px;
}
.sidebar>:first-child{
  font-weight:600;font-size:16px;letter-spacing:.4px;
  padding:0 var(--s2) var(--s4);color:var(--text);
}
.nav{
  display:flex;align-items:center;gap:var(--s2);
  padding:9px var(--s2);margin:2px 0;border-radius:var(--rad-sm);
  color:var(--mid);font-size:13.5px;font-weight:500;
  border:1px solid transparent;cursor:pointer;
  transition:background .14s ease,color .14s ease;
}
.nav:hover{background:var(--bg3);color:var(--text);}
.nav.on{
  background:var(--bg4);color:var(--text);
  border:1px solid var(--border2);
  box-shadow:inset 2px 0 0 var(--acc),var(--shadow);
}
.side-foot{
  margin-top:auto;padding:var(--s3) var(--s2) 0;
  border-top:1px solid var(--hair);
  font-family:var(--mono);font-size:11px;letter-spacing:.4px;
  color:var(--dim);text-transform:uppercase;
}

/* ---- Chat header ---- */
.chat-header{
  background:linear-gradient(180deg,var(--bg2),var(--bg));
  border-bottom:1px solid var(--border);
  padding:var(--s4) var(--s5);gap:var(--s3);
}
.chat-header>:first-child{
  font-size:20px;font-weight:600;letter-spacing:-.2px;color:var(--text);
}
.chat-header select{
  background:var(--bg3);color:var(--mid);
  border:1px solid var(--border2);border-radius:var(--rad-sm);
  padding:6px 10px;font-size:12.5px;font-family:var(--mono);letter-spacing:.3px;
}
.chat-header select:hover{border-color:var(--acc-line);color:var(--text);}

/* ---- Messages ---- */
.msgs{padding:var(--s5) var(--s5) var(--s4);}
.msg{margin:0 0 var(--s5);max-width:760px;}
.who{
  font-family:var(--mono);font-size:10.5px;font-weight:500;
  text-transform:uppercase;letter-spacing:1.4px;
  color:var(--dim);margin:0 0 var(--s2);padding-left:2px;
}
.bubble{
  background:var(--bg3);
  border:1px solid var(--border2);
  border-radius:var(--rad);
  padding:var(--s3) var(--s4);
  line-height:1.62;font-size:14.5px;color:var(--text);
  box-shadow:var(--shadow);
}
.bubble p{margin:0 0 .7em;}.bubble p:last-child{margin-bottom:0;}
.bubble a{color:var(--acc);text-decoration:none;border-bottom:1px solid var(--acc-line);}
.bubble code{
  font-family:var(--mono);font-size:12.5px;
  background:var(--bg);border:1px solid var(--hair);
  border-radius:5px;padding:1px 5px;color:var(--text);
}
.svg-preview,.html-preview{
  background:#0a0a0b;border:1px solid var(--border);
  border-radius:0 0 var(--rad-sm) var(--rad-sm);overflow:hidden;margin-top:0;
}
.artifact-wrap{margin:var(--s2) 0;}
.artifact-bar{
  display:flex;align-items:center;gap:var(--s2);
  background:var(--bg4);border:1px solid var(--border);border-bottom:none;
  border-radius:var(--rad-sm) var(--rad-sm) 0 0;
  padding:6px 10px;
}
.artifact-tag{
  font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:1px;
  text-transform:uppercase;color:var(--dim);
}
.artifact-btn{
  background:var(--bg3);color:var(--mid);
  border:1px solid var(--border2);border-radius:9999px;
  font-size:11.5px;font-weight:500;padding:3px 11px;cursor:pointer;
  transition:all .14s ease;white-space:nowrap;
}
.artifact-btn:hover{color:var(--acc);border-color:var(--acc-line);background:var(--acc-soft);}
.html-preview iframe{min-height:380px;}

/* ---- project preview picker ---- */
#projectPicker{position:absolute;inset:0;overflow-y:auto;background:var(--bg);padding:var(--s3);}
.pp-head{display:flex;align-items:center;justify-content:space-between;
  font-family:var(--mono);font-size:11px;letter-spacing:.5px;text-transform:uppercase;
  color:var(--mid);padding:0 2px var(--s2);}
.pp-sub{font-family:var(--mono);font-size:10.5px;letter-spacing:.5px;text-transform:uppercase;
  color:var(--dim);padding:var(--s4) 2px var(--s2);border-top:1px solid var(--hair);margin-top:var(--s3);}
.pp-list{display:flex;flex-direction:column;gap:4px;}
.pp-row{display:flex;align-items:center;gap:var(--s2);padding:8px 10px;
  background:var(--bg2);border:1px solid var(--border);border-radius:var(--rad-sm);
  cursor:pointer;transition:border-color .12s ease,background .12s ease;}
.pp-row:hover{border-color:var(--acc-line);background:var(--bg3);}
.pp-row-off{opacity:.5;cursor:default;}
.pp-row-off:hover{border-color:var(--border);background:var(--bg2);}
.pp-name{font-size:13px;font-weight:600;color:var(--text);min-width:130px;}
.pp-tag{font-family:var(--mono);font-size:10px;font-weight:600;text-transform:uppercase;
  letter-spacing:.5px;padding:1px 7px;border:1px solid;border-radius:9999px;}
.pp-reason{font-size:11px;color:var(--dim);margin-left:auto;text-align:right;max-width:55%;}
.pp-loading{color:var(--dim);font-size:13px;padding:var(--s4);text-align:center;}
#previewStatus{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:var(--s2);background:var(--bg);color:var(--text);font-size:14px;text-align:center;padding:var(--s4);}
.pp-spin,.pp-loading::before{}
.pp-spin{width:22px;height:22px;border:2px solid var(--border2);border-top-color:var(--acc);
  border-radius:50%;animation:ppspin .7s linear infinite;}
@keyframes ppspin{to{transform:rotate(360deg)}}

/* ---- Composer ---- */
.composer{
  background:linear-gradient(180deg,var(--bg),var(--bg2));
  border-top:1px solid var(--border);
  padding:var(--s3) var(--s5) var(--s4);gap:var(--s2);
}
.composer textarea{
  background:var(--bg3);color:var(--text);
  border:1px solid var(--border2);border-radius:var(--rad);
  padding:var(--s3) var(--s3);font-size:14.5px;line-height:1.55;font-family:var(--sans);
  transition:border-color .14s ease,box-shadow .14s ease;
}
.composer textarea::placeholder{color:var(--dim);}
.composer textarea:focus{
  outline:none;border-color:var(--acc-line);
  box-shadow:0 0 0 3px var(--acc-soft);
}

/* ---- Pills / chips / buttons ---- */
.filter-pill{
  background:var(--bg3);color:var(--mid);
  border:1px solid var(--border2);
  padding:5px 13px;font-size:12.5px;font-weight:500;
  transition:all .14s ease;
}
.filter-pill:hover{color:var(--text);border-color:var(--acc-line);}
.filter-pill.on{background:var(--acc-soft);color:var(--acc);border-color:var(--acc-line);}

/* ---- Tasks / Kanban ---- */
.kb-col{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:var(--rad);padding:var(--s3);
}
.kb-col-head{
  font-family:var(--mono);font-size:11px;font-weight:500;
  text-transform:uppercase;letter-spacing:1px;color:var(--mid);
  padding:0 var(--s1) var(--s3);
  border-bottom:1px solid var(--hair);margin-bottom:var(--s3);
}
.kb-card{
  background:var(--bg4);border:1px solid var(--border2);
  border-radius:var(--rad-sm);padding:var(--s3);margin-bottom:var(--s2);
  box-shadow:var(--shadow);transition:transform .12s ease,border-color .14s ease;
}
.kb-card:hover{transform:translateY(-1px);border-color:var(--acc-line);}
.kb-card-title{font-size:13.5px;font-weight:600;color:var(--text);line-height:1.4;margin-bottom:6px;}
.kb-card-meta{
  font-family:var(--mono);font-size:11px;letter-spacing:.3px;color:var(--dim);
}
.kb-card-actions{display:flex;gap:6px;margin-top:var(--s2);}
.kb-card-actions button{
  background:var(--bg3);color:var(--mid);border:1px solid var(--border2);
  font-size:11.5px;padding:4px 10px;transition:all .14s ease;
}
.kb-card-actions button:hover{color:var(--text);border-color:var(--acc-line);}

/* ---- Tabs ---- */
.tab{
  color:var(--dim);font-size:13px;font-weight:500;
  padding:8px 2px;border-bottom:2px solid transparent;transition:color .14s ease;
}
.tab:hover{color:var(--mid);}
.tab.on{color:var(--text);border-bottom-color:var(--acc);}
/* Graft from [1] Warp Terminal Pro: the active-nav brass stripe is crisper
   than the winner's inset box-shadow rule. Layer on top of Editorial Dark.
   Equal/append specificity wins by source order. */
.nav{position:relative;}
.nav.on{
  background:linear-gradient(90deg,var(--acc-soft),transparent 70%);
  box-shadow:var(--shadow);
}
.nav.on::before{
  content:"";position:absolute;left:5px;top:50%;
  transform:translateY(-50%);
  width:3px;height:16px;border-radius:3px;
  background:var(--acc);
}
</style>
</head>
<body>

<!-- sidebar -->
<div class="side">
  <div class="brand">Sid<span class="ai">ka</span></div>
  <div class="nav on" data-tab="chat">Chat</div>
  <div class="nav" data-tab="autos">Automations</div>
  <div class="nav" data-tab="runs">Runs</div>
  <div class="nav" data-tab="skills">Skills</div>
  <div class="nav" data-tab="quick">Quick</div>
  <div class="nav" data-tab="tasks">Tasks</div>
  <div class="side-foot" id="foot">local &middot; :8765</div>
</div>

<!-- main -->
<div class="main">

  <!-- CHAT TAB -->
  <div class="tab on" id="tab-chat">

    <!-- file tree column -->
    <div class="filetree-col" id="filetreeCol">
      <div class="ft-header">
        <span>Files</span>
        <span class="ft-project" id="ftProject"></span>
      </div>
      <div class="ft-body" id="ftBody"></div>
    </div>

    <!-- chat column -->
    <div class="chat-col">
      <div class="chat-header">
        <h2>Chat</h2>
        <button class="ghost" id="filesBtn" title="Toggle file tree">Files</button>
        <div class="spacer"></div>
        <label class="ctrl"><input type="checkbox" id="agentChk"> cockpit mode</label>
        <select id="model"></select>
        <select id="session"></select>
        <button class="ghost" id="previewBtn" title="Toggle live preview">Preview</button>
        <button class="ghost" id="exportBtn">export</button>
        <button class="ghost" id="clearBtn">clear</button>
      </div>
      <div class="msgs" id="msgs"></div>
      <div id="qcChips"></div>
      <div class="composer" style="position:relative">
        <div id="slashMenu" style="display:none;position:absolute;bottom:100%;left:0;right:0;background:var(--bg2);border:1px solid var(--border2);border-radius:8px;margin-bottom:4px;overflow:hidden;z-index:10"></div>
        <textarea id="input" placeholder="Ask anything... or /status /standup /journal /workon nova-daw /design timeline editor /diagram /chart" rows="1"></textarea>
        <button id="send">Send</button>
      </div>
    </div>

    <!-- cockpit column -->
    <div class="cockpit-col" id="cockpitCol">
      <div class="cockpit-header">
        <span class="label">COCKPIT</span>
        <div class="model-badge">
          <span class="dot" id="modelDot"></span>
          <span id="modelName">&#8212;</span>
        </div>
      </div>
      <div class="cockpit-body">
        <div class="pending-section" id="pendingSection" style="display:none">
          <div class="pending-title">Pending Approvals</div>
          <div id="pendingCards"></div>
        </div>
        <div class="activity-section">
          <div class="activity-title">Activity</div>
          <div class="activity-log" id="activityLog"><div class="muted" style="padding:6px 4px">Agent output will appear here...</div></div>
        </div>
      </div>
    </div>

    <!-- preview column -->
    <div class="preview-col" id="previewCol">
      <div class="preview-header">
        <span class="label">PREVIEW</span>
        <span class="port-badge" id="previewBadge" style="display:none"></span>
        <div style="flex:1"></div>
        <button class="ghost" id="previewProjectsBtn" title="Preview a Cld project" style="font-size:11px;padding:2px 8px">&#9638; projects</button>
        <button class="ghost" id="previewProbeBtn" title="Find running dev server" style="font-size:11px;padding:2px 8px">probe</button>
        <button class="ghost" id="previewRefreshBtn" title="Reload preview" style="font-size:11px;padding:2px 8px">reload</button>
        <button class="ghost" id="previewStopBtn" title="Stop dev server" style="font-size:11px;padding:2px 8px;display:none">stop</button>
        <button class="ghost" id="previewNewTabBtn" title="Open in new tab" style="font-size:11px;padding:2px 8px">&#8599;</button>
      </div>
      <div class="preview-body" id="previewBody" style="position:relative">
        <div class="preview-empty" id="previewEmpty">
          <div style="font-size:13px;color:var(--dim)">Pick a project or connect a port</div>
          <button class="ghost" onclick="openProjectPicker()" style="font-size:12px">&#9638; Browse Cld projects</button>
          <div class="preview-port-input">
            <input type="number" id="previewPortInput" placeholder="port" min="1" max="65535">
            <button class="ghost" id="previewConnectBtn">Connect</button>
          </div>
        </div>
        <div id="projectPicker" style="display:none"></div>
        <div id="previewStatus" style="display:none"></div>
        <iframe id="previewFrame" style="display:none"></iframe>
      </div>
    </div>

  </div><!-- end chat tab -->

<!-- file viewer overlay -->
<div id="fileOverlay" onclick="if(event.target===this)closeFile()">
  <div class="file-modal">
    <div class="file-modal-head">
      <span class="fpath" id="filePath"></span>
      <button class="ghost" style="font-size:11px;padding:2px 8px" onclick="copyFileContent()">copy</button>
      <button class="ghost" style="font-size:11px;padding:2px 8px" onclick="sendFileToAgent()">ask agent ↗</button>
      <button class="ghost" style="font-size:11px;padding:2px 8px" onclick="closeFile()">&#x2715;</button>
    </div>
    <div class="file-modal-body"><pre id="fileContent"></pre></div>
  </div>
</div>

  <!-- AUTOMATIONS TAB -->
  <div class="tab" id="tab-autos">
    <div class="tab-scroll">
      <h2>Automations</h2>
      <div class="card">
        <h3>New automation</h3>
        <div class="flex" style="margin-top:8px">
          <input type="text" id="aName" placeholder="name e.g. Notify on build" class="full" style="flex:1">
          <select id="aTrigger">
            <option value="manual">manual</option>
            <option value="webhook">webhook</option>
          </select>
        </div>
        <div id="actions"></div>
        <div class="row" style="margin-top:8px">
          <button class="ghost" id="addAction">+ action</button>
          <div class="spacer"></div>
          <button id="saveAuto">Save automation</button>
        </div>
        <div class="muted" style="margin-top:6px">Args are JSON. Use {{key}} to pull from webhook payload.</div>
      </div>
      <div id="autoList"></div>
    </div>
  </div>

  <!-- RUNS TAB -->
  <div class="tab" id="tab-runs">
    <div class="tab-scroll">
      <div class="row" style="margin-bottom:14px">
        <h2 style="margin:0">Runs</h2>
        <div class="spacer"></div>
        <button class="ghost" id="refreshRuns">refresh</button>
      </div>
      <div id="runList"></div>
    </div>
  </div>

  <!-- SKILLS TAB -->
  <div class="tab" id="tab-skills">
    <div class="tab-scroll">
      <div class="row" style="margin-bottom:14px">
        <h2 style="margin:0">Skills</h2>
        <div class="spacer"></div>
        <button class="ghost" id="reloadSkills">reload</button>
      </div>
      <div id="skillList"></div>
    </div>
  </div>

  <!-- QUICK COMMANDS TAB -->
  <div class="tab" id="tab-quick">
    <div class="tab-scroll">
      <h2>Quick Commands</h2>
      <div class="card">
        <h3>Save a command</h3>
        <div style="margin-top:8px;display:flex;flex-direction:column;gap:8px">
          <input type="text" id="qcLabel" placeholder="Short name (e.g. Git Status All)">
          <textarea id="qcPrompt" placeholder="The full prompt..." style="min-height:60px;resize:vertical;background:var(--bg3);border:1px solid var(--border2);border-radius:6px;color:var(--text);font:13px/1.5 inherit;padding:8px 10px;outline:none"></textarea>
          <label class="ctrl"><input type="checkbox" id="qcAgent"> use cockpit / agent mode</label>
          <button id="saveQc">Save command</button>
        </div>
      </div>
      <div id="qcList"></div>
    </div>
  </div>

  <!-- TASKS TAB -->
  <div class="tab" id="tab-tasks">
    <div class="tasks-toolbar">
      <h2>Tasks</h2>
      <button class="ghost" id="newTaskBtn" style="font-size:12px;padding:3px 10px">+ New task</button>
      <div style="width:1px;height:18px;background:var(--border);margin:0 4px"></div>
      <span style="font-size:11px;color:var(--dim)">filter:</span>
      <div id="projectFilter" style="display:flex;gap:4px;flex-wrap:wrap"></div>
      <div style="width:1px;height:18px;background:var(--border);margin:0 4px"></div>
      <div id="categoryFilter" style="display:flex;gap:4px;flex-wrap:wrap"></div>
      <div style="flex:1"></div>
      <button class="ghost" id="refreshTasksBtn" style="font-size:11px;padding:2px 8px">&#8635;</button>
    </div>
    <div class="tasks-board" id="tasksBoard">
      <div class="kb-col" id="col-todo">
        <div class="kb-col-head" style="color:#5080d0">TODO <span class="kb-count" id="cnt-todo">0</span></div>
        <div class="new-task-form" id="newTaskForm">
          <input id="ntTitle"    placeholder="Task title *" style="font-size:13px;padding:5px 8px">
          <div style="display:flex;gap:6px">
            <select id="ntProject"  style="flex:1"></select>
            <select id="ntCategory">
              <option value="dev">dev</option><option value="brand">brand</option>
              <option value="campaign">campaign</option><option value="content">content</option>
              <option value="crm">crm</option><option value="release">release</option>
              <option value="research">research</option>
            </select>
            <select id="ntPriority">
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="low">low</option>
            </select>
          </div>
          <textarea id="ntNotes" placeholder="Notes (optional)"></textarea>
          <div style="display:flex;gap:6px">
            <button class="ghost" onclick="submitNewTask()" style="flex:1">Create</button>
            <button class="ghost" onclick="closeNewTaskForm()">Cancel</button>
          </div>
        </div>
        <div class="kb-cards" id="cards-todo"></div>
      </div>
      <div class="kb-col" id="col-doing">
        <div class="kb-col-head" style="color:#d4a020">DOING <span class="kb-count" id="cnt-doing">0</span></div>
        <div class="kb-cards" id="cards-doing"></div>
      </div>
      <div class="kb-col" id="col-blocked">
        <div class="kb-col-head" style="color:#e05050">BLOCKED <span class="kb-count" id="cnt-blocked">0</span></div>
        <div class="kb-cards" id="cards-blocked"></div>
      </div>
      <div class="kb-col" id="col-done">
        <div class="kb-col-head" style="color:#58a058">DONE <span class="kb-count" id="cnt-done">0</span></div>
        <div class="kb-cards" id="cards-done"></div>
      </div>
    </div>
  </div><!-- end tasks tab -->

</div><!-- end main -->

<script>
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

let STATE = { skills: [], automations: [], runs: [], models: [], sessions: [], agent_model: '', agent_model_preferred: '', quickcmds: [] };
let SKILL_NAMES = [];
let pendingActions = {}; // action_id -> action data with status

// ---- tab navigation ----
$$('.nav').forEach(n => {
  n.onclick = () => {
    $$('.nav').forEach(x => x.classList.remove('on'));
    n.classList.add('on');
    $$('.tab').forEach(t => t.classList.remove('on'));
    $('#tab-' + n.dataset.tab).classList.add('on');
    if (n.dataset.tab === 'runs') loadState();
  };
});

async function loadState() {
  STATE = await (await fetch('/api/state')).json();
  SKILL_NAMES = STATE.skills.map(s => s.name);

  const m = $('#model');
  m.innerHTML = STATE.models.map(x => `<option>${x}</option>`).join('');

  const s = $('#session');
  const cur = s.value;
  s.innerHTML = STATE.sessions.map(x => `<option>${x}</option>`).join('');
  if (cur && STATE.sessions.includes(cur)) s.value = cur;

  if (!_chatInitialized) {
    _chatInitialized = true;
    chatRestore(s.value || 'default');
  }

  $('#foot').textContent = `local · :8765 · ${STATE.skills.length} skills`;

  // cockpit model indicator
  const preferred = STATE.agent_model === STATE.agent_model_preferred;
  $('#modelDot').className = 'dot ' + (preferred ? 'green' : 'amber');
  $('#modelName').textContent = STATE.agent_model || '—';

  renderAutos();
  renderRuns();
  renderSkills();
  loadQuickcmds();
}

// ---- cockpit toggle ----
$('#agentChk').onchange = () => {
  const on = $('#agentChk').checked;
  $('#cockpitCol').classList.toggle('visible', on);
};

// ---- utils ----
function esc(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmt(t) {
  const parts = [];
  const re = /```(\w*)\n([\s\S]*?)```/g;
  let last = 0, m;
  while ((m = re.exec(t)) !== null) {
    if (m.index > last) parts.push({k: 'text', v: t.slice(last, m.index)});
    parts.push({k: 'code', lang: (m[1] || '').toLowerCase(), v: m[2]});
    last = re.lastIndex;
  }
  if (last < t.length) parts.push({k: 'text', v: t.slice(last)});

  return parts.map(p => {
    if (p.k === 'text') {
      return esc(p.v)
        .replace(/\*\*(.+?)\*\*/g, (_, x) => `<strong>${x}</strong>`)
        .replace(/`([^`]+)`/g, (_, c) => `<code>${esc(c)}</code>`);
    }
    const lang = p.lang;
    if (lang === 'svg') {
      const safe = p.v.replace(/<script[\s\S]*?<\/script>/gi, '');
      const id = registerArtifact('svg', p.v);
      return `<div class="artifact-wrap">${artifactBar(id, 'svg')}<div class="svg-preview">${safe}</div></div>`;
    }
    if (lang === 'html') {
      const id = registerArtifact('html', p.v);
      const srcdoc = injectHarness(p.v).replace(/"/g, '&quot;');
      return `<div class="artifact-wrap">${artifactBar(id, 'html')}` +
             `<div class="html-preview"><iframe srcdoc="${srcdoc}" sandbox="allow-scripts allow-forms allow-modals"></iframe></div></div>`;
    }
    const header = lang ? `<div class="code-header">${esc(lang)}</div>` : '';
    return `${header}<pre>${esc(p.v)}</pre>`;
  }).join('');
}

// ---- artifact registry: lets buttons re-open generated code as a live preview ----
window._artifacts = window._artifacts || {};
let _artifactSeq = 0;

// surface JS errors as a visible overlay inside the inline iframe (not a black void)
const _PREVIEW_HARNESS = `<script>(function(){function report(m){var d=document.getElementById('__sidka_err__');if(!d){d=document.createElement('div');d.id='__sidka_err__';d.style.cssText='position:fixed;left:0;right:0;bottom:0;z-index:2147483647;background:#2a0d0d;color:#ffb4b4;font:12px/1.5 ui-monospace,Menlo,monospace;padding:10px 14px;border-top:2px solid #e05050;white-space:pre-wrap;max-height:45%;overflow:auto';(document.body||document.documentElement).appendChild(d);}d.textContent='\\u26a0 '+m;try{window.parent.postMessage({type:'sidka-preview-error',msg:m},'*');}catch(x){}}window.addEventListener('error',function(e){var l=e.filename?' ('+String(e.filename).split('/').pop()+':'+e.lineno+':'+e.colno+')':'';report((e.message||'Script error')+l);},true);window.addEventListener('unhandledrejection',function(e){report('Unhandled rejection: '+((e.reason&&e.reason.message)||e.reason));});})();<\/script>`;
function injectHarness(html) {
  if (html.indexOf('__sidka_err__') !== -1) return html;
  const m = html.match(/<head[^>]*>/i);
  if (m) { const i = m.index + m[0].length; return html.slice(0, i) + _PREVIEW_HARNESS + html.slice(i); }
  const h = html.match(/<html[^>]*>/i);
  if (h) { const i = h.index + h[0].length; return html.slice(0, i) + '<head>' + _PREVIEW_HARNESS + '</head>' + html.slice(i); }
  return _PREVIEW_HARNESS + html;
}
function registerArtifact(kind, code) {
  const id = 'art_' + (++_artifactSeq);
  window._artifacts[id] = { kind, code };
  return id;
}
function artifactBar(id, kind) {
  const label = kind === 'html' ? 'HTML' : 'SVG';
  return `<div class="artifact-bar">
    <span class="artifact-tag">${label}</span>
    <span style="flex:1"></span>
    <button class="artifact-btn" onclick="openLivePreview('${id}')" title="Run full-size in the preview pane">&#9654; Live preview</button>
    <button class="artifact-btn" onclick="popoutArtifact('${id}')" title="Open in a new browser window">&#8599; Pop out</button>
  </div>`;
}

async function saveArtifact(id) {
  const a = window._artifacts[id];
  if (!a) return null;
  // SVG: wrap in a minimal HTML doc so it serves as a full page
  const html = a.kind === 'html' ? a.code
    : `<!doctype html><meta charset="utf-8"><style>html,body{margin:0;height:100%;display:flex;align-items:center;justify-content:center;background:#0b0b0c}svg{max-width:100%;max-height:100%}</style>${a.code}`;
  const r = await fetch('/api/preview/save', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ html, session: ($('#session')?.value || 'scratch'), name: id })
  });
  return r.ok ? (await r.json()) : null;
}

async function openLivePreview(id) {
  const res = await saveArtifact(id);
  if (!res) return;
  const col = $('#previewCol');
  if (!col.classList.contains('visible')) {
    col.classList.add('visible');
    $('#previewBtn').style.color = 'var(--acc)';
  }
  connectPreview(res.url, 'live');
}

async function popoutArtifact(id) {
  const res = await saveArtifact(id);
  if (res) window.open(res.url, '_blank');
}

function addMsg(who, cls) {
  const d = document.createElement('div');
  d.className = 'msg ' + (cls || '');
  d.innerHTML = `<div class="who">${esc(who)}</div><div class="bubble pre"></div>`;
  $('#msgs').appendChild(d);
  scrollMsgs();
  return d.querySelector('.bubble');
}

function scrollMsgs() {
  const m = $('#msgs');
  m.scrollTop = m.scrollHeight;
}

// ---- chat persistence ----
let _chatInitialized = false;

function chatKey() {
  return 'runai_chat_' + (($('#session') && $('#session').value) || 'default');
}

function chatLog(bubble) {
  if (!bubble) return;
  const msgDiv = bubble.parentElement;
  if (!msgDiv) return;
  const who      = msgDiv.querySelector('.who')?.textContent || '';
  const msgCls   = [...msgDiv.classList].filter(c => c !== 'msg').join(' ');
  const bubbleCls = [...bubble.classList].filter(c => c !== 'bubble').join(' ');
  const html     = bubble.innerHTML || '';
  if (!html.trim()) return;
  try {
    const key = chatKey();
    const saved = JSON.parse(localStorage.getItem(key) || '[]');
    saved.push({who, msgCls, bubbleCls, html, ts: Date.now()});
    if (saved.length > 300) saved.splice(0, saved.length - 300);
    localStorage.setItem(key, JSON.stringify(saved));
  } catch(e) {}
}

function chatRestore(session) {
  const key = 'runai_chat_' + (session || 'default');
  $('#msgs').innerHTML = '';
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '[]');
    for (const m of saved) {
      const d = document.createElement('div');
      d.className = 'msg ' + (m.msgCls || '');
      d.innerHTML = `<div class="who">${esc(m.who)}</div><div class="bubble ${m.bubbleCls || ''}">${m.html}</div>`;
      $('#msgs').appendChild(d);
    }
    if (saved.length) {
      scrollMsgs();
      const ts = saved[0]?.ts;
      if (ts) {
        const d = new Date(ts);
        const label = document.createElement('div');
        label.style.cssText = 'text-align:center;font-size:10px;color:#444;padding:8px 0 4px;letter-spacing:0.5px';
        label.textContent = 'history from ' + d.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'});
        $('#msgs').insertBefore(label, $('#msgs').firstChild);
      }
    }
  } catch(e) {}
}

function chatClear(session) {
  try { localStorage.removeItem('runai_chat_' + (session || 'default')); } catch(e) {}
  $('#msgs').innerHTML = '';
}

// ---- activity log ----
let activityItems = [];
function pushActivity(html) {
  activityItems.push(html);
  if (activityItems.length > 20) activityItems.shift();
  const log = $('#activityLog');
  log.innerHTML = activityItems.map(x => `<div class="log-item">${x}</div>`).join('');
  log.scrollTop = log.scrollHeight;
}

// ---- diff rendering ----
function renderDiff(diff) {
  if (!diff) return '';
  const lines = diff.split('\n');
  return lines.map(line => {
    if (line.startsWith('+')) return `<span class="diff-add">${esc(line)}</span>`;
    if (line.startsWith('-')) return `<span class="diff-del">${esc(line)}</span>`;
    if (line.startsWith('@')) return `<span class="diff-hunk">${esc(line)}</span>`;
    return `<span class="diff-ctx">${esc(line)}</span>`;
  }).join('\n');
}

// ---- pending approvals ----
function renderPending() {
  const cards = $('#pendingCards');
  const section = $('#pendingSection');
  const items = Object.values(pendingActions);
  section.style.display = items.length ? '' : 'none';

  // Approve All button — only show when 2+ actions are unresolved
  const unresolved = items.filter(a => a.status === undefined);
  const approveAllBtn = unresolved.length > 1
    ? `<button class="ok-btn" style="width:100%;margin-bottom:10px;font-size:12px"
         onclick="approveAll()">&#10003; APPROVE ALL (${unresolved.length})</button>`
    : '';

  const cardHtml = items.map(a => {
    const resolved = a.status !== undefined;
    const diffHtml = a.diff ? `<div class="diff-wrap">${renderDiff(a.diff)}</div>` : '';
    const btns = resolved
      ? `<div class="action-resolved ${a.status === 'approved' ? 'ok' : 'err'}">${a.status === 'approved' ? '&#10003; Approved' : '&#10007; Denied'}</div>`
      : `<div class="action-btns">
           <button class="ok-btn"  onclick="resolveAction('${a.id}','approve')">APPROVE</button>
           <button class="err-btn" onclick="resolveAction('${a.id}','deny')">DENY</button>
         </div>`;
    return `<div class="action-card" id="acard-${a.id}">
      <div class="action-card-top">
        <span class="badge ${esc(a.badge)}">${esc(a.badge)}</span>
        <span class="action-label">${esc(a.label)}</span>
      </div>
      ${diffHtml}
      ${btns}
    </div>`;
  }).join('');

  cards.innerHTML = approveAllBtn + cardHtml;
}

async function resolveAction(id, verdict) {
  await fetch(`/api/action/${id}/${verdict}`, { method: 'POST' });
  // Optimistically mark resolved in UI immediately; SSE will confirm
  if (pendingActions[id]) {
    pendingActions[id].status = verdict === 'approve' ? 'approved' : 'denied';
    renderPending();
  }
}

async function approveAll() {
  const ids = Object.keys(pendingActions).filter(id => pendingActions[id].status === undefined);
  for (const id of ids) {
    if (pendingActions[id] && pendingActions[id].status === undefined) {
      await resolveAction(id, 'approve');
      await new Promise(r => setTimeout(r, 150)); // let agent unblock between actions
    }
  }
}

// ---- cockpit send ----
async function sendCockpit(txt, bubble) {
  const session = $('#session').value;
  bubble.parentElement.querySelector('.who').textContent = 'agent · connecting...';
  bubble.textContent = 'Routing to agent model...';

  // reset activity for this turn
  activityItems = [];
  $('#activityLog').innerHTML = '';
  pendingActions = {};
  renderPending();

  let currentReader = null;
  try {
    const resp = await fetch('/api/agent/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session, message: txt }),
    });

    const reader = resp.body.getReader();
    currentReader = reader;
    const dec = new TextDecoder();
    let buf = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });

      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (!line.startsWith('data: ')) continue;
        let e;
        try { e = JSON.parse(line.slice(6)); } catch { continue; }

        if (e.type === 'meta') {
          bubble.parentElement.querySelector('.who').textContent = `agent · ${e.model || ''}`;
          bubble.textContent = 'Thinking...';
        } else if (e.type === 'thinking') {
          const preview = e.text.slice(0, 80) + (e.text.length > 80 ? '...' : '');
          bubble.textContent = `Thinking: ${preview}`;
        } else if (e.type === 'tool_call') {
          const argsStr = JSON.stringify(e.args || {}).slice(0, 120);
          pushActivity(`<span class="log-fn">${esc(e.fn)}</span> ${esc(argsStr)}`);
          bubble.textContent = `Calling ${e.fn}...`;
        } else if (e.type === 'action_pending') {
          const a = e.action;
          pendingActions[a.id] = a;
          renderPending();
          bubble.textContent = '⏳ Waiting for your approval...';
        } else if (e.type === 'action_resolved') {
          if (pendingActions[e.id]) {
            pendingActions[e.id].status = e.approved ? 'approved' : 'denied';
            renderPending();
          }
          pushActivity(`<span class="log-fn">${e.approved ? '✓' : '✗'} ${e.id}</span> <span class="log-out">${e.approved ? 'approved' : 'denied'}</span>`);
        } else if (e.type === 'tool_result') {
          const out = (e.output || '').slice(0, 100);
          pushActivity(`<span class="log-out">→ ${esc(out)}</span>`);
        } else if (e.type === 'done') {
          const tools = (e.tools || []).map(t => t.skill || t.fn).filter(Boolean);
          const who = 'agent' + (tools.length ? ' · ' + tools.slice(0, 4).join(', ') : '');
          bubble.parentElement.querySelector('.who').textContent = who;
          bubble.innerHTML = fmt(e.reply || '(done)');
          if (tools.length) {
            const tags = document.createElement('div');
            tags.className = 'tool-tags';
            tools.forEach(t => {
              const span = document.createElement('span');
              span.className = 'tool-tag';
              span.textContent = t;
              tags.appendChild(span);
            });
            bubble.appendChild(tags);
          }
          scrollMsgs();
          chatLog(bubble);
          loadState();
          _lastAgentReply = e.reply || '';
          _showReviewBtn();
        } else if (e.type === 'error') {
          bubble.innerHTML = `<span style="color:var(--err)">${esc(e.text)}</span>`;
        }
      }
    }
  } catch (err) {
    bubble.innerHTML = `<span style="color:var(--err)">Stream error: ${esc(String(err))}</span>`;
  }
}

// ---- plain chat send ----
async function sendChat(txt, bubble) {
  const session = $('#session').value;
  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session, message: txt, model: $('#model').value }),
  });

  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '', acc = '';
  const who = bubble.parentElement.querySelector('.who');

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (!line.startsWith('data: ')) continue;
      let e;
      try { e = JSON.parse(line.slice(6)); } catch { continue; }
      if (e.type === 'meta') {
        who.textContent = `ai · ${e.category} · ${e.model}` + (e.notes ? ` · +${e.notes} notes` : '');
      } else if (e.type === 'token') {
        acc += e.text;
        bubble.innerHTML = fmt(acc);
        scrollMsgs();
      } else if (e.type === 'error') {
        bubble.innerHTML = `<span style="color:var(--err)">${esc(e.text)}</span>`;
      }
    }
  }
  chatLog(bubble);
}

// ---- send dispatcher ----
// ---- slash command router ----
// Fast commands — call /api/cmd/<name> directly, no LLM, result in 2-3s
const FAST_CMDS = new Set(['/status', '/journal', '/today', '/index', '/standup', '/tasks']);

// Agent commands — still go through the cockpit but with a pre-written prompt
const SLASH_CMDS = {
  '/project-state': { prompt: (arg) => 'Use project_status to get git status across all projects. Then write a journal entry (journal skill, action="write") summarizing the current state. Give me a brief.' },
  '/workon':        { prompt: (arg) => `I am switching to work on ${arg}. Use the git skill on ~/Desktop/Cld/${arg} to: (1) check status (2) show last 7 commits. Brief me on the current branch, recent changes, and active work area.` },
  '/design':        { prompt: (arg) => `Use the design skill (action="spec", description="${arg || 'UI mockup'}") to get the design spec. Then generate a self-contained SVG or HTML mockup for: ${arg || 'a UI component'}. Output the graphic in a fenced code block tagged \`\`\`svg or \`\`\`html. No explanations before or after — just the code block.` },
  '/diagram':       { prompt: (arg) => `Generate a clear SVG diagram for: ${arg || 'system architecture'}. Use a dark background (#1a1a1a), white/gray text, colored boxes for different component types. Output ONLY a fenced \`\`\`svg code block. No prose.` },
  '/chart':         { prompt: (arg) => `Generate an SVG chart for: ${arg || 'a bar chart'}. Dark background, clean axes, labeled data. Output ONLY a fenced \`\`\`svg code block. Make the SVG self-contained with embedded data.` },
  '/task':          { prompt: (arg) => `Create a task using the tasks skill. Parse this request: "${arg || 'new task'}". Infer the project, category (dev/brand/campaign/content/crm/release/research), and priority from context. Call tasks(action="create", ...) then confirm with the task ID and a one-line summary.` },
};

async function runFastCmd(cmd, arg) {
  const name = cmd.slice(1);
  const userBubble = addMsg('you', 'me');
  userBubble.textContent = cmd + (arg ? ' ' + arg : '');
  chatLog(userBubble);
  const bubble = addMsg('ai', '');
  bubble.textContent = 'Running ' + cmd + '...';
  try {
    const r = await fetch('/api/cmd/' + name, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(arg ? {arg} : {}),
    });
    const d = await r.json();
    const text = d.result || d.error || '(no result)';
    renderFastResult(bubble, cmd, text);
    bubble.parentElement.querySelector('.who').textContent = cmd;
    chatLog(bubble);
  } catch(e) {
    bubble.textContent = 'Error: ' + e.message;
    chatLog(bubble);
  }
}

function renderFastResult(bubble, cmd, text) {
  if (cmd === '/status') { renderStatusResult(bubble, text); return; }
  if (cmd === '/journal' || cmd === '/today' || cmd === '/standup') { renderMarkdownResult(bubble, text); return; }
  if (cmd === '/tasks') { renderTasksSummary(bubble, text); return; }
  bubble.textContent = text;
}

function renderTasksSummary(bubble, text) {
  bubble.classList.remove('pre');
  const lines = text.split('\n');
  let html = '<div style="font-size:13px;line-height:1.7">';
  for (const line of lines) {
    if (!line.trim()) { html += '<div style="height:6px"></div>'; continue; }
    if (line.startsWith('===')) {
      html += `<div style="font-size:14px;font-weight:700;color:var(--acc);margin-bottom:6px">${esc(line.replace(/=/g,'').trim())}</div>`;
    } else if (line.startsWith('By status:') || line.startsWith('By priority:') || line.startsWith('By category:') || line.startsWith('Top projects:')) {
      const [label, rest] = line.split(':');
      const badges = (rest||'').trim().split(/\s{2,}/).filter(Boolean);
      html += `<div style="margin:2px 0;display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <span style="font-size:10px;color:var(--dim);width:80px">${esc(label)}</span>`;
      for (const b of badges) {
        const [key, val] = b.split(':');
        html += `<span style="background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:1px 8px;font-size:11px">
          <span style="color:var(--dim)">${esc(key)}</span> <span style="color:var(--text);font-weight:600">${esc(val||'')}</span></span>`;
      }
      html += '</div>';
    } else if (line.startsWith('BLOCKED') || line.startsWith('HIGH PRIORITY')) {
      html += `<div style="font-size:11px;font-weight:700;letter-spacing:0.5px;color:${line.startsWith('BLOCKED')?'#e05050':'#d4a020'};margin-top:6px">${esc(line)}</div>`;
    } else if (/^\s{2}t_/.test(line)) {
      const m = line.trim().match(/^(t_\w+)\s+(.+?)\s+\[(.+?)\]$/);
      if (m) {
        html += `<div style="font-size:12px;color:var(--mid);padding:2px 0 2px 12px">
          <span style="font-family:monospace;color:var(--dim);font-size:10px">${esc(m[1])}</span>
          <span style="margin:0 8px">${esc(m[2])}</span>
          <span style="font-size:10px;color:var(--dim)">[${esc(m[3])}]</span></div>`;
      } else {
        html += `<div style="font-size:12px;color:var(--mid);padding:2px 0 2px 12px">${esc(line.trim())}</div>`;
      }
    } else {
      html += `<div style="color:var(--mid)">${esc(line)}</div>`;
    }
  }
  html += `<div style="margin-top:10px">
    <button class="ghost" style="font-size:11px;padding:3px 10px" onclick="document.querySelector('.nav[data-tab=tasks]').click()">Open Kanban board ↗</button>
  </div>`;
  html += '</div>';
  bubble.innerHTML = html;
}

function renderStatusResult(bubble, text) {
  bubble.classList.remove('pre');
  const lines = text.split('\n');
  let html = '';
  let mode = '';

  const row = (name, detail, badge, badgeColor) => {
    const bc = badgeColor === 'red' ? '#5a1a1a' : badgeColor === 'amber' ? '#4a3800' : '#1a3a5a';
    const tc = badgeColor === 'red' ? '#f08080' : badgeColor === 'amber' ? '#e0c040' : '#80b0f0';
    return `<div style="display:flex;align-items:baseline;gap:8px;padding:4px 0;border-bottom:1px solid #1c1c1c">
      <span style="font-family:monospace;font-size:12px;color:#ddd;min-width:160px;flex-shrink:0">${esc(name)}</span>
      ${badge ? `<span style="font-size:10px;padding:1px 7px;border-radius:10px;background:${bc};color:${tc};flex-shrink:0">${esc(badge)}</span>` : ''}
      <span style="font-size:11px;color:#888;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(detail)}</span>
    </div>`;
  };

  const section = (label, color) => {
    const c = color === 'red' ? '#a04040' : color === 'amber' ? '#a08020' : color === 'green' ? '#40803a' : color === 'blue' ? '#3060a0' : '#666';
    return `<div style="font-size:10px;letter-spacing:1.2px;font-weight:600;color:${c};margin:12px 0 4px;text-transform:uppercase">${label}</div>`;
  };

  for (const line of lines) {
    if (line.startsWith('Checked ')) {
      const m = line.match(/(\d+) repos.*?(\d+) dirty.*?(\d+) clean.*?(\d+) ahead.*?(\d+) behind/);
      if (m) {
        const chip = (n, label, c) => `<span style="font-size:11px;padding:2px 9px;border-radius:12px;background:#1a1a1a;border:1px solid #333;color:${c}">${n} ${label}</span>`;
        html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          ${chip(m[1], 'repos', '#888')}
          ${m[2]!='0' ? chip(m[2], 'dirty', '#d4a020') : ''}
          ${m[3]!='0' ? chip(m[3], 'clean', '#58a058') : ''}
          ${m[4]!='0' ? chip(m[4], 'ahead ⚠', '#e05050') : ''}
          ${m[5]!='0' ? chip(m[5], 'behind', '#5080d0') : ''}
        </div>`;
      }
      mode = '';
      continue;
    }
    if (line === 'PUSH NEEDED:')   { mode = 'push';   html += section('Push needed', 'red');   continue; }
    if (line === 'BEHIND REMOTE:') { mode = 'behind'; html += section('Behind remote', 'blue'); continue; }
    if (line === 'DIRTY:')         { mode = 'dirty';  html += section('Dirty', 'amber');        continue; }
    if (line.startsWith('CLEAN:')) {
      mode = 'clean';
      html += section('Clean', 'green');
      const names = line.replace('CLEAN:', '').trim().split(/\s{2,}/);
      html += '<div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px">';
      for (const n of names) if (n.trim()) html += `<span style="font-size:11px;padding:1px 8px;border-radius:10px;background:#182818;color:#5a9a5a">${esc(n.trim())}</span>`;
      html += '</div>';
      continue;
    }
    if (line.startsWith('ERRORS:')) {
      html += section('Errors', 'red') + `<div style="font-size:12px;color:#e07070">${esc(line.replace('ERRORS:','').trim())}</div>`;
      continue;
    }

    if (mode === 'push' && line.match(/^  \S/)) {
      const m = line.trim().match(/^(\S+)\s+\((\d+) commits ahead\)\s+—\s+(.*)$/);
      if (m) html += row(m[1], m[3].trim(), m[2]+' ahead', 'red');
      continue;
    }
    if (mode === 'behind' && line.match(/^  \S/)) {
      const m = line.trim().match(/^(\S+)\s+\((\d+) behind\)\s+—\s+(.*)$/);
      if (m) html += row(m[1], m[3].trim(), m[2]+' behind', 'blue');
      continue;
    }
    if (mode === 'dirty' && line.match(/^  \S/)) {
      const m = line.trim().match(/^(\S+)\s{2,}(.+)$/);
      if (m) { html += row(m[1], m[2].trim(), '', 'amber'); }
      continue;
    }
    if (mode === 'dirty' && line.match(/^\s{30}/)) {
      const last = line.replace(/^\s+last:\s*/, '');
      html += `<div style="font-size:11px;color:#555;padding:0 0 3px 170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(last)}</div>`;
      continue;
    }
    if (line.trim() && mode !== 'clean') {
      html += `<div style="font-size:11px;color:#555;margin:2px 0">${esc(line)}</div>`;
    }
  }
  bubble.innerHTML = html || `<span style="color:#888">${esc(text)}</span>`;
}

function renderMarkdownResult(bubble, text) {
  bubble.classList.remove('pre');
  let html = '';
  for (const line of text.split('\n')) {
    if (line.startsWith('## ')) {
      html += `<div style="font-weight:600;font-size:13px;margin:10px 0 3px;color:#bbb">${esc(line.slice(3))}</div>`;
    } else if (line.startsWith('# ')) {
      html += `<div style="font-weight:600;font-size:14px;margin:10px 0 4px;color:#ddd">${esc(line.slice(2))}</div>`;
    } else if (line.startsWith('- ') || line.startsWith('* ')) {
      html += `<div style="padding:1px 0 1px 12px;color:#ccc;font-size:13px">&bull; ${esc(line.slice(2))}</div>`;
    } else if (line.trim() === '') {
      html += '<div style="height:6px"></div>';
    } else {
      html += `<div style="font-size:13px;color:#ccc;line-height:1.6">${esc(line)}</div>`;
    }
  }
  bubble.innerHTML = html || `<span style="color:#888">${esc(text)}</span>`;
}

async function handleSlash(txt) {
  const parts = txt.trim().split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const arg = parts.slice(1).join(' ');

  // Fast path — no LLM needed
  if (FAST_CMDS.has(cmd)) {
    await runFastCmd(cmd, arg);
    return true;
  }

  // Agent path — cockpit with pre-written prompt
  const def = SLASH_CMDS[cmd];
  if (!def) return false;

  if (cmd === '/workon' && arg) {
    const sel = $('#session');
    const opts = Array.from(sel.options).map(o => o.value);
    if (opts.includes(arg)) sel.value = arg;
  }

  const userBubble2 = addMsg('you', 'me');
  userBubble2.textContent = txt;
  chatLog(userBubble2);
  $('#agentChk').checked = true;
  $('#cockpitCol').classList.add('visible');
  const bubble = addMsg('agent · ...', 'agent');
  await sendCockpit(def.prompt(arg), bubble);
  return true;
}

async function send() {
  const txt = $('#input').value.trim();
  if (!txt) return;
  $('#input').value = '';

  // Intercept slash commands before normal send
  if (txt.startsWith('/')) {
    const handled = await handleSlash(txt);
    if (handled) return;
  }

  const userBubble3 = addMsg('you', 'me');
  userBubble3.textContent = txt;
  chatLog(userBubble3);

  if ($('#agentChk').checked) {
    const bubble = addMsg('agent · ...', 'agent');
    await sendCockpit(txt, bubble);
  } else {
    const bubble = addMsg('ai', '');
    await sendChat(txt, bubble);
  }
}

$('#send').onclick = send;
$('#input').onkeydown = e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
};
$('#clearBtn').onclick = async () => {
  const session = $('#session').value;
  await fetch('/api/session/' + session + '/clear', { method: 'POST' });
  chatClear(session);
  activityItems = [];
  $('#activityLog').innerHTML = '<div class="muted" style="padding:6px 4px">Agent output will appear here...</div>';
  pendingActions = {};
  renderPending();
};

$('#session').onchange = function () {
  chatRestore(this.value);
};

// auto-grow textarea + slash autocomplete
const SLASH_HINTS = [
  { cmd: '/status',       desc: 'Git status across all projects' },
  { cmd: '/standup',      desc: 'Generate a daily developer standup' },
  { cmd: '/journal',      desc: 'Read work journal (last 7 days)' },
  { cmd: '/project-state',desc: 'Snapshot all project states + write journal entry' },
  { cmd: '/workon',       desc: 'Switch to a project  e.g. /workon nova-daw' },
  { cmd: '/index',        desc: 'Re-embed ~/Desktop/Cld into the RAG index' },
  { cmd: '/design',       desc: 'Generate a UI mockup SVG  e.g. /design timeline editor' },
  { cmd: '/diagram',      desc: 'Generate an architecture or flow diagram SVG' },
  { cmd: '/chart',        desc: 'Generate a chart SVG  e.g. /chart project activity' },
  { cmd: '/tasks',        desc: 'Show a summary of all active tasks' },
  { cmd: '/task',         desc: 'Create a task  e.g. /task fix login bug on nova-daw' },
];

$('#input').oninput = function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 140) + 'px';

  const val = this.value;
  const menu = $('#slashMenu');
  if (val.startsWith('/') && !val.includes(' ')) {
    const q = val.toLowerCase();
    const hits = SLASH_HINTS.filter(h => h.cmd.startsWith(q));
    if (hits.length) {
      menu.style.display = 'block';
      menu.innerHTML = hits.map(h =>
        `<div class="slash-item" onclick="insertSlash('${h.cmd}')"
              style="padding:8px 14px;cursor:pointer;display:flex;gap:10px;align-items:center">
           <span style="font-family:monospace;color:var(--acc);font-size:12px">${h.cmd}</span>
           <span style="color:var(--dim);font-size:12px">${h.desc}</span>
         </div>`
      ).join('');
      return;
    }
  }
  menu.style.display = 'none';
};

function insertSlash(cmd) {
  $('#input').value = cmd + ' ';
  $('#slashMenu').style.display = 'none';
  $('#input').focus();
}

document.addEventListener('click', e => {
  if (!e.target.closest('.composer')) $('#slashMenu').style.display = 'none';
});

// ---- export button ----
$('#exportBtn').onclick = () => {
  const name = $('#session').value;
  const a = document.createElement('a');
  a.href = '/api/session/' + encodeURIComponent(name) + '/export';
  a.download = name + '-session.md';
  a.click();
};

// ---- quick commands ----
async function loadQuickcmds() {
  const cmds = await (await fetch('/api/quickcmds')).json();

  // Render tab list
  $('#qcList').innerHTML = cmds.map(c => `
    <div class="card" style="display:flex;align-items:flex-start;gap:10px">
      <div style="flex:1">
        <div style="font-weight:600;margin-bottom:2px">${esc(c.label)}</div>
        <div class="muted">${esc(c.prompt.slice(0,100))}${c.prompt.length > 100 ? '...' : ''}</div>
        ${c.agent ? '<span class="pill" style="margin-top:4px">cockpit</span>' : ''}
      </div>
      <button onclick="fireQc(${JSON.stringify(c).replace(/"/g, '&quot;')})" style="flex-shrink:0">Run</button>
      <button class="ghost" onclick="delQc('${c.id}')" style="flex-shrink:0">del</button>
    </div>`).join('') || '<div class="muted">No quick commands yet.</div>';

  // Render chips in chat tab (up to 6)
  $('#qcChips').innerHTML = cmds.slice(0, 6).map(c =>
    `<button class="ghost" onclick="fireQc(${JSON.stringify(c).replace(/"/g, '&quot;')})" style="font-size:11px;padding:4px 10px;border-radius:20px">${esc(c.label)}</button>`
  ).join('');
}

function fireQc(cmd) {
  // Switch to chat tab
  $$('.nav').forEach(n => n.classList.remove('on'));
  $$('.tab').forEach(t => t.classList.remove('on'));
  document.querySelector('[data-tab="chat"]').classList.add('on');
  $('#tab-chat').classList.add('on');
  // Set agent mode
  $('#agentChk').checked = cmd.agent;
  $('#cockpitCol').classList.toggle('visible', cmd.agent);
  // Fire
  $('#input').value = cmd.prompt;
  send();
}

async function delQc(id) {
  if (confirm('Delete this quick command?')) {
    await fetch('/api/quickcmds/' + id, { method: 'DELETE' });
    loadQuickcmds();
  }
}

$('#saveQc').onclick = async () => {
  const label = $('#qcLabel').value.trim();
  const prompt = $('#qcPrompt').value.trim();
  if (!label || !prompt) return alert('Label and prompt are required.');
  await fetch('/api/quickcmds', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, prompt, agent: $('#qcAgent').checked }),
  });
  $('#qcLabel').value = ''; $('#qcPrompt').value = '';
  loadQuickcmds();
};

// ---- automations ----
function actionRow() {
  const d = document.createElement('div');
  d.className = 'action-row';
  d.innerHTML = `<select>${SKILL_NAMES.map(s => `<option>${esc(s)}</option>`).join('')}</select>` +
    `<textarea placeholder='{"key":"value"}'></textarea>` +
    `<button class="ghost" onclick="this.parentElement.remove()" style="flex-shrink:0">&times;</button>`;
  $('#actions').appendChild(d);
}
$('#addAction').onclick = actionRow;

$('#saveAuto').onclick = async () => {
  const actions = [];
  let bad = false;
  $$('#actions .action-row').forEach(r => {
    const skill = r.querySelector('select').value;
    const raw = r.querySelector('textarea').value.trim() || '{}';
    try { actions.push({ skill, args: JSON.parse(raw) }); }
    catch (e) { bad = true; r.querySelector('textarea').style.borderColor = 'var(--err)'; }
  });
  if (bad) return alert('One of the args fields is not valid JSON.');
  if (!actions.length) return alert('Add at least one action.');
  await fetch('/api/automations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: $('#aName').value || 'Untitled', trigger: $('#aTrigger').value, actions }),
  });
  $('#aName').value = '';
  $('#actions').innerHTML = '';
  loadState();
};

function renderAutos() {
  $('#autoList').innerHTML = STATE.automations.map(a => {
    const hookUrl = location.origin + '/hook/' + a.webhook_id;
    return `<div class="card">
      <div class="flex">
        <h3 style="margin:0">${esc(a.name)}</h3>
        <span class="pill">${esc(a.trigger)}</span>
        <div class="spacer"></div>
        <button class="ghost" onclick="runAuto('${a.id}')">Run now</button>
        <button class="ghost" onclick="delAuto('${a.id}')">delete</button>
      </div>
      <div class="muted" style="margin-top:5px">${a.actions.map(x => esc(x.skill)).join(' → ')}</div>
      ${a.trigger === 'webhook' ? `<div class="sep"></div><div class="muted" style="margin-bottom:4px">Webhook URL:</div><div class="url-box">${esc(hookUrl)}</div>` : ''}
    </div>`;
  }).join('') || '<div class="muted">No automations yet.</div>';
}

async function runAuto(id) {
  const r = await (await fetch('/api/automations/' + id + '/run', { method: 'POST' })).json();
  alert('Status: ' + r.status + '\n\n' + r.steps.map(s => s.skill + ':\n' + s.output).join('\n\n'));
  loadState();
}

async function delAuto(id) {
  if (confirm('Delete this automation?')) {
    await fetch('/api/automations/' + id, { method: 'DELETE' });
    loadState();
  }
}

// ---- runs ----
$('#refreshRuns').onclick = loadState;

function renderRuns() {
  $('#runList').innerHTML = STATE.runs.map(r => `
    <div class="card">
      <div class="flex">
        <b>${esc(r.name)}</b>
        <span class="pill ${r.status === 'ok' ? 'ok' : 'err'}">${esc(r.status)}</span>
        <span class="pill">${esc(r.source)}</span>
        <div class="spacer"></div>
        <span class="muted">${esc(r.ts)}</span>
      </div>
      <div class="steps">
        ${r.steps.map(s => `<div class="step"><span class="kv">${esc(s.skill)}</span> ${esc(JSON.stringify(s.args))}<br>&rarr; ${esc(s.output)}</div>`).join('')}
      </div>
    </div>`).join('') || '<div class="muted">No runs yet. Trigger an automation to see it here.</div>';
}

// ---- skills ----
$('#reloadSkills').onclick = async () => {
  const btn = $('#reloadSkills');
  btn.textContent = 'Reloading...';
  btn.disabled = true;
  const r = await fetch('/api/reload', { method: 'POST' });
  const d = await r.json();
  btn.textContent = 'Reload skills';
  btn.disabled = false;
  await loadState();
  const errCount = Object.keys(d.errors || {}).length;
  const msg = errCount
    ? `Loaded ${d.skills.length} skills — ${errCount} errors (see Skills tab)`
    : `Loaded ${d.skills.length} skills — all OK`;
  alert(msg);
};

function renderSkills() {
  const errors = STATE.skill_errors || {};
  const errHtml = Object.entries(errors).map(([name, err]) => `
    <div class="card" style="border-color:var(--err);background:#1a0404">
      <h3 style="color:var(--err)">${esc(name)} <span style="font-size:10px;font-weight:400">LOAD ERROR</span></h3>
      <div class="muted" style="font-family:monospace;font-size:11px;color:#f87171">${esc(err)}</div>
    </div>`).join('');
  const skillHtml = STATE.skills.map(s => `
    <div class="card">
      <h3>${esc(s.name)}</h3>
      <div class="muted">${esc(s.description)}</div>
    </div>`).join('') || '<div class="muted">No skills found in ~/.runai/skills</div>';
  $('#skillList').innerHTML = errHtml + skillHtml;
  // Live footer count
  const errCount = Object.keys(errors).length;
  const errBadge = errCount ? ` · <span style="color:var(--err)">${errCount} error${errCount>1?'s':''}</span>` : '';
  $('#foot').innerHTML = `local &middot; :8765 &middot; ${STATE.skills.length} skills${errBadge}`;
}

// ---- file tree ----

const EXT_ICON = {
  js:'JS', ts:'TS', tsx:'TSX', jsx:'JSX', py:'PY', json:'{}', md:'MD',
  css:'CSS', scss:'CSS', html:'HTML', svg:'SVG', sh:'SH', toml:'CFG',
  yaml:'CFG', yml:'CFG', env:'ENV', txt:'TXT', png:'IMG', jpg:'IMG',
  gif:'IMG', webp:'IMG', mp4:'VID', mp3:'AUD', woff:'FNT', woff2:'FNT',
};

function ftIcon(node) {
  if (node.type === 'dir') return '▸';
  return EXT_ICON[node.ext] || '·';
}

function ftColorClass(node) {
  if (node.type === 'dir') return 'color:var(--dim)';
  const c = {
    ts:'#4a9eff', tsx:'#4a9eff', js:'#f0c040', jsx:'#f0c040',
    py:'#58a0d8', json:'#8a8', css:'#d84090', scss:'#d84090',
    html:'#e07040', md:'#a0c0f0', svg:'#c070e0', sh:'#6fcf6f',
    env:'#e06060',
  };
  return `color:${c[node.ext] || '#777'}`;
}

function buildFtNode(node, depth) {
  const indent = depth * 14;
  if (node.type === 'dir') {
    const childrenHtml = (node.children || []).map(c => buildFtNode(c, depth + 1)).join('');
    return `<div class="ft-node">
      <div class="ft-row" style="padding-left:${8 + indent}px" onclick="toggleDir(this)">
        <span class="ft-icon" style="color:var(--dim)">▸</span>
        <span class="ft-name" style="color:var(--mid)">${esc(node.name)}/</span>
      </div>
      <div class="ft-children">${childrenHtml}</div>
    </div>`;
  } else {
    const icon = EXT_ICON[node.ext] || '·';
    const col = ftColorClass(node);
    const kb = node.size > 1024 ? (node.size/1024).toFixed(0)+'k' : node.size+'b';
    return `<div class="ft-row ft-file" style="padding-left:${8 + indent}px;${col}"
      title="${esc(node.path)}"
      onclick="openFile('${esc(node.path).replace(/'/g,"\\'")}')">
      <span class="ft-icon" style="font-size:9px;${col}">${icon}</span>
      <span class="ft-name">${esc(node.name)}</span>
      <span style="margin-left:auto;font-size:10px;color:#444">${kb}</span>
    </div>`;
  }
}

function toggleDir(el) {
  const children = el.nextElementSibling;
  const arrow = el.querySelector('.ft-icon');
  if (!children) return;
  const open = children.classList.toggle('open');
  if (arrow) arrow.textContent = open ? '▾' : '▸';
  el.classList.toggle('ft-open', open);
}

let _ftSession = '';
async function loadFileTree(session) {
  if (!session || session === _ftSession) return;
  _ftSession = session;
  $('#ftProject').textContent = session;
  $('#ftBody').innerHTML = '<div style="padding:16px 12px;color:var(--dim);font-size:12px">Loading...</div>';
  try {
    const r = await fetch('/api/filetree?session=' + encodeURIComponent(session));
    const tree = await r.json();
    if (tree.error) {
      $('#ftBody').innerHTML = `<div style="padding:16px 12px;color:var(--dim);font-size:12px">${esc(tree.error)}</div>`;
      return;
    }
    const html = (tree.children || []).map(c => buildFtNode(c, 0)).join('');
    $('#ftBody').innerHTML = html || '<div style="padding:16px;color:var(--dim);font-size:12px">Empty directory</div>';
  } catch(e) {
    $('#ftBody').innerHTML = `<div style="padding:16px 12px;color:var(--err);font-size:12px">Error: ${esc(e.message)}</div>`;
  }
}

$('#filesBtn').onclick = () => {
  const col = $('#filetreeCol');
  const on = col.classList.toggle('visible');
  $('#filesBtn').style.color = on ? 'var(--acc)' : '';
  if (on) { _ftSession = ''; loadFileTree($('#session').value); }
};

// ---- file viewer ----
let _fileContent = '';
async function openFile(path) {
  try {
    const r = await fetch('/api/file?path=' + encodeURIComponent(path));
    const d = await r.json();
    if (d.error) { alert(d.error); return; }
    _fileContent = d.content;
    $('#filePath').textContent = path;
    const truncNote = d.truncated ? `\n\n... [truncated at 150KB, full size: ${(d.size/1024).toFixed(0)}KB]` : '';
    $('#fileContent').textContent = d.content + truncNote;
    $('#fileOverlay').classList.add('open');
  } catch(e) { alert('Error: ' + e.message); }
}

function closeFile() { $('#fileOverlay').classList.remove('open'); }

function copyFileContent() {
  navigator.clipboard.writeText(_fileContent).catch(() => {});
}

function sendFileToAgent() {
  const path = $('#filePath').textContent;
  closeFile();
  $('#input').value = `Review this file: ${path}\n\n\`\`\`\n${_fileContent.slice(0, 4000)}\n\`\`\``;
  $('#agentChk').checked = true;
  $('#cockpitCol').classList.add('visible');
  $('#input').focus();
}

// ---- preview pane ----
let _previewUrl = '';

async function probeDevServer() {
  const session = $('#session').value;
  $('#previewProbeBtn').textContent = 'probing...';
  try {
    const r = await fetch('/api/probe?session=' + encodeURIComponent(session));
    const d = await r.json();
    if (d.port) {
      connectPreview(`http://localhost:${d.port}/`);
    } else {
      $('#previewEmpty').style.display = 'flex';
      $('#previewFrame').style.display = 'none';
      $('#previewBadge').style.display = 'none';
      _previewUrl = '';
    }
  } catch(e) {}
  $('#previewProbeBtn').textContent = 'probe';
}

function connectPreview(url, label) {
  _previewUrl = url;
  // clear any stale error banner from the previous artifact
  $('#previewErrBanner').style.display = 'none';
  _lastPreviewErr = '';
  const frame = $('#previewFrame');
  const badge = $('#previewBadge');
  if (label) {
    badge.textContent = label;
  } else {
    const portMatch = url.match(/:(\d+)/);
    badge.textContent = portMatch ? ':' + portMatch[1] : url;
  }
  badge.style.display = '';
  // cache-bust so reload always shows the latest saved artifact
  frame.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now();
  frame.style.display = 'block';
  $('#previewEmpty').style.display = 'none';
}

$('#previewBtn').onclick = async () => {
  const col = $('#previewCol');
  const on = col.classList.toggle('visible');
  $('#previewBtn').style.color = on ? 'var(--acc)' : '';
  if (on && !_previewUrl) probeDevServer();
};

$('#previewProbeBtn').onclick = probeDevServer;

$('#previewRefreshBtn').onclick = () => {
  const frame = $('#previewFrame');
  if (_previewUrl) frame.src = _previewUrl + (_previewUrl.includes('?') ? '&' : '?') + 't=' + Date.now();
};

$('#previewNewTabBtn').onclick = () => {
  if (_previewUrl) window.open(_previewUrl, '_blank');
};

$('#previewConnectBtn').onclick = () => {
  const port = $('#previewPortInput').value.trim();
  if (port) connectPreview(`http://localhost:${port}/`);
};

// ---- project preview picker ----
const PROJ_TYPE_STYLE = {
  vite:        ['#5cd693', 'web app'],
  next:        ['#5cd693', 'web app'],
  cra:         ['#5cd693', 'web app'],
  static:      ['#5cd693', 'static'],
  'electron-web': ['#f0a24e', 'renderer only'],
  cep:         ['#f3837e', 'Premiere'],
  python:      ['#6a6a70', 'no UI'],
  node:        ['#f0a24e', 'needs install'],
  unknown:     ['#6a6a70', '—'],
  missing:     ['#6a6a70', '—'],
};

function showPreviewPane() {
  const col = $('#previewCol');
  if (!col.classList.contains('visible')) {
    col.classList.add('visible');
    $('#previewBtn').style.color = 'var(--acc)';
  }
}

async function openProjectPicker() {
  showPreviewPane();
  const picker = $('#projectPicker');
  $('#previewEmpty').style.display = 'none';
  $('#previewFrame').style.display = 'none';
  $('#previewStatus').style.display = 'none';
  picker.style.display = 'block';
  picker.innerHTML = '<div class="pp-loading">scanning Cld projects…</div>';
  try {
    const r = await fetch('/api/projects');
    const projects = await r.json();
    const can  = projects.filter(p => p.previewable);
    const cant = projects.filter(p => !p.previewable);
    const row = p => {
      const [color, tag] = PROJ_TYPE_STYLE[p.type] || ['#6a6a70', p.type];
      const dis = p.previewable ? '' : 'pp-row-off';
      const click = p.previewable ? `onclick="previewProject('${p.name}')"` : '';
      const reason = p.reason ? `<span class="pp-reason">${esc(p.reason)}</span>` : '';
      return `<div class="pp-row ${dis}" ${click}>
        <span class="pp-name">${esc(p.name)}</span>
        <span class="pp-tag" style="color:${color};border-color:${color}55">${esc(tag)}</span>
        ${reason}
      </div>`;
    };
    picker.innerHTML =
      `<div class="pp-head">
        <span>${can.length} previewable</span>
        <button class="ghost" onclick="closeProjectPicker()" style="font-size:11px;padding:2px 8px">close</button>
      </div>
      <div class="pp-list">${can.map(row).join('')}</div>
      <div class="pp-sub">Can't preview in a browser (${cant.length})</div>
      <div class="pp-list">${cant.map(row).join('')}</div>`;
  } catch (e) {
    picker.innerHTML = '<div class="pp-loading">failed to scan projects</div>';
  }
}

function closeProjectPicker() {
  $('#projectPicker').style.display = 'none';
  if (_previewUrl) $('#previewFrame').style.display = 'block';
  else $('#previewEmpty').style.display = 'flex';
}

async function previewProject(name) {
  $('#projectPicker').style.display = 'none';
  const status = $('#previewStatus');
  status.style.display = 'flex';
  status.innerHTML = `<div class="pp-spin"></div>
    <div>Starting <b>${esc(name)}</b>…</div>
    <div style="font-size:11px;color:var(--dim)">dev servers can take 10–40s to boot</div>`;
  try {
    const r = await fetch('/api/preview/project', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name })
    });
    const d = await r.json();
    if (!d.ok) {
      status.innerHTML = `<div style="color:var(--err);font-weight:600">Can't preview ${esc(name)}</div>
        <pre style="white-space:pre-wrap;font-size:11px;color:var(--dim);max-width:90%;text-align:left">${esc(d.reason || 'unknown')}</pre>
        <button class="ghost" onclick="openProjectPicker()" style="font-size:11px">← back to projects</button>`;
      return;
    }
    status.style.display = 'none';
    const label = d.mode === 'static' ? name + ' (static)' : name + (d.type === 'electron-web' ? ' (renderer)' : '');
    connectPreview(d.url, label);
    $('#previewStopBtn').style.display = (d.mode === 'dev-server') ? '' : 'none';
    if (d.note) {
      // brief non-blocking note about renderer-only previews
      const badge = $('#previewBadge');
      badge.title = d.note;
    }
  } catch (e) {
    status.innerHTML = `<div style="color:var(--err)">Error: ${esc(e.message)}</div>`;
  }
}

$('#previewProjectsBtn').onclick = openProjectPicker;

$('#previewStopBtn').onclick = async () => {
  await fetch('/api/preview/project/stop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
  $('#previewStopBtn').style.display = 'none';
  $('#previewFrame').style.display = 'none';
  $('#previewBadge').style.display = 'none';
  _previewUrl = '';
  $('#previewEmpty').style.display = 'flex';
};

$('#previewPortInput').onkeydown = e => {
  if (e.key === 'Enter') $('#previewConnectBtn').click();
};

// ---- preview error feedback loop ----
let _lastPreviewErr = '';

window.addEventListener('message', e => {
  if (!e.data || e.data.type !== 'sidka-preview-error') return;
  _lastPreviewErr = e.data.msg || '';
  const banner = $('#previewErrBanner');
  $('#previewErrText').textContent = '⚠ ' + _lastPreviewErr;
  banner.style.display = 'flex';
});

function dismissPreviewErr() {
  $('#previewErrBanner').style.display = 'none';
  _lastPreviewErr = '';
}

async function fixPreviewError() {
  if (!_lastPreviewErr) return;
  dismissPreviewErr();
  // compose a targeted fix request and submit it as a chat message
  const inp = $('#input');
  inp.value = 'The preview just threw this error: "' + _lastPreviewErr + '". Please fix the code and show the corrected version.';
  await send();
}

// refresh tree + re-probe on session change (patch the existing onchange)
const _origSessionChange = $('#session').onchange;
$('#session').onchange = function() {
  if (_origSessionChange) _origSessionChange.call(this);
  _ftSession = '';
  if ($('#filetreeCol').classList.contains('visible')) loadFileTree(this.value);
  if ($('#previewCol').classList.contains('visible')) probeDevServer();
};

// ---- tasks / kanban ----

const CAT_COLORS = {
  dev:'#5080d0', brand:'#7c6af7', campaign:'#d4a020',
  content:'#40a0b0', crm:'#d04090', release:'#58a058', research:'#888'
};
const PRI_COLORS = { high:'#e05050', medium:'#d4a020', low:'#555' };

let _tasks = [], _filterProject = '', _filterCategory = '';

async function loadTasks() {
  const params = new URLSearchParams();
  if (_filterProject)  params.set('project', _filterProject);
  if (_filterCategory) params.set('category', _filterCategory);
  const r = await fetch('/api/tasks?' + params.toString());
  _tasks = await r.json();
  renderBoard();
}

function renderBoard() {
  const cols = { todo:[], doing:[], blocked:[], done:[], cancelled:[] };
  for (const t of _tasks) cols[t.status]?.push(t);

  for (const [status, cards] of Object.entries(cols)) {
    const el = document.getElementById('cards-' + status);
    const cnt = document.getElementById('cnt-' + status);
    if (!el) continue;
    if (cnt) cnt.textContent = cards.length;
    el.innerHTML = cards.map(renderCard).join('');
  }
  renderProjectFilter();
}

function renderCard(t) {
  const catCol = CAT_COLORS[t.category] || '#888';
  const priCol = PRI_COLORS[t.priority] || '#555';
  const age = taskAge(t.created);
  const proj = t.project !== 'general' ? `<span style="font-size:10px;color:var(--dim)">${esc(t.project)}</span>` : '';
  const notes = t.notes ? `<div class="kb-card-notes">${esc(t.notes.slice(0,200))}</div>` : '';
  const tags  = (t.tags||[]).map(g => `<span style="font-size:9px;color:var(--dim);background:var(--bg3);padding:1px 5px;border-radius:6px">${esc(g)}</span>`).join('');

  const movebtns = {
    todo:    ['doing','blocked'],
    doing:   ['done','blocked','todo'],
    blocked: ['todo','doing'],
    done:    ['todo'],
    cancelled: ['todo'],
  }[t.status] || [];

  const actionBtns = movebtns.map(s =>
    `<button onclick="moveTask('${t.id}','${s}',event)">${s}</button>`
  ).join('') +
  `<button onclick="askAgentAboutTask('${t.id}',event)">ask agent ↗</button>` +
  `<button class="btn-danger" onclick="deleteTask('${t.id}',event)">delete</button>`;

  return `<div class="kb-card" id="card-${t.id}" onclick="toggleCard('${t.id}')">
    <div class="kb-card-title">${esc(t.title)}</div>
    <div class="kb-card-meta">
      <div class="pri-dot" style="background:${priCol}" title="${t.priority} priority"></div>
      <span class="cat-badge" style="background:${catCol}22;color:${catCol};border:1px solid ${catCol}44">${t.category}</span>
      ${proj}${tags}
      <span class="kb-card-age">${age}</span>
    </div>
    <div class="kb-card-detail">
      ${notes}
      <div style="font-size:10px;color:var(--dim);margin-bottom:6px">id: ${t.id}</div>
      <div class="kb-card-actions">${actionBtns}</div>
    </div>
  </div>`;
}

function toggleCard(id) {
  const el = document.getElementById('card-' + id);
  if (el) el.classList.toggle('expanded');
}

function taskAge(iso) {
  try {
    const d = new Date(iso), now = new Date();
    const sec = (now - d) / 1000;
    if (sec < 3600) return Math.floor(sec/60) + 'm';
    if (sec < 86400) return Math.floor(sec/3600) + 'h';
    return Math.floor(sec/86400) + 'd';
  } catch { return ''; }
}

function renderProjectFilter() {
  const projects = [...new Set(_tasks.map(t => t.project).filter(Boolean))].sort();
  const el = document.getElementById('projectFilter');
  if (!el) return;
  el.innerHTML = `<button class="filter-pill${_filterProject===''?' on':''}" onclick="setProjectFilter('')">all</button>` +
    projects.map(p => `<button class="filter-pill${_filterProject===p?' on':''}" onclick="setProjectFilter('${esc(p)}')">${esc(p)}</button>`).join('');
  const cats = ['dev','brand','campaign','content','crm','release','research'];
  const cf = document.getElementById('categoryFilter');
  if (cf) cf.innerHTML = cats.map(c =>
    `<button class="filter-pill${_filterCategory===c?' on':''}" onclick="setCategoryFilter('${c}')"
      style="color:${CAT_COLORS[c]};border-color:${CAT_COLORS[c]}44">${c}</button>`
  ).join('');
}

function setProjectFilter(p)  { _filterProject  = p; loadTasks(); }
function setCategoryFilter(c) { _filterCategory = _filterCategory===c?'':c; loadTasks(); }

async function moveTask(id, status, e) {
  e.stopPropagation();
  await fetch(`/api/tasks/${id}`, {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({status})
  });
  loadTasks();
}

async function deleteTask(id, e) {
  e.stopPropagation();
  if (!confirm('Delete this task?')) return;
  await fetch(`/api/tasks/${id}`, {method:'DELETE'});
  loadTasks();
}

function askAgentAboutTask(id, e) {
  e.stopPropagation();
  const t = _tasks.find(x => x.id === id);
  if (!t) return;
  $('#input').value = `Look at task ${t.id}: "${t.title}" (project: ${t.project}, category: ${t.category}). Give me a brief plan to tackle this, then use the tasks skill to mark it as doing.`;
  document.querySelector('.nav[data-tab="chat"]').click();
  $('#agentChk').checked = true;
  $('#cockpitCol').classList.add('visible');
  $('#input').focus();
}

// new task form
$('#newTaskBtn').onclick = () => {
  const form = document.getElementById('newTaskForm');
  form.classList.toggle('open');
  if (form.classList.contains('open')) {
    // populate project dropdown from sessions
    const sel = document.getElementById('ntProject');
    sel.innerHTML = (STATE.sessions||[]).map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');
    sel.value = $('#session')?.value || 'general';
    document.getElementById('ntTitle').focus();
  }
};

function closeNewTaskForm() {
  document.getElementById('newTaskForm').classList.remove('open');
  document.getElementById('ntTitle').value = '';
  document.getElementById('ntNotes').value = '';
}

async function submitNewTask() {
  const title = document.getElementById('ntTitle').value.trim();
  if (!title) { document.getElementById('ntTitle').focus(); return; }
  await fetch('/api/tasks', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      title,
      project:  document.getElementById('ntProject').value,
      category: document.getElementById('ntCategory').value,
      priority: document.getElementById('ntPriority').value,
      notes:    document.getElementById('ntNotes').value.trim(),
    })
  });
  closeNewTaskForm();
  loadTasks();
}

document.getElementById('ntTitle')?.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitNewTask(); }
});

$('#refreshTasksBtn').onclick = loadTasks;

// load tasks when Tasks tab is clicked
document.querySelectorAll('.nav').forEach(n => {
  if (n.dataset.tab === 'tasks') {
    n.addEventListener('click', loadTasks);
  }
});

// ---- self-review loop ----
let _lastAgentReply = '';

function _showReviewBtn() {
  const existing = document.getElementById('reviewBar');
  if (existing) existing.remove();
  if (!_lastAgentReply || _lastAgentReply.length < 80) return;

  const bar = document.createElement('div');
  bar.id = 'reviewBar';
  bar.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 16px;border-top:1px solid var(--border);background:var(--bg2);flex-shrink:0';
  bar.innerHTML = `
    <span style="font-size:11px;color:var(--dim)">Agent finished —</span>
    <button class="ghost" style="font-size:11px;padding:3px 10px" onclick="triggerReview('critique')">Review output</button>
    <button class="ghost" style="font-size:11px;padding:3px 10px" onclick="triggerReview('syntax')">Check syntax</button>
    <button class="ghost" style="font-size:11px;padding:3px 10px;margin-left:auto" onclick="this.parentElement.remove()">✕</button>
  `;
  const chatCol = document.querySelector('.chat-col');
  chatCol.appendChild(bar);
}

async function triggerReview(action) {
  document.getElementById('reviewBar')?.remove();
  const lang = _lastAgentReply.includes('def ') || _lastAgentReply.includes('import ') ? 'python'
             : _lastAgentReply.includes('function ') || _lastAgentReply.includes('const ') ? 'javascript'
             : '';
  const prompt = action === 'syntax'
    ? `Use the review skill (action="syntax", language="${lang}", text=<the code below>) to check for syntax errors.\n\n${_lastAgentReply.slice(0, 3000)}`
    : `Use the review skill (action="critique", language="${lang}", text=<the output below>) to self-review. Fix anything significant you find.\n\n${_lastAgentReply.slice(0, 3000)}`;

  addMsg('you', 'me').textContent = action === 'syntax' ? '⚙ Syntax check' : '🔍 Review last output';
  $('#agentChk').checked = true;
  $('#cockpitCol').classList.add('visible');
  const bubble = addMsg('agent · review', 'agent');
  await sendCockpit(prompt, bubble);
}

// ---- init ----
loadState();
</script>
<div id="previewErrBanner" style="display:none;position:fixed;left:50%;bottom:18px;transform:translateX(-50%);max-width:min(720px,92vw);background:#2a0d0d;color:#ffb4b4;font:11px/1.5 ui-monospace,Menlo,monospace;padding:9px 12px;border:1px solid #e05050;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.5);align-items:flex-start;gap:8px;z-index:2147483646">
  <span style="flex:1;white-space:pre-wrap;word-break:break-word" id="previewErrText"></span>
  <button onclick="fixPreviewError()" style="flex-shrink:0;background:#e05050;color:#fff;border:none;border-radius:9999px;padding:3px 12px;font:11px ui-monospace,Menlo,monospace;cursor:pointer">&#9889; Fix it</button>
  <button onclick="dismissPreviewErr()" style="flex-shrink:0;background:none;border:none;color:#ffb4b4;font:13px monospace;cursor:pointer;opacity:.7">&#x2715;</button>
</div>
</body>
</html>"""


if __name__ == "__main__":
    errs = f", {len(SKILL_ERRORS)} errors" if SKILL_ERRORS else ""
    for name, err in SKILL_ERRORS.items():
        print(f"  [skill error] {name}: {err}")
    print(f"Sidka running at http://localhost:{PORT}  ({len(SKILLS)} skills{errs})")
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)
