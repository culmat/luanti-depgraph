"""Which commit of luanti-depgraph is running: recorded in the database and the explorer.

Known only when running from a git checkout of this repository (as in GitHub Actions);
installed from PyPI there is no history, and the result is empty.
"""

import subprocess
from pathlib import Path

SOURCE = Path(__file__).resolve().parent


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=SOURCE, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def source_commit() -> dict:
    """{"commit": sha, "commit_date": ISO date} of the running code, or {} when unknown."""
    top = _git("rev-parse", "--show-toplevel")
    # An installed package can sit inside some other repository (a project's .venv).
    if not top or Path(top).resolve() / "src" / "luanti_depgraph" != SOURCE:
        return {}
    sha = _git("rev-parse", "HEAD")
    date = _git("log", "-1", "--format=%cI", "HEAD")
    return {k: v for k, v in (("commit", sha), ("commit_date", date)) if v}
