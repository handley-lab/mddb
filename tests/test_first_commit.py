import subprocess

import pytest

import mddb
from mddb import _index


def git(root, *args, date=""):
    env = None
    if date:
        import os

        env = os.environ | {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout


def write_card(root, relpath, card_id):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: {card_id}\ntitle: {path.stem}\nsummary: s\n---\n")
    return path


def commit(root, message, date):
    git(root, "add", "-A")
    git(root, "commit", "-m", message, date=date)


def reopened(root):
    _index.cache_path(root).unlink()
    return mddb.MDDB(root)


def first_commit_of(db, relpath):
    return db.conn.execute(
        "SELECT first_commit FROM entries WHERE relpath = ?", (relpath,)
    ).fetchone()[0]


def test_rebuild_populates_canonical_utc_dates(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    write_card(root, "a.md", "a" * 32)
    commit(root, "add: a", "2024-03-01T10:00:00+02:00")
    write_card(root, "b.md", "b" * 32)
    commit(root, "add: b", "2025-06-02T09:30:00+01:00")
    db = mddb.MDDB(root)
    assert first_commit_of(db, "a.md") == "2024-03-01T08:00:00+00:00"
    assert first_commit_of(db, "b.md") == "2025-06-02T08:30:00+00:00"


def test_editor_created_card_gets_commit_author_date(db):
    with db.editor(rationale="create card to check first_commit stamping") as editor:
        card = editor.create(title="Fresh", summary="s")
    stored = db.conn.execute(
        "SELECT first_commit FROM entries WHERE id = ?", (card.id,)
    ).fetchone()[0]
    head_date = _index.utc_iso(git(db.root, "log", "-1", "--format=%aI").strip())
    assert stored == head_date
    assert stored.endswith("+00:00")


def test_rename_keeps_original_date(db):
    with db.editor(rationale="create card that will be renamed") as editor:
        card = editor.create(title="Original", summary="s")
    original = db.conn.execute(
        "SELECT first_commit FROM entries WHERE id = ?", (card.id,)
    ).fetchone()[0]
    with db.editor(rationale="rename the card") as editor:
        editor.move(card.id, "renamed.md")
    db2 = reopened(db.root)
    assert first_commit_of(db2, "renamed.md") == original


def test_delete_then_readd_resets_to_the_readd(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    write_card(root, "x.md", "d" * 32)
    commit(root, "add: x", "2023-01-01T00:00:00+00:00")
    (root / "x.md").unlink()
    commit(root, "delete: x", "2023-06-01T00:00:00+00:00")
    write_card(root, "x.md", "e" * 32)
    commit(root, "add: x again", "2024-01-01T00:00:00+00:00")
    db = mddb.MDDB(root)
    assert first_commit_of(db, "x.md") == "2024-01-01T00:00:00+00:00"


def test_uncommitted_file_crashes_rebuild(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    write_card(root, "a.md", "a" * 32)
    commit(root, "add: a", "2024-01-01T00:00:00+00:00")
    write_card(root, "stray.md", "f" * 32)
    with pytest.raises(ValueError, match="no git lineage"):
        _index.rebuild_index(root)


def test_bootstrap_empty_deck_no_ops(tmp_path):
    db = mddb.MDDB.init(tmp_path / "fresh")
    assert db.list() == []


def test_stale_schema_version_forces_rebuild(db, seed):
    card = seed(title="Survivor", summary="s")
    with db.conn:
        db.conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    db.conn.close()
    db2 = mddb.MDDB(db.root)
    stored = db2.conn.execute(
        "SELECT first_commit FROM entries WHERE id = ?", (card.id,)
    ).fetchone()[0]
    assert stored.endswith("+00:00")


def test_side_branch_delete_of_kept_file_preserves_lineage(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    write_card(root, "kept.md", "a" * 32)
    commit(root, "add: kept", "2022-05-05T00:00:00+00:00")
    git(root, "checkout", "-q", "-b", "side")
    (root / "kept.md").unlink()
    commit(root, "delete on side branch", "2022-06-06T00:00:00+00:00")
    git(root, "checkout", "-q", "master")
    write_card(root, "other.md", "b" * 32)
    commit(root, "add: other", "2022-07-07T00:00:00+00:00")
    git(root, "merge", "-q", "-s", "ours", "side", "-m", "merge, keeping kept.md")
    db = mddb.MDDB(root)
    assert first_commit_of(db, "kept.md") == "2022-05-05T00:00:00+00:00"


def test_side_branch_add_discarded_by_merge_creates_no_lineage(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    write_card(root, "base.md", "a" * 32)
    commit(root, "add: base", "2022-01-01T00:00:00+00:00")
    git(root, "checkout", "-q", "-b", "side")
    write_card(root, "discarded.md", "c" * 32)
    commit(root, "add on side branch", "2022-02-02T00:00:00+00:00")
    git(root, "checkout", "-q", "master")
    git(root, "merge", "-q", "--no-ff", "-s", "ours", "side", "-m", "merge, discarding")
    db = mddb.MDDB(root)
    assert not (root / "discarded.md").exists()
    relpaths = [r[0] for r in db.conn.execute("SELECT relpath FROM entries")]
    assert relpaths == ["base.md"]


def test_rename_through_merge_preserves_original_date(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    write_card(root, "before.md", "a" * 32)
    commit(root, "add: before", "2021-03-03T00:00:00+00:00")
    git(root, "checkout", "-q", "-b", "side")
    git(root, "mv", "before.md", "after.md")
    commit(root, "rename on side branch", "2021-04-04T00:00:00+00:00")
    git(root, "checkout", "-q", "master")
    write_card(root, "other.md", "b" * 32)
    commit(root, "add: other", "2021-05-05T00:00:00+00:00")
    git(root, "merge", "-q", "side", "-m", "merge the rename")
    db = mddb.MDDB(root)
    assert first_commit_of(db, "after.md") == "2021-03-03T00:00:00+00:00"


def test_copy_does_not_inherit_source_date(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    body = "\n".join(f"shared boilerplate line {n}" for n in range(40))
    (root / "source.md").write_text(
        f"---\nid: {'a' * 32}\ntitle: source\nsummary: s\n---\n{body}\n"
    )
    commit(root, "add: source", "2020-01-01T00:00:00+00:00")
    (root / "copy.md").write_text(
        f"---\nid: {'b' * 32}\ntitle: copy\nsummary: s\n---\n{body}\n"
    )
    commit(root, "add: copy of source", "2024-12-31T00:00:00+00:00")
    db = mddb.MDDB(root)
    assert first_commit_of(db, "copy.md") == "2024-12-31T00:00:00+00:00"


def test_delete_vs_move_merged_keeping_move_dates_from_the_add(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    write_card(root, "foo.md", "a" * 32)
    commit(root, "add: foo", "2019-02-02T00:00:00+00:00")
    git(root, "checkout", "-q", "-b", "side")
    (root / "archive").mkdir()
    git(root, "mv", "foo.md", "archive/foo.md")
    commit(root, "move foo into archive", "2019-08-08T00:00:00+00:00")
    git(root, "checkout", "-q", "master")
    (root / "foo.md").unlink()
    commit(root, "delete foo as obsolete", "2019-05-05T00:00:00+00:00")
    subprocess.run(["git", "-C", str(root), "merge", "--no-commit", "side"])
    git(root, "checkout", "side", "--", "archive/foo.md")
    (root / "foo.md").unlink(missing_ok=True)
    commit(root, "resolve: keep the moved card", "2019-09-09T00:00:00+00:00")
    db = mddb.MDDB(root)
    assert first_commit_of(db, "archive/foo.md") == "2019-02-02T00:00:00+00:00"


def test_unrelated_history_merge_preserves_both_sides(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "master")
    write_card(main, "old.md", "a" * 32)
    commit(main, "add: old", "2020-01-01T00:00:00+00:00")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "master")
    write_card(other, "events/imported.md", "b" * 32)
    commit(other, "import: event", "2026-06-01T00:00:00+00:00")
    git(main, "fetch", "-q", str(other), "master")
    git(
        main,
        "merge",
        "-q",
        "--allow-unrelated-histories",
        "FETCH_HEAD",
        "-m",
        "fold the other deck in",
    )
    db = mddb.MDDB(main)
    assert first_commit_of(db, "old.md") == "2020-01-01T00:00:00+00:00"
    assert first_commit_of(db, "events/imported.md") == "2026-06-01T00:00:00+00:00"
