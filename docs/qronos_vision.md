# Qronos Vision

Last updated: 2026-08-29

How Qronos looks at things: the model, what it costs, what it is allowed to
see, and every number that decides those.

Every figure here was measured on the development machine — an RTX 5080, a
3840x2160 display at 125% scaling — with the harnesses in `tools/`. Nothing in
this document is taken from documentation or from memory, and where a measured
number disagreed with the documented one, it is said so.

---

## The model

`qwen3-vl:4b-instruct`, Q4_K_M, 3.3 GB on disk. Its own model, loaded only when
there is something to look at, and unloaded the moment it has answered — like
every other model Qronos runs.

An earlier plan was to replace the text Fast Brain with a vision model so that
one model did both jobs. That was dropped, and the reason is worth keeping: the
attraction was "one model, one load", and that benefit does not exist. Qronos
already unloads after every turn, so two models are never both on the card.
Combining them would have made every ordinary chat turn load a vision tower it
never uses, to save disk space that is not short.

| | |
|---|---|
| Declared context | **4,096 tokens**, and measured to be honoured — `/api/ps` reports 4,096, not the model's own default. |
| The model's default | **262,144**, with no `num_ctx` in its params. This is the trap that once put a 2.3 GB model into 15.7 GB of card. |
| Graphics memory | **+4,475 MiB at peak, during generation.** Not the load figure, which is much smaller. Taking the load figure is the mistake already made once on the voice runtime, where it let a model onto a card it could only crawl on. |
| Cold start | 2.66 s, of which 2.16 s is loading. |
| Warm | 0.26 s. |
| Determinism | Identical output across five seeds at temperature zero, so any difference between two configurations is a real one. |

---

## What a picture costs

The documented rule for this model is one token per 32x32 patch. Above roughly
a megapixel that is exactly right. Below it, it is not:

| image | pixels | tokens |
|---|---|---|
| 64x64 | 4,096 | 1,041 |
| 512x512 | 262,144 | 1,041 |
| 1024x576 | 589,824 | 1,049 |
| 1280x720 | 921,600 | 1,092 |
| 1920x1080 | 2,073,600 | 2,057 |

There is a **floor of about 1,040 tokens**. The server enlarges anything
smaller before the model sees it, so everything below roughly 1280x720 costs
the same.

That has a direct consequence, and it is the one the whole capture strategy
turns on. Shrinking a screenshot past that point buys nothing and loses detail.
Measured on the same text at the same moment:

| long edge | tokens | time | character error |
|---|---|---|---|
| native (3072) | 1,740 | 3.5 s | 0.000 |
| **1280** | **1,081** | **0.5 s** | **0.000** |
| 1024 | 1,081 | 0.5 s | 0.353 |
| 512 | 1,081 | 0.5 s | 0.324 |

**1280 is the operating point.** Seven times faster than native for identical
accuracy; anything smaller is free in exactly the wrong sense.

`core/vision_image.py` holds this as `SEND_LONG_EDGE`, and resizing rounds to
whole 32-pixel patches, because a part-used patch costs a whole token.

---

## What it can read

Measured against a corpus of forty generated images (`tools/vision_corpus.py`).
Every one is drawn from HTML through headless Chrome rather than captured,
because a screenshot of a real desktop is a photograph of whatever was open on
it and this repository is public. The generator is committed; no image is.

| | |
|---|---|
| English, one dialog | **0.000 character error**, 1.000 word recall, at every font size from 12px to 32px |
| Persian, one dialog | **0.156 character error**, 0.850 word recall |
| Error codes | **6 of 6 exact**, including codes embedded in Persian sentences |
| Interface or not | 3 of 3 |
| Understanding | 8 of 8 — counting buttons, naming the primary one, reading a checkbox, reading a progress bar |
| Pointing at things | overlap **0.92** against a box measured from the rendered pixels |

Font size makes no difference, which follows from the token floor: 12px and
32px score identically because both images are enlarged to the same minimum.

The grounding result is worth a note, because upstream reports bounding boxes
being "significantly off". They are not, here — but the first version of that
test compared against a box estimated by eye, scored the model at zero overlap,
and was itself the thing that was wrong. The corpus now finds the button by its
fill colour after rendering, because only the pixels know where the browser put
it.

### The condition that changes the answer

All of the above is one dialog filling the frame. Qronos is never shown that.
It is shown a whole 4K screen shrunk to a 1280-pixel long edge, where the same
text is a third of the size — and there the model is **not** exact:

| | character error | error codes |
|---|---|---|
| model alone | 0.096 | 0 of 2 |
| Windows OCR alone | 0.096 | 2 of 2, in scrambled order |
| **both** | **0.009** | **2 of 2** |

Alone, the model reads the layout perfectly and invents the numbers: it turned
`0x8024402C` into `0x862480C` and `1,482 of 3,907 files` into `1.402 of
5.900 MB`. Alone, OCR reads the characters and destroys the order, interleaving
lines from four different windows. They fail in opposite directions, and that
is what makes one useful to the other.

---

## Windows OCR, as a hint

`core/windows_ocr.py`. Free in the two senses that matter — no tokens, no
graphics memory — and about 0.3 s on the CPU.

It is a **hint and never a replacement.** The picture is sent regardless, so a
reading that finds nothing costs nothing. The prompt says the reading may be
wrong and may be out of order — both true, both measured — so the model
reconciles it against the pixels rather than copying it.

It runs on the **full-resolution capture**, at the moment of capture, because
that is the only moment those pixels exist and reading them at full size is the
entire point. The reading rides on the picture as `PreparedImage.hint`.

**There is no Persian recogniser.** This is not a missing language pack:
Microsoft's list has no Arabic script in it at all, so no amount of installing
helps. This machine offers `en-US` and `nb`. Shown Persian, the engine does not
fail — it emits confident Latin gibberish like `I O _Lå IO_JiI an_Ä Qi`, which
would otherwise be handed to the model as a fact about a picture the model
reads at 0.941 recall on its own.

Checking the output for Persian characters would never fire, because the engine
cannot produce any. What gives it away is that the output is not made of words:
1.00 word-likeness for real English, 0.17 for the gibberish. Three things are
thrown away — a failed reading, a reading of almost nothing, and a reading that
is not made of words — and a long reading is truncated rather than refused,
because "there is too much on your screen" is not an answer anybody wants to a
question about their screen.

It is reached through PowerShell rather than a Python WinRT binding, because
those are Windows-only wheels and adding one would stop the project's
dependency list installing on the Linux half of CI, for a capability that does
not exist there. The picture goes over standard input as base64 and is decoded
into an in-memory stream, so a capture that must never become a file does not
become one.

---

## Capturing the screen

`core/screen_capture.py` — the repository's **first executor**, and the first
entry in `EXECUTOR_MODULES`. Windows GDI through `ctypes`, following
`core/whisper_cpp_vad_runtime.py`'s precedent rather than adding a screenshot
dependency.

**The process stays DPI-unaware on purpose.** A DPI-aware process on this
display sees all 3840 pixels; an unaware one is handed 3072x1728, scaled down
by Windows before it arrives. Fewer pixels for free, no less legible, and 1080p
on a 4K laptop at 200%. Both numbers are read and recorded, because otherwise
"we got fewer pixels than the panel has" is indistinguishable from a bug.

**One window is captured from the screen, not from the window.** The obvious
call returns solid black for anything drawn on the GPU — the browser, the
terminal, the editor. Measured: one distinct colour from the window's own
device context, over a hundred thousand from the screen's at the same
coordinates. So the window's rectangle is looked up and that region of the
composited screen is copied.

**The cost of that is occlusion, and it is a real limitation.** A window with
something on top of it captures the thing on top. That is fine for the case it
was built for — the foreground window is on top by definition, and its handle
is read at the instant the hotkey fires, before focus can move. It is not fine
for a window that is behind another one, and there is no honest way to do that
with this approach.

**A blank capture is reported rather than sent.** It is what a locked screen, a
sleeping display and protected video all look like, and also what a broken
capture looks like. Answering it without a model is faster and more honest:
shown a flat rectangle, the model spends five seconds describing a flat
rectangle.

**Nothing is written to disk.** The bitmap goes from GDI into memory, is
encoded there, and is handed on. No temporary file, no janitor to trust,
nothing left behind if the process is killed halfway. That is the retention
policy: there is no retention.

---

## Permissions

| category | level | why |
|---|---|---|
| `READ_SCREEN` | UI confirmation | Nobody chooses what is on their screen at the moment somebody asks to look at it. A spoken "yes" is consent given without being shown what is about to be looked at. |
| `WATCH_CAMERA` | UI confirmation | Granted once, to *begin watching*. The session is the unit, not the frame. |
| `HIDDEN_SURVEILLANCE` | forbidden | Unchanged. Capture with no indicator, or that the user did not start, is this. |

### Watching sessions

`PermissionLevel` has five levels and all five describe *how* you confirm. None
describes *how long* a yes lasts. That is fine for every action Qronos has had,
because they happen once. Watching a camera does not.

The answer is not a sixth level — "session" is not a way of confirming, it is a
property of the grant, and the levels are an ordered `IntEnum` that gets
compared. So `security/watching.py` holds the session as its own object, with
four rules:

- **The user starts it.** Never Qronos, never a model, never a plan step.
- **It is visible the whole time**, as a state you can see rather than a
  notification when it began.
- **It ends by itself** — ten minutes maximum, two minutes idle. Forgetting is
  the normal case, not the exceptional one.
- **Stopping is one action away** and takes effect on the very next frame.

Expiry is decided when a frame is asked for, not by a timer, so a session
cannot outlive its limit because a thread did not get scheduled. There is a
minimum interval between frames so that asking faster does not get frames
faster — which is what stops a watching session being turned into a recorder.

---

## The two-step turn

Looking at the screen needs UI confirmation, and the runtime cannot give itself
one. So a spoken request to look is answered in two halves:

```text
hotkey pressed
    |
    +-- the foreground window handle is read, here and nowhere later
    |
    v
transcript -> router -> VISION
    |
    +-- voice_needs_screen  ------------------>  the desktop asks the person
    |                                                     |
    |                                            yes      |      no
    |                                             |       |       |
    v                                             v       |       v
qronos.look_at_screen(approved: true) <-----------+       |   nothing happens
    |                                                     |
    +-- capture -> OCR hint -> vision model -> answer     |
```

Saying no ends the turn cleanly and is not an error. Forgetting to answer is
the same as saying no. The question is forgotten either way, so one asked five
minutes ago cannot be answered against whatever is on the screen now. Both of
those happen **before** anything prepares the microphone stack: saying no to a
screen capture must never be the thing that turns a microphone on.

---

## Routing

`TaskType.VISION` existed from the beginning and routed to nothing. Worse, most
requests that needed it could not reach it: `COMPUTER` is checked first and its
keywords include `file`, `app`, `windows` and `فایل`, so "read the file name in
this screenshot" went to `COMPUTER` and "what is on my screen" fell through to
`FAST`.

Reordering is not the fix — "open the photo app" contains `photo` and is
genuinely a `COMPUTER` request. What separates them is that a real vision
request names both an **act of looking** and a **thing to look at**. That
compound rule is checked ahead of `COMPUTER` and after `BROWSER`, because "go
to the site and look at the chart" has to navigate before there is anything to
look at.

---

## One model sees, another thinks

The 4B turns pixels into words very well and is not the thing to ask *why the
build failed*. So `VisionWorker` asks it to describe, and when the question
wants more than description, hands the description to the heavy brain as text.
The heavy brain never sees the picture and does not need to: by then the
picture is words.

Where that line falls is a stated rule (`needs_reasoning`), tested in both
languages, and deliberately narrow — escalating costs ten gigabytes and several
seconds, and the description is returned either way. A heavy brain that fails,
or says nothing, still returns the description: it was really produced, and
dropping it would turn a partial answer into no answer.

---

## Watching something move

`core/watching_eyes.py`. A frame source, a model that says what is in one, and
a loop that asks the session before **every** frame. A loop that asked once at
the start would keep taking frames past the time limit, past the idle limit and
past somebody pressing stop, and would look exactly like a loop that did not.

Every frame is described and dropped. What survives a session is a list of
sentences, not a recording.

**The camera source is not written.** There was no camera on the machine this
was developed on, and a driver that has never produced a frame is a promise
rather than a capability. `camera_available()` returns False. Everything it
would plug into is built and exercised, through a region of the screen — which
is also a useful capability in its own right, and is how the whole path was
verified: a generated scene playing in a browser window, watched at the rate a
camera would be.

Twelve frames over sixty seconds, all verified as being of the scene: it
reported the person arriving, the empty room in between, both colours of the
card being held up, and read the on-screen counter.

That scene is drawn by this project, which makes it reproducible and offline
and also makes it the easy case — flat colours, hard edges, no motion blur, no
compression. It proves the session and the plumbing. It does not prove the
model can do the job.

So there is a second harness against **real footage of a real person**: "Me at
the zoo", the first video uploaded to YouTube, played in a browser and watched
through the screen. It is a person talking straight into a hand-held camera at
close range, in 240p from 2005 — blown-out highlights, heavy compression,
motion blur, worse than any webcam. And he turns around partway through, so
"is the person facing the camera" has an answer that changes during the run and
a watcher that says the same thing every time fails rather than passes.

Twenty-three frames over ninety-seven seconds:

| | |
|---|---|
| Answered in the form asked | **23 of 23** |
| A person detected | **23 of 23** |
| Facing direction stated | **23 of 23** — 19 facing the camera, 4 turned away |
| Caught the moment he turns around | yes, 4 frames |
| Described the surroundings too | 23 of 23 mention the zoo enclosure |

The facing-direction number took a prompt change to reach. An earlier
`WATCH_INSTRUCTION` put "whether a person is visible and facing the camera" at
the end of a general request to describe the frame, and the model answered it
about seven times in ten — it is a request for one or two sentences, and the
last clause is the one that gets dropped. Measured across runs: 21 of 21, then
20 of 21, then 15 of 21.

Asking for the two facts **first**, as their own numbered questions, fixed it.
They are also the only two a camera is watched for: is somebody there, and are
they facing you. The reply now comes back as

```text
1. Yes
2. Facing the camera
3. An elephant is visible behind a fence to the right of the person.
```

which is both more useful and far easier to check than prose.

The clip is public rather than from this machine, which is the rule: there is
video of identifiable people on any development machine and none of it goes
near this project. The page is served from `127.0.0.1` and embeds the no-cookie
player, so no consent banner appears and nothing is agreed to on anybody's
behalf — a YouTube embed refuses to play from a `file://` page, which is worth
knowing before trying it.

It is not in the test suite. It needs the internet and it needs somebody else's
video to still exist.

### What a watching session costs

Sampled every 250 ms for the whole ninety-seven seconds, not only while the
model was talking — a session that holds four gigabytes throughout and one that
holds it for two seconds at a time look identical otherwise.

| | |
|---|---|
| Graphics memory | peak **+4,490 MiB** over what the card already held |
| Graphics load | busy **91%** of the session, 58% mean |
| Card temperature | 48-63 C, mean 55 |
| The model server's processor share | **1%**, peak 7% |
| The model server's memory | **117 MiB** |
| The whole machine's processor | 45% mean — most of it something else entirely |

Two things worth reading off that.

**Nothing accumulates.** The session's peak is the same +4,490 MiB that a
single answer costs. Twenty-three frames do not cost more than one, so no
memory is being kept between them.

**The processor is not where this happens.** The model server averaged 1% of
it. That figure is measured against the server's own processes rather than the
machine total, which matters: two runs an hour apart measured 43% and 62% for
the whole machine doing identical work, because something else was running the
second time. A check against the machine total would have called that a Qronos
regression. The architecture document asks for this attribution for exactly
this reason.

### One frame every 4.2 seconds, and half of it is loading

The model is unloaded after every frame and loaded again for the next — 0 of 23
frames found it still on the card. That is Qronos's rule working as written:
nothing stays loaded between turns, and a watching session is not exempt.

The cost is about 2.2 s of every 4.2 s spent reloading a model that is about to
be asked the same question again.

**Whether that is the right trade is an open question, and it is not mine to
settle.** The argument for holding it is that a watching session is one
continuous operation, not a series of turns: it has an explicit start, a
visible indicator, a hard time limit and a stop button, so the model would be
resident for a bounded period the user can see and end. That is exactly the
case the no-residency rule was not written about — the rule exists because the
Fast Brain was being held for ten minutes after a turn nobody was watching.
The argument against is that it is 3.3 GB of somebody's card, held while they
are doing something else.

Holding it for the session would roughly halve the frame interval. Nothing in
this branch does it.

---

## Running the measurements

```bash
python tools/vision_corpus.py                  # draw the corpus
python tools/test_qronos_vision_live.py        # what the model can do      12/12
python tools/test_qronos_ocr_hint_live.py      # whether OCR earns its place 26/26
python tools/test_qronos_watching_live.py      # watching something move    14/14
python tools/test_qronos_webcam_video_live.py # a real person, real footage 15/15
```

Each needs Ollama running and the model pulled. The last two open a browser
window and ask not to be interrupted while they run, and the last one needs the
internet.

---

## What is not built

- **A camera device source.** See above. What a camera would produce has been
  tested against real footage; the twenty lines that talk to Windows Media
  Capture are what is missing.
- **Cropping to a region by coordinate.** Grounding is accurate enough for it
  (0.92 overlap), and the token floor means a crop of a screen costs the same
  as the whole screen, so there is nothing to save until there is a second
  question about the same picture.
- **Anything that acts on what it sees.** Qronos can look and say. Clicking
  what it found is `COMPUTER`, and that is a different permission and a
  different piece of work.
