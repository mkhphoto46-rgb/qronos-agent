# Device Hub (H1) — printer chunk

Status: the printer part of H1 is implemented and fixture-tested. 3D printers, other device classes, Wake-on-LAN
(H3) and live acceptance on real hardware are still required.

## What it does

The desktop's **دستگاه‌ها** drawer lists the print queues Windows already has:

- local printers, and LAN printers Windows has connected (`EnumPrinters`, local and connections);
- for each: a stable id (queue name + port + driver), local or network, default, and status problems/warnings;
- its real capabilities read from the driver (`DeviceCapabilities`): maximum copies, colour, duplex, collate and
  paper sizes. Workflows are offered only when the driver reports them.

Workflows:

- **Print one exact file**: an absolute path, copies, colour and pages, checked against the device's limits. It is
  handed to the existing reviewed print step (`AppWorkflowCoordinator.begin_print`): exact file, exact queue, the
  `DEVICE_CONTROL` gate, a single-use approval with the typed secret in the existing print dialog, one verified
  spool job, bounded recovery and audit.
- **Print queue**: the selected device's jobs, marked as yours or another user's.
- **Pause / resume / cancel**: ONLY your own job. Each change passes the `DEVICE_CONTROL` gate, then:
  - **allowed**: it runs;
  - **confirmation required**: a single-use `DEVICE_ACTION` approval;
  - **typed secret required**: the same approval plus the secret;
  - **refused**: nothing runs.

  At the answer, the current policy decides again, and the job must still be the same document. Every decision
  and result goes to the action audit (history).

Unsupported, shown as such (no fake capability):

- a queue whose name or driver says 3D/3MF: detected as a 3D printer, offered no workflow, not even document
  printing;
- scanners, cameras and other local devices.

## Rules kept

- **H2:** only the Windows spooler and drivers. No socket, HTTP, vendor cloud or smart-device API.
- **Pause:** refuses changes; listing and queues stay readable.
- **Lifecycle:** Pause, Stop and shutdown revoke open `DEVICE_ACTION` approvals by kind. Their payload is read only
  by `core/device_hub.py`.
- **Wrong kind:** a Device Hub approval sent as a file decision is refused as `wrong_kind` and left untouched.
- **Secret:** the typed secret is never echoed or stored. The desktop sends it only with the one decide request and
  clears it on submit and hide.

## Interfaces

- **Action:** `qronos.device_hub` `{op, requestId, ...}`, where `op` is one of `devices.list`, `device.jobs`,
  `job.control` (`deviceId`, `jobId`, `command`), `print.request` (`deviceId`, `path`, `copies`, `color`, `pages`),
  `decide`, `decline`.
- **Answer:** the `device_hub_result` event `{requestId, op, ok, ...}`; refusals carry stable codes.
- **Tests:** `tests/test_device_hub.py` and `desktop/tests/device-hub-store.test.mjs` use a recording fake spooler
  and driver only. No real printer is enumerated and no job is touched.
