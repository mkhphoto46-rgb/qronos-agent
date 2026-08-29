"""
The gate cannot be bypassed, checked structurally rather than by convention.

Every other test in the security seam asks whether the gate answers correctly.
This one asks whether anybody has to call it. That is a different question, and
it is the one that decays: the gate can be perfect and still be irrelevant if
an executor is added next month that simply does not use it.

The check is static — it reads the source, the way
``tests/test_release_hygiene.py`` reads it to prove product code does not import
from ``tools`` — because the alternative is running every executor and hoping
the test covers the path where somebody forgot.

There are no executors yet. That is exactly why this is written now: the rule
is cheap to state while the list is empty and expensive to retrofit once it is
not.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# Modules that carry out changes to the machine. A worker added under core/
# that performs actions belongs here, and the test below then requires it to
# reach the permission gate.
#
# This list was empty for a long time, which made the rule below true and
# vacuous. core.screen_capture is the first entry, and it arrived here the
# intended way: it imported ctypes, the test failed, and somebody had to
# decide which list it belonged in.
EXECUTOR_MODULES: tuple[str, ...] = (
    "core.screen_capture",
)

# The one sanctioned route to performing an action.
GATE_MODULE = "security.gate"

# Standard-library modules that can change the machine on their own. A module
# that imports one of these and is not an executor is either fine — reading,
# not writing — or is an executor nobody declared.
DANGEROUS_IMPORTS = frozenset({"subprocess", "shutil", "winreg", "ctypes"})

# Modules that legitimately touch these without being action executors, with
# the reason. Anything not listed has to be justified by adding it here, which
# is the point: the list is short and someone has to look at it.
KNOWN_SYSTEM_MODULES: dict[str, str] = {
    "core.link_devices": (
        "Runs icacls to restrict the device key file to its owner."
    ),
    "core.ollama_controller": (
        "Starts and stops the local model server."
    ),
    "core.whisper_cpp_runtime": (
        "Runs the whisper binary to transcribe an audio file."
    ),
    "core.whisper_cpp_vad_runtime": (
        "Runs the whisper binary for voice activity detection."
    ),
    "core.resource_guard": (
        "Runs nvidia-smi to read GPU status."
    ),
    "core.storage_guard": (
        "Calls shutil.disk_usage, which only reads free space."
    ),
    "core.storage_janitor": (
        "Deletes files it owns, under the storage budget, not on request."
    ),
    "core.storage_manager": (
        "Coordinates the janitor over Qronos-owned directories."
    ),
    "core.model_store": (
        "Measures and removes downloaded model files."
    ),
    "core.runtime_bridge": (
        "Reconfigures its own stdio streams."
    ),
}


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def product_modules() -> tuple[Path, ...]:
    found: list[Path] = []

    for package in ("core", "security"):
        found.extend(sorted((ROOT / package).rglob("*.py")))

    return tuple(found)


def imported_names(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)

    return frozenset(names)


class TestExecutorsReachTheGate(unittest.TestCase):
    def test_every_declared_executor_imports_the_gate(self) -> None:
        for name in EXECUTOR_MODULES:
            path = ROOT / Path(*name.split(".")).with_suffix(".py")

            with self.subTest(module=name):
                self.assertTrue(path.exists(), f"{name} does not exist")
                self.assertIn(
                    GATE_MODULE,
                    imported_names(path),
                    f"{name} performs actions without importing the gate",
                )

    def test_the_gate_is_importable_and_has_the_two_entry_points(
        self,
    ) -> None:
        # Guards against the rule above passing vacuously because the gate was
        # renamed or gutted.
        from security import gate

        self.assertTrue(callable(gate.evaluate))
        self.assertTrue(callable(gate.require))


class TestNoUndeclaredSystemAccess(unittest.TestCase):
    def test_modules_that_can_change_the_machine_are_accounted_for(
        self,
    ) -> None:
        # A new module that shells out, edits the registry or moves files is
        # either a deliberate, explained exception or an executor that has to
        # go through the gate. Failing here is the prompt to decide which.
        unexplained: list[str] = []

        for path in product_modules():
            name = module_name(path)

            if name in KNOWN_SYSTEM_MODULES or name in EXECUTOR_MODULES:
                continue

            if imported_names(path) & DANGEROUS_IMPORTS:
                unexplained.append(name)

        self.assertEqual(
            unexplained,
            [],
            "These modules can change the machine but are neither declared "
            "executors nor listed as known exceptions. Add them to "
            "EXECUTOR_MODULES so they must reach the permission gate, or to "
            "KNOWN_SYSTEM_MODULES with the reason.",
        )

    def test_the_exception_list_has_no_stale_entries(self) -> None:
        # An exception that no longer applies is an exception nobody will
        # notice being used again later.
        existing = {module_name(path) for path in product_modules()}

        for name in KNOWN_SYSTEM_MODULES:
            with self.subTest(module=name):
                self.assertIn(name, existing)

    def test_every_exception_carries_a_reason(self) -> None:
        for name, reason in KNOWN_SYSTEM_MODULES.items():
            with self.subTest(module=name):
                self.assertTrue(reason.strip())


class TestTheAuditTrailIsWiredInProduction(unittest.TestCase):
    """
    "Every decision is recorded" was a claim about the test suite.

    ``set_default_audit_sink`` existed, worked, and was called from
    ``tests/test_gate.py`` and from nowhere else. So in a real run the gate had
    no default sink, and an executor that omitted the audit argument produced
    no trail — silently, and indistinguishably from a call that was never made.

    Static, like the rest of this file, because the alternative is starting the
    voice runtime, which needs a microphone.
    """

    SINK_INSTALLER = "set_default_audit_sink"

    def installers(self) -> tuple[str, ...]:
        return tuple(
            module_name(path)
            for path in product_modules()
            if self.SINK_INSTALLER in path.read_text(encoding="utf-8")
        )

    def test_something_outside_the_tests_installs_the_sink(self) -> None:
        callers = [
            name
            for name in self.installers()
            if name != GATE_MODULE
        ]

        self.assertTrue(
            callers,
            "Nothing in core/ or security/ installs the gate's default audit "
            "sink, so in production no decision is recorded unless every "
            "caller remembers to pass one.",
        )

    def test_the_runtime_bridge_installs_it_while_preparing(self) -> None:
        # Specifically in prepare(), not at import: installing a sink as a
        # side effect of importing a module would write to the user's audit
        # file from any script that happened to import the bridge.
        source = (ROOT / "core" / "runtime_bridge.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        prepare = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "prepare"
        )

        called = {
            node.func.id
            for node in ast.walk(prepare)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertIn(self.SINK_INSTALLER, called)


if __name__ == "__main__":
    unittest.main()
