# Foreground inference lifecycle

Text, voice and approved screen-decision worker entry points each admit one
fresh `LocalInferenceCancellation` into their runtime's `ForegroundLifecycle`.
The scope is bound inside the worker thread, including push-to-talk preparation
and nested speech cleanup. A second foreground worker sees busy; a closed
runtime never admits another scope or warms its speech resources.

Stop, Pause and shutdown close the corresponding foreground request sockets.
Their transition gate remains held through acknowledgement and cleanup, so an
older Stop cannot clear a newer turn's playback, native-action epoch or token.
The native `ActiveActionRegistry` and approval revocation remain separate and
continue to decide how an actual computer action unwinds. This gate grants no
permission and does not weaken screen or file approval contracts.

History and generated response publication share the same gate. If publication
wins, its history/event completes before Stop acknowledges. If Stop wins, no
generated assistant history, text, screen answer or audio chunk is published.
Cancelled provider failure results are checked before resource deferral,
fallback speech or another provider call. Voice cancellation closes the active
follow-up session and reports a cancelled latency status rather than inviting
an automatic retry. Native/file result reports may still describe the actual
completed or partially cancelled work; they never manufacture rollback or
successful completion of unexecuted steps, and a cancelled voice action does
not synthesize new speech.

Text cancellation uses the existing `text_error` envelope with event status
`cancelled` and payload `status: cancelled`, a fixed reason and the request ID.
It writes no fabricated assistant message to history. Voice and screen use
`voice_turn_stopped` with a fixed reason/channel. Terminal publication and
cancellation report claims prevent a stopped request from emitting duplicate
answer/error/cancellation outcomes.

Closing HTTP does not prove immediate GPU release or server-side termination.
Native actions, CPU preparation, local recording and filesystem work remain
cooperative at their next checkpoint; no thread or user daemon is force-killed.
The separate background queue owns its own tokens and commit gate. Screen
approval answers carry their single-use `approval_id` through both the public
entry and its scoped implementation. An unidentified answer never becomes a
direct screen read.
