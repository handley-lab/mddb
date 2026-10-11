import fcntl
import sqlite3
import subprocess

import pytest

import mddb
from mddb import _index


def test_current_index_is_readonly_and_does_not_open_rebuilding_path(
    db, seed, monkeypatch
):
    card = seed(title="Current", summary="Indexed card", body="body")

    def forbidden(*args, **kwargs):
        pytest.fail("current-index reader entered the rebuilding path")

    monkeypatch.setattr(_index, "open_index", forbidden)
    monkeypatch.setattr(_index, "rebuild_index", forbidden)
    monkeypatch.setattr(_index, "refresh_index", forbidden)
    conn = mddb.open_current_index(db.root)
    try:
        assert conn.in_transaction
        assert conn.execute("SELECT id FROM entries").fetchall() == [(card.id,)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM entries")
    finally:
        conn.close()


def test_missing_index_is_not_created(db):
    path = _index.cache_path(db.root)
    db.conn.close()
    path.unlink()
    with pytest.raises(mddb.IndexUnavailable, match="index is missing"):
        mddb.open_current_index(db.root)
    assert not path.exists()


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("schema_version", "old", "schema"),
        ("git_head", "old", "HEAD"),
    ],
)
def test_stale_index_is_not_rebuilt(db, key, value, reason):
    with db.conn:
        db.conn.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
    path = _index.cache_path(db.root)
    before = path.read_bytes()
    with pytest.raises(mddb.IndexUnavailable, match=f"index {reason} is stale"):
        mddb.open_current_index(db.root)
    assert path.read_bytes() == before


def test_advanced_git_head_is_not_refreshed(db):
    path = _index.cache_path(db.root)
    before = path.read_bytes()
    subprocess.run(
        ["git", "-C", str(db.root), "commit", "--allow-empty", "-qm", "Advance"],
        check=True,
    )
    with pytest.raises(mddb.IndexUnavailable, match="index HEAD is stale"):
        mddb.open_current_index(db.root)
    assert path.read_bytes() == before
    opened = mddb.MDDB(db.root)
    opened.conn.close()
    mddb.open_current_index(db.root).close()


def test_deck_writer_causes_immediate_failure_instead_of_wait(db, monkeypatch):
    with _index.deck_lock(db.root):
        flock = fcntl.flock

        def nonblocking(fd, operation):
            assert operation & fcntl.LOCK_NB
            flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", nonblocking)
        with pytest.raises(mddb.IndexUnavailable, match="deck writer holds"):
            mddb.open_current_index(db.root)
    mddb.open_current_index(db.root).close()


def test_current_readers_can_share_the_deck_lock(db):
    with _index.deck_lock(db.root, fcntl.LOCK_SH):
        mddb.open_current_index(db.root).close()


def test_read_snapshot_survives_index_replacement(db, seed):
    card = seed(title="Current", summary="Indexed card", body="body")
    conn = mddb.open_current_index(db.root)
    try:
        db.conn.close()
        _index.cache_path(db.root).unlink()
        opened = mddb.MDDB(db.root)
        opened.conn.close()
        assert conn.execute("SELECT id FROM entries").fetchall() == [(card.id,)]
    finally:
        conn.close()
