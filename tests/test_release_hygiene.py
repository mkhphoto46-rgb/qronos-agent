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

    def test_the_whole_tools_directory_is_excluded(self) -> None:
        # Not only tools/debug. Eleven live harnesses sat outside it —
        # unmarked, unlisted, and therefore invisible to every check in this
        # file — and would have shipped: scripts that open the microphone,
        # talk to the local model server and write audio to disk.
        #
        # Adding tools/ to .gitignore did not fix it. Ignore rules do not
        # affect files Git already tracks.
        entries = manifest_entries()

        for path in sorted((ROOT / "tools").rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()

            with self.subTest(path=relative):
                self.assertTrue(
                    is_excluded(relative, entries),
                    f"{relative} would ship",
                )


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



# ---------------------------------------------------------------------------
# Nothing from the machine that wrote the code.
# ---------------------------------------------------------------------------

#: Patterns that mean somebody's own computer has leaked into the repository.
#:
#: Written as fragments rather than whole paths because the interesting failure
#: is a half-path in a docstring or a commit-message-shaped comment, not a
#: tidy absolute path somebody would have noticed.
PRIVATE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("c:\\users\\", "a Windows home directory"),
    ("/c/users/", "a Windows home directory, in POSIX form"),
    ("c:/users/", "a Windows home directory, with forward slashes"),
    ("/users/", "a macOS or Linux home directory"),
    ("\\appdata\\", "a Windows AppData path"),
    ("/appdata/", "a Windows AppData path"),
    ("onedrive", "a personal cloud folder"),
)

#: Files that may legitimately contain a home path, with the reason.
#:
#: Kept deliberately short. An entry here is a promise that a reader has looked
#: at the line and decided it names nobody.
#: Applications whose presence would say what is installed on the machine the
#: code was written on.
#:
#: An explicit list rather than a rule, because nothing can tell "this
#: developer happens to run a 3D slicer" from "Qronos integrates with this"
#: except knowing which is which. Everything Qronos genuinely depends on —
#: Ollama, Chrome, Tauri, whisper.cpp, CrispASR — is deliberately absent, and
#: naming those is correct and expected.
#:
#: When something here becomes a real integration, delete it from this list
#: rather than excepting the file that mentions it.
PERSONAL_SOFTWARE: tuple[str, ...] = (
    "comfyui",
    "bambu studio",
    "bambulab",
    "telegram desktop",
    "shadowplay",
    "signalrgb",
    "rustdesk",
)

PRIVATE_PATTERN_EXCEPTIONS: dict[str, str] = {
    "tests/test_release_hygiene.py": (
        "This file lists the patterns it looks for."
    ),
    "tests/test_link_server.py": (
        "Fabricates a path-shaped exception message to prove the link server "
        "does not forward one to a phone. The path names nobody, and making "
        "it unrealistic would weaken the test it exists for."
    ),
}


def text_files() -> tuple[Path, ...]:
    """Every tracked text file worth reading, excluding what does not ship."""

    skip_directories = {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        "target",
        "dist",
        "runtime",
        "temp",
        # Tauri regenerates this on every build and none of it is tracked.
        # Its schemas quote example URLs that look like home paths.
        "gen",
    }
    suffixes = {
        ".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml",
        ".rs", ".ts", ".tsx", ".js", ".css", ".html", ".ps1", ".sh",
    }

    found: list[Path] = []

    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue

        if skip_directories & set(path.relative_to(ROOT).parts):
            continue

        found.append(path)

    return tuple(found)


class TestNothingPersonalIsCommitted(unittest.TestCase):
    """
    The repository must not describe the machine it was written on.

    This is not hypothetical tidiness. Three commits in this project's history
    carried a home directory and a Windows account name, and two files named
    the application that happened to be using the graphics card while a
    measurement was taken. All four were written by someone documenting real
    numbers carefully — which is exactly how it happens, because the detail
    feels like evidence at the time.

    Rewriting published history to remove them is disruptive and slow. Failing
    a test before the commit is neither.
    """

    def test_no_tracked_file_names_somebodys_home_directory(self) -> None:
        offences: list[str] = []

        for path in text_files():
            relative = path.relative_to(ROOT).as_posix()

            if relative in PRIVATE_PATTERN_EXCEPTIONS:
                continue

            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            lowered = content.lower()

            for pattern, description in PRIVATE_PATTERNS:
                if pattern in lowered:
                    line = next(
                        (
                            number
                            for number, text in enumerate(
                                content.splitlines(), start=1
                            )
                            if pattern in text.lower()
                        ),
                        0,
                    )
                    offences.append(
                        f"{relative}:{line} contains {description} "
                        f"({pattern!r})"
                    )
                    break

        self.assertEqual(
            offences,
            [],
            "These files describe the machine they were written on. Replace "
            "the path with a placeholder, or an environment variable if it is "
            "an instruction somebody will run:\n  "
            + "\n  ".join(offences),
        )

    def test_no_tracked_file_names_the_authors_other_software(self) -> None:
        """
        What is installed on a developer's machine is nobody else's business.

        The softer half of the same mistake. Recording that a measurement was
        taken "while <some application> held the graphics card" feels like
        diligence — it is the honest reason the number looked the way it did —
        and it still tells a stranger what that person was running.

        The measurement survives the fix intact. "Another application was
        holding the card" carries every part of the meaning that mattered.
        """
        offences: list[str] = []

        for path in text_files():
            relative = path.relative_to(ROOT).as_posix()

            if relative in PRIVATE_PATTERN_EXCEPTIONS:
                continue

            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            lowered = content.lower()

            for name in PERSONAL_SOFTWARE:
                if name in lowered:
                    line = next(
                        (
                            number
                            for number, line_text in enumerate(
                                content.splitlines(), start=1
                            )
                            if name in line_text.lower()
                        ),
                        0,
                    )
                    offences.append(f"{relative}:{line} names {name!r}")
                    break

        self.assertEqual(
            offences,
            [],
            "These files name software that happens to be installed on "
            "somebody's machine. Say what mattered about it instead, or if "
            "Qronos now genuinely integrates with it, remove it from "
            "PERSONAL_SOFTWARE:\n  " + "\n  ".join(offences),
        )

    def test_every_exception_still_exists(self) -> None:
        # An exception for a file that has been deleted or renamed is a hole
        # nobody is watching.
        for relative in PRIVATE_PATTERN_EXCEPTIONS:
            with self.subTest(path=relative):
                self.assertTrue(
                    (ROOT / relative).is_file(),
                    f"{relative} is excepted from the private-path check but "
                    "no longer exists.",
                )

    def test_every_exception_carries_a_reason(self) -> None:
        for relative, reason in PRIVATE_PATTERN_EXCEPTIONS.items():
            with self.subTest(path=relative):
                self.assertTrue(
                    reason.strip(),
                    f"{relative} is excepted with no reason given.",
                )


if __name__ == "__main__":
    unittest.main()
