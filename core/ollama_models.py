from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from core.local_ipc import local_model_base_url, requests_client as requests


OLLAMA_BASE_URL = "http://127.0.0.1:11434"

QUERY_TIMEOUT_SECONDS = 5
DELETE_TIMEOUT_SECONDS = 30


# Ollama reports RFC 3339 timestamps with nanosecond precision, for example
# "2026-08-20T10:11:12.123456789Z". datetime.fromisoformat accepts at most
# microseconds, so the fractional part is truncated to six digits before
# parsing and the trailing Z is rewritten as an explicit UTC offset.
_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<head>.*?)(?:\.(?P<fraction>\d+))?(?P<zone>Z|[+-]\d{2}:?\d{2})?$"
)


def parse_ollama_timestamp(value: str | None) -> float | None:
    """
    Convert an Ollama timestamp into a Unix epoch value.

    Returns None rather than raising when the value is missing or unparseable.
    A timestamp is used only for ordering eviction candidates, so an unknown one
    should degrade the ordering, never break the caller.
    """
    if not value:
        return None

    text = value.strip()

    if not text:
        return None

    match = _TIMESTAMP_PATTERN.match(text)

    if match is None:
        return None

    head = match.group("head")
    fraction = match.group("fraction") or ""
    zone = match.group("zone") or ""

    if zone == "Z":
        zone = "+00:00"

    rebuilt = head

    if fraction:
        rebuilt += "." + fraction[:6]

    rebuilt += zone

    try:
        return datetime.fromisoformat(rebuilt).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class InstalledModel:
    """One model present in the local Ollama store."""

    name: str
    size_bytes: int
    digest: str = ""
    modified_at_raw: str = ""
    modified_at_epoch: Optional[float] = None

    @property
    def has_known_age(self) -> bool:
        return self.modified_at_epoch is not None


@dataclass(frozen=True)
class ModelDetails:
    """Metadata for one model, as reported by Ollama."""

    name: str
    family: str = ""
    parameter_size: str = ""
    quantization_level: str = ""


class OllamaModelCatalog:
    """
    Read and manage the local Ollama model store.

    Deliberately separate from :class:`core.ollama_controller.OllamaController`.
    The controller owns inference and model lifecycle — chat, load, unload. This
    class owns the store on disk — what is installed, how large it is, and
    removing or fetching it. Keeping them apart means storage work does not
    touch the inference path.

    Every method raises :class:`RuntimeError` on transport failure rather than
    returning a placeholder, so a caller can never mistake "the daemon is down"
    for "nothing is installed". Treating an unreachable daemon as an empty store
    would make a preflight check pass when it should fail.
    """

    def __init__(self, base_url: str = OLLAMA_BASE_URL) -> None:
        self.base_url = base_url

    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = local_model_base_url(value)

    # ------------------------------------------------------------------ reads

    def health_check(self) -> bool:
        """True when the local Ollama API answers."""
        try:
            response = requests.get(
                f"{self.base_url}/api/version",
                timeout=QUERY_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            return isinstance(data, dict) and isinstance(data.get("version"), str)
        except (requests.RequestException, ValueError):
            return False

    def list_installed_models(self) -> tuple[InstalledModel, ...]:
        """
        Return every model in the local store.

        Uses ``/api/tags``, which reports name, size and modification time.
        """
        data = self._get_json("/api/tags")

        models: list[InstalledModel] = []

        entries = data.get("models", [])
        if not isinstance(entries, list):
            raise RuntimeError("Local Ollama installed-model metadata has an invalid shape.")
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            name = entry.get("name", "")
            if not isinstance(name, str) or len(name) > 256 or any(ord(c) < 32 for c in name):
                raise RuntimeError("Local Ollama model name metadata is invalid.")
            name = name.strip()

            if not name:
                continue

            raw_modified = str(entry.get("modified_at", ""))

            models.append(
                InstalledModel(
                    name=name,
                    size_bytes=self._as_int(entry.get("size")),
                    digest=str(entry.get("digest", "")),
                    modified_at_raw=raw_modified,
                    modified_at_epoch=parse_ollama_timestamp(raw_modified),
                )
            )

        return tuple(models)

    def find_installed(self, model_name: str) -> InstalledModel | None:
        """Return one installed model by exact name, or None."""
        for model in self.list_installed_models():
            if model.name == model_name:
                return model

        return None

    def is_installed(self, model_name: str) -> bool:
        return self.find_installed(model_name) is not None

    def total_installed_bytes(self) -> int:
        """Total size of every installed model."""
        return sum(
            model.size_bytes
            for model in self.list_installed_models()
        )

    def show_model(self, model_name: str) -> ModelDetails:
        """Return metadata for one model, using ``/api/show``."""
        data = self._post_json(
            "/api/show",
            {"name": model_name},
            timeout=QUERY_TIMEOUT_SECONDS,
        )

        details = data.get("details", {})
        if not isinstance(details, dict) or any(
            not isinstance(details.get(key, ""), str)
            for key in ("family", "parameter_size", "quantization_level")
        ):
            raise RuntimeError("Local Ollama model details metadata is invalid.")

        return ModelDetails(
            name=model_name,
            family=str(details.get("family", "")),
            parameter_size=str(details.get("parameter_size", "")),
            quantization_level=str(details.get("quantization_level", "")),
        )

    # ----------------------------------------------------------------- writes

    def delete_model(self, model_name: str) -> None:
        """
        Remove a model from the local store.

        Irreversible and re-downloadable: the model has to be pulled again to
        come back. Callers must consult :class:`core.model_store.ModelStore`
        first, which refuses to propose evicting a model the running
        configuration requires.
        """
        if not model_name.strip():
            raise ValueError("model_name must not be empty.")

        try:
            response = requests.delete(
                f"{self.base_url}/api/delete",
                json={"name": model_name},
                timeout=DELETE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException:
            raise RuntimeError(
                "Could not delete the requested local model."
            ) from None

    def pull_model(self, model_name: str) -> None:
        """Terminal policy refusal: a local /api/pull would fetch public content."""
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must not be empty.")
        raise PermissionError(
            "Model registry downloads are disabled by the offline policy. "
            "Obtain approved weights through the visible browser workflow, "
            "then import them locally. Storage preflight does not authorize a download."
        )

    # ---------------------------------------------------------------- helpers

    def _get_json(self, route: str) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.base_url}{route}",
                timeout=QUERY_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException:
            raise RuntimeError(
                f"Ollama API is unavailable: {route}"
            ) from None

        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("Local Ollama returned invalid JSON metadata.") from None
        return self._as_dict(data, route)

    def _post_json(
        self,
        route: str,
        payload: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        try:
            response = requests.post(
                f"{self.base_url}{route}",
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            raise RuntimeError(
                f"Ollama API is unavailable: {route}"
            ) from None

        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("Local Ollama returned invalid JSON metadata.") from None
        return self._as_dict(data, route)

    @staticmethod
    def _as_dict(value: Any, route: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError(
                f"Ollama API returned an unexpected shape for {route}."
            )

        return value

    @staticmethod
    def _as_int(value: Any) -> int:
        """
        Coerce a reported size to a whole number of bytes.

        Sizes drive storage decisions, so an unparseable value becomes zero and
        is treated downstream as an unknown size, which the storage policy
        refuses rather than approves.
        """
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0


def main() -> None:
    """List the locally installed Ollama models."""
    catalog = OllamaModelCatalog()

    if not catalog.health_check():
        print("Ollama API: unavailable")
        return

    models = catalog.list_installed_models()

    print("=== Installed Ollama Models ===")

    for model in models:
        print(
            f"{model.name}: "
            f"{model.size_bytes / (1024 ** 3):.2f} GB"
        )

    print(
        f"Total: "
        f"{catalog.total_installed_bytes() / (1024 ** 3):.2f} GB"
    )


if __name__ == "__main__":
    main()
