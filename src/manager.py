#!/usr/bin/env python3
"""
AI Agent Manager v6 - local multi-model agent over Ollama.

v6 adds over v5:
  - Metadata-aware RAG: each indexed chunk stores project, type, language, rel_path.
  - Project-filtered retrieve(): pass project= to search only that project's index.
  - Per-session system_prompt: sessions can carry a custom system prompt for project isolation.
  - cmd_status_all(): git status snapshot across all ~/Desktop/Cld projects.
  - Per-project sessions created at startup for all 18 projects.
  - v1 banner updated to v5.

v4 adds on top of v3:
  - a pluggable SKILLS system. Drop a module in ~/.runai/skills/ and it becomes a
    tool the model can call in agent mode. Each skill exposes NAME, DESCRIPTION,
    SCHEMA, and run(args). Secrets live in ~/.runai/secrets.json.
  - starter skills: files, webhook (Slack/Discord/FSWorkpad), github, cloudflare.
  - /skills (list) and /reload (rescan the folder without restarting).

v3 features: rich UI (live markdown + tok/s), /file, /image (llava vision),
agent mode tools (calculator/run_shell/web_fetch), and /index RAG.
"""

import ollama
import glob
import importlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

# ---- rich (graceful fallback if somehow missing) ----------------------
try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.live import Live
    console = Console()
    RICH = True
except Exception:                                  # pragma: no cover
    RICH = False
    class _Plain:
        def print(self, *a, **k):
            print(*[x for x in a if isinstance(x, str)])
        def rule(self, text=""):
            print("-" * 8, re.sub(r"\[/?[^\]]*\]", "", str(text)))
    console = _Plain()


# ---------------------------------------------------------------- config
MODELS = {
    "fast":      "llama3.2:1b",
    "technical": "qwen2.5:1.5b",
    "creative":  "gemma2:2b",
    "logic":     "phi3",
}
ROUTER_MODEL = "llama3.2:1b"
VISION_MODEL = "llava:latest"
EMBED_MODEL  = "nomic-embed-text"

# Dedicated agent model — code-optimized, supports tool calling reliably.
# qwen2.5-coder:7b is the sweet spot for Intel i9 32GB (no GPU): fits in RAM,
# runs at ~2-4 tok/s, and actually understands multi-file edits + tool schemas.
# Falls back to qwen2.5:1.5b if the 7b model hasn't been pulled yet.
_AGENT_MODEL_PREFERRED = "qwen2.5-coder:7b"
_AGENT_MODEL_FALLBACK  = "qwen2.5:1.5b"

def _agent_model():
    try:
        models = [m.model if hasattr(m, "model") else m["model"]
                  for m in ollama.list().models]
        if any(_AGENT_MODEL_PREFERRED in m for m in models):
            return _AGENT_MODEL_PREFERRED
    except Exception:
        pass
    return _AGENT_MODEL_FALLBACK

# "build" routes any make/create/generate request to the capable coder model so it
# can actually produce working code + live HTML/SVG previews (not the 1.5b stub).
MODELS["build"] = _agent_model()

SYSTEM_PROMPTS = {
    "fast":      "You are a concise local AI assistant for DJ, a solo developer and creative. Answer directly and keep it short. Never invent names or personal details about the user.",
    "technical": "You are an expert software engineer assisting DJ, a solo developer. Be precise and correct. Show minimal code only when it actually helps. Never invent names or personal details.",
    "creative":  "You are an imaginative writer assisting DJ. Be original and evocative. Keep responses focused on what was asked.",
    "logic":     "You are a clear-headed reasoner assisting DJ. Give a direct, concise answer. Reason through the problem only if it genuinely requires it — never pad the response. Never invent names or personal details.",
    "build":     ("You are an expert build agent for DJ, a solo developer. When asked to make, build, or create "
                  "something visual or interactive — a game, app, UI, component, page, animation, chart, or demo — "
                  "you BUILD IT, you never refuse or defer. Output a complete, self-contained, working artifact. "
                  "For anything visual or interactive, emit a single ```html block (full HTML+CSS+JS, no external "
                  "deps) — it renders live in the chat. For a static graphic, emit a ```svg block. Write the whole "
                  "thing; do not output partial snippets or 'you could do X' suggestions. Never invent personal details."),
    "agent":     ("You are an expert coding agent for DJ, a solo developer managing multiple projects. "
                  "You have access to tools for reading/writing files, running shell commands, calling webhooks, "
                  "GitHub, and Cloudflare. When given a task: think through the steps, use tools to do the work, "
                  "confirm what you did. Be precise. Never invent names or details. Never run destructive commands "
                  "without stating exactly what you're about to do."),
}

# Appended to EVERY agent-mode system prompt so all sessions (including project-isolated
# ones) know about rendering and self-review capabilities.
_AGENT_CAPABILITIES = """

=== RENDERING CAPABILITIES ===
The chat UI renders these code block types inline — use them proactively:
- ```svg  → live SVG graphic rendered directly in the chat bubble
- ```html → sandboxed interactive iframe (JS allowed)
- ```js, ```python, etc. → syntax-highlighted code block

=== WHEN TO GENERATE VISUALS ===
- Data, metrics, comparisons → call the chart skill, paste SVG in a ```svg block
- Architecture, flow, sequences → draw an SVG diagram (dark bg #111, text #e8e8e8)
- UI mockups, wireframes → use the design skill for tokens, then output ```svg or ```html
- Status reports → a chart is better than a bullet list when you have numbers

=== CHART SKILL USAGE ===
Call: chart(type="bar"|"line"|"pie"|"sparkline", data=[{"label":..,"value":..}], title="..")
The skill returns complete SVG markup. Paste it verbatim inside a ```svg block.

=== SELF-REVIEW LOOP ===
After completing any code task or producing a significant output:
1. Use the review skill: review(action="critique", text="<your output>")
2. If it finds real issues, fix them and note what changed.
3. Only skip review for trivial one-liners.

=== TASK MANAGEMENT ===
You have a tasks skill for tracking work across DJ's 18+ projects.
Call it whenever a user mentions a bug, feature, TODO, promo, campaign task, or any trackable work item.
- tasks(action="create", title="..", project="..", category="dev|brand|campaign|content|crm|release|research", priority="high|medium|low")
- tasks(action="list", project="..", status="todo|doing|blocked")
- tasks(action="update", id="t_...", field="status", value="doing")
- tasks(action="close", id="t_...")
- tasks(action="summary")  — use for /tasks command
After creating a task, confirm with the task ID. After closing/moving a task, confirm the new status.
Proactively create tasks when: user describes a bug, feature request, or "I need to" statement.

=== DESIGN TOKENS (for hand-drawn SVGs) ===
bg:#111111  surface:#1a1a1a  border:#2e2e2e
text:#e8e8e8  muted:#555  accent:#7c6af7
green:#58a058  amber:#d4a020  red:#e05050  blue:#5080d0"""

KEYWORD_ROUTES = [
    ("technical", r"\b(code|coding|program|function|class|bug|debug|python|javascript|typescript|rust|sql|regex|api|stack ?trace|compile|algorithm|math|calculat|equation|derivative|integral)\b"),
    ("creative",  r"\b(poem|story|stories|song|lyric|haiku|sonnet|imagine|fiction|novel|character|screenplay|write me a)\b"),
    ("logic",     r"\b(prove|proof|deduce|logic|puzzle|riddle|step.?by.?step|brain.?teaser|reasoning)\b"),
]

MAX_CONTEXT_MESSAGES = 16
SUMMARY_TRIGGER      = 28

RUNAI_DIR     = os.path.expanduser("~/.runai")
SESSION_DIR   = os.path.join(RUNAI_DIR, "sessions")
INDEX_FILE    = os.path.join(RUNAI_DIR, "index.json")
SKILLS_DIR    = os.path.join(RUNAI_DIR, "skills")
LEGACY_MEMORY = os.path.expanduser("~/memory.json")

# runtime-only state (not persisted)
RT = {"override": None, "agent": False, "rag": True, "file": None}  # file = (name, content)


# ---------------------------------------------------------------- ollama accessors
def _attr(obj, key):
    return getattr(obj, key) if hasattr(obj, key) else obj[key]

def _safe(obj, key):
    try:
        return _attr(obj, key)
    except Exception:
        return None

def _chunk_text(chunk):
    return _attr(_attr(chunk, "message"), "content")

def _gen_text(resp):
    return _attr(resp, "response")


class OllamaDown(Exception):
    pass

def _guard(call):
    try:
        return call()
    except Exception as e:
        blob = str(e).lower()
        if "connect" in blob or "refused" in blob or "11434" in blob:
            raise OllamaDown() from e
        raise


# ---------------------------------------------------------------- sessions
def _session_path(name):
    return os.path.join(SESSION_DIR, f"{name}.json")

def load_session(name):
    os.makedirs(SESSION_DIR, exist_ok=True)
    path = _session_path(name)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            data.setdefault("summary", "")
            data.setdefault("messages", [])
            return data
        except (json.JSONDecodeError, OSError):
            console.print(f"  (warning) {name}.json was unreadable; starting fresh.")
    if name == "default" and os.path.exists(LEGACY_MEMORY):
        try:
            with open(LEGACY_MEMORY) as f:
                msgs = json.load(f)
            if isinstance(msgs, list) and msgs:
                console.print(f"  imported {len(msgs)} messages from old memory.json")
                return {"summary": "", "messages": msgs}
        except (json.JSONDecodeError, OSError):
            pass
    return {"summary": "", "messages": []}

def save_session(name, data):
    os.makedirs(SESSION_DIR, exist_ok=True)
    with open(_session_path(name), "w") as f:
        json.dump(data, f, indent=2)

def list_sessions():
    if not os.path.isdir(SESSION_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SESSION_DIR) if f.endswith(".json"))


# ---------------------------------------------------------------- routing
_HARD_KEYWORDS = re.compile(
    r"\b(code|coding|program|function|class|bug|debug|python|javascript|typescript|"
    r"rust|sql|regex|api|stack.?trace|compile|algorithm|math|calculat|equation|"
    r"derivative|integral|prove|proof|deduce|logic|puzzle|riddle|step.?by.?step|"
    r"brain.?teaser|reasoning|poem|story|stories|song|lyric|haiku|sonnet|imagine|"
    r"fiction|novel|character|screenplay|write me a|"
    # build vocabulary — so short requests like "make a snake game" reach the router
    r"make|build|create|generate|scaffold|prototype|develop|implement|render|clone|"
    r"game|app|website|web ?site|landing|html|css|canvas|svg|ui|component|dashboard|"
    r"widget|mockup|snake|tetris|pong|calculator|preview|interactive|animation)\b"
)

# Build requests: must have both a build-verb and a build-noun anywhere in the text.
# Lookaheads make it order-independent and avoid stealing "write me a poem" (creative).
_BUILD_ROUTE = re.compile(
    r"(?=.*\b(make|build|create|generate|scaffold|prototype|develop|implement|render|clone|design|code)\b)"
    r"(?=.*\b(game|app|application|web ?site|site|page|landing|html|css|canvas|svg|ui|component|"
    r"dashboard|widget|form|preview|demo|tool|clock|calculator|timer|snake|tetris|pong|chart|"
    r"animation|mockup|interactive|button|menu|navbar|modal|card|layout|visualizer|simulation)\b)"
)

def route(user_prompt):
    if RT["override"]:
        return RT["override"]
    low = user_prompt.lower()
    # Build requests win outright — route to the capable coder model regardless of length.
    if _BUILD_ROUTE.search(low):
        return "build"
    # Short casual messages: skip LLM router entirely to avoid misclassification.
    # "are u faster", "hello", "what's up", etc. should never hit phi3/gemma.
    if len(user_prompt.strip()) < 40 and not _HARD_KEYWORDS.search(low):
        return "fast"
    for category, pattern in KEYWORD_ROUTES:
        if re.search(pattern, low):
            return category
    prompt = (
        "Classify the user request into exactly one category: fast, technical, creative, logic.\n"
        "technical = code/math, creative = writing/art, logic = reasoning/puzzles, fast = everything else.\n"
        f'Request: "{user_prompt}"\n'
        "Answer with one word only."
    )
    try:
        text = _gen_text(_guard(lambda: ollama.generate(model=ROUTER_MODEL, prompt=prompt))).strip().lower()
        word = re.sub(r"[^a-z]", "", text.split()[0]) if text else ""
        return word if word in MODELS else "fast"
    except OllamaDown:
        raise
    except Exception:
        return "fast"


# ---------------------------------------------------------------- context
def build_messages(session, category, extras=None):
    # Per-session system prompt (project isolation) overrides the category default.
    prompt = session.get("system_prompt") or SYSTEM_PROMPTS.get(category) or SYSTEM_PROMPTS["fast"]
    # Inject live-render + self-review capabilities for every build/code-capable model,
    # not just cockpit "agent" mode, so normal chat can produce live HTML/SVG previews.
    if category in ("agent", "build", "technical"):
        prompt = prompt + _AGENT_CAPABILITIES
    msgs = [{"role": "system", "content": prompt}]
    if session["summary"]:
        msgs.append({"role": "system", "content": "Summary of earlier conversation: " + session["summary"]})
    if RT["file"]:
        name, content = RT["file"]
        msgs.append({"role": "system",
                     "content": f"The user attached the file '{name}'. Use it to answer:\n\n{content[:8000]}"})
    for ex in (extras or []):
        msgs.append({"role": "system", "content": ex})
    msgs.extend(session["messages"][-MAX_CONTEXT_MESSAGES:])
    return msgs

def maybe_summarize(session):
    if len(session["messages"]) <= SUMMARY_TRIGGER:
        return
    keep     = session["messages"][-MAX_CONTEXT_MESSAGES:]
    overflow = session["messages"][:-MAX_CONTEXT_MESSAGES]
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in overflow)
    prompt = ("Summarize this conversation excerpt in 3-4 sentences, preserving names, facts, and decisions.\n\n"
              f"Previous summary: {session['summary'] or '(none)'}\n\n{convo}")
    try:
        session["summary"] = _gen_text(_guard(lambda: ollama.generate(model=ROUTER_MODEL, prompt=prompt))).strip()
        session["messages"] = keep
        console.print(f"  [dim](compacted {len(overflow)} older messages into memory)[/dim]" if RICH
                      else f"  (compacted {len(overflow)} older messages)")
    except Exception:
        pass


# ---------------------------------------------------------------- streaming + render
def stream_reply(model, messages):
    parts, meta, start = [], {}, time.time()
    def run():
        if RICH:
            with Live(console=console, refresh_per_second=12, vertical_overflow="visible") as live:
                for chunk in ollama.chat(model=model, messages=messages, stream=True):
                    tok = _chunk_text(chunk)
                    if tok:
                        parts.append(tok)
                        live.update(Markdown("".join(parts)))
                    if _safe(chunk, "done"):
                        meta["count"], meta["dur"] = _safe(chunk, "eval_count"), _safe(chunk, "eval_duration")
        else:
            for chunk in ollama.chat(model=model, messages=messages, stream=True):
                tok = _chunk_text(chunk)
                if tok:
                    parts.append(tok)
                    print(tok, end="", flush=True)
            print()
    _guard(run)
    elapsed = time.time() - start
    tok_s = (meta["count"] / (meta["dur"] / 1e9)) if meta.get("count") and meta.get("dur") else None
    stat = f"{elapsed:.1f}s" + (f" · {tok_s:.0f} tok/s" if tok_s else "")
    console.print(f"[dim]{stat}[/dim]" if RICH else stat)
    return "".join(parts)


# ---------------------------------------------------------------- built-in tools
def tool_calculator(args):
    expr = str(args.get("expression", ""))
    if not re.fullmatch(r"[0-9eE\.\s\+\-\*\/\(\)%]+", expr):
        return "Error: only basic arithmetic is allowed."
    try:
        return str(eval(expr, {"__builtins__": {}}, {}))
    except Exception as e:
        return f"Error: {e}"

def tool_run_shell(args):
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "Error: empty command."
    console.print(f"[yellow]  wants to run:[/yellow] {cmd}" if RICH else f"  wants to run: {cmd}")
    if not os.environ.get("RUNAI_WEB_MODE"):
        try:
            ok = input("  allow? [y/N] ").strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            ok = False
        if not ok:
            return "User denied permission to run this command."
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return ((out.stdout + out.stderr).strip() or "(no output)")[:4000]
    except Exception as e:
        return f"Error: {e}"

def tool_web_fetch(args):
    url = str(args.get("url", ""))
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(300000).decode("utf-8", "ignore")
        for tag in ("script", "style"):
            html = re.sub(fr"<{tag}.*?</{tag}>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()[:4000] or "(no readable text)"
    except Exception as e:
        return f"Error: {e}"

BUILTIN_FNS = {"calculator": tool_calculator, "run_shell": tool_run_shell, "web_fetch": tool_web_fetch}
BUILTIN_TOOLS = [
    {"type": "function", "function": {
        "name": "calculator", "description": "Evaluate a basic arithmetic expression.",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "run_shell", "description": "Run a shell command on the user's macOS machine and return its output. Good for listing files, checking dates, reading file info.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "web_fetch", "description": "Fetch a web page and return its readable text.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
]


# ---------------------------------------------------------------- pluggable skills
def load_skills():
    """Import every ~/.runai/skills/*.py (except _underscore files) that defines
    NAME + run(). Returns {name: module}."""
    skills = {}
    if not os.path.isdir(SKILLS_DIR):
        return skills
    if SKILLS_DIR not in sys.path:
        sys.path.insert(0, SKILLS_DIR)
    for fn in sorted(os.listdir(SKILLS_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        mod_name = fn[:-3]
        try:
            mod = importlib.reload(importlib.import_module(mod_name))
            if hasattr(mod, "NAME") and hasattr(mod, "run"):
                skills[mod.NAME] = mod
        except Exception as e:
            console.print(f"  (skill '{mod_name}' failed to load: {e})")
    return skills

def all_tools(skills):
    tools = list(BUILTIN_TOOLS)
    for name, mod in skills.items():
        tools.append({"type": "function", "function": {
            "name": name,
            "description": getattr(mod, "DESCRIPTION", name),
            "parameters": getattr(mod, "SCHEMA", {"type": "object", "properties": {}}),
        }})
    return tools

def dispatch(name, args, skills):
    if name in BUILTIN_FNS:
        return BUILTIN_FNS[name](args)
    if name in skills:
        try:
            return str(skills[name].run(args))
        except Exception as e:
            return f"Error in skill '{name}': {e}"
    return f"unknown tool: {name}"


def agent_turn(session, session_name, user_input, skills):
    model = _agent_model()
    session["messages"].append({"role": "user", "content": user_input})
    msgs = build_messages(session, "agent")
    tools = all_tools(skills)
    console.rule(f"[magenta]agent[/magenta] · {model} · {len(tools)} tools" if RICH else f"agent · {model}")
    for _ in range(6):
        try:
            resp = _guard(lambda: ollama.chat(model=model, messages=msgs, tools=tools))
        except OllamaDown:
            console.print("  Ollama isn't responding. Start it with `ollama serve`.")
            session["messages"].pop()
            return
        msg = _attr(resp, "message")
        calls = _safe(msg, "tool_calls")
        content = _safe(msg, "content") or ""
        msgs.append(msg)
        if not calls:
            console.print(Markdown(content) if RICH else content)
            session["messages"].append({"role": "assistant", "content": content})
            maybe_summarize(session)
            save_session(session_name, session)
            return
        for tc in calls:
            fn = _attr(_attr(tc, "function"), "name")
            raw = _attr(_attr(tc, "function"), "arguments")
            args = raw if isinstance(raw, dict) else json.loads(raw)
            console.print(f"[dim]  tool {fn}({args})[/dim]" if RICH else f"  tool {fn}({args})")
            msgs.append({"role": "tool", "content": dispatch(fn, args, skills)})
    console.print("  (stopped after several tool rounds)")


# ---------------------------------------------------------------- knowledge base (RAG)
TEXT_EXTS = {".txt", ".md", ".rst", ".py", ".js", ".ts", ".jsx", ".tsx", ".json",
             ".html", ".css", ".sh", ".yml", ".yaml", ".toml", ".csv"}

def embed(text):
    return _attr(_guard(lambda: ollama.embeddings(model=EMBED_MODEL, prompt=text)), "embedding")

def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0

def load_index():
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE) as f:
                return json.load(f).get("items", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []

def save_index(items):
    os.makedirs(RUNAI_DIR, exist_ok=True)
    with open(INDEX_FILE, "w") as f:
        json.dump({"items": items}, f)

_MAX_CHUNK = 3000  # nomic-embed-text context limit in chars (safe margin below 8192 tok)

def chunk_text(text, size=800):
    """Split text into chunks. Never exceeds _MAX_CHUNK chars regardless of paragraph boundaries."""
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        # If a single paragraph is already over the hard limit, split it by lines.
        if len(para) > _MAX_CHUNK:
            if cur.strip():
                chunks.append(cur.strip())
                cur = ""
            lines, block = para.splitlines(), ""
            for line in lines:
                if len(block) + len(line) + 1 > _MAX_CHUNK and block:
                    chunks.append(block.strip())
                    block = line
                else:
                    block = (block + "\n" + line) if block else line
            if block.strip():
                cur = block
            continue
        if len(cur) + len(para) > size and cur:
            chunks.append(cur.strip())
            cur = para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur.strip():
        # Final chunk: hard-cap at _MAX_CHUNK
        chunks.append(cur.strip()[:_MAX_CHUNK])
    return [c for c in chunks if c]

_LANG_MAP = {
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".py": "python", ".md": "markdown",
    ".json": "json", ".css": "css", ".html": "html",
    ".sh": "shell", ".bash": "shell",
    ".yaml": "yaml", ".yml": "yaml",
    ".rs": "rust", ".go": "go", ".swift": "swift",
    ".c": "c", ".cpp": "cpp", ".h": "c",
}

def _extract_metadata(path, index_root):
    """Derive project, type, language, rel_path from a file path."""
    abs_root = os.path.abspath(os.path.expanduser(index_root))
    abs_path = os.path.abspath(path)
    try:
        rel = os.path.relpath(abs_path, abs_root)
    except ValueError:
        rel = abs_path
    parts = rel.split(os.sep)
    project = parts[0] if len(parts) > 1 else os.path.basename(abs_root)
    rel_path = os.path.join(*parts[1:]) if len(parts) > 1 else rel
    ext = os.path.splitext(path)[1].lower()
    language = _LANG_MAP.get(ext, "text")
    basename = os.path.basename(path).lower()
    low_parts = [p.lower() for p in parts]
    if basename in ("readme.md", "changelog.md", "contributing.md", "claude.md"):
        ftype = "docs"
    elif "docs" in low_parts or "documentation" in low_parts:
        ftype = "docs"
    elif basename in ("package.json", "tsconfig.json", "pyproject.toml", "setup.py"):
        ftype = "config"
    elif ".test." in basename or ".spec." in basename or "test" in low_parts or "__tests__" in low_parts:
        ftype = "test"
    elif ext == ".md":
        ftype = "docs"
    else:
        ftype = "source"
    return {"project": project, "type": ftype, "language": language, "rel_path": rel_path}

def cmd_index(folder):
    folder = os.path.expanduser(folder)
    if not os.path.isdir(folder):
        console.print(f"  not a folder: {folder}")
        return
    # Remove stale entries from this folder to avoid duplicates on re-index.
    existing = load_index()
    abs_folder = os.path.abspath(folder)
    items = [i for i in existing if not os.path.abspath(i.get("file","")).startswith(abs_folder)]
    added = 0
    try:
        for root, dirs, files in os.walk(folder):
            # Skip hidden dirs and common noise dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                       ("node_modules", "__pycache__", "dist", "build", ".git",
                        "vendor", ".next", "coverage", "venv", ".runai-venv")]
            for fn in files:
                if os.path.splitext(fn)[1].lower() not in TEXT_EXTS:
                    continue
                path = os.path.join(root, fn)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                meta = _extract_metadata(path, folder)
                for ch in chunk_text(text):
                    ch = ch[:_MAX_CHUNK]  # hard-cap before embedding
                    try:
                        vec = embed(ch)
                    except Exception:
                        # Truncate harder and retry once; skip if still fails.
                        try:
                            vec = embed(ch[:1500])
                        except Exception:
                            continue
                    items.append({"file": path, "chunk": ch, "vec": vec, **meta})
                    added += 1
                    if added % 10 == 0:
                        console.print(f"  ...embedded {added} chunks")
    except OllamaDown:
        console.print(f"  Ollama isn't responding (need `ollama pull {EMBED_MODEL}` and a running server).")
        return
    except Exception as e:
        if "not found" in str(e).lower():
            console.print(f"  Embedding model missing. Run: ollama pull {EMBED_MODEL}")
            return
        raise
    save_index(items)
    console.print(f"  indexed {added} new chunks ({len(items)} total).")

def retrieve(query, k=4, project=None):
    items = load_index()
    if not items:
        return []
    try:
        qv = embed(query)
    except Exception:
        return []
    if project:
        filtered = [i for i in items if i.get("project") == project]
        pool = filtered if filtered else items  # fall back to all if no project hits
    else:
        pool = items
    scored = sorted(pool, key=lambda it: _cosine(qv, it["vec"]), reverse=True)
    return scored[:k]

def cmd_status_all(folder="~/Desktop/Cld"):
    """Run git status across every git repo in folder. Returns list of dicts."""
    import subprocess as _sp
    folder = os.path.expanduser(folder)
    results = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return [{"project": "error", "status": f"Cannot list {folder}", "last": ""}]
    for name in names:
        proj = os.path.join(folder, name)
        if not os.path.isdir(proj) or not os.path.isdir(os.path.join(proj, ".git")):
            continue
        try:
            st = _sp.run(["git", "status", "--short", "--branch"],
                         cwd=proj, capture_output=True, text=True, timeout=5)
            last = _sp.run(["git", "log", "--oneline", "-1"],
                           cwd=proj, capture_output=True, text=True, timeout=5)
            results.append({
                "project": name,
                "status":  st.stdout.strip() or "(clean)",
                "last":    last.stdout.strip() or "no commits",
            })
        except Exception as e:
            results.append({"project": name, "status": f"error: {e}", "last": ""})
    return results


# ---------------------------------------------------------------- path helpers
def _resolve(p):
    """Resolve a user-typed path to a real file. Tolerates ~, surrounding quotes,
    shell backslash-escapes, and whitespace that doesn't match byte-for-byte
    (e.g. the U+202F narrow space macOS puts in screenshot names)."""
    if not p:
        return None
    p = p.strip().strip("'\"").replace("\\ ", " ")
    p = os.path.expanduser(p)
    if os.path.isfile(p):
        return p
    hits = [h for h in glob.glob(re.sub(r"\s+", "*", p)) if os.path.isfile(h)]
    return hits[0] if hits else None

def _split_path_question(arg):
    """Split '<path> [question]' tolerating spaces in the path and optional quotes."""
    arg = arg.strip()
    if arg and arg[0] in "\"'":                      # quoted path
        end = arg.find(arg[0], 1)
        if end != -1:
            return _resolve(arg[1:end]), arg[end + 1:].strip()
    whole = _resolve(arg)                             # whole arg is the path
    if whole:
        return whole, ""
    for m in reversed(list(re.finditer(r"\s+", arg))):
        hit = _resolve(arg[:m.start()])
        if hit:
            return hit, arg[m.end():].strip()
    first = re.split(r"\s+", arg, maxsplit=1)
    return _resolve(first[0]), (first[1].strip() if len(first) > 1 else "")


# ---------------------------------------------------------------- normal / file / image turns
def handle_turn(session, session_name, user_input, reuse_last=False):
    try:
        category = route(user_input)
    except OllamaDown:
        console.print("  Ollama isn't responding. Start it with `ollama serve`, or re-launch RunAI.")
        return
    model = MODELS[category]
    if not reuse_last:
        session["messages"].append({"role": "user", "content": user_input})

    extras = []
    if RT["rag"]:
        hits = retrieve(user_input)
        if hits:
            note = "\n\n---\n".join(f"From {os.path.basename(h['file'])}:\n{h['chunk']}" for h in hits)
            extras.append("Relevant context from the user's indexed notes:\n" + note)

    tag = f"[cyan]{category}[/cyan] · {model}" + (" · +notes" if extras else "")
    console.rule(tag if RICH else f"{category} · {model}")
    try:
        reply = stream_reply(model, build_messages(session, category, extras))
    except OllamaDown:
        console.print("  Lost the connection to Ollama. Start it and try again.")
        if not reuse_last and session["messages"] and session["messages"][-1]["role"] == "user":
            session["messages"].pop()
        return
    except KeyboardInterrupt:
        console.print("\n  [cancelled]")
        return
    session["messages"].append({"role": "assistant", "content": reply})
    maybe_summarize(session)
    save_session(session_name, session)

def cmd_file(arg, session, session_name):
    if arg.strip().lower() == "clear":
        RT["file"] = None
        console.print("  detached file.")
        return
    path = _resolve(arg)
    if not path:
        console.print(f"  no such file: {arg.strip()}")
        return
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError as e:
        console.print(f"  couldn't read it: {e}")
        return
    RT["file"] = (os.path.basename(path), content)
    lines = content.count("\n") + 1
    console.print(f"  attached '{os.path.basename(path)}' ({lines} lines). Ask away - /file clear to detach.")

def cmd_image(arg, session, session_name):
    if not arg.strip():
        console.print("  usage: /image <path> [question]")
        return
    path, question = _split_path_question(arg)
    if not path or not os.path.isfile(path):
        console.print(f"  no such file: {arg.strip()}")
        return
    question = question or "Describe this image in detail."
    console.rule(f"[green]vision[/green] · {VISION_MODEL}" if RICH else f"vision · {VISION_MODEL}")
    try:
        reply = stream_reply(VISION_MODEL, [{"role": "user", "content": question, "images": [path]}])
    except OllamaDown:
        console.print("  Ollama isn't responding.")
        return
    session["messages"].append({"role": "user", "content": f"[image: {os.path.basename(path)}] {question}"})
    session["messages"].append({"role": "assistant", "content": reply})
    save_session(session_name, session)


# ---------------------------------------------------------------- repl
HELP = """\
Chat normally, or use a command:
  /file <path>          attach a document to chat about ( /file clear to detach )
  /image <path> [q]     ask about an image (uses llava vision)
  /agent                toggle agent mode (model can use tools + your skills)
  /skills               list installed skills        /reload   rescan skills folder
  /index <folder>       embed a folder into the knowledge base (RAG)
  /status-all [folder]  git status snapshot across all projects (default ~/Desktop/Cld)
  /rag                  toggle whether answers use the knowledge base
  /model <category>     force a model (fast | technical | creative | logic)
  /auto                 return to automatic routing
  /session <name>       switch to (or create) a named conversation
  /sessions             list conversations           /history   print this one
  /retry                regenerate last response      /clear     wipe this conversation
  /models               list specialists             /help      this list
  exit | quit           leave
"""

def banner():
    console.print("\n[bold cyan]=== AI Agent Manager v6 ===[/bold cyan]" if RICH else "\n=== AI Agent Manager v6 ===")
    am = _agent_model()
    flag = "[green]✓[/green]" if am == _AGENT_MODEL_PREFERRED else "[yellow]⚠[/yellow]"
    console.print(f"  {flag} agent model: [magenta]{am}[/magenta]" if RICH
                  else f"  agent model: {am}{'  ← fallback, run: ollama pull ' + _AGENT_MODEL_PREFERRED if am != _AGENT_MODEL_PREFERRED else ''}")

def main():
    banner()
    session_name = "default"
    session = load_session(session_name)
    skills = load_skills()
    idx = len(load_index())
    console.print(f"Session: {session_name} ({len(session['messages'])} msgs) · "
                  f"{idx} kb chunks · {len(skills)} skills ({', '.join(skills) or 'none'}) · /help\n")

    while True:
        try:
            flags = "".join(f"[{x}]" for x in
                            ([RT["override"]] if RT["override"] else []) +
                            (["agent"] if RT["agent"] else []))
            user_input = input(f"[{session_name}]{flags} >> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nbye")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            console.print("bye")
            break

        if user_input.startswith("/"):
            bits = user_input.split(maxsplit=1)
            cmd = bits[0].lower()
            arg = bits[1].strip() if len(bits) > 1 else ""
            if cmd == "/help":
                console.print(HELP)
            elif cmd == "/models":
                for c, m in MODELS.items():
                    console.print(f"  {c:10} -> {m}")
            elif cmd == "/model":
                if arg in MODELS:
                    RT["override"] = arg
                    console.print(f"Forcing '{arg}' until /auto.")
                else:
                    console.print(f"Pick one of: {', '.join(MODELS)}")
            elif cmd == "/auto":
                RT["override"] = None
                console.print("Automatic routing on.")
            elif cmd == "/agent":
                RT["agent"] = not RT["agent"]
                console.print(f"Agent mode {'ON - model can use tools + skills' if RT['agent'] else 'off'}.")
            elif cmd == "/skills":
                if not skills:
                    console.print(f"  no skills in {SKILLS_DIR}")
                for name, mod in skills.items():
                    console.print(f"  [bold]{name}[/bold] - {getattr(mod, 'DESCRIPTION', '')[:90]}" if RICH
                                  else f"  {name} - {getattr(mod, 'DESCRIPTION', '')[:90]}")
                console.print("  (skills are callable in /agent mode)")
            elif cmd == "/reload":
                skills = load_skills()
                console.print(f"  reloaded {len(skills)} skills: {', '.join(skills) or 'none'}")
            elif cmd == "/rag":
                RT["rag"] = not RT["rag"]
                console.print(f"Knowledge base {'ON' if RT['rag'] else 'off'}.")
            elif cmd == "/index":
                if not arg:
                    console.print("Usage: /index <folder>")
                else:
                    cmd_index(arg)
            elif cmd == "/status-all":
                folder = arg or "~/Desktop/Cld"
                console.print(f"  Checking git status across {folder} ...\n")
                for row in cmd_status_all(folder):
                    console.print(f"  [{row['project']}]")
                    for line in row["status"].splitlines()[:6]:
                        console.print(f"    {line}")
                    console.print(f"    last: {row['last']}\n")
            elif cmd == "/file":
                if not arg:
                    console.print("Usage: /file <path>  (or /file clear)")
                else:
                    cmd_file(arg, session, session_name)
            elif cmd == "/image":
                cmd_image(arg, session, session_name)
            elif cmd == "/session":
                if not arg:
                    console.print("Usage: /session <name>")
                else:
                    save_session(session_name, session)
                    session_name = re.sub(r"[^\w-]", "_", arg)
                    session = load_session(session_name)
                    console.print(f"Switched to '{session_name}' ({len(session['messages'])} messages).")
            elif cmd == "/sessions":
                names = list_sessions()
                console.print("  " + (", ".join(names) if names else "(none yet)"))
            elif cmd == "/history":
                if not session["messages"]:
                    console.print("  (empty)")
                for m in session["messages"]:
                    who = "you" if m["role"] == "user" else " ai"
                    console.print(f"  {who}: {m['content'][:200]}")
            elif cmd == "/retry":
                if len(session["messages"]) >= 2 and session["messages"][-1]["role"] == "assistant":
                    session["messages"].pop()
                    handle_turn(session, session_name, session["messages"][-1]["content"], reuse_last=True)
                else:
                    console.print("Nothing to retry.")
            elif cmd == "/clear":
                session = {"summary": "", "messages": []}
                save_session(session_name, session)
                console.print("Conversation wiped.")
            else:
                console.print("Unknown command. /help for the list.")
            continue

        if RT["agent"]:
            agent_turn(session, session_name, user_input, skills)
        else:
            handle_turn(session, session_name, user_input)


if __name__ == "__main__":
    main()
