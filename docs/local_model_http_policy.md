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

Ollama documents [cloud routing through the local daemon](https://docs.ollama.com/cloud)
and [disabling cloud features](https://docs.ollama.com/faq).
