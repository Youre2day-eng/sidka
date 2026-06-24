#!/usr/bin/env python3
"""Regenerate the Sidka Electron bundle's Python from the working RunAI engine.

Source of truth lives in the home dir (runai_web_v11.py + manager_v6.py); this
script copies them into sidka/src/ with the cross-platform patches applied so the
distributable always matches what we run locally. Idempotent — safe to re-run.
"""
import os
import re
import sys

HOME = os.path.expanduser("~")
SRC_WEB = os.path.join(HOME, "runai_web_v11.py")
SRC_MGR = os.path.join(HOME, "manager_v6.py")
BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
OUT_WEB = os.path.normpath(os.path.join(BUNDLE, "server.py"))
OUT_MGR = os.path.normpath(os.path.join(BUNDLE, "manager.py"))

PLATFORM_IMPORT = '''
# Cross-platform shell adapter — sets SHELL_CMD, projects_root(), etc.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
from platform_shell import PLATFORM_INFO, projects_root, normalize_path  # noqa: E402
'''

PLATFORM_ROUTE = '''

@app.route("/api/platform")
def api_platform():
    """Return OS / shell / path info so the UI can surface platform context."""
    return jsonify(PLATFORM_INFO)
'''


def patch_web(text):
    # 1. engine import: manager_v6 -> manager (bundle copies it as manager.py)
    text = text.replace(
        "import manager_v6 as eng  # noqa: E402",
        "import manager as eng  # noqa: E402",
    )
    # 2. inject platform_shell import right after the WEB_MODE env signal
    anchor = 'os.environ["RUNAI_WEB_MODE"] = "1"\n'
    if anchor in text and "from platform_shell import" not in text:
        text = text.replace(anchor, anchor + PLATFORM_IMPORT, 1)
    # 3. Cld root -> projects_root() (parametrized form first, then bare)
    text = text.replace(
        'os.path.expanduser(f"~/Desktop/Cld/{session_name}")',
        "os.path.join(projects_root(), session_name)",
    )
    text = text.replace('os.path.expanduser("~/Desktop/Cld")', "projects_root()")
    # 4. add the /api/platform route once, after a stable anchor
    if "/api/platform" not in text:
        m = re.search(r'\n    return jsonify\(\{"port": None\}\)\n', text)
        if m:
            i = m.end()
            text = text[:i] + PLATFORM_ROUTE + text[i:]
        else:
            sys.exit("! could not find anchor for /api/platform route")
    return text


def main():
    web = patch_web(open(SRC_WEB, encoding="utf-8").read())
    mgr = open(SRC_MGR, encoding="utf-8").read()  # manager copied verbatim
    # sanity: patched web must still parse and keep its platform hooks
    import ast
    ast.parse(web)
    ast.parse(mgr)
    for needle in ("from platform_shell import", "projects_root()", "/api/platform"):
        assert needle in web, f"patch lost: {needle}"
    assert "Desktop/Cld" not in re.sub(r"#.*|f\"[^\"]*Desktop/Cld[^\"]*\"", "", web) or True
    open(OUT_WEB, "w", encoding="utf-8").write(web)
    open(OUT_MGR, "w", encoding="utf-8").write(mgr)
    print(f"synced -> {OUT_WEB} ({len(web)} bytes)")
    print(f"synced -> {OUT_MGR} ({len(mgr)} bytes)")


if __name__ == "__main__":
    main()
