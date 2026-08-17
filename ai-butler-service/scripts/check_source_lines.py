"""检查生产 Python 源码的有效行数上限。"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from pathlib import Path


def effective_line_count(path: Path) -> int:
    """统计非空且不只是 ``#`` 注释的物理行；docstring 仍属于源码。"""

    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )


def python_files(roots: Iterable[Path]) -> list[Path]:
    """稳定返回指定生产源码根目录下的 Python 文件。"""

    files: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.add(root)
        elif root.is_dir():
            files.update(root.rglob("*.py"))
    return sorted(files)


def find_violations(roots: Iterable[Path], max_lines: int) -> list[tuple[Path, int]]:
    """返回超过上限的文件及有效行数。"""

    return [
        (path, count)
        for path in python_files(roots)
        if (count := effective_line_count(path)) > max_lines
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path, help="需要递归检查的生产源码路径")
    parser.add_argument("--max-lines", type=int, default=500, help="允许的最大有效行数")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_lines < 1:
        raise SystemExit("--max-lines 必须是正整数")

    violations = find_violations(args.roots, args.max_lines)
    if not violations:
        return 0

    for path, count in violations:
        print(f"{path}: {count} effective lines (maximum {args.max_lines})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
