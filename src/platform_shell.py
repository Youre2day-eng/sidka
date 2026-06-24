"""
Cross-platform shell execution adapter for Sidka.

Usage in skills or server code:
    from platform_shell import run_shell, shell_cmd, is_windows, normalize_path

    result = run_shell("git status")          # works on Mac, Linux, Windows
    result = run_shell("ls ~/Desktop/Cld")    # tilde expanded on all platforms
"""
import os
import sys
import subprocess
import platform
import shutil

# ── platform detection ────────────────────────────────────────────────────────
is_windows = sys.platform == "win32"
is_mac     = sys.platform == "darwin"
is_linux   = sys.platform.startswith("linux")

# ── shell detection ───────────────────────────────────────────────────────────
def _detect_shell():
    if is_windows:
        # Prefer PowerShell Core (pwsh), fall back to Windows PowerShell, then cmd
        if shutil.which("pwsh"):
            return ["pwsh", "-NoProfile", "-Command"]
        if shutil.which("powershell"):
            return ["powershell", "-NoProfile", "-Command"]
        return ["cmd", "/c"]
    # Mac / Linux
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell and shutil.which("zsh"):
        return ["zsh", "-c"]
    if shutil.which("bash"):
        return ["bash", "-c"]
    return ["sh", "-c"]

SHELL_CMD = _detect_shell()

# ── path helpers ──────────────────────────────────────────────────────────────
def normalize_path(path: str) -> str:
    """Expand ~ and env vars; return a clean absolute path string."""
    return os.path.normpath(os.path.expandvars(os.path.expanduser(path)))

def home() -> str:
    return os.path.expanduser("~")

def runai_dir() -> str:
    return os.path.join(home(), ".runai")

def projects_root() -> str:
    """Primary code workspace — ~/Desktop/Cld on Mac, ~/Documents/Cld on Windows."""
    if is_windows:
        return os.path.join(home(), "Documents", "Cld")
    return os.path.join(home(), "Desktop", "Cld")

# ── shell runner ──────────────────────────────────────────────────────────────
def run_shell(
    cmd: str,
    cwd: str = None,
    timeout: int = 30,
    env: dict = None,
) -> tuple[int, str, str]:
    """
    Run a shell command cross-platform.

    Returns (returncode, stdout, stderr).
    Raises subprocess.TimeoutExpired on timeout.
    """
    if cwd:
        cwd = normalize_path(cwd)

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    # On Windows, expand ~ in the command string manually
    if is_windows:
        cmd = cmd.replace("~/", home().replace("\\", "/") + "/")
        cmd = cmd.replace("~\\", home() + "\\")

    result = subprocess.run(
        SHELL_CMD + [cmd],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        env=merged_env,
    )
    return result.returncode, result.stdout, result.stderr


def check_output(cmd: str, cwd: str = None, timeout: int = 15) -> str:
    """Run command, return stdout on success or raise on error."""
    code, out, err = run_shell(cmd, cwd=cwd, timeout=timeout)
    if code != 0:
        raise RuntimeError(err.strip() or f"Command failed: {cmd}")
    return out.strip()


# ── git helper ────────────────────────────────────────────────────────────────
def git_available() -> bool:
    return shutil.which("git") is not None


# ── python helper ─────────────────────────────────────────────────────────────
def python_bin() -> str:
    """Return the Python executable that should be used to run subprocesses."""
    venv = os.path.join(home(), ".runai-venv", "bin",
                        "python.exe" if is_windows else "python")
    if os.path.exists(venv):
        return venv
    return "python" if is_windows else "python3"


# ── startup info ──────────────────────────────────────────────────────────────
PLATFORM_INFO = {
    "os":       platform.system(),
    "version":  platform.version(),
    "machine":  platform.machine(),
    "shell":    " ".join(SHELL_CMD),
    "home":     home(),
    "projects": projects_root(),
    "runai":    runai_dir(),
    "git":      git_available(),
    "python":   python_bin(),
}
