"""macOS notification skill — sends a system notification via osascript."""
import subprocess

NAME = "notify"
DESCRIPTION = "Send a macOS system notification to DJ. Use when a long agent task finishes or something needs attention."
SCHEMA = {
    "type": "object",
    "properties": {
        "title":    {"type": "string", "description": "notification title"},
        "message":  {"type": "string", "description": "body text"},
        "subtitle": {"type": "string", "description": "optional subtitle line"},
    },
    "required": ["title", "message"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]


def run(args):
    title    = str(args.get("title", "RunAI"))
    message  = str(args.get("message", ""))
    subtitle = str(args.get("subtitle", ""))

    parts = [f'display notification {_q(message)} with title {_q(title)}']
    if subtitle:
        parts[0] += f' subtitle {_q(subtitle)}'

    script = "\n".join(parts)
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=5, capture_output=True)
        return f"Notification sent: {title} — {message}"
    except subprocess.CalledProcessError as e:
        return f"osascript error: {e.stderr.decode().strip()}"
    except Exception as e:
        return f"Error: {e}"


def _q(s):
    return '"' + str(s).replace('"', '\\"') + '"'
