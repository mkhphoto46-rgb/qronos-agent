# Computer control implementation checkpoint

## Implemented in this checkpoint

- Persian drive variants and redirected Windows known-folder lookup.
- Preserve Qronos inside folder names while removing leading invocation.
- Human-readable, operation-specific voice acknowledgements.
- Pytest discovery restricted to tests; existing permission isolation fixture used.
- Legacy direct HTTP web transport disabled; removed its production registration.
- Offline rule with explicit paired mobile WAN transport exception.
- Paint drawing intent/schema prototype; native drawing deliberately disabled.

## Remaining work (not completed capabilities)

1. Audit all network-capable modules, including configurable local model URLs,
   model download operations, redirects and proxy behavior. Preserve Device Link.
2. Locate and integrate the user's browser-control implementation. The current
   web provider module was direct HTTP, not browser control. Browser research is
   currently unavailable through the removed worker.
3. Implement fresh screenshot/window identity binding and validated Vision target
   coordinates; integrate mouse/keyboard actions with cancellation, focus checks,
   permission checks, audit and observed postconditions.
4. Live app/file/drive testing with memory and sidecar monitoring.
5. Telegram exact-contact/latest-incoming-file workflow and printer workflow.
6. Device Link internet transport and native phone app; no relay deployed yet.

No GUI automation success is claimed by this checkpoint. The broader roadmap
is in qronos_computer_control_roadmap.md. Existing Device Link design is in
QRONOS_DEVICE_LINK.md. User permission preferences were not changed.
