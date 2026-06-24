"""Clipboard skill — read from or write to the macOS clipboard via pbpaste/pbcopy."""
import subprocess

NAME = "clipboard"
DESCRIPTION = "Read text from or write text to the macOS clipboard. Read: get what's currently copied. Write: put text on the clipboard."
SCHEMA = {
    "type": "object",
    "properties": {
        "action":  {"type": "string", "enum": ["read", "write"], "description": "'read' returns clipboard contents, 'write' sets it"},
        "content": {"type": "string", "description": "text to write to clipboard (required for write action)"},
    },
    "required": ["action"],
}
TOOLS = [{"type": "function", "function": {"name": NAME, "description": DESCRIPTION, "parameters": SCHEMA}}]


def run(args):
    action = (args.get("action") or "").lower()

    if action == "read":
        try:
            result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            text = result.stdout
            if not text:
                return "(clipboard is empty)"
            return text[:6000] + ("... (truncated)" if len(text) > 6000 else "")
        except Exception as e:
            return f"Error reading clipboard: {e}"

    if action == "write":
        content = args.get("content", "")
        try:
            subprocess.run(["pbcopy"], input=content, text=True, check=True, timeout=5)
            return f"Wrote {len(content)} chars to clipboard."
        except Exception as e:
            return f"Error writing clipboard: {e}"

    return f"Unknown action: {action}. Use 'read' or 'write'."
