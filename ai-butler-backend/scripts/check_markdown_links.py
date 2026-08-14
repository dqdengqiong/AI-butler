from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^]]*]\(([^)]+)\)")


def main() -> None:
    failures: list[str] = []
    for document in ROOT.rglob("*.md"):
        if ".venv" in document.parts:
            continue
        for target in LINK.findall(document.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = unquote(target.split("#", maxsplit=1)[0])
            if relative and not (document.parent / relative).resolve().exists():
                failures.append(f"{document.relative_to(ROOT)} -> {target}")
    if failures:
        print("Broken Markdown links:", *failures, sep="\n- ")
        sys.exit(1)
    print("Markdown links are valid")


if __name__ == "__main__":
    main()
