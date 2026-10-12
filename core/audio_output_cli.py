from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio_output import play_wave_file


def _payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Audio output payload must be a JSON object.")
    return value


def main() -> int:
    if len(sys.argv) < 2:
        raise ValueError("Audio output operation is required.")

    operation = sys.argv[1].strip().lower()
    payload = _payload()

    if operation == "play":
        path = str(payload.get("path", "")).strip()
        device_id = str(payload.get("deviceId", "")).strip()
        if not path:
            raise ValueError("Native audio playback requires path.")
        if not device_id:
            raise ValueError("Native audio playback requires explicit deviceId.")
        result = play_wave_file(path, device_id)
    else:
        raise ValueError(f"Unsupported audio output operation: {operation}")

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "errorType": type(exc).__name__,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raise SystemExit(1)
