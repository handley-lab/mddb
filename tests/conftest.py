import os
import subprocess

import pytest

import mddb


@pytest.fixture(autouse=True)
def _xdg_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(autouse=True, scope="session")
def _git_identity():
    if not _git_config("user.email"):
        os.environ["GIT_AUTHOR_NAME"] = "mddb test"
        os.environ["GIT_AUTHOR_EMAIL"] = "test@mddb"
        os.environ["GIT_COMMITTER_NAME"] = "mddb test"
        os.environ["GIT_COMMITTER_EMAIL"] = "test@mddb"


def _git_config(key):
    r = subprocess.run(
        ["git", "config", "--global", "--get", key], capture_output=True, text=True
    )
    return r.stdout.strip()


@pytest.fixture
def db(tmp_path):
    return mddb.MDDB(tmp_path)


@pytest.fixture
def seed(db):
    def _seed(**kwargs):
        rationale = kwargs.pop("rationale", "seed")
        with db.transaction(rationale=rationale) as tx:
            return tx.create(**kwargs)

    return _seed
