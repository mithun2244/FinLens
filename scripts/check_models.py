"""Groq model liveness probe (phases.md Phase 1, task 6).

Groq retires preview models on short notice. The brief for this project specified
Llama 3.2 Vision, whose Groq endpoints have since been decommissioned (decision D-3).
This script asks Groq which models it currently serves and checks that every ID
configured in ``src/config.py`` is among them.

    python scripts/check_models.py

Exit code 0 when all configured models are served, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    MODELS_BY_ROLE,
    REASONING_MODEL_FALLBACKS,
    VISION_MODEL_FALLBACKS,
    get_settings,
)

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def main() -> int:
    settings = get_settings()

    if not settings.groq_configured:
        print(f"{FAIL} GROQ_API_KEY is not set.")
        print("       Copy .env.example to .env and add a free key from")
        print("       https://console.groq.com/keys  (no credit card required).")
        return 1

    try:
        from groq import Groq
    except ImportError:
        print(f"{FAIL} The 'groq' package is not installed.")
        print("       Run: pip install -r requirements.txt")
        return 1

    print("Querying Groq for currently served models...\n")
    try:
        client = Groq(api_key=settings.groq_api_key, timeout=30.0)
        served = {model.id for model in client.models.list().data}
    except Exception as exc:  # noqa: BLE001 - surface any transport/auth failure verbatim
        print(f"{FAIL} Could not reach Groq: {type(exc).__name__}: {exc}")
        print("       Check the API key in .env and your network connection.")
        return 1

    print(f"Groq is serving {len(served)} models.\n")

    failures: list[str] = []
    for role, model_id in MODELS_BY_ROLE.items():
        if model_id in served:
            print(f"{OK}   {role:<10} {model_id}")
        else:
            failures.append(model_id)
            print(f"{FAIL} {role:<10} {model_id}  <-- NOT SERVED")

    if failures:
        print("\nAvailable fallbacks configured in src/config.py:")
        for candidate in (*VISION_MODEL_FALLBACKS, *REASONING_MODEL_FALLBACKS):
            marker = OK if candidate in served else WARN
            print(f"  {marker} {candidate}")

        print("\nOther served models containing 'llama' or 'vision':")
        for model_id in sorted(served):
            lowered = model_id.lower()
            if "llama" in lowered or "vision" in lowered or "scout" in lowered:
                print(f"       {model_id}")

        print(
            "\nAction: update the affected constant in src/config.py "
            "(decision D-4 — model IDs live in exactly one place),\n"
            "        then log the change in memory.md's Decision Log (Rule 3)."
        )
        return 1

    print("\nAll configured models are currently served.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
