"""A directory junction a test owns, removed in ``finally`` and only if it is still that junction.

Cleanup removes the link alone (``os.rmdir`` on a junction deletes the reparse
point, never the target's contents), and only after checking that the path is
still a junction pointing at the fixture's own target. Anything else is left in
place and reported, so a cleanup can never reach a folder the test did not make.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _plain(path: str) -> str:
    for prefix in ("\\\\?\\", "\\??\\"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    return os.path.normcase(os.path.normpath(path))


def remove_owned_junction(link: Path, target: Path) -> None:
    """Remove ``link`` only if it is still a junction to ``target``; the target is kept."""
    if not os.path.lexists(link):
        return
    if not os.path.isjunction(link) or _plain(os.readlink(link)) != _plain(os.path.abspath(target)):
        raise AssertionError(f"{link} is no longer this fixture's junction to {target}; it was left in place.")
    os.rmdir(link)
    if not os.path.isdir(target):
        raise AssertionError(f"The junction target {target} is gone after removing the link.")


@contextmanager
def owned_junction(target: Path, link: Path) -> Iterator[Path]:
    """Create ``link`` -> ``target`` (``link`` must not exist yet) and always remove exactly it afterwards."""
    import _winapi

    if os.path.lexists(link):
        raise AssertionError(f"{link} already exists; the fixture only removes junctions it created.")
    _winapi.CreateJunction(str(target), str(link))
    try:
        yield link
    finally:
        remove_owned_junction(link, target)
