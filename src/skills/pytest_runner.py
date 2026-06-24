DESCRIPTION = "Run pytest on a Python project and return pass/fail results"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to project or test file (expanduser)"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "-k filter pattern"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds before killing",
                        "default": 60
                    }
                },
                "required": ["path"]
            }
        }
    }
]


import os
import re
import subprocess


def run(args: dict) -> str:
    path = os.path.expanduser(args["path"])
    pattern = args.get("pattern")
    timeout = args.get("timeout", 60)

    cmd = ["python", "-m", "pytest", "-v", "--tb=short", path]
    if pattern:
        cmd += ["-k", pattern]

    cwd = path if os.path.isdir(path) else os.path.dirname(path)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout
        )
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        return f"Timeout: tests did not complete within {timeout} seconds."
    except FileNotFoundError:
        return f"Error: could not find Python or pytest at path '{path}'."

    summary = ""
    for line in stdout.splitlines():
        m = re.search(r"(\d+ passed|\d+ failed|\d+ error)", line)
        if m:
            summary = line.strip()

    if not summary:
        counts = {"passed": 0, "failed": 0, "errors": 0}
        for line in stdout.splitlines():
            if "PASSED" in line:
                counts["passed"] += 1
            elif "FAILED" in line:
                counts["failed"] += 1
            elif "ERROR" in line:
                counts["errors"] += 1
        summary = f"{counts['passed']} passed, {counts['failed']} failed, {counts['errors']} errors"

    tail = stdout[-3000:] if len(stdout) > 3000 else stdout
    stderr_snippet = stderr[:500] if stderr else ""

    parts = [f"Summary: {summary}", "", tail]
    if stderr_snippet:
        parts += ["", "Stderr:", stderr_snippet]
    return "\n".join(parts)
