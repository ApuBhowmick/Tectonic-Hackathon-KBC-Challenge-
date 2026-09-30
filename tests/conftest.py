"""Shared fixtures: a temp DB built from the real CSVs, with and without the D001-D007 hard cases."""
import importlib.util
import shutil
import sqlite3
import sys
from pathlib import Path

import bcrypt
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _load_script(name: str):
    return importlib.import_module(name)  # scripts/ is on sys.path


@pytest.fixture(scope="session")
def base_db(tmp_path_factory) -> Path:
    """Only the 400 imported customers (no hard cases)."""
    mod = _load_script("import_data")
    mod.hash_pin = lambda pin: bcrypt.hashpw(pin.encode(), bcrypt.gensalt(rounds=4)).decode()  # fast
    path = tmp_path_factory.mktemp("db") / "base.db"
    mod.main(path, verbose=False)
    return path


@pytest.fixture(scope="session")
def db_file(base_db, tmp_path_factory) -> Path:
    """Imported customers plus D001-D007."""
    path = tmp_path_factory.mktemp("db") / "full.db"
    shutil.copy(base_db, path)
    _load_script("add_demo_customers").main(path, verbose=False)
    return path


@pytest.fixture()
def conn(db_file):
    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture()
def base_conn(base_db):
    c = sqlite3.connect(base_db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()
