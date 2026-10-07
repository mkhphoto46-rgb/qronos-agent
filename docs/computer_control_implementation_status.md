# Computer control implementation checkpoint

## Implemented in this checkpoint

- Persian drive variants and redirected Windows known-folder lookup.
- Preserve Qronos inside folder names while removing leading invocation.
- Human-readable, operation-specific voice acknowledgements.
- Pytest discovery restricted to tests; existing permission isolation fixture used.
- Legacy direct HTTP web transport disabled; removed its production registration.
- Offline rule with explicit paired mobile WAN transport exception.
- Paint drawing intent/schema prototype; native drawing deliberately disabled.
- Cooperative Stop for in-flight computer/Paint actions: one cancellation token
  per action, checked between native steps and inside stroke loops; every exit
  path after mouse-down releases the button. Runtime shutdown cancels the same
  way. Cancelled/partial outcomes are audited as `cancelled`.
- Honest undo records: Windows-control actions are journaled as irreversible
  with a reason. A Paint drawing is journaled as exactly reversible only when
  its edit count is certain and canvas evidence exists; undo rechecks
  permission, exact window/process/layout and an unchanged canvas before
  sending any Ctrl+Z, stops as soon as the canvas matches its pre-drawing
  state, and never replays a partial undo. Covered by fake-input tests only;
  no live Paint validation and no user-facing "undo" command yet.

## Remaining work (not completed capabilities)

1. Audit all network-capable modules, including configurable local model URLs,
   model download operations, redirects and proxy behavior. Preserve Device Link.
2. Locate and integrate the user's browser-control implementation. The current
   web provider module was direct HTTP, not browser control. Browser research is
   currently unavailable through the removed worker.
3. Implement fresh screenshot/window identity binding and validated Vision target
   coordinates; integrate mouse/keyboard actions with focus checks, permission
   checks, audit and observed postconditions. Stop/Pause/shutdown cancel Paint,
   ComputerExecutor, Calculator, accessibility and visual actions at step
   boundaries, discard pending screen/Paint approvals and refuse their late
   answers. A single native call (one UIA Invoke, click or typed string) is
   not interrupted mid-call; Stop takes effect at the next boundary.
4. Live app/file/drive testing with memory and sidecar monitoring.
5. Telegram exact-contact/latest-incoming-file workflow and printer workflow.
6. Device Link internet transport and native phone app; no relay deployed yet.

No GUI automation success is claimed by this checkpoint. The broader roadmap
is in qronos_computer_control_roadmap.md. Existing Device Link design is in
QRONOS_DEVICE_LINK.md. User permission preferences were not changed.
