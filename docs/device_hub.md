# Device Hub (H1): printers and 3D printers

Status: the printer and 3D printer parts of H1 are implemented and fixture-tested. Other device classes, Wake-on-LAN
(H3) and live acceptance on real hardware are still required.

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
- Scanners, cameras and other local devices are not supported yet.

## Rules kept

- **H2:** only the Windows spooler, the Print Ticket API and drivers. No socket, HTTP, process, shell, vendor cloud
  or smart-device API.
- **Pause:** refuses changes; listing and queues stay readable.
- **Lifecycle:** Pause, Stop and shutdown revoke open `DEVICE_ACTION` approvals by kind. Their payload is read only
  by `core/device_hub.py`.
- **Wrong kind:** a Device Hub approval sent as a file decision is refused as `wrong_kind` and left untouched.
- **Secret:** the typed secret is never echoed or stored. The desktop sends it only with the one decide request and
  clears it on submit and hide.

## Interfaces

- **Action:** `qronos.device_hub` `{op, requestId, ...}`. The `op` is one of:
  - `devices.list`, `device.jobs`;
  - `job.control` (`deviceId`, `jobId`, `command`);
  - `print.request` (`deviceId`, `path`, `copies`, `color`, `pages`);
  - `print3d.request` (`deviceId`, `path`);
  - `decide`, `decline`.
- **Answer:** the `device_hub_result` event `{requestId, op, ok, ...}`; refusals carry stable codes.
- **Tests:** these use a recording fake spooler and driver, and fake COM vtables, only:
  - `tests/test_device_hub.py`;
  - `tests/test_print3d_native.py`;
  - `desktop/tests/device-hub-store.test.mjs`.

  No real printer is enumerated or queried, and no job is touched. The live-device boundary blocks the real
  spooler and 3D driver classes. The one real COM use in tests is a self-check of OPC part names and memory
  streams, which touches no device.
