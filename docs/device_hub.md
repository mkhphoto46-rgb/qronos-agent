# Device Hub (H1): printers, 3D printers, scanners, cameras and waking this PC (H3)

Status: the printer, 3D printer, WIA scanner and camera (one still frame) parts of H1, and waking this PC with a
hardware wake timer (H3), are implemented and fixture-tested. WIA 1.0 still cameras, phones and other device
classes Windows does not expose, and live acceptance on real hardware, are still required.

## What it does

The desktop's **دستگاه‌ها** drawer lists the print queues Windows already has:

- local printers, and LAN printers Windows has connected (`EnumPrinters`, local and connections);
- for each: a stable id (queue name + port + driver), local or network, default, and status problems/warnings;
- its real capabilities, read from the driver. Workflows are offered only when the driver reports them:
  - **printers** (`DeviceCapabilities`): maximum copies, colour, duplex, collate and paper sizes;
  - **3D printers**: a queue is a 3D printer only when its driver's PrintCapabilities (Print Ticket API) carry the
    3D manufacturing keywords. These are the build volume (`Job3DOutputArea`), the 3MF version and the 3MF
    extensions.

Workflows:

- **Print one exact file**: an absolute path, copies, colour and pages, checked against the device's limits. It is
  handed to the existing reviewed print step (`AppWorkflowCoordinator.begin_print`): exact file, exact queue, the
  `DEVICE_CONTROL` gate, a single-use approval with the typed secret in the existing print dialog, one verified
  spool job, bounded recovery and audit.
- **Hand one exact .3mf to a proven 3D printer** (`print3d.request`):
  1. The Files read gate decides before any byte is read.
  2. The `DEVICE_CONTROL` gate decides.
  3. Always a single-use `DEVICE_ACTION` approval with the typed secret.
  4. At the answer: both current policies again, the 3D capability again, and the package is re-read and
     re-hashed. The hashed bytes are the bytes handed over.
  5. Windows' own 3D print path takes the package: `CreateDocumentPackageTargetForPrintJob`, the
     `OPENXPS_WITH_3D` target, `GetXpsOMPackageWriter3D`, the textures, then close. Any failure cancels the job.

  The answer says only that Windows accepted the package for the queue, with the spooler job id when it lists
  the unique job name. It never says a part was printed.
- **Print queue**: the selected device's jobs, marked as yours or another's.
- **Pause / resume / cancel**: ONLY your own job. A job is yours only when its user is you AND it came from this
  computer: its origin exactly matches this computer's own names. An empty origin counts only on a local, unshared
  queue. Each change passes the `DEVICE_CONTROL` gate, then:
  - **allowed**: it runs;
  - **confirmation required**: a single-use `DEVICE_ACTION` approval;
  - **typed secret required**: the same approval plus the secret;
  - **refused**: nothing runs.

  At the answer, the current policy decides again, and the job must still be yours and the same document.

Every decision and result goes to the action audit (history). When the current policy refuses at the answer, the
history says so; it never records that the person refused.

## The .3mf package

A .3mf is treated as an untrusted OPC/ZIP. Only its one 3D model part and that model's textures are handed over,
as opaque streams. The model's 3MF version and required extensions must match what the driver declares. Refused:

- external relationships;
- relationship targets with a scheme, drive, UNC, backslash, percent-encoding or dot segment;
- members with traversal, absolute, drive or duplicate names (also by case);
- more than one model;
- archive bombs, oversized model or textures, and too many members.

## Limits, shown as such

- A queue whose name or driver says 3D/3MF but whose driver does not prove it (`3d_capability_unproven`): no
  workflow, not 3D printing and not document printing.
- 3D print options (quality, density, supports, raft, material) stay in the driver or on the device. Qronos sends
  no print ticket of its own and does not check that the model fits the build volume; the driver and device do.
- There is no G-code, serial, network or vendor path. A printer without a Windows 3D driver is not reachable.
- WIA 1.0 still cameras, phones and other local devices are not supported yet (scanners and cameras: see below).

## Rules kept

- **H2:** only the Windows spooler, the Print Ticket API and drivers. No socket, HTTP, process, shell, vendor cloud
  or smart-device API.
- **Pause:** refuses changes; listing and queues stay readable.
- **Lifecycle:** Pause, Stop and shutdown revoke open `DEVICE_ACTION` approvals by kind. Their payload is read only
  by `core/device_hub.py`.
- **Wrong kind:** a Device Hub approval sent as a file decision is refused as `wrong_kind` and left untouched.
- **Secret:** the typed secret is never echoed or stored. The desktop sends it only with the one decide request and
  clears it on submit and hide.

## Scanning one page into one new image file (WIA 2.0 scanners)

Windows Image Acquisition 2.0 serves **scanners** (`core/wia_imaging.py`). The SDK's own note says still cameras are
WIA 1.0 and webcams are no longer WIA devices, so those are listed as not supported and never captured.

- **List** (`imaging.list`, read-only, also while paused): the devices WIA lists, with their kind (from the device
  type's high word). Only a scanner gets the scan workflow.
- **Scan** (`image.capture`):
  - one listed scanner and one exact NEW `.png`/`.jpg`/`.jpeg` path in an existing, non-linked folder;
  - the `DEVICE_CONTROL` gate (`imaging:<scanner>/scan`) and the Files `CREATE_OR_EDIT_FILE` gate (the path) both
    decide; then always a single-use `DEVICE_ACTION` approval with the typed secret;
  - at the answer, both current policies, the secret, the scanner still listed, and the path still new are checked;
    Pause/Stop/shutdown is checked before Windows' dialog opens and again after it closes;
  - Windows' own WIA dialog drives the scan (`IWiaDevMgr2::GetImageDlg`), into a fresh private staging folder;
  - exactly one staged image is decoded (bounded size and pixels) and re-encoded to the path with exclusive create
    (never an overwrite; no metadata copied);
  - the staging folder is removed; a cancel, a failure or a Pause writes nothing;
  - the answer states exactly what was written: path, SHA-256, bytes, size and format.

Not supported, shown as such: WIA cameras, webcams (through WIA; see the camera section), phones, multi-page
feeds (one page per scan), and any capture without Windows' dialog. Live acceptance on a real scanner is still
required.

## One still frame from a camera into one new image file (Media Foundation)

`core/camera_capture.py` uses Windows Media Foundation, the OS camera stack, through ctypes COM. It needs no helper,
driver, service, vendor SDK or network: `MFStartup` runs with `MFSTARTUP_LITE`, so no socket library is loaded. Every
vtable slot, GUID and constant is checked against the Windows SDK 10.0.26100 headers in
`tests/test_camera_capture.py`.

- **List** (`camera.list`, read-only, also while paused): what Windows itself lists as video capture devices
  (`MFEnumDeviceSources`, VIDCAP). Each is shown by name; its identity is `cam-` plus a hash of its symbolic link.
  Listing never turns a camera on.
- **Capture** (`camera.capture`):
  - one listed camera and one exact NEW `.png`/`.jpg`/`.jpeg` path in an existing, non-linked folder;
  - the `DEVICE_CONTROL` gate (`camera:<name>/capture`) and the Files `CREATE_OR_EDIT_FILE` gate (the path) both
    decide; then always a single-use `DEVICE_ACTION` approval with the typed secret;
  - at the answer, these are checked: both current policies, the secret, the same camera (symbolic link) still
    listed, and the path still new;
  - Pause/Stop/shutdown is checked before the camera turns on, polled while it is on (a stop shuts the media source
    down at once), and checked again after;
  - only the approved camera is opened, video stream only (never audio). Windows' own video processing converts its
    format to RGB32. Up to 4 frames are dropped in memory while the exposure settles, and the next one is kept;
  - the camera is on for at most 10 seconds. Windows shows its own camera indicator. The frame never touches the disk
    before the final write;
  - the frame (at most 7680x4320) is encoded to the path with exclusive create: never an overwrite, and no metadata;
  - the answer states exactly what was written: path, SHA-256, bytes, size, format, frames read, and whether the image
    is almost completely dark (for example a covered lens).
- **Honest refusals:**
  - Windows camera privacy off: `camera_privacy_denied`;
  - in use by another app: `camera_busy`;
  - unplugged: `camera_gone`;
  - a format Windows cannot convert: `camera_format_unsupported`;
  - no frame in time: `camera_timeout`.
  Nothing is written in any of these cases.

Not supported, shown as such:
- WIA 1.0 still cameras; phones, network cameras or other devices that Windows does not itself list as a camera;
- video or sound recording, a live preview, and any face or person recognition (no model runs on the image).

A camera that Windows does list (virtual, network or phone cameras included) works the same way, but only built-in
and USB webcams are the intended case; none of this has been tried on real hardware. Live acceptance on real cameras
is still required.

## Waking this PC from sleep (H3)

A sleeping PC runs nothing: not Qronos's voice, wake word or brain. Qronos therefore does not claim voice wake from
sleep. What it offers is the hardware path Windows provides: a **wake timer** on this PC (`core/power_wake.py`).

- **Capability, read-only:** shown before anything is asked.
  - `GetPwrCapabilities` reports sleep states, Modern Standby and the firmware's RTC wake state.
  - The power plan's **Allow wake timers** value for mains and for battery (off, on, or important timers only).
  - The current power source.
  - Qronos never changes these settings. Off or "important only" for the current power source means a Qronos
    timer would not wake the PC; this is said, and nothing is armed.
- **Setting a time** (`wake.schedule`):
  - one exact time with its UTC offset, 2 minutes to 7 days ahead, plus 0–120 minutes to stay awake after it;
  - one armed wake at a time;
  - the `DEVICE_CONTROL` gate (target `power:this-pc/wake-timer`), then always a single-use `DEVICE_ACTION`
    approval with the typed secret;
  - at the answer, the current policy, the time window and the capability are checked again.
- **Arming:**
  - `CreateWaitableTimerExW` + `SetWaitableTimer` with resume, on a thread that stays alive while the timer is
    armed (Windows cancels a timer whose setting thread exits).
  - The answer says only that **Windows accepted the wake timer** (`claim: windows_accepted_wake_timer`). If Windows
    reports that this system cannot resume on a timer, the answer is `wake_not_supported` and nothing is armed.
- **When it fires:** the system is kept from idling back to sleep for the approved minutes, then released. The event
  says the timer fired, never that the PC woke: it may already have been awake.
- **Cancelling** (`wake.cancel`) passes the same gate (allowed, or an approval). Pause, Stop and shutdown cancel an
  armed wake (or end a fired wake's stay-awake hold) and revoke open approvals; a wake whose approval was being
  answered when Pause came is cancelled, never left armed. Until a fired wake's hold ends, no new wake is set.

Limits, shown with every status and approval:

- Qronos must keep running. Shutdown, sign-out, or hibernation without wake support removes the timer.
- Waking depends on the firmware and on the power plan.
- On Modern Standby the processor wakes, but Windows keeps the display off.
- Nothing is persisted: a wake timer lives only with the running process.

Wake-on-LAN is not used. It would need a network sender outside Qronos's offline Core and narrow DeviceLink scope.

## Interfaces

- **Action:** `qronos.device_hub` `{op, requestId, ...}`. The `op` is one of:
  - `devices.list`, `device.jobs`;
  - `job.control` (`deviceId`, `jobId`, `command`);
  - `print.request` (`deviceId`, `path`, `copies`, `color`, `pages`);
  - `print3d.request` (`deviceId`, `path`);
  - `imaging.list` (read), `image.capture` (`deviceId`, `destination`);
  - `camera.list` (read), `camera.capture` (`deviceId`, `destination`);
  - `wake.status` (read), `wake.schedule` (`at` with its offset, `stayAwakeMinutes`), `wake.cancel`;
  - `decide`, `decline`.
- **Answer:** the `device_hub_result` event `{requestId, op, ok, ...}`; refusals carry stable codes.
- **Tests:** these use a recording fake spooler and driver, and fake COM vtables, only:
  - `tests/test_device_hub.py`;
  - `tests/test_print3d_native.py`;
  - `tests/test_power_wake.py` (a fake kernel32 timer API);
  - `tests/test_wia_imaging.py` (fake WIA COM vtables, synthetic images);
  - `tests/test_camera_capture.py` (fake Media Foundation COM vtables, synthetic frames);
  - `desktop/tests/device-hub-store.test.mjs`.

  No real printer is enumerated or queried, and no job is touched. The live-device boundary blocks the real
  spooler and 3D driver classes. The one real COM use in tests is a self-check of OPC part names and memory
  streams, which touches no device.
