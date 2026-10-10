import shutil
from pathlib import Path

import pytest

from luanti_depgraph import db
from luanti_depgraph.corpus import MARKER
from luanti_depgraph.resolve import Index

FIXTURES = Path(__file__).parent / "fixtures"
MODS = FIXTURES / "mods"


@pytest.fixture(scope="session")
def db_file(tmp_path_factory) -> Path:
    """A database built offline from the synthetic ContentDB fixtures, with one corpus package."""
    root = tmp_path_factory.mktemp("ldg")
    raw = root / "cache" / "raw"
    raw.mkdir(parents=True)
    for f in (FIXTURES / "contentdb").glob("*.json"):
        shutil.copy(f, raw / f.name)
    corpus = root / "corpus"
    pkg_dir = corpus / "mt-mods" / "pipeworks"
    shutil.copytree(MODS / "my_modpack", pkg_dir)
    (pkg_dir / MARKER).write_text("201 test\n")
    path = root / "luanti-deps.sqlite"
    db.build(path, root / "cache", corpus, offline=True, log=lambda _msg: None)
    return path


@pytest.fixture()
def index(db_file) -> Index:
    return Index(db.connect(db_file))
