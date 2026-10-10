"""Where the database, caches and the source corpus live.

Everything sits under LDG_HOME (default ~/.local/share/luanti-depgraph).
LDG_DB and LDG_CORPUS override individual locations.
"""

import os
from pathlib import Path


def home() -> Path:
    if value := os.environ.get("LDG_HOME"):
        return Path(value).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "luanti-depgraph"


def db_path() -> Path:
    if value := os.environ.get("LDG_DB"):
        return Path(value).expanduser()
    return home() / "luanti-deps.sqlite"


def graph_path() -> Path:
    return db_path().with_suffix(".lbug")


def cache_dir() -> Path:
    return home() / "cache"


def corpus_dir() -> Path:
    if value := os.environ.get("LDG_CORPUS"):
        return Path(value).expanduser()
    return home() / "corpus"
