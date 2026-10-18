# Local model HTTP policy

Qronos model HTTP clients accept explicit loopback origins only. Requests bypass
environment and system proxies, ignore netrc credentials, refuse redirects, and
use finite deadlines and bounded response sizes. They can use an existing
user-owned local Ollama installation; ownership of that daemon is not required.

Before sending prompts, images, embedding text, or generation requests, Qronos
reads local `/api/show` metadata. Remotely backed models and cloud aliases are
refused before inference data is sent. Missing or malformed provenance fails
closed. Qronos starts its own Ollama processes with `OLLAMA_NO_CLOUD=1`.

`OllamaModelCatalog.pull_model()` refuses downloads immediately with `PermissionError`.
It makes no request, launches no transfer, and performs no retry. Calling a
loopback `/api/pull` would still instruct the daemon to fetch public registry
content, which bypasses the browser-only internet policy. A storage install plan
is a capacity assessment, not permission to download.

Obtain approved weights through Qronos's visible, permission-gated browser
workflow and import the resulting local files into the local model runtime.
Model inspection, local inference, and existing local-file import facilities
remain available. This change does not add an import API or a download fallback.

Device Link retains its separate authenticated, encrypted transport exception.
Legacy direct web transports remain disabled before network IO. These exceptions
do not authorize public inference, model downloads, or HTTP search.

## Memory sidecar lifetime proof

Qronos's own CPU-only bge-m3 sidecar (`core/memory_runtime.py`, `127.0.0.1:11435`, keep-alive 1 h, one loaded
model; profile unchanged) starts inside a private Job Object owned by its guard (`core/windows_sidecar_guard.py`):

- The server is created suspended, made a Job member through its own process handle, and only then resumed, so it
  and every runner it starts belong to the Job from their first instruction. The Job permits no breakaway; a
  process that cannot be contained is ended before it ever runs.
- Each start is a generation with an immutable `SidecarLifecycle` capability (`manager.lifecycle`): generation,
  server pid and creation time, and `state()`.
  - `tree_exited` comes ONLY from observing the Job's active process count at zero while its handle was held; it
    is then sticky for that generation.
  - `running` means some member is alive, for example a runner whose server already exited.
  - `unknown` covers no guard, a failed query, or a handle closed before that observation.
- The connection owner is unchanged: `owned_process` is still the psutil identity of the exact server, and only
  while that server owns the listener. The lifecycle capability cannot stop, unload or talk to the server.
- Never proof: a parent pid that stopped running, an HTTP close or timeout, an empty `/api/ps`, issuing a
  terminate, closing a Job or elapsed time.
- Stop: the exact server is ended (as before), its tree gets a short grace, then the owned backstop ends this Job's
  own members only, and the empty Job must be observed. Only then are the Job and the singleton released.
  Otherwise the guard is retained: no second sidecar starts, and a later start or stop re-checks it. Windows still
  ends the Job when Qronos exits.
- A server that died while a runner lingers keeps everything owned (`running`) until a retire observes the tree
  ended. A new generation starts only after that.

Consumers that hold resources for the sidecar's model may free them on their
captured generation's `tree_exited` only; that wiring belongs to the consumer and is not part of this module.

Ollama documents [cloud routing through the local daemon](https://docs.ollama.com/cloud)
and [disabling cloud features](https://docs.ollama.com/faq).
