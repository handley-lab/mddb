import subprocess
import sys

import pytest
import yaml

import mddb
from mddb._index import rebuild_index
from mddb._merge import install
from mddb.card import Card

ID = "11111111-1111-4111-8111-111111111111"


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _make_deck(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@mddb")
    _git(root, "config", "user.name", "mddb test")
    return root


def _register(root):
    _git(
        root,
        "config",
        "merge.mddb-card.driver",
        f"{sys.executable} -m mddb._merge %O %A %B %P",
    )
    (root / ".gitattributes").write_text("*.md merge=mddb-card\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-q", "-m", "register merge driver")


def _write_card(root, name, yaml_lines, body):
    (root / name).write_text(f"---\n{yaml_lines}---\n{body}")
    _git(root, "add", name)


def _seed(root, name, body):
    _write_card(
        root,
        name,
        f"id: {ID}\ntitle: Card\nsummary: base\ntags:\n- a\n",
        body,
    )
    _git(root, "commit", "-q", "-m", "seed card")


def _branch_edit_merge(root, name, ours_yaml, ours_body, theirs_yaml, theirs_body):
    """Diverge `name` on master (ours) and a branch (theirs), then merge into master."""
    _git(root, "branch", "other")
    _write_card(root, name, ours_yaml, ours_body)
    _git(root, "commit", "-q", "-m", "ours edit")
    _git(root, "checkout", "-q", "other")
    _write_card(root, name, theirs_yaml, theirs_body)
    _git(root, "commit", "-q", "-m", "theirs edit")
    _git(root, "checkout", "-q", "master")
    return _git(root, "merge", "--no-edit", "other", check=False)


def test_e2e_non_conflicting_divergence_clean_merge(tmp_path):
    root = _make_deck(tmp_path)
    _register(root)
    _seed(root, "card.md", "L1\nL2\nL3\n")
    merge = _branch_edit_merge(
        root,
        "card.md",
        f"id: {ID}\ntitle: Card\nsummary: from-ours\ntags:\n- a\n- m\n",
        "M1\nL2\nL3\n",
        f"id: {ID}\ntitle: Card\nsummary: base\ntags:\n- a\n- b\n",
        "L1\nL2\nB3\n",
    )
    assert merge.returncode == 0
    card = Card.from_text((root / "card.md").read_text())
    assert card.yaml["tags"] == ["a", "m", "b"]
    assert card.yaml["summary"] == "from-ours"
    assert card.body == "M1\nL2\nB3\n"


def test_e2e_control_without_driver_mangles_frontmatter(tmp_path):
    root = _make_deck(tmp_path)
    _seed(root, "card.md", "L1\nL2\nL3\n")
    merge = _branch_edit_merge(
        root,
        "card.md",
        f"id: {ID}\ntitle: Card\nsummary: base\ntags:\n- a\n- m\n",
        "L1\nL2\nL3\n",
        f"id: {ID}\ntitle: Card\nsummary: base\ntags:\n- a\n- b\n",
        "L1\nL2\nL3\n",
    )
    assert merge.returncode != 0
    assert "<<<<<<<" in (root / "card.md").read_text()


def test_e2e_conflicting_scalar_marks_frontmatter(tmp_path):
    root = _make_deck(tmp_path)
    _register(root)
    _seed(root, "card.md", "body\n")
    merge = _branch_edit_merge(
        root,
        "card.md",
        f"id: {ID}\ntitle: Card\nsummary: ours-summary\ntags:\n- a\n",
        "body\n",
        f"id: {ID}\ntitle: Card\nsummary: theirs-summary\ntags:\n- a\n",
        "body\n",
    )
    assert merge.returncode != 0
    text = (root / "card.md").read_text()
    assert "ours-summary" in text and "theirs-summary" in text
    assert "<<<<<<<" in text


def test_e2e_same_line_body_conflict_marks_body(tmp_path):
    root = _make_deck(tmp_path)
    _register(root)
    _seed(root, "card.md", "shared line\n")
    merge = _branch_edit_merge(
        root,
        "card.md",
        f"id: {ID}\ntitle: Card\nsummary: base\ntags:\n- a\n",
        "ours line\n",
        f"id: {ID}\ntitle: Card\nsummary: base\ntags:\n- a\n",
        "theirs line\n",
    )
    assert merge.returncode != 0
    parsed = Card.from_text((root / "card.md").read_text())
    assert parsed.yaml["summary"] == "base"
    assert "<<<<<<<" in parsed.body


def test_e2e_add_add_distinct_ids_conflict(tmp_path):
    root = _make_deck(tmp_path)
    _register(root)
    _git(root, "commit", "-q", "--allow-empty", "-m", "root")
    _git(root, "branch", "other")
    _write_card(root, "new.md", f"id: {ID}\ntitle: Mine\nsummary: ours\n", "ours\n")
    _git(root, "commit", "-q", "-m", "ours adds new.md")
    _git(root, "checkout", "-q", "other")
    _write_card(
        root,
        "new.md",
        "id: 22222222-2222-4222-8222-222222222222\ntitle: Theirs\nsummary: theirs\n",
        "theirs\n",
    )
    _git(root, "commit", "-q", "-m", "theirs adds new.md")
    _git(root, "checkout", "-q", "master")
    merge = _git(root, "merge", "--no-edit", "other", check=False)
    assert merge.returncode != 0
    assert "<<<<<<<" in (root / "new.md").read_text()


def test_install_idempotent(tmp_path):
    db = mddb.MDDB.init(tmp_path / "deck")
    install(db.root)
    install(db.root)
    assert (db.root / ".gitattributes").read_text().count("*.md merge=mddb-card") == 1
    driver = _git(db.root, "config", "merge.mddb-card.driver").stdout.strip()
    assert driver == "mddb-merge %O %A %B %P"


def test_install_preserves_existing_and_distinguishes_lookalikes(tmp_path):
    """Existing lines survive; a no-trailing-newline last line and commented/
    path-prefixed lookalikes do not suppress the exact `*.md merge=mddb-card` rule."""
    db = mddb.MDDB.init(tmp_path / "deck")
    (db.root / ".gitattributes").write_text(
        "*.png filter=lfs diff=lfs merge=lfs -text\n"
        "# *.md merge=mddb-card\n"
        "docs/*.md merge=mddb-card"
    )
    install(db.root)
    install(db.root)
    lines = (db.root / ".gitattributes").read_text().splitlines()
    assert "*.png filter=lfs diff=lfs merge=lfs -text" in lines
    assert "# *.md merge=mddb-card" in lines
    assert "docs/*.md merge=mddb-card" in lines
    assert lines.count("*.md merge=mddb-card") == 1


def test_conflicted_frontmatter_breaks_reads_and_rebuild(tmp_path):
    db = mddb.MDDB.init(tmp_path / "deck")
    with db.editor(rationale="seed card") as editor:
        card = editor.create(title="Card", summary="base", relpath="card.md")
    conflicted = (
        f"---\nid: {card.id}\ntitle: Card\nsummary: "
        "<<<<<<< card.md (ours)\nours\n=======\ntheirs\n>>>>>>> card.md (theirs)\n"
        "---\nbody\n"
    )
    (db.root / "card.md").write_text(conflicted)
    with pytest.raises(yaml.YAMLError):
        Card.from_file(db.root / "card.md")
    with pytest.raises(yaml.YAMLError):
        db.read(card.id)
    with pytest.raises(yaml.YAMLError):
        rebuild_index(db.root)
