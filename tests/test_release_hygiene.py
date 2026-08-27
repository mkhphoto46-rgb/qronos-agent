"""
Keeps test and debug code out of a release.

``release-exclude.txt`` is the list a packaging step reads. A list is only
useful while it matches the repository, so this module checks that it does, on
every test run.

The rule worth stating: ``tools/debug/link_reachability.py`` opens an
unauthenticated HTTP socket on the local network. That is fine on a developer's
machine for ten minutes and is not fine in software a user installs. Everything
here exists so that cannot happen by forgetting.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "release-exclude.txt"

# Directories holding code that ships. Nothing in here may depend on anything
# that does not.
PRODUCT_PACKAGES = ("core", "security")

# The marker a debug script carries.
DEBUG_MARKER = "QRONOS_DEBUG_TOOL"

DEBUG_DIRECTORY = "tools/debug/"


def manifest_entries() -> tuple[str, ...]:
    """The paths listed for exclusion, comments and blanks dropped."""

    lines = MANIFEST.read_text(encoding="utf-8").splitlines()

    return tuple(
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    )


def is_excluded(relative: str, entries: tuple[str, ...]) -> bool:
    """Would a packaging step reading the manifest leave this path out?"""

    for entry in entries:
        if entry.endswith("/"):
            if relative == entry.rstrip("/") or relative.startswith(entry):
                return True
        elif relative == entry:
            return True

    return False


def python_files(*packages: str) -> tuple[Path, ...]:
    found: list[Path] = []

    for package in packages:
        found.extend(sorted((ROOT / package).rglob("*.py")))

    return tuple(
        path for path in found if "__pycache__" not in path.parts
    )


def imported_roots(path: Path) -> set[str]:
    """The top-level module names a file imports."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):  # pragma: no cover - would fail elsewhere
        return set()

    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue

            if node.module:
                roots.add(node.module.split(".")[0])

    return roots


class TestManifestExists(unittest.TestCase):
    def test_the_manifest_is_present(self) -> None:
        self.assertTrue(
            MANIFEST.exists(),
            "release-exclude.txt is what a packaging step reads; without it "
            "nothing is excluded",
        )

    def test_it_lists_something(self) -> None:
        self.assertTrue(manifest_entries())


class TestManifestMatchesTheRepository(unittest.TestCase):
    def test_every_entry_still_exists(self) -> None:
        # A stale entry is how a list stops being trustworthy: nobody knows
        # whether the others are current either.
        for entry in manifest_entries():
            with self.subTest(entry=entry):
                self.assertTrue(
                    (ROOT / entry.rstrip("/")).exists(),
                    f"release-exclude.txt names {entry}, which does not exist",
                )

    def test_the_test_suite_is_excluded(self) -> None:
        self.assertTrue(is_excluded("tests/test_release_hygiene.py",
                                    manifest_entries()))

    def test_the_debug_directory_is_excluded(self) -> None:
        self.assertTrue(is_excluded(DEBUG_DIRECTORY, manifest_entries()))


class TestDebugToolsAreCovered(unittest.TestCase):
    def test_every_file_in_the_debug_directory_is_excluded(self) -> None:
        entries = manifest_entries()

        for path in python_files("tools/debug"):
            relative = path.relative_to(ROOT).as_posix()

            with self.subTest(path=relative):
                self.assertTrue(
                    is_excluded(relative, entries),
                    f"{relative} would ship",
                )

    def test_every_marked_file_lives_in_the_debug_directory(self) -> None:
        # So a debug tool cannot be added somewhere the manifest does not cover.
        for path in python_files("core", "security", "tools", "tests"):
            relative = path.relative_to(ROOT).as_posix()

            if DEBUG_MARKER not in path.read_text(encoding="utf-8"):
                continue

            if relative == "tests/test_release_hygiene.py":
                continue

            with self.subTest(path=relative):
                self.assertTrue(
                    relative.startswith(DEBUG_DIRECTORY),
                    f"{relative} is marked as a debug tool but is outside "
                    f"{DEBUG_DIRECTORY}",
                )

    def test_the_debug_tools_carry_the_marker(self) -> None:
        marked = [
            path.relative_to(ROOT).as_posix()
            for path in python_files("tools/debug")
            if DEBUG_MARKER in path.read_text(encoding="utf-8")
        ]

        self.assertIn("tools/debug/link_selfcheck.py", marked)
        self.assertIn("tools/debug/link_reachability.py", marked)

    def test_the_riskiest_tool_refuses_to_run_in_a_release(self) -> None:
        # It opens a socket. A packaging mistake must be inert, not just
        # untidy.
        source = (
            ROOT / "tools/debug/link_reachability.py"
        ).read_text(encoding="utf-8")

        self.assertIn("require_debug_context", source)


class TestProductCodeStandsAlone(unittest.TestCase):
    def test_product_code_does_not_import_from_tools(self) -> None:
        for path in python_files(*PRODUCT_PACKAGES):
            relative = path.relative_to(ROOT).as_posix()

            with self.subTest(path=relative):
                self.assertNotIn(
                    "tools",
                    imported_roots(path),
                    f"{relative} imports from tools, which does not ship",
                )

    def test_product_code_does_not_import_the_test_suite(self) -> None:
        for path in python_files(*PRODUCT_PACKAGES):
            relative = path.relative_to(ROOT).as_posix()

            with self.subTest(path=relative):
                self.assertNotIn("tests", imported_roots(path), relative)


class TestPrivateDataStaysOut(unittest.TestCase):
    def test_the_directories_holding_keys_are_git_ignored(self) -> None:
        # data/ holds the device link's pairing keys. It is outside version
        # control by construction rather than by remembering.
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

        for directory in ("data/", "logs/"):
            with self.subTest(directory=directory):
                self.assertIn(directory, ignored)


if __name__ == "__main__":
    unittest.main()
