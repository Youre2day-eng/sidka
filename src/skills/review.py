"""Review skill — self-review loop: syntax checks, second-pass LLM critique, and output verification."""
import os, subprocess, tempfile

NAME = "review"
DESCRIPTION = (
    "Self-review skill. Use after generating code or completing a task to check your own work. "
    "Actions: critique (second LLM pass to find issues), syntax (parse/compile check), diff (compare expected vs actual). "
    "Returns a list of issues found, or 'LGTM' if nothing significant."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["critique", "syntax", "diff"],
            "description": "critique = LLM self-review; syntax = parse/compile check; diff = compare expected vs actual output",
        },
        "text": {"type": "string", "description": "The code or text to review"},
        "language": {"type": "string", "description": "Language hint for syntax check: python | javascript | typescript | json"},
        "expected": {"type": "string", "description": "For diff action: what you expected the output to be"},
        "actual": {"type": "string", "description": "For diff action: what was actually produced"},
        "context": {"type": "string", "description": "Optional: the original task/question this output was supposed to answer"},
    },
    "required": ["action"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]

_WEB_MODE = bool(os.environ.get("RUNAI_WEB_MODE"))


def run(args):
    action   = (args.get("action") or "critique").lower()
    text     = (args.get("text") or "").strip()
    language = (args.get("language") or "").lower()
    context  = (args.get("context") or "").strip()

    if action == "syntax":
        return _syntax_check(text, language)

    if action == "diff":
        expected = (args.get("expected") or "").strip()
        actual   = (args.get("actual") or "").strip()
        return _diff_check(expected, actual)

    if action == "critique":
        return _critique(text, context, language)

    return f"Unknown action: {action}"


def _syntax_check(text, language):
    if not text:
        return "No text provided for syntax check."

    issues = []

    if language in ("python", "py", ""):
        # try ast.parse
        try:
            import ast
            ast.parse(text)
            issues.append("Python: syntax OK")
        except SyntaxError as e:
            issues.append(f"Python SyntaxError: line {e.lineno}: {e.msg}")

    if language in ("javascript", "js", "typescript", "ts"):
        # try node --check if available
        try:
            with tempfile.NamedTemporaryFile(suffix=".js", mode="w", delete=False) as f:
                f.write(text)
                fname = f.name
            r = subprocess.run(["node", "--check", fname], capture_output=True, text=True, timeout=5)
            os.unlink(fname)
            if r.returncode == 0:
                issues.append("JS/Node: syntax OK")
            else:
                issues.append(f"JS SyntaxError: {r.stderr.strip()[:400]}")
        except FileNotFoundError:
            issues.append("JS: node not found, skipping syntax check")
        except Exception as e:
            issues.append(f"JS check error: {e}")

    if language == "json":
        try:
            import json
            json.loads(text)
            issues.append("JSON: valid")
        except json.JSONDecodeError as e:
            issues.append(f"JSON error: {e}")

    if language in ("svg", "html", "xml"):
        try:
            import xml.etree.ElementTree as ET
            ET.fromstring(text)
            issues.append(f"{language.upper()}: well-formed XML/HTML")
        except ET.ParseError as e:
            issues.append(f"{language.upper()} parse error: {e}")

    return "\n".join(issues) if issues else "No syntax checks ran (specify language)."


def _diff_check(expected, actual):
    if not expected or not actual:
        return "Need both expected and actual for diff."
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    issues = []
    if expected.strip() == actual.strip():
        return "LGTM — output matches expected exactly."
    missing = [l for l in exp_lines if l.strip() and l not in act_lines]
    extra   = [l for l in act_lines if l.strip() and l not in exp_lines]
    if missing:
        issues.append(f"Missing from output ({len(missing)} lines):\n" + "\n".join(f"  - {l}" for l in missing[:5]))
    if extra:
        issues.append(f"Extra in output ({len(extra)} lines):\n" + "\n".join(f"  + {l}" for l in extra[:5]))
    return "\n\n".join(issues) if issues else "Minor whitespace/ordering difference only."


def _critique(text, context, language):
    if not text:
        return "No text provided for critique."

    # Static heuristics first (fast, no LLM cost)
    issues = _static_checks(text, language)

    # Second-pass LLM critique via Ollama
    llm_feedback = _llm_critique(text, context, language)
    if llm_feedback:
        issues.append(llm_feedback)

    if not issues:
        return "LGTM — no significant issues found."
    return "\n\n".join(issues)


def _static_checks(text, language):
    issues = []
    lines = text.splitlines()

    # Generic checks
    todo_lines = [f"  line {i+1}: {l.strip()}" for i, l in enumerate(lines)
                  if any(t in l.upper() for t in ("TODO", "FIXME", "HACK", "XXX", "PLACEHOLDER"))]
    if todo_lines:
        issues.append("Unresolved TODOs/FIXMEs:\n" + "\n".join(todo_lines[:5]))

    hardcoded = [f"  line {i+1}: {l.strip()}" for i, l in enumerate(lines)
                 if any(t in l for t in ("localhost", "127.0.0.1", "password=", "secret=", "api_key="))]
    if hardcoded:
        issues.append("Possible hardcoded values (verify these are intentional):\n" + "\n".join(hardcoded[:3]))

    # Code-specific checks
    if language in ("python", "py", ""):
        bare_except = [f"  line {i+1}" for i, l in enumerate(lines) if l.strip() == "except:"]
        if bare_except:
            issues.append(f"Bare except clauses (should specify exception type): {', '.join(bare_except)}")
        unused_import = [l.strip() for l in lines if l.strip().startswith("import ") and
                         l.split()[-1] not in text.replace(l, "")]
        if unused_import:
            issues.append(f"Possibly unused imports: {', '.join(unused_import[:3])}")

    if language in ("javascript", "js", "typescript", "ts"):
        console_logs = [f"line {i+1}" for i, l in enumerate(lines) if "console.log" in l]
        if len(console_logs) > 3:
            issues.append(f"Many console.log statements ({len(console_logs)}) — remove debug output before shipping")
        anys = sum(1 for l in lines if ": any" in l or "<any>" in l)
        if any(language == "ts") and anys > 2:
            issues.append(f"{anys} uses of `any` type — consider more specific types")

    return issues


def _llm_critique(text, context, language):
    if not _WEB_MODE:
        return ""
    try:
        import ollama
        prompt = "Review the following"
        if language:
            prompt += f" {language}"
        prompt += " output and identify any significant issues, bugs, or improvements needed.\n"
        if context:
            prompt += f"The task was: {context}\n"
        prompt += f"\nOutput to review:\n{text[:3000]}\n\n"
        prompt += ("List only REAL issues (not style preferences). "
                   "If it looks correct and complete, say 'LGTM'. "
                   "Be concise — max 5 bullet points.")
        resp = ollama.generate(model="qwen2.5:1.5b", prompt=prompt, options={"temperature": 0.2})
        feedback = (resp.response or "").strip()
        if feedback and feedback.upper() != "LGTM":
            return f"LLM review:\n{feedback[:800]}"
    except Exception:
        pass
    return ""
