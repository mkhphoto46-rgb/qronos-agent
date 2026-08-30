"""
Test images for the vision work, drawn from code so the answers are known.

Every image here is generated, never captured. That is not tidiness: a
screenshot of a real desktop is a photograph of whatever the person had open,
and this repository is public. Generating them also means the ground truth is
the *input* rather than something somebody had to label, so a score is a
measurement rather than an opinion.

**Rendered through Chrome, not through Pillow.** Pillow on this machine is
built without raqm, so it cannot shape Arabic script — Persian comes out as
disconnected letters in left-to-right order, which no model could read and
which would make the model look worse than it is. Chrome shapes and orders it
the way a real application does, which is the point: these images should look
like something that was actually on a screen.

Four families, matching what the vision work has to do:

    read      known text at known sizes, Persian and English
    identify  a screenshot, a photograph, a chart — which is it
    count     interfaces whose contents are known by construction
    locate    elements at known positions, for bounding boxes

Nothing here is committed as an image. The generator is the artefact; the
pictures live under ``temp/`` and are rewritten on every run.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = ROOT / "temp" / "vision_corpus"

#: Where Chrome lives on Windows. Checked in order; the first that exists wins.
CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)


def browser() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate

    raise SystemExit(
        "No Chrome or Edge found. One of them renders the test images, "
        "because Pillow here cannot shape Persian."
    )


@dataclass
class Item:
    """One image and everything known to be true about it."""

    name: str
    family: str
    html: str
    width: int
    height: int

    #: What the image says, exactly, when the question is "read this".
    text: str = ""

    #: The language of that text, for scoring.
    language: str = ""

    #: A single unambiguous token to ask for — an error code, a name.
    target: str = ""

    #: Answers to closed questions, keyed by question id.
    answers: dict = field(default_factory=dict)

    #: Element boxes as fractions of width and height, for grounding.
    boxes: dict = field(default_factory=dict)

    #: Roughly how large the text is drawn, in CSS pixels.
    font_px: int = 0


# ---------------------------------------------------------------- templates


def page(body: str, *, rtl: bool = False, style: str = "") -> str:
    direction = 'lang="fa" dir="rtl"' if rtl else 'lang="en" dir="ltr"'

    return f"""<!doctype html><html {direction}><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #f3f3f3;
          font-family: 'Segoe UI', Tahoma, 'Iranian Sans', sans-serif;
          -webkit-font-smoothing: antialiased; }}
  {style}
</style></head><body>{body}</body></html>"""


def visible_text(title: str, message: str, buttons: tuple[str, ...]) -> str:
    """
    Everything a reader would see in the window, in reading order.

    The ground truth has to be everything, because "transcribe this image" is
    answered with everything — the title bar and the button labels included.
    Scoring a complete transcription against only the message counts correct
    reading as error, which is what the first run of this did.
    """
    return " ".join([title, message, *buttons])


def dialog(title: str, message: str, font_px: int, *, rtl: bool = False,
           buttons: tuple[str, ...] = ("OK",)) -> str:
    """A window that looks like a real one, because real ones are the subject."""
    rendered = "".join(
        f'<button class="btn{" primary" if i == 0 else ""}">{label}</button>'
        for i, label in enumerate(buttons)
    )

    return page(
        f"""<div class="win">
  <div class="bar"><span class="t">{title}</span><span class="x">&#10005;</span></div>
  <div class="body"><p class="msg">{message}</p></div>
  <div class="foot">{rendered}</div>
</div>""",
        rtl=rtl,
        style=f"""
  .win {{ margin: 18px; background: #fff; border: 1px solid #c8c8c8;
          border-radius: 6px; box-shadow: 0 2px 10px rgba(0,0,0,.12); }}
  .bar {{ display: flex; justify-content: space-between; align-items: center;
          padding: 8px 12px; background: #eaeaea;
          border-bottom: 1px solid #d8d8d8; font-size: 13px; }}
  .t {{ font-weight: 600; color: #222; }}
  .x {{ color: #666; }}
  .body {{ padding: 18px 20px; }}
  .msg {{ margin: 0; font-size: {font_px}px; line-height: 1.55; color: #111; }}
  .foot {{ padding: 12px 16px; display: flex; gap: 8px;
           justify-content: flex-end; border-top: 1px solid #eee; }}
  .btn {{ padding: 6px 18px; font-size: 13px; border: 1px solid #bbb;
          border-radius: 4px; background: #fafafa; color: #111; }}
  .primary {{ background: #0067c0; border-color: #0067c0; color: #fff; }}
""",
    )


# ------------------------------------------------------------------ corpus

#: Persian lines with their exact text. Real sentences of the kind an
#: assistant would be asked to read off a screen.
PERSIAN_LINES = (
    ("fa_short", "پرونده ذخیره شد."),
    ("fa_error", "اتصال به سرور برقرار نشد. دوباره تلاش کنید."),
    ("fa_long",
     "به‌روزرسانی سامانه با موفقیت انجام شد و همه تنظیمات شما حفظ شده است."),
)

ENGLISH_LINES = (
    ("en_short", "The file was saved."),
    ("en_error", "Could not reach the server. Check your connection."),
    ("en_long",
     "The update completed successfully and all of your settings were kept."),
)

#: Sizes in CSS pixels. 12 is ordinary interface text; 32 is a heading.
FONT_SIZES = (12, 16, 24, 32)

#: The fill of the primary button, as drawn in the stylesheet above. Used to
#: measure where it actually landed once Chrome has laid the page out, which
#: is more reliable than any arithmetic over padding and font metrics.
PRIMARY_RGB = (0, 103, 192)

#: A code that is unambiguous to score: exact match or nothing.
ERROR_CODES = (
    ("code_hex", "0xC0000225"),
    ("code_mixed", "ERR-4471-BX"),
)


def build_items() -> list[Item]:
    items: list[Item] = []

    # --- read: the same sentence at four sizes, both languages -------------
    for language, lines, rtl, title in (
        ("fa", PERSIAN_LINES, True, "پیام سامانه"),
        ("en", ENGLISH_LINES, False, "System message"),
    ):
        for key, text in lines:
            for size in FONT_SIZES:
                buttons = ("تأیید",) if rtl else ("OK",)
                items.append(
                    Item(
                        name=f"read_{key}_{size}px",
                        family="read",
                        html=dialog(title, text, size, rtl=rtl,
                                    buttons=buttons),
                        width=760,
                        height=260,
                        text=visible_text(title, text, buttons),
                        language=language,
                        font_px=size,
                    )
                )

    # --- read: an error code, scored by exact match ------------------------
    for key, code in ERROR_CODES:
        for size in (12, 20):
            message = f"The operation failed. Error code: {code}"
            items.append(
                Item(
                    name=f"target_{key}_{size}px",
                    family="target",
                    html=dialog("Error", message, size),
                    width=760,
                    height=260,
                    text=message,
                    language="en",
                    target=code,
                    font_px=size,
                )
            )

        # And the same code inside Persian text, which is the real case:
        # Windows is English, the user is not.
        message_fa = f"عملیات ناموفق بود. کد خطا: {code}"
        items.append(
            Item(
                name=f"target_{key}_fa",
                family="target",
                html=dialog("خطا", message_fa, 18, rtl=True,
                            buttons=("تأیید",)),
                width=760,
                height=260,
                text=message_fa,
                language="fa",
                target=code,
                font_px=18,
            )
        )

    # --- identify: screenshot, chart, photograph ---------------------------
    items.append(
        Item(
            name="identify_screenshot",
            family="identify",
            html=dialog("Settings", "Choose how Qronos starts with Windows.",
                        15, buttons=("Save", "Cancel")),
            width=760,
            height=260,
            answers={"kind": "screenshot", "interface": "yes"},
        )
    )

    bars = "".join(
        f'<div class="bar" style="height:{h}%"><span>{h}</span></div>'
        for h in (35, 62, 48, 81, 27)
    )
    items.append(
        Item(
            name="identify_chart",
            family="identify",
            html=page(
                f'<div class="chart">{bars}</div><p class="cap">Weekly totals</p>',
                style="""
  .chart { display: flex; align-items: flex-end; gap: 22px; height: 200px;
           padding: 20px 30px; background: #fff; margin: 16px; }
  .bar { width: 54px; background: #3b7dd8; color: #fff; font-size: 12px;
         display: flex; align-items: flex-start; justify-content: center;
         padding-top: 4px; border-radius: 3px 3px 0 0; }
  .cap { text-align: center; font-size: 14px; margin: 0 0 12px; color: #333; }
""",
            ),
            width=520,
            height=300,
            answers={"kind": "chart", "interface": "no"},
        )
    )

    # A photograph cannot be drawn in CSS, and calling this one would measure
    # the test rather than the model — asked whether a gradient landscape is a
    # photograph, it reasonably said "screenshot", because it is a rendered
    # image. So the question asked of this family is the one the product
    # actually needs: is this a computer interface, or is it not.
    items.append(
        Item(
            name="identify_scene",
            family="identify",
            html=page(
                '<div class="sky"><div class="sun"></div><div class="hill"></div>'
                '<div class="hill two"></div></div>',
                style="""
  .sky { position: relative; width: 100%; height: 300px; overflow: hidden;
         background: linear-gradient(#7fb2e5, #cfe3f5 60%, #e8f1f8); }
  .sun { position: absolute; top: 40px; left: 380px; width: 70px; height: 70px;
         border-radius: 50%; background: #ffd76a; box-shadow: 0 0 40px #ffe9a8; }
  .hill { position: absolute; bottom: -60px; left: -40px; width: 380px;
          height: 220px; border-radius: 50%; background: #5b8f5a; }
  .hill.two { left: 240px; bottom: -80px; width: 420px; height: 250px;
              background: #477a48; }
""",
            ),
            width=520,
            height=300,
            answers={"interface": "no"},
        )
    )

    # --- count and state: answers fixed by construction --------------------
    items.append(
        Item(
            name="count_buttons_three",
            family="count",
            html=dialog("Confirm", "Save changes before closing?", 15,
                        buttons=("Save", "Don't save", "Cancel")),
            width=760,
            height=250,
            answers={"buttons": "3", "primary": "Save"},
        )
    )
    items.append(
        Item(
            name="count_buttons_two",
            family="count",
            html=dialog("Confirm", "Delete this item?", 15,
                        buttons=("Delete", "Cancel")),
            width=760,
            height=250,
            answers={"buttons": "2", "primary": "Delete"},
        )
    )

    for ticked, label in ((True, "yes"), (False, "no")):
        mark = "&#10003;" if ticked else ""
        items.append(
            Item(
                name=f"state_checkbox_{label}",
                family="state",
                html=page(
                    f"""<div class="win"><div class="bar">Startup</div>
<div class="body"><label class="row"><span class="box">{mark}</span>
<span class="lbl">Start Qronos when Windows starts</span></label></div></div>""",
                    style="""
  .win { margin: 18px; background: #fff; border: 1px solid #c8c8c8;
         border-radius: 6px; }
  .bar { padding: 8px 12px; background: #eaeaea; font-size: 13px;
         font-weight: 600; border-bottom: 1px solid #d8d8d8; }
  .body { padding: 22px; }
  .row { display: flex; align-items: center; gap: 10px; font-size: 15px; }
  .box { width: 18px; height: 18px; border: 1px solid #666; background: #fff;
         display: inline-flex; align-items: center; justify-content: center;
         font-size: 13px; color: #0067c0; }
""",
                ),
                width=620,
                height=170,
                answers={"checked": label},
            )
        )

    for percent in (25, 70):
        items.append(
            Item(
                name=f"state_progress_{percent}",
                family="state",
                html=page(
                    f"""<div class="win"><div class="bar">Copying files</div>
<div class="body"><div class="track"><div class="fill" style="width:{percent}%"></div></div>
<p class="pct">{percent}% complete</p></div></div>""",
                    style="""
  .win { margin: 18px; background: #fff; border: 1px solid #c8c8c8;
         border-radius: 6px; }
  .bar { padding: 8px 12px; background: #eaeaea; font-size: 13px;
         font-weight: 600; border-bottom: 1px solid #d8d8d8; }
  .body { padding: 22px; }
  .track { height: 16px; background: #e6e6e6; border-radius: 8px;
           overflow: hidden; }
  .fill { height: 100%; background: #0067c0; }
  .pct { font-size: 14px; color: #222; margin: 12px 0 0; }
""",
                ),
                width=620,
                height=180,
                answers={"percent": str(percent)},
            )
        )

    # --- locate: one element at a known place ------------------------------
    items.append(
        Item(
            name="locate_primary_button",
            family="locate",
            html=dialog("Install", "Ready to install the update.", 15,
                        buttons=("Install now", "Later")),
            width=760,
            height=250,
            # The box is measured from the rendered pixels after drawing, not
            # written down here. The first attempt at this file estimated it
            # by eye, scored the model as completely wrong, and was itself the
            # thing that was wrong — the model had it almost exactly.
            boxes={},
            answers={"primary": "Install now"},
        )
    )

    return items


# ----------------------------------------------------------------- render


def measure_primary_button(image_path: Path) -> tuple[float, ...] | None:
    """
    Where the primary button actually is, as fractions of the image.

    Found by its fill colour rather than calculated, because the browser
    decides the final layout and only the pixels know what it decided. Exact
    colour matching, not near-matching: antialiasing at the window's edges is
    close enough to the button's blue to swallow the whole dialog otherwise,
    which is exactly what a first attempt did.
    """
    from PIL import Image

    with Image.open(image_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        pixels = image.load()

        xs: list[int] = []
        ys: list[int] = []

        for y in range(height):
            for x in range(width):
                if pixels[x, y] == PRIMARY_RGB:
                    xs.append(x)
                    ys.append(y)

    if not xs:
        return None

    return (
        min(xs) / width,
        min(ys) / height,
        max(xs) / width,
        max(ys) / height,
    )


def render(items: list[Item], scale: int = 1) -> Path:
    """Draw every item and write a manifest beside them."""
    chrome = browser()

    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)

    OUT.mkdir(parents=True, exist_ok=True)
    pages = OUT / "_html"
    pages.mkdir(exist_ok=True)

    manifest = []

    for item in items:
        source = pages / f"{item.name}.html"
        source.write_text(item.html, encoding="utf-8")

        target = OUT / f"{item.name}.png"

        subprocess.run(
            [
                str(chrome),
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--force-device-scale-factor={scale}",
                f"--screenshot={target}",
                f"--window-size={item.width},{item.height}",
                str(source),
            ],
            capture_output=True,
            timeout=120,
        )

        if not target.is_file():
            print(f"  FAILED to render {item.name}")
            continue

        record = asdict(item)
        record.pop("html")
        record["path"] = str(target)

        if item.family == "locate":
            found = measure_primary_button(target)

            if found is not None:
                record["boxes"] = {"primary": list(found)}
        record["sha"] = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
        manifest.append(record)

    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return OUT / "manifest.json"


def load() -> list[dict]:
    path = OUT / "manifest.json"

    if not path.is_file():
        raise SystemExit(
            f"No corpus yet. Run: .venv\\Scripts\\python.exe {Path(__file__).name}"
        )

    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    items = build_items()
    print(f"drawing {len(items)} images through {browser().name} ...")

    manifest = render(items)
    records = json.loads(manifest.read_text(encoding="utf-8"))

    families: dict[str, int] = {}
    for record in records:
        families[record["family"]] = families.get(record["family"], 0) + 1

    print(f"\n{len(records)} images written to {OUT}")
    for family, count in sorted(families.items()):
        print(f"  {family:<10} {count}")

    print(f"\nmanifest: {manifest}")

    return 0 if len(records) == len(items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
