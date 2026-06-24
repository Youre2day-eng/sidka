DESCRIPTION = "Profile a Python script with cProfile and return the top slowest functions"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "profile_script",
            "description": DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to Python script (expanduser)"
                    },
                    "args": {
                        "type": "string",
                        "description": "Command-line args to pass to the script",
                        "default": ""
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "How many functions to show",
                        "default": 15
                    }
                },
                "required": ["path"]
            }
        }
    }
]


import os
import shlex
import subprocess


def run(args: dict) -> str:
    path = os.path.expanduser(args["path"])
    script_args = args.get("args", "")
    top_n = args.get("top_n", 15)

    cmd = ["python", "-m", "cProfile", "-s", "cumulative", path]
    if script_args:
        cmd += shlex.split(script_args)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120
        )
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        return "Timeout: profiling did not complete within 120 seconds."
    except FileNotFoundError:
        return f"Error: script not found at '{path}'."

    lines = stdout.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if "ncalls" in line and "cumtime" in line:
            header_idx = i
            break

    if header_idx is None:
        error_info = stderr[:500] if stderr else "No output captured."
        return f"Could not parse profiler output.\n{error_info}"

    table_lines = [lines[header_idx]] + lines[header_idx + 1: header_idx + 1 + top_n]
    table = "\n".join(table_lines)

    parts = [f"Top {top_n} functions by cumulative time:", "", table]
    if stderr:
        parts += ["", "Stderr:", stderr[:500]]
    return "\n".join(parts)
