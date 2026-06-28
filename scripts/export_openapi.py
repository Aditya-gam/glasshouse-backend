"""Export the OpenAPI schema to openapi.json — the published contract (M5.C).

Run: ``uv run python -m scripts.export_openapi [path]``. The CI publishes the artifact and
the frontend generates its typed client from it; a drift-guard fails if the committed copy is
stale, so the output is sorted for a stable diff.
"""

import json
import sys
from pathlib import Path

from app.main import app

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "openapi.json"


def export(path: Path) -> None:
    """Write the app's OpenAPI schema to `path` (sorted, trailing newline, deterministic)."""
    document = json.dumps(app.openapi(), indent=2, sort_keys=True)
    # Python ≥3.13 renamed HTTP 422's reason phrase ("Unprocessable Entity" → "…Content"),
    # which leaks into FastAPI's auto 422 descriptions. Canonicalize so the artifact is
    # reproducible across interpreter versions (the drift-guard compares byte-for-byte).
    document = document.replace("Unprocessable Content", "Unprocessable Entity")
    path.write_text(document + "\n")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_OUT
    export(out)
    print(f"wrote OpenAPI schema → {out}")


if __name__ == "__main__":
    main()
