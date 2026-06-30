"""Git merge driver + helpers for mddb cards (`mddb-merge` console script).

Reached only via the ``mddb-merge`` console script (git invokes it as a custom
merge driver) and direct import in a REPL (``install``, ``conflict_rationales``).
The core ``import mddb`` never imports this module — like ``_mcp.py``, it is a
thin git-boundary adapter, not part of the substrate API.

Merge semantics (three-way, per the card's natural granularity):

- ``tags``: three-way SET merge — a base-present tag survives iff neither side
  removed it (deletion wins); a base-absent tag is added iff either side added
  it (additions unioned). Never conflicts.
- body: git's own line-level three-way merge via ``git merge-file``.
- ``id``: immutable — an existing card (base has ``id``) requires
  ours == base == theirs, else drift raises. add/add (no base ``id``) treats
  ``id`` as an ordinary scalar (differing → conflict marker).
- every other scalar: per-field three-way; genuine divergence emits standard
  git conflict markers inside the frontmatter block.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .card import Card

_MISSING = object()
_DRIVER = "mddb-merge %O %A %B %P"


def _read_card(path):
    text = path.read_text()
    return Card(yaml={}, body="") if not text else Card.from_text(text)


def _tags_of(card):
    tags = card.yaml.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(f"tags must be a list of strings: {tags!r}")
    return tags


def _merge_tags(base, ours, theirs):
    b, o, t = _tags_of(base), _tags_of(ours), _tags_of(theirs)
    bset = set(b)
    removed = (bset - set(o)) | (bset - set(t))
    out, seen = [], set()
    for tag in [*b, *(x for x in o if x not in bset), *(x for x in t if x not in bset)]:
        if tag not in removed and tag not in seen:
            out.append(tag)
            seen.add(tag)
    return out


def _dump(key, value):
    return yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True)


def _conflict_block(key, ours_value, theirs_value, path):
    ours_frag = "" if ours_value is _MISSING else _dump(key, ours_value)
    theirs_frag = "" if theirs_value is _MISSING else _dump(key, theirs_value)
    return (
        f"<<<<<<< {path} (ours)\n{ours_frag}"
        f"=======\n{theirs_frag}"
        f">>>>>>> {path} (theirs)\n"
    )


def _merge_body(base, ours, theirs, path):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ours_path, base_path, theirs_path = tmp / "ours", tmp / "base", tmp / "theirs"
        ours_path.write_text(ours)
        base_path.write_text(base)
        theirs_path.write_text(theirs)
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                f"{path} (ours)",
                "-L",
                "base",
                "-L",
                f"{path} (theirs)",
                str(ours_path),
                str(base_path),
                str(theirs_path),
            ],
            capture_output=True,
            text=True,
        )
    if result.returncode == 0:
        return result.stdout, True
    if 1 <= result.returncode <= 127:
        return result.stdout, False
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


def merge_cards(base, ours, theirs, path):
    """Three-way merge two card versions against their common ancestor.

    Args:
        base: The common-ancestor :class:`~mddb.card.Card` (``yaml={}``/``body=""``
            for an add/add merge with no merge base).
        ours: The current-side card.
        theirs: The other-side card.
        path: The card's real pathname (``%P``), used in conflict-marker labels.

    Returns:
        A ``(text, clean)`` tuple: ``text`` is the merged ``.md`` content (with
        git conflict markers where a scalar or the body genuinely diverged);
        ``clean`` is False iff any conflict marker was emitted.

    Raises:
        ValueError: ``id`` immutability drift on an existing-card merge, or a
            ``tags`` value that is not a list of strings.
        KeyError: an existing-card merge where a side lacks ``id``.
        subprocess.CalledProcessError: ``git merge-file`` failed (rc >= 128).
    """
    if "id" in base.yaml and not (
        ours.yaml["id"] == base.yaml["id"] == theirs.yaml["id"]
    ):
        raise ValueError(
            f"{path}: id is immutable but diverged "
            f"(base {base.yaml['id']}, ours {ours.yaml['id']}, theirs {theirs.yaml['id']})"
        )

    keys = list(ours.yaml)
    keys += [k for k in theirs.yaml if k not in ours.yaml]
    keys += [k for k in base.yaml if k not in ours.yaml and k not in theirs.yaml]

    merged_tags = _merge_tags(base, ours, theirs)
    parts, scalar_conflict = [], False
    for key in keys:
        if key == "tags":
            if merged_tags:
                parts.append(_dump("tags", merged_tags))
            continue
        ours_value = ours.yaml.get(key, _MISSING)
        theirs_value = theirs.yaml.get(key, _MISSING)
        base_value = base.yaml.get(key, _MISSING)
        if ours_value == theirs_value:
            resolved = ours_value
        elif ours_value == base_value:
            resolved = theirs_value
        elif theirs_value == base_value:
            resolved = ours_value
        else:
            parts.append(_conflict_block(key, ours_value, theirs_value, path))
            scalar_conflict = True
            continue
        if resolved is not _MISSING:
            parts.append(_dump(key, resolved))

    body, body_clean = _merge_body(base.body, ours.body, theirs.body, path)
    return f"---\n{''.join(parts)}---\n{body}", (not scalar_conflict) and body_clean


def main():
    """Entry point for the ``mddb-merge`` git merge driver.

    git invokes ``mddb-merge %O %A %B %P``; reads exactly those four paths from
    ``sys.argv``, writes the merged result back to the ``%A`` (ours) path, and
    exits 0 (clean) or 1 (conflict). Missing or extra args raise (registration
    drift).
    """
    ancestor, ours, theirs, path = sys.argv[1:]
    text, clean = merge_cards(
        _read_card(Path(ancestor)),
        _read_card(Path(ours)),
        _read_card(Path(theirs)),
        path,
    )
    Path(ours).write_text(text)
    raise SystemExit(0 if clean else 1)


def install(root):
    """Register the ``mddb-card`` merge driver for the deck at ``root``.

    Sets the per-clone git config (``merge.mddb-card.{name,driver}``) and
    idempotently ensures ``<root>/.gitattributes`` routes ``*.md`` through the
    driver. Does NOT commit ``.gitattributes`` — the operator commits it once,
    matching the LFS-policy stance (``MDDB.init`` does not write it either).

    Args:
        root: Path to the deck root (a git working tree). Required — no
            current-directory fallback.

    Raises:
        subprocess.CalledProcessError: a ``git config`` invocation failed.
    """
    root = Path(root)
    _configure_driver([], cwd=root)
    attributes = root / ".gitattributes"
    line = "*.md merge=mddb-card"
    existing = attributes.read_text() if attributes.exists() else ""
    if line not in existing.splitlines():
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with attributes.open("a") as handle:
            handle.write(f"{prefix}{line}\n")


def install_global():
    """Register the ``mddb-card`` driver in the user's global git config.

    The driver command (``merge.mddb-card.driver``) lives in git config, which
    is never cloned, so it cannot travel with a deck. Setting it ``--global``
    once per user/machine (the ``git lfs install`` model) makes every deck on
    that account merge correctly off its committed ``.gitattributes`` — fresh
    clones need no per-clone step. Without it, git silently falls back to its
    built-in text merge (mangling frontmatter, resurrecting deleted tags), so
    provision this in each agent image / service account.

    Raises:
        subprocess.CalledProcessError: a ``git config`` invocation failed.
    """
    _configure_driver(["--global"])


def _configure_driver(scope, cwd=None):
    subprocess.run(
        ["git", "config", *scope, "merge.mddb-card.name", "mddb semantic card merge"],
        check=True,
        cwd=cwd,
    )
    subprocess.run(
        ["git", "config", *scope, "merge.mddb-card.driver", _DRIVER],
        check=True,
        cwd=cwd,
    )


def conflict_rationales(root, path):
    """Return the commit rationales touching ``path`` on each side of a merge.

    Surfaces intent for resolving a conflict: the commit messages on ``ours``
    (``MERGE_HEAD..HEAD``) and ``theirs`` (``HEAD..MERGE_HEAD``) that touched the
    card. Requires an in-progress merge (``.git/MERGE_HEAD`` present); otherwise
    the underlying ``git log`` errors and the exception propagates.

    Args:
        root: Path to the deck root.
        path: The card's relpath within the deck.

    Returns:
        ``{"ours": [{"sha": str, "message": str}, ...], "theirs": [...]}``,
        newest first per side. Does not follow renames (unlike
        :meth:`mddb.MDDB.history`), so a card moved on one side may omit
        pre-move commits.

    Raises:
        subprocess.CalledProcessError: no merge in progress, or ``git log`` failed.
    """
    return {
        "ours": _log_rationales(root, "MERGE_HEAD..HEAD", path),
        "theirs": _log_rationales(root, "HEAD..MERGE_HEAD", path),
    }


def _log_rationales(root, revrange, path):
    out = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--pretty=format:%H%x00%B%x1e",
            revrange,
            "--",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    commits = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        sha, message = chunk.split("\x00", 1)
        commits.append({"sha": sha, "message": message})
    return commits


if __name__ == "__main__":
    main()
