"""Files skill: list / read / write / move / delete files on the user's Mac."""
import os
import shutil

from _lib import confirm

NAME = "files"
DESCRIPTION = (
    "Work with files on the user's Mac. Actions: 'list' a directory, 'read' a text file, "
    "'write' (create/overwrite) a file, 'move' a file, 'delete' a file. "
    "Paths may start with ~ for the home directory. Destructive actions ask the user first."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "action":  {"type": "string", "enum": ["list", "read", "write", "move", "delete"]},
        "path":    {"type": "string", "description": "the target file or directory"},
        "dest":    {"type": "string", "description": "destination path (for move)"},
        "content": {"type": "string", "description": "text to write (for write)"},
    },
    "required": ["action", "path"],
}


def run(args):
    action = (args.get("action") or "").lower()
    path = os.path.expanduser(args.get("path", "") or "")
    if not path:
        if action == "list":          # small models often omit the path; default to home
            path = os.path.expanduser("~")
        else:
            return "Error: no path given (need a file path for read/write/move/delete)."
    try:
        if action == "list":
            base = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
            entries = sorted(os.listdir(base))
            return f"{base} ({len(entries)} items):\n" + "\n".join(entries[:200]) if entries else "(empty)"
        if action == "read":
            if not os.path.isfile(path):
                return f"No such file: {path}"
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()[:6000]
        if action == "write":
            content = args.get("content", "")
            if os.path.exists(path) and not confirm(f"overwrite {path}?"):
                return "User declined to overwrite."
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Wrote {len(content)} chars to {path}."
        if action == "move":
            dest = os.path.expanduser(args.get("dest", "") or "")
            if not dest:
                return "Error: move needs a 'dest'."
            if not confirm(f"move {path} -> {dest}?"):
                return "User declined the move."
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            shutil.move(path, dest)
            return f"Moved to {dest}."
        if action == "delete":
            if not os.path.exists(path):
                return f"Nothing to delete at {path}."
            if not confirm(f"DELETE {path}?"):
                return "User declined the delete."
            os.remove(path)
            return f"Deleted {path}."
        return f"Unknown action: {action}"
    except Exception as e:
        return f"Error: {e}"
