"""
Marks a script as a debug tool, and keeps it out of a release.

Every file in ``tools/debug`` is a diagnostic. None of it is part of the
product, and some of it — the reachability server in particular — opens an
unauthenticated socket, which is fine on a developer's machine for ten minutes
and is not fine in something a user installs.

Three layers stop it shipping, deliberately overlapping because any one of them
can be forgotten:

    The directory. ``tools/debug`` is one path for a packaging step to exclude,
    rather than a list of filenames to keep in step with.

    ``release-exclude.txt``. A plain list at the repository root that any build
    script can read, whatever it is written in.

    ``tests/test_release_hygiene.py``. Fails the suite if a debug-marked file is
    not covered by the manifest, if the manifest names something that no longer
    exists, or if product code ever imports from ``tools``. The marking cannot
    quietly rot.

And one more at run time: ``require_debug_context`` refuses to run a tool that
somehow ended up in a release build, so a packaging mistake is inert rather than
exploitable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# The marker the hygiene test looks for. Any script carrying this must live
# under tools/debug and must be covered by release-exclude.txt.
QRONOS_DEBUG_TOOL = True

# Set this in a packaged build. Nothing in tools/debug will run.
RELEASE_ENV_VAR = "QRONOS_RELEASE"


def repository_root() -> Path:
    """
    The repository root, from a script two directories inside it.

    Scripts in ``tools/debug`` are run directly rather than imported, so they
    have to put the root on ``sys.path`` themselves.
    """

    return Path(__file__).resolve().parent.parent.parent


def on_path() -> Path:
    """Put the repository root on ``sys.path`` and return it."""

    root = repository_root()

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    return root


def looks_like_a_release() -> bool:
    """
    Is this a packaged build rather than a checkout?

    Two signals. An explicit environment variable, which a build sets, and the
    absence of the test suite, which no release would carry. Neither is
    conclusive on its own; together they are enough to refuse.
    """

    if os.environ.get(RELEASE_ENV_VAR, "").strip():
        return True

    return not (repository_root() / "tests").is_dir()


def banner(name: str) -> None:
    """Say plainly what this is, every time it runs."""

    print()
    print(f"  [debug tool]  {name}")
    print("  Not part of Qronos. Diagnostics only, safe to delete.")


def require_debug_context(name: str) -> None:
    """
    Refuse to run outside a development checkout.

    The last line of defence. If a packaging step forgets to exclude
    ``tools/debug``, the tool that would otherwise open a socket does nothing
    instead.
    """

    if not looks_like_a_release():
        return

    print(f"{name} is a debug tool and will not run in a release build.")
    print()
    print("  It is meant to be run from a development checkout of")
    print("  qronos-agent. If you are seeing this in an installed copy of")
    print("  Qronos, the debug tools were packaged by mistake and should be")
    print("  removed; tools/debug is listed in release-exclude.txt.")
    print()
    print(f"  ({RELEASE_ENV_VAR} is set, or the test suite is absent.)")

    raise SystemExit(2)
