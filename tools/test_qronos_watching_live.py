"""
Watching something move: can the model tell what is happening in a live view?

There is no camera on the machine this was built on, so the camera itself is
stood in for the way you asked: **a video playing in a browser window**, watched
through the screen at the rate a camera would be watched at. That is not a
weaker test than a real camera would be. Every part that is not the twenty
lines talking to the device is the same code — the session, the pacing, the
limits, the indicator, the describing, the dropping of frames — and a moving
picture of a scene is what a camera produces.

The video is generated here rather than fetched or taken from this machine. A
clip off the internet is a dependency on somebody's server staying up, and a
clip off this machine is footage of identifiable people, which is not going
anywhere near a public repository. So it is a drawn scene that changes in ways
with known answers: a figure that appears and leaves, an object it is holding
that changes colour, and a counter that says which second it is.

What is being asked:

    **Does it see the difference between frames?** A watcher that says the same
    thing about every frame is not watching, it is describing a still.

    **Does it notice a person arriving and leaving?** The load-bearing question
    for a camera: "is somebody there".

    **Does it read what is being held up?** The one thing a webcam is genuinely
    better than a screen at — showing the assistant something.

    **Does the session actually end?** On its limit, and on being stopped, with
    frames stopping at the same moment.

Run with Chrome installed:

    python tools/test_qronos_watching_live.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ollama_controller import OllamaController  # noqa: E402
from core.screen_capture import (  # noqa: E402
    ScreenCapture,
    window_titled,
)
from core.vision_worker import brain_describe_fn  # noqa: E402
from core.watching_eyes import Eyes, ScreenRegionSource  # noqa: E402
from security.permissions import ActionCategory  # noqa: E402
from security.watching import (  # noqa: E402
    WatchEnded,
    WatchingSession,
    begin_watching,
)
from tools.vision_corpus import browser  # noqa: E402
from tools.vision_eval import rule, unload  # noqa: E402

MODEL = "qwen3-vl:4b-instruct"

RESULTS: list[tuple[str, bool, str]] = []


def check(title: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((title, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {title}")

    if detail:
        print(f"        {detail}")


#: The window is found by this rather than by whichever one has focus. A first
#: version of this harness used the foreground window, opened a browser that
#: did not take focus, and confidently reported on a game somebody had left
#: running. Finding your own window by name is the fix.
PAGE_TITLE = "Qronos watching test scene"

#: The scene repeats on a twelve-second loop, so a watcher taking a frame every
#: five or six seconds — which is how fast the model can describe one — sees
#: every state of it more than once during a run, including the empty room.
#:
#: 0-4s    an empty room
#: 4-8s    a figure, facing forward, holding a RED card
#: 8-12s   the same figure holding a GREEN card
SCENE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title><style>
  html, body { margin: 0; background: #202226; overflow: hidden; }
  .room { position: relative; width: 900px; height: 510px;
          background: linear-gradient(#3a3f47, #24272c); }
  .desk { position: absolute; left: 0; bottom: 0; width: 100%; height: 120px;
          background: #5a4632; }
  .clock { position: absolute; top: 10px; left: 18px; color: #e8e8e8;
           font: 24px "Segoe UI", sans-serif; }
  .person { position: absolute; left: 250px; bottom: 96px;
            width: 190px; opacity: 0; transition: opacity .25s; }
  .head { width: 108px; height: 124px; margin: 0 auto; border-radius: 52px;
          background: #d8b08a; position: relative; }
  .eye { position: absolute; top: 50px; width: 14px; height: 14px;
         border-radius: 50%; background: #23242a; }
  .eye.l { left: 24px; } .eye.r { right: 24px; }
  .mouth { position: absolute; bottom: 24px; left: 36px; width: 36px;
           height: 8px; border-radius: 4px; background: #8a4a44; }
  .torso { width: 190px; height: 120px; border-radius: 22px 22px 0 0;
           background: #33608c; }
  .card { position: absolute; right: -160px; bottom: 30px; width: 150px;
          height: 100px; border-radius: 8px; border: 4px solid #101215; }
  .visible { opacity: 1; }
</style></head><body>
<div class="room">
  <div class="clock" id="clock">0</div>
  <div class="desk"></div>
  <div class="person" id="person">
    <div class="head"><div class="eye l"></div><div class="eye r"></div>
      <div class="mouth"></div></div>
    <div class="torso"></div>
    <div class="card" id="card"></div>
  </div>
</div>
<script>
  const started = Date.now();
  const person = document.getElementById('person');
  const card = document.getElementById('card');
  const clock = document.getElementById('clock');

  setInterval(() => {
    const second = Math.floor((Date.now() - started) / 1000);
    const phase = second % 12;

    clock.textContent = second;
    person.classList.toggle('visible', phase >= 4);
    card.style.background = phase < 8 ? '#c0392b' : '#27ae60';
  }, 100);
</script>
</body></html>
""".replace("__TITLE__", PAGE_TITLE)


#: The desk in the scene, as drawn. Used to prove the capture is of the scene
#: and not of whatever was behind it — see :func:`shows_the_scene`.
DESK_RGB = (0x5A, 0x46, 0x32)


def bring_to_front(handle: int) -> None:
    """
    Put the window on top, and keep it there.

    Capturing one window copies the screen at that window's coordinates, so a
    window with something on top of it captures the thing on top — see the note
    in ``core/screen_capture.py``.

    Bringing it forward once is not enough. On the machine this was written on
    a full-screen application kept reclaiming the foreground, and a run that
    started correctly ended up describing that instead. ``HWND_TOPMOST`` keeps
    the scene above it for the length of the run, and every frame is checked
    anyway, because "keep it there" is a request and not a guarantee.
    """
    import ctypes

    user32 = ctypes.windll.user32

    SW_RESTORE = 9
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040

    user32.ShowWindow(handle, SW_RESTORE)
    user32.SetForegroundWindow(handle)
    user32.BringWindowToTop(handle)
    user32.SetWindowPos(
        ctypes.c_void_p(handle),
        ctypes.c_void_p(HWND_TOPMOST),
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )


def shows_the_scene(png: bytes) -> bool:
    """
    Whether this really is a picture of the scene.

    A hard gate, not a nicety. The first version of this harness captured the
    wrong window for an entire run and every content check passed anyway,
    because a check that looks for the word "person" in a description finds one
    in a description of almost anything. Proving the pixels are the right
    pixels has to come before believing a single word about them.

    It looks over the bottom third rather than at one row. The window includes
    the browser's own tab bar, so where the desk lands depends on how tall that
    is — and a version of this that sampled a single computed row missed the
    desk entirely and failed a run that was perfectly correct.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        pixels = image.convert("RGB")
        width, height = pixels.size
        data = pixels.load()

        matches = 0

        for y in range(int(height * 0.66), height, 3):
            for x in range(0, width, 6):
                red, green, blue = data[x, y]

                if (
                    abs(red - DESK_RGB[0]) < 24
                    and abs(green - DESK_RGB[1]) < 24
                    and abs(blue - DESK_RGB[2]) < 24
                ):
                    matches += 1

                    # A run of desk-coloured pixels, not one stray pixel that
                    # happens to be brown in a photograph of something else.
                    if matches > 200:
                        return True

    return False


def describes_person(text: str) -> bool:
    lowered = text.lower()

    return any(
        word in lowered
        for word in ("person", "man", "woman", "figure", "someone", "face",
                     "human", "character")
    )


def describes_empty(text: str) -> bool:
    lowered = text.lower()

    return any(
        word in lowered
        for word in ("empty", "no person", "nobody", "no one", "unoccupied",
                     "no people", "no visible person", "does not show")
    )


def main() -> int:
    chrome = browser()
    folder = Path(tempfile.mkdtemp())
    scene = folder / "scene.html"
    scene.write_text(SCENE, encoding="utf-8")

    runtime = OllamaController()

    if not runtime.health_check():
        print("Ollama is not running. Nothing measured.")
        return 1

    describe = brain_describe_fn(runtime)

    print("Opening the scene. Do not touch the mouse or keyboard.\n")

    # Its own profile, so it is its own process and its own window rather
    # than a tab in whatever browser was already open.
    profile = folder / "profile"

    window = subprocess.Popen(
        [
            str(chrome),
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            "--window-size=980,700",
            "--window-position=40,40",
            f"file:///{scene.as_posix()}",
        ]
    )

    try:
        rule("The window is there and it is not blank")

        handle = None
        waited = 0.0

        while handle is None and waited < 25.0:
            time.sleep(0.5)
            waited += 0.5
            handle = window_titled(PAGE_TITLE)

        check(
            "the scene window opened and was found by name",
            handle is not None,
            f"looked for {PAGE_TITLE!r} for {waited:.1f}s",
        )

        if handle is None:
            return 1

        bring_to_front(handle)

        # A moment for the first paint and for the window to come forward.
        # Chrome creates its window, and names it, before it has drawn
        # anything into it.
        time.sleep(4.0)

        capture = ScreenCapture()
        first = capture.capture(approved=True, window=handle)

        check(
            "the scene is on screen",
            not first.blank,
            first.describe(),
        )

        check(
            "and it is the scene, not whatever was behind it",
            shows_the_scene(first.image.data),
            "found the scene's desk colour in the capture",
        )

        if first.blank or not shows_the_scene(first.image.data):
            print(
                "\n  Nothing below this would mean anything: the capture is "
                "not of the scene.\n"
            )

            return 1

        rule("Watching: does it see a scene change?")

        session = begin_watching(
            category=ActionCategory.WATCH_CAMERA,
            started_by="the live watching harness",
            summary="Watch a generated scene as a stand-in for a camera.",
            # A second between attempts. Describing a frame takes the
            # model five or six seconds, so it is the pacing for frames that
            # count — but a frame discarded for being of the wrong window
            # costs nothing, and with no interval the loop spins on those.
            max_seconds=60.0,
            idle_seconds=30.0,
            min_frame_interval=1.0,
            approved=True,
        )

        off_scene = []

        def describe_verified(question, images):
            """
            Describe the frame, but only after proving it is the right frame.

            Every frame, not just the first. A run that starts on the scene can
            end up somewhere else the moment another window takes the front, and
            a description of the wrong thing reads exactly like a description of
            the right thing.
            """
            if not all(shows_the_scene(image.data) for image in images):
                off_scene.append(1)

                return "[not the scene]"

            return describe(question, images)

        eyes = Eyes(
            session=session,
            source=ScreenRegionSource(session, window=handle, capture=capture),
            describe_fn=describe_verified,
        )

        seen: list[str] = []
        started = time.perf_counter()

        for observation in eyes.frames():
            if observation.description == "[not the scene]":
                print(
                    f"  {time.perf_counter() - started:5.1f}s  "
                    "(something covered the window; frame discarded)"
                )
                continue

            seen.append(observation.description)
            print(
                f"  {time.perf_counter() - started:5.1f}s  "
                f"{observation.description[:110]}"
            )

        check(
            "most of the run was actually of the scene",
            len(seen) >= len(off_scene),
            f"{len(seen)} frames of the scene, {len(off_scene)} of something "
            "else. A machine somebody is using has other windows on it; what "
            "matters is that the wrong ones were thrown away rather than "
            "described.",
        )

        check(
            "it took several frames",
            len(seen) >= 5,
            f"{len(seen)} frames over {time.perf_counter() - started:.1f}s, "
            f"ended {session.ended.value}",
        )

        check(
            "the frames are not all described the same way",
            len(set(seen)) > 1,
            f"{len(set(seen))} distinct descriptions from {len(seen)} frames",
        )

        check(
            "it noticed somebody was there",
            any(describes_person(text) for text in seen),
            "at least one frame reports a person",
        )

        check(
            "it noticed the room without anybody in it",
            any(
                describes_empty(text) or not describes_person(text)
                for text in seen
            ),
            "at least one frame reports no person",
        )

        colours = " ".join(seen).lower()

        check(
            "it read the object being held up, in both colours",
            "red" in colours and "green" in colours,
            f"red seen: {'red' in colours}, green seen: {'green' in colours}",
        )

        check(
            "it is describing this scene and not something else",
            sum(
                1
                for text in seen
                if any(
                    word in text.lower()
                    # Words that fit this drawn scene and not a photograph
                    # or a game. An earlier version listed "wooden", which
                    # matched a description of some wooden beams in a game and
                    # let a whole wrong run pass; "cartoon" and "avatar" cannot
                    # do that.
                    for word in ("desk", "table", "card", "rectangle",
                                 "cartoon", "avatar", "stylized", "stylised",
                                 "2d", "simple")
                )
            )
            >= len(seen) // 2,
            "most frames mention the desk or the card, which are what is in it",
        )

        rule("The session ends when it says it will")

        quick = WatchingSession(
            category=ActionCategory.WATCH_CAMERA,
            started_by="the live watching harness",
            max_seconds=3.0,
            idle_seconds=10.0,
            min_frame_interval=1.0,
        )

        quick_eyes = Eyes(
            session=quick,
            source=ScreenRegionSource(quick, window=handle, capture=capture),
            describe_fn=describe,
        )

        summary = quick_eyes.watch()

        check(
            "it stopped at its own time limit",
            summary.ended is WatchEnded.REACHED_TIME_LIMIT,
            summary.describe(),
        )

        check(
            "the indicator went down when it stopped",
            not quick.indicator_visible,
            f"state {quick.state.value}",
        )

        check(
            "a stopped session takes no more frames",
            not quick.may_take_frame().allowed,
            quick.may_take_frame().reason,
        )

        rule("Nothing was kept")

        check(
            "what survives the session is sentences, not pictures",
            all(
                isinstance(observation.description, str)
                and not isinstance(observation.picture, bytes)
                for observation in summary.observations
            ),
            f"{len(summary.observations)} observations, no frames retained",
        )

    finally:
        window.terminate()

        try:
            window.wait(timeout=10)
        except subprocess.TimeoutExpired:
            window.kill()

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
