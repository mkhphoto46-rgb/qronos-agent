from dataclasses import replace
import unittest
from unittest.mock import patch

from core.window_binding import (
    WindowBinder,
    WindowBindingAmbiguous,
    WindowBindingStale,
    WindowBindingUnavailable,
    WindowRect,
    WindowSnapshot,
    WindowTarget,
)


def snapshot(
    *,
    hwnd: int = 101,
    pid: int = 501,
    thread_id: int = 701,
    title: str = "Untitled - Paint",
    process_path: str = r"C:\Windows\System32\mspaint.exe",
    started: int = 123456789,
    rect: WindowRect = WindowRect(100, 100, 1100, 800),
    client: WindowRect = WindowRect(108, 140, 1092, 792),
    dpi: int = 120,
    visible: bool = True,
    minimized: bool = False,
    foreground: bool = True,
) -> WindowSnapshot:
    return WindowSnapshot(
        hwnd=hwnd,
        pid=pid,
        thread_id=thread_id,
        title=title,
        process_path=process_path,
        process_start_time_100ns=started,
        window_rect=rect,
        client_rect=client,
        dpi=dpi,
        visible=visible,
        minimized=minimized,
        foreground=foreground,
    )


def paint_target(*, executable: str = "mspaint.exe") -> WindowTarget:
    return WindowTarget(
        app_id="paint",
        display_name="Paint",
        aliases=("Microsoft Paint", "Paint"),
        executable=executable,
        process_names=("mspaint.exe",),
    )


class WindowBindingTests(unittest.TestCase):
    def test_binds_one_strong_process_match(self) -> None:
        item = snapshot()
        binder = WindowBinder(
            enumerate_fn=lambda: (item,),
            snapshot_fn=lambda hwnd: item if hwnd == item.hwnd else None,
        )
        binding = binder.bind(paint_target(), require_foreground=True)
        self.assertEqual(binding.hwnd, item.hwnd)
        self.assertEqual(binding.app_id, "paint")
        self.assertEqual(len(binding.identity_token), 32)
        self.assertEqual(len(binding.layout_token), 32)

    def test_exact_discovered_executable_path_is_required(self) -> None:
        item = snapshot(process_path=r"C:\Apps\Paint\mspaint.exe")
        binder = WindowBinder(
            enumerate_fn=lambda: (item,),
            snapshot_fn=lambda hwnd: item,
        )
        with self.assertRaises(WindowBindingUnavailable):
            binder.bind(paint_target(executable=r"D:\Other\Paint\mspaint.exe"))

    def test_title_only_target_is_not_trusted_as_application_identity(self) -> None:
        item = snapshot()
        binder = WindowBinder(
            enumerate_fn=lambda: (item,),
            snapshot_fn=lambda hwnd: item,
        )
        target = WindowTarget(
            app_id="unknown",
            display_name="Paint",
            aliases=("Paint",),
        )
        with self.assertRaises(WindowBindingUnavailable):
            binder.bind(target)


    def test_expected_pid_selects_exact_launched_process(self) -> None:
        old = snapshot(hwnd=101, pid=111)
        launched = snapshot(hwnd=202, pid=222)
        binder = WindowBinder(
            enumerate_fn=lambda: (old, launched),
            snapshot_fn=lambda hwnd: old if hwnd == old.hwnd else launched,
        )

        binding = binder.bind(
            paint_target(),
            expected_pid=222,
        )

        self.assertEqual(binding.hwnd, launched.hwnd)
        self.assertEqual(binding.snapshot.pid, 222)

    def test_expected_pid_does_not_fall_back_to_other_matching_process(self) -> None:
        other = snapshot(hwnd=101, pid=111)
        binder = WindowBinder(
            enumerate_fn=lambda: (other,),
            snapshot_fn=lambda hwnd: other,
        )

        with self.assertRaisesRegex(WindowBindingUnavailable, "pid 222"):
            binder.bind(
                paint_target(),
                expected_pid=222,
            )

    def test_expected_pid_must_be_positive(self) -> None:
        binder = WindowBinder(enumerate_fn=lambda: ())
        with self.assertRaises(ValueError):
            binder.bind(paint_target(), expected_pid=0)

    def test_equal_best_windows_are_ambiguous(self) -> None:
        first = snapshot(hwnd=101)
        second = snapshot(hwnd=102)
        binder = WindowBinder(
            enumerate_fn=lambda: (first, second),
            snapshot_fn=lambda hwnd: first if hwnd == first.hwnd else second,
        )
        with self.assertRaises(WindowBindingAmbiguous):
            binder.bind(paint_target())

    def test_exact_snapshot_binding_avoids_same_process_window_ambiguity(self) -> None:
        first = snapshot(
            hwnd=101,
            pid=501,
            title="First Browser Window",
        )
        second = snapshot(
            hwnd=102,
            pid=501,
            title="Second Browser Window",
        )

        windows = {
            first.hwnd: first,
            second.hwnd: second,
        }

        binder = WindowBinder(
            enumerate_fn=lambda: (first, second),
            snapshot_fn=lambda hwnd: windows.get(hwnd),
        )

        binding = binder.bind_snapshot(
            second,
            app_id="browser",
            display_name=second.title,
        )

        self.assertEqual(binding.hwnd, 102)
        self.assertEqual(binding.snapshot.pid, 501)
        self.assertEqual(binding.app_id, "browser")


    def test_exact_snapshot_binding_rejects_identity_change(self) -> None:
        initial = snapshot(
            hwnd=101,
            pid=501,
        )

        changed = replace(
            initial,
            process_start_time_100ns=initial.process_start_time_100ns + 1,
        )

        binder = WindowBinder(
            enumerate_fn=lambda: (initial,),
            snapshot_fn=lambda hwnd: changed,
        )

        with self.assertRaises(WindowBindingStale):
            binder.bind_snapshot(
                initial,
                app_id="example",
                display_name="Example",
            )


    def test_minimized_window_is_not_bindable(self) -> None:
        item = snapshot(minimized=True)
        binder = WindowBinder(
            enumerate_fn=lambda: (item,),
            snapshot_fn=lambda hwnd: item,
        )
        with self.assertRaises(WindowBindingUnavailable):
            binder.bind(paint_target())

    def test_revalidation_allows_title_change(self) -> None:
        initial = snapshot(title="Untitled - Paint")
        changed = replace(initial, title="drawing.png - Paint")
        current = {"value": initial}
        binder = WindowBinder(
            enumerate_fn=lambda: (initial,),
            snapshot_fn=lambda hwnd: current["value"],
        )
        binding = binder.bind(paint_target())
        current["value"] = changed
        result = binder.revalidate(binding)
        self.assertEqual(result.title, "drawing.png - Paint")

    def test_pid_reuse_is_rejected_by_process_start_time(self) -> None:
        initial = snapshot()
        reused = replace(initial, process_start_time_100ns=initial.process_start_time_100ns + 1)
        current = {"value": initial}
        binder = WindowBinder(
            enumerate_fn=lambda: (initial,),
            snapshot_fn=lambda hwnd: current["value"],
        )
        binding = binder.bind(paint_target())
        current["value"] = reused
        with self.assertRaises(WindowBindingStale):
            binder.revalidate(binding)

    def test_process_path_change_is_rejected(self) -> None:
        initial = snapshot()
        changed = replace(initial, process_path=r"C:\Temp\mspaint.exe")
        current = {"value": initial}
        binder = WindowBinder(
            enumerate_fn=lambda: (initial,),
            snapshot_fn=lambda hwnd: current["value"],
        )
        binding = binder.bind(paint_target())
        current["value"] = changed
        with self.assertRaises(WindowBindingStale):
            binder.revalidate(binding)

    def test_layout_change_expires_grounded_coordinates(self) -> None:
        initial = snapshot()
        moved = replace(initial, window_rect=WindowRect(120, 100, 1120, 800))
        current = {"value": initial}
        binder = WindowBinder(
            enumerate_fn=lambda: (initial,),
            snapshot_fn=lambda hwnd: current["value"],
        )
        binding = binder.bind(paint_target())
        current["value"] = moved
        with self.assertRaisesRegex(WindowBindingStale, "coordinates expired"):
            binder.revalidate(binding, require_same_layout=True)
        self.assertEqual(
            binder.revalidate(binding, require_same_layout=False).window_rect,
            moved.window_rect,
        )

    def test_focus_loss_is_rejected_when_required(self) -> None:
        initial = snapshot(foreground=True)
        background = replace(initial, foreground=False)
        current = {"value": initial}
        binder = WindowBinder(
            enumerate_fn=lambda: (initial,),
            snapshot_fn=lambda hwnd: current["value"],
        )
        binding = binder.bind(paint_target(), require_foreground=True)
        current["value"] = background
        with self.assertRaisesRegex(WindowBindingStale, "foreground"):
            binder.revalidate(binding, require_foreground=True)

    def test_hosted_content_window_accepts_matching_foreground_root(self) -> None:
        initial = snapshot(
            foreground=True
        )

        hosted = replace(
            initial,
            foreground=False,
        )

        current = {
            "value":
                initial
        }

        binder = WindowBinder(
            enumerate_fn=lambda: (
                initial,
            ),
            snapshot_fn=lambda hwnd:
                current[
                    "value"
                ],
        )

        binding = binder.bind(
            paint_target(),
            require_foreground=True,
        )

        current[
            "value"
        ] = hosted

        with patch(
            "core.window_binding.window_is_foreground",
            return_value=True,
        ):
            result = binder.revalidate(
                binding,
                require_foreground=True,
            )

        self.assertEqual(
            result.hwnd,
            hosted.hwnd,
        )

    def test_missing_window_is_stale(self) -> None:
        initial = snapshot()
        current = {"value": initial}
        binder = WindowBinder(
            enumerate_fn=lambda: (initial,),
            snapshot_fn=lambda hwnd: current["value"],
        )
        binding = binder.bind(paint_target())
        current["value"] = None
        with self.assertRaises(WindowBindingStale):
            binder.revalidate(binding)


if __name__ == "__main__":
    unittest.main()
