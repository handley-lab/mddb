#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

import tomli


def pyproject_version(content: str) -> str | None:
    return tomli.loads(content).get("project", {}).get("version")


def main() -> int:
    local = pyproject_version(Path("pyproject.toml").read_text(encoding="utf-8"))
    if not local:
        print("could not read local pyproject.toml version", file=sys.stderr)
        return 1

    subprocess.run(
        ["git", "fetch", "origin", "master"], check=False, capture_output=True
    )
    show = subprocess.run(
        ["git", "show", "origin/master:pyproject.toml"],
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        print(f"version {local} (no origin/master to compare against)")
        return 0

    master = pyproject_version(show.stdout)
    if master and local == master:
        print(
            f"version {local} matches origin/master — bump pyproject.toml before merging",
            file=sys.stderr,
        )
        return 1
    print(f"version {local} (origin/master at {master})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
