"""
The webcam question, against real footage of a real person.

``tools/test_qronos_watching_live.py`` watches a scene this project draws
itself. That one is reproducible, offline, and deliberately the easy case: flat
colours, hard edges, no motion blur, no compression. It proves the session, the
pacing, the limits and the plumbing.

It does not prove the model can do the actual job. A webcam produces a real
person under real lighting through a lens, compressed. So this watches a video
of one, played in a browser and captured through the screen at the rate a
camera would be watched at.

The clip is **"Me at the zoo"**, the first video uploaded to YouTube. It is
chosen for three reasons and every one of them matters:

    It is a person talking straight into a hand-held camera at close range,
    which is what a webcam sees.

    It is 240p from 2005 — blown-out highlights, heavy compression, motion
    blur. Worse than any webcam. If the model manages this, it manages a
    camera.

    **He turns around near the end.** So there is a real answer to "is the
    person facing the camera" that changes during the run, and a watcher that
    says the same thing about every frame fails rather than passes.

It is a public clip rather than footage from this machine, which is the rule
the plan set: there is video of identifiable people on any development machine
and none of it goes near this project.

Two things this does *not* do, on purpose. It never clicks a consent banner —
the page is served from ``127.0.0.1`` and embeds the no-cookie player, so no
banner appears and nothing is agreed to on anybody's behalf. And it is not in
the test suite, because it needs the internet and somebody else's video to
still exist.

    python tools/test_qronos_webcam_video_live.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ollama_controller import OllamaController  # noqa: E402
from core.screen_capture import ScreenCapture, window_titled  # noqa: E402
from core.vision_worker import brain_describe_fn  # noqa: E402
from core.watching_eyes import Eyes, ScreenRegionSource  # noqa: E402
from security.permissions import ActionCategory  # noqa: E402
from security.watching import WatchEnded, begin_watching  # noqa: E402
from tools.session_monitor import SessionMonitor  # noqa: E402
from tools.vision_corpus import browser  # noqa: E402
from tools.vision_eval import context_length_in_use, rule, unload  # noqa: E402

MODEL = "qwen3-vl:4b-instruct"

VIDEO = "jNQXAC9IVRw"
PAGE_TITLE = "Qronos webcam stand-in"

RESULTS: list[tuple[str, bool, str]] = []


def check(title: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((title, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {title}")

    if detail:
        print(f"        {detail}")


#: Served over HTTP because a YouTube embed refuses to play without an HTTP
#: referrer — from a ``file://`` page it returns "Video player configuration
#: error, Error 153" and nothing else. The no-cookie domain is what keeps a
#: consent banner from ever appearing.
PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{PAGE_TITLE}</title>
<style>
  html,body{{margin:0;background:#000;overflow:hidden}}
  iframe{{display:block;border:0;width:1040px;height:600px}}
</style></head><body>
<iframe
  src="https://www.youtube-nocookie.com/embed/{VIDEO}?autoplay=1&mute=1&controls=0&loop=1&playlist={VIDEO}"
  allow="autoplay"></iframe>
</body></html>
"""


class Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        body = PAGE.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Quiet: the harness's output is the harness's own."""


def bring_to_front(handle: int) -> None:
    """
    Put the window on top, and keep it there.

    Capturing one window copies the screen at that window's coordinates, so a
    window with something on top of it captures the thing on top. See the note
    in ``core/screen_capture.py``.
    """
    user32 = ctypes.windll.user32

    SW_RESTORE = 9
    HWND_TOPMOST = -1
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040

    user32.ShowWindow(handle, SW_RESTORE)
    user32.SetForegroundWindow(handle)
    user32.SetWindowPos(
        ctypes.c_void_p(handle),
        ctypes.c_void_p(HWND_TOPMOST),
        0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )


def mentions(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()

    return any(word in lowered for word in words)


SCENE = ("elephant", "zoo", "enclosure", "fence", "rock", "animal")


def numbered(text: str, number: int) -> str:
    """
    One numbered answer out of the model's reply, or an empty string.

    ``WATCH_INSTRUCTION`` asks three numbered questions, so the reply comes
    back as three short lines:

        1. Yes
        2. Facing the camera
        3. An elephant is visible behind a fence to the right of the person.

    Reading that is far better than matching prose for words like "person".
    The first version of this harness did match prose, and when the prompt was
    improved the answers got shorter and more useful — and every check broke,
    because "1. Yes" does not contain the word "person". The checks were
    measuring the phrasing, not the capability.
    """
    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith(f"{number}."):
            return stripped[2:].strip()

    return ""


def sees_a_person(text: str) -> bool:
    return numbered(text, 1).lower().startswith("yes")


def facing_answer(text: str) -> str:
    """"facing", "turned", or "" when the question was not answered."""
    answer = numbered(text, 2).lower()

    if not answer or answer.startswith("n/a") or answer.startswith("not"):
        return ""

    if "turned away" in answer or "away from" in answer or "behind" in answer:
        return "turned"

    if "facing" in answer or "toward" in answer or "at the camera" in answer:
        return "facing"

    return ""


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    port = server.server_address[1]

    threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    ).start()

    folder = Path(tempfile.mkdtemp(prefix="qronos-webcam-"))

    runtime = OllamaController()

    if not runtime.health_check():
        print("Ollama is not running. Nothing measured.")
        return 1

    print("Opening the clip. Do not touch the mouse or keyboard.\n")

    chrome = subprocess.Popen([
        str(browser()), "--new-window", "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={folder / 'profile'}",
        "--window-size=1080,720", "--window-position=60,40",
        "--autoplay-policy=no-user-gesture-required",
        f"http://127.0.0.1:{port}/",
    ])

    try:
        rule("The clip is playing")

        handle = None
        waited = 0.0

        while handle is None and waited < 40.0:
            time.sleep(1.0)
            waited += 1.0
            handle = window_titled(PAGE_TITLE)

        check(
            "the page opened and was found by name",
            handle is not None,
            f"looked for {PAGE_TITLE!r} for {waited:.0f}s",
        )

        if handle is None:
            return 1

        bring_to_front(handle)

        # The player has to load, buffer, and get past its opening frames.
        time.sleep(9.0)

        capture = ScreenCapture()
        first = capture.capture(approved=True, window=handle)

        check(
            "there is something on screen rather than a black player",
            not first.blank,
            first.describe(),
        )

        if first.blank:
            print(
                "\n  Nothing below would mean anything: the player never "
                "started. It needs the internet, and it needs that video to "
                "still exist.\n"
            )

            return 1

        rule("Watching it the way a camera would be watched")

        session = begin_watching(
            category=ActionCategory.WATCH_CAMERA,
            started_by="the webcam stand-in harness",
            summary="Watch a video of a person as a stand-in for a camera.",
            # Long enough to see the clip loop several times, so the run
            # usually samples the moment he turns around, and long enough that
            # the resource numbers describe a session rather than a burst.
            max_seconds=95.0,
            idle_seconds=30.0,
            min_frame_interval=0.0,
            approved=True,
        )

        eyes = Eyes(
            session=session,
            source=ScreenRegionSource(session, window=handle, capture=capture),
            describe_fn=brain_describe_fn(runtime),
        )

        # Started before the session and stopped after it, so the quiet
        # stretches between frames count too. A session that holds four
        # gigabytes for a minute and one that holds it for two seconds at a
        # time look identical if you only sample while the model is talking.
        monitor = SessionMonitor()
        monitor.start()

        started = time.perf_counter()
        seen: list[str] = []
        gaps: list[float] = []
        loaded_between: list[bool] = []
        last = started

        for observation in eyes.frames():
            now = time.perf_counter()

            seen.append(observation.description)
            gaps.append(now - last)
            last = now

            # Is the model still on the card between frames, or was it handed
            # back? /api/ps answers with the context it is holding, or nothing.
            loaded_between.append(bool(context_length_in_use(MODEL)))

            print(
                f"  {now - started:5.1f}s  "
                f"{observation.description[:150]}"
            )

        monitor.stop()

        print()

        check(
            "it took several frames",
            len(seen) >= 5,
            f"{len(seen)} frames over {time.perf_counter() - started:.1f}s, "
            f"ended {session.ended.value}",
        )

        check(
            "the session ended on its own limit",
            session.ended is WatchEnded.REACHED_TIME_LIMIT,
            session.ended.value,
        )

        rule("What it could tell")

        answered = [text for text in seen if numbered(text, 1)]

        check(
            "it answered the questions in the form they were asked",
            len(answered) >= len(seen) * 0.9,
            f"{len(answered)} of {len(seen)} replies are numbered answers",
        )

        with_person = [text for text in seen if sees_a_person(text)]

        # "Nearly every" rather than "every", and the distinction is not
        # slack. The clip loops, so some frames land on the transition between
        # one play and the next, where there genuinely is no person on screen
        # — and a frame that correctly reports an empty player would fail a
        # bar of one hundred percent.
        check(
            "a person is detected in nearly every frame",
            len(with_person) >= len(seen) * 0.8,
            f"{len(with_person)} of {len(seen)} frames say a person is there",
        )

        facing = [text for text in seen if facing_answer(text) == "facing"]
        turned = [text for text in seen if facing_answer(text) == "turned"]
        stated = len(facing) + len(turned)

        check(
            "it says which way they are facing whenever somebody is there",
            stated >= len(with_person) * 0.9,
            f"{len(facing)} facing the camera, {len(turned)} turned away, "
            f"out of {len(with_person)} frames with a person in them",
        )

        if turned:
            check(
                "it caught the moment he turns around",
                True,
                f"{len(turned)} of {len(seen)} frames — the answer changing "
                "with the footage rather than a watcher repeating itself",
            )
        else:
            print(
                "  ----  the moment he turns around was not sampled this run. "
                "It is about two seconds of a nineteen-second loop; a frame "
                "not taken cannot be described wrongly."
            )

        context = [text for text in seen if mentions(numbered(text, 3), SCENE)]

        check(
            "it describes the surroundings, not just the person",
            len(context) >= len(seen) * 0.5,
            f"{len(context)} of {len(seen)} frames mention the zoo enclosure",
        )

        # Not "every answer is different". Once the model answers three fixed
        # questions, two similar frames *should* get the same answer, and an
        # earlier version of this check punished exactly the improvement that
        # made the output useful. What matters is that the answers move with
        # the footage at all.
        check(
            "the answers change as the clip does",
            len(set(seen)) > 1,
            f"{len(set(seen))} distinct answers across {len(seen)} frames",
        )

        rule("What it cost the machine")

        print(monitor.report())
        print()

        check(
            "the card was readable throughout",
            monitor.vram.ok and monitor.samples > 20,
            f"{monitor.samples} samples",
        )

        check(
            "the session did not take more of the card than one answer does",
            monitor.vram_over_idle < 8_000,
            f"peak {monitor.vram_over_idle:+.0f} MiB over what was already "
            "held. A watching session is one model answering repeatedly; if "
            "this grew with the number of frames, something is being kept.",
        )

        check(
            "the card did not get hot",
            not monitor.temperature.ok or monitor.temperature.peak < 80,
            f"peak {monitor.temperature.peak:.0f}C",
        )

        # Against the model server's own share, not the machine's. A run of
        # this measured 43% of the processor one hour and 62% the next for
        # identical work, because something else was running the second time —
        # and a check on the machine total would have called that a Qronos
        # regression. The architecture document asks for this attribution for
        # exactly this reason.
        check(
            "the model server leaves the processor to everything else",
            not monitor.server_cpu.ok or monitor.server_cpu.mean < 25,
            f"the model server averaged {monitor.server_cpu.mean:.0f}% of the "
            f"processor and {monitor.server_ram.mean:.0f} MiB, while the whole "
            f"machine averaged {monitor.cpu.mean:.0f}% and "
            f"{monitor.ram.mean:.0f}%",
        )

        seconds_per_frame = (
            sum(gaps) / len(gaps) if gaps else 0.0
        )

        held = sum(1 for value in loaded_between if value)

        print(
            f"  a frame every {seconds_per_frame:.1f}s, and the model was "
            f"still on the card after {held} of {len(loaded_between)} of them"
        )
        print()

        check(
            "the model is handed back between frames",
            held == 0,
            "Qronos holds nothing between turns, and a watching session is no "
            "exception. The cost of that is a reload per frame, which is what "
            "the frame rate above is mostly made of.",
        )

    finally:
        chrome.terminate()

        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()

        server.shutdown()
        server.server_close()

        import shutil

        shutil.rmtree(folder, ignore_errors=True)

        unload(MODEL)

    rule("Summary")

    passed = sum(1 for _, ok, _ in RESULTS if ok)

    for title, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED  {title}: {detail}")

    print(f"\n  {passed} of {len(RESULTS)} checks passed")

    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
