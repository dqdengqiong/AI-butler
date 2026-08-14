from __future__ import annotations

import json
from pathlib import Path

from ai_butler.api.app import create_app


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / "openapi.json"
    rendered = json.dumps(
        create_app().openapi(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    destination.write_text(f"{rendered}\n", encoding="utf-8")
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
