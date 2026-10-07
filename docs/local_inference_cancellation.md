# Cancelling local inference

Each interactive turn or queued work item must own a new
`LocalInferenceCancellation` from `core.inference_cancellation`. Bind its token
inside the actual worker thread with `inference_cancellation_scope(token)`;
ContextVars do not automatically propagate into newly created threads.
Alternatively, a direct caller can use `OllamaController.chat(...,
cancellation=token)`. Calls without either keep their existing behavior.

Stop calls `token.cancel("user_stop")`; Pause uses `"runtime_paused"`, shutdown
uses `"runtime_shutdown"`, and queue preemption uses `"preempted"`. The first
reason wins. Cancellation is terminal for that token. A future turn needs a
fresh token; retaining an old token can never close a newer token's connections.

Cancellation synchronously shuts down and closes every connection registered
to that token. Registration happens before the numeric loopback connect;
nonblocking connect checks cancellation at most every 50 ms. Silent HTTP
headers, partial bodies and generation waits are interrupted through socket
shutdown. The transport checks again before sending inference after its local
model provenance probe and before returning a buffered result. The controller
also checks after parsing and refuses intentional cancellation as an ordinary
connection failure: it does not bootstrap or retry a cancelled request.

`LocalInferenceCancelled` is a separate exception with a fixed reason and no
prompt, image, response body or chained transport diagnostics. Runtime/queue
integration must treat it as cancellation and preserve its existing atomic
Stop-versus-delivery/commit checks; a transport cannot order an application
commit occurring after the provider has returned. Bind the whole worker turn
so routing, memory and model calls inherit the same token. The shared owned
sidecar HTTP helper also honors the context while preserving its captured
process and accepted-connection ownership check before sending bytes.

This closes only request connections. It never kills a daemon, unloads all
models, or proves immediate GPU release: Ollama decides when server-side
generation stops after client disconnect. CPU preparation, JSON parsing,
filesystem operations and a subprocess launch already entered are cooperative
phases; cancellation is checked at their next boundary. No worker thread is
force-killed. Endpoint, proxy/netrc, redirect, response-size, elapsed-deadline
and local-model/cloud-provenance policy checks remain in force.

Tests use synthetic loopback HTTP peers with silent headers and partial bodies,
a saturated real listen backlog, a pre-connect registration barrier, and
provenance/delivery barriers. They verify EOF, prompt refusal, late-output
refusal and isolation of an old cancellation token from a new request.
