import subprocess
import sys

import pytest

from mddb._merge import conflict_rationales

ID = "11111111-1111-4111-8111-111111111111"


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _deck(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@mddb")
    _git(root, "config", "user.name", "mddb test")
    _git(
        root,
        "config",
        "merge.mddb-card.driver",
        f"{sys.executable} -m mddb._merge %O %A %B %P",
    )
    (root / ".gitattributes").write_text("*.md merge=mddb-card\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-q", "-m", "register driver")
    return root


def _card(root, name, summary):
    (root / name).write_text(
        f"---\nid: {ID}\ntitle: Card\nsummary: {summary}\n---\nbody\n"
    )
    _git(root, "add", name)


def _conflicting_merge(tmp_path):
    root = _deck(tmp_path)
    _card(root, "card.md", "base")
    _git(root, "commit", "-q", "-m", "seed card")
    _git(root, "branch", "other")
    _card(root, "card.md", "ours-summary")
    _git(root, "commit", "-q", "-m", "ours: bumped priority")
    _git(root, "checkout", "-q", "other")
    _card(root, "card.md", "theirs-summary")
    _git(root, "commit", "-q", "-m", "theirs: rescheduled")
    _git(root, "checkout", "-q", "master")
    merge = _git(root, "merge", "--no-edit", "other", check=False)
    assert merge.returncode != 0
    return root


def test_conflict_rationales_returns_each_side(tmp_path):
    root = _conflicting_merge(tmp_path)
    rationales = conflict_rationales(root, "card.md")
    ours = [c["message"] for c in rationales["ours"]]
    theirs = [c["message"] for c in rationales["theirs"]]
    assert any("ours: bumped priority" in m for m in ours)
    assert any("theirs: rescheduled" in m for m in theirs)
    assert all("theirs: rescheduled" not in m for m in ours)
    assert all("ours: bumped priority" not in m for m in theirs)


def test_conflict_rationales_path_filter_excludes_other_cards(tmp_path):
    root = _deck(tmp_path)
    _card(root, "card.md", "base")
    _git(root, "commit", "-q", "-m", "seed card")
    _git(root, "branch", "other")
    _card(root, "unrelated.md", "x")
    _git(root, "commit", "-q", "-m", "ours: touches unrelated card")
    _card(root, "card.md", "ours-summary")
    _git(root, "commit", "-q", "-m", "ours: bumped priority")
    _git(root, "checkout", "-q", "other")
    _card(root, "card.md", "theirs-summary")
    _git(root, "commit", "-q", "-m", "theirs: rescheduled")
    _git(root, "checkout", "-q", "master")
    assert _git(root, "merge", "--no-edit", "other", check=False).returncode != 0
    rationales = conflict_rationales(root, "card.md")
    assert any("ours: bumped priority" in c["message"] for c in rationales["ours"])
    assert all("touches unrelated card" not in c["message"] for c in rationales["ours"])


def test_conflict_rationales_raises_without_merge_in_progress(tmp_path):
    root = _deck(tmp_path)
    _card(root, "card.md", "base")
    _git(root, "commit", "-q", "-m", "seed card")
    with pytest.raises(subprocess.CalledProcessError):
        conflict_rationales(root, "card.md")
